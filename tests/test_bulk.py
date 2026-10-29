# -*- coding: utf-8 -*-
"""上千筆項目走完整條刪除流程時的帳目正確性。

重點在合併列 (「其他 N 個較小項目」一列裡塞著上千條路徑)、per_entry 的
成功/失敗統計、失敗的列要留下來給人重試, 以及過程中畫面不能凍住。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def run():
    M = H.load_app()
    c = H.Checks('bulk')
    N = 3000 if os.environ.get('DC_TEST_SLOW') else 1200

    # ---------------------------------- 個別垃圾檔 (會產生合併列)
    with H.Tree() as t:
        for i in range(N):
            ext = '.tmp' if i % 2 else '.log'
            t.file('src/pkg%02d/j%05d%s' % (i % 30, i, ext), 100 + i % 900)
        with H.Session(M) as s:
            app, root = s.app, s.root
            s.scan(t.root)
            c.eq(app.result.root.nfiles, N, '掃到全部 %d 個檔案' % N)
            c.eq(len(app.result.junk), N, '全部 %d 個都被判為垃圾' % N)
            merged = [e for e in app.entry_meta.values() if e.count > 1]
            c.ok(merged, '有產生合併列', '%d 列' % len(merged))
            c.ok(all(len(e.items) == e.count for e in app.entry_meta.values()),
                 '每一列的 count 與實際路徑數一致')
            c.ok(len(app.entry_meta) <= (M.MAX_PER_CATEGORY + 1) * 4,
                 '列數受 MAX_PER_CATEGORY 控制 (%d 列)'
                 % len(app.entry_meta))

            app.select_all(True)
            root.update()
            n_jobs = sum(app.entry_meta[i].count for i in app.checked)
            c.eq(n_jobs, N, '全選之後待刪清單就是 %d 筆' % N)

            # 鎖住幾個, 逼出失敗分支
            locked = []
            for i in (5, N // 2, N - 1):
                ext = '.tmp' if i % 2 else '.log'
                p = t.path('src', 'pkg%02d' % (i % 30), 'j%05d%s' % (i, ext))
                if os.path.exists(p):
                    locked.append(open(p, 'rb'))
            c.eq(len(locked), 3, '鎖住 3 個檔案')

            gaps = []
            last = [None]

            def tick():
                now = time.perf_counter()
                if last[0] is not None:
                    gaps.append(now - last[0])
                last[0] = now
                root.after(16, tick)

            app.trash_var.set(False)          # 永久刪除, 不碰回收桶
            root.after(16, tick)
            t0 = time.perf_counter()
            app.do_delete()
            done = s.pump(300, lambda: app.mode == 'idle')
            wall = time.perf_counter() - t0
            for f in locked:
                f.close()

            c.ok(done, '刪除流程結束並回到閒置')
            c.note('%d 筆花 %.1fs (%.2f ms/筆)'
                   % (n_jobs, wall, wall * 1000 / n_jobs))
            if gaps:
                g = sorted(gaps)
                c.note('刪除期間主迴圈: 中位數 %.1fms 最大 %.1fms'
                       % (g[len(g) // 2] * 1000, g[-1] * 1000))
                c.ok(g[-1] < 1.0, '刪除期間沒有超過 1 秒的停頓',
                     'max %.0f ms' % (g[-1] * 1000))

            left = []
            for dp, _dn, fn in os.walk(t.root):
                left += [os.path.join(dp, x) for x in fn]
            c.eq(len(left), 3, '磁碟上只剩被鎖住的 3 個檔案')
            c.eq(len(app.checked), 0, '勾選狀態已清空')
            remaining = sum(e.count for e in app.entry_meta.values())
            c.eq(remaining, 3, '清單上只留下 3 條待重試的路徑')
            c.ok(all(os.path.exists(p) for e in app.entry_meta.values()
                     for p, _s in e.items),
                 '留下來的路徑都真的還在磁碟上')
            txt = app.status.cget('text')
            c.ok('未完成 3' in txt, '狀態列回報未完成 3 個', txt[:90])
            c.ok(app.stale, '標記統計已過期')

            # 群組列如果沒有子列了就要一起消失
            for gid, leaves in app.group_leaves.items():
                c.ok(leaves, '沒有留下空的群組列')

    # ---------------------------------- 垃圾「目錄」整批刪 + 容器重建
    with H.Tree() as t:
        for d in range(20):
            for i in range(30):
                t.file('proj%02d/__pycache__/m%03d.pyc' % (d, i), 256)
        for d in range(5):
            t.file('area%d/temp/scratch%d.tmp' % (d, d), 4096)
        with H.Session(M) as s:
            app, root = s.app, s.root
            s.scan(t.root)
            app.select_all(True)
            root.update()
            n_jobs = sum(app.entry_meta[i].count for i in app.checked)
            app.trash_var.set(False)
            app.do_delete()
            c.ok(s.pump(120, lambda: app.mode == 'idle'),
                 '刪除 %d 個垃圾目錄結束' % n_jobs)
            gone = [d for d in range(20)
                    if not os.path.exists(t.path('proj%02d' % d,
                                                 '__pycache__'))]
            c.eq(len(gone), 20, '20 個 __pycache__ 全部刪掉')
            kept = [d for d in range(5)
                    if os.path.isdir(t.path('area%d' % d, 'temp'))
                    and not os.listdir(t.path('area%d' % d, 'temp'))]
            c.eq(len(kept), 5,
                 'temp 這類容器目錄被清空但保留 (RECREATE_DIR_CATEGORIES)')
            c.eq(len(app.entry_meta), 0, '全部成功時清單清空')
            c.eq(len(app.group_leaves), 0, '群組列也清空')
    return c.done()


if __name__ == '__main__':
    H.main(run)
