# -*- coding: utf-8 -*-
"""樹狀圖的顯示上限 —— 就是防止「展開一個大資料夾就凍住」的那套邏輯。

覆蓋三件事:
  * 每層最多插入 DISPLAY_CAP 列 (子資料夾與檔案各自算), 其餘併成一列。
  * _dir_files 只保留最大的 DISPLAY_CAP 筆 (固定大小最小堆), 被換掉的併入
    「另有 N 個較小檔案」。
  * 讀滿 SCAN_ON_EXPAND_CAP 筆就停, 並老實說明只讀了前 N 筆。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def run():
    M = H.load_app()
    c = H.Checks('display')
    CAP = M.DISPLAY_CAP

    # ---------------------------------------- _dir_files: 堆汰換 + 精確帳目
    with H.Tree() as t:
        n = CAP + 200
        for i in range(n):
            t.file('d/f%05d.dat' % i, i + 1)          # 大小 1..n, 互不相同
        keep, rest_n, rest_size, trunc = M.App._dir_files(t.path('d'))
        c.eq(len(keep), CAP, '_dir_files 只留 DISPLAY_CAP 筆')
        c.eq(rest_n, n - CAP, '其餘筆數正確')
        c.ok(not trunc, '沒有觸發提早停止 (%d < %d 筆)'
             % (n, M.App.SCAN_ON_EXPAND_CAP))
        sizes = [s for s, _nm in keep]
        c.eq(sizes, sorted(sizes, reverse=True), '回傳結果由大到小排序')
        c.eq(sizes[0], n, '留下的最大就是整個目錄最大的')
        c.eq(sizes[-1], n - CAP + 1, '留下的最小 = 第 DISPLAY_CAP 大')
        c.eq(sorted(sizes), list(range(n - CAP + 1, n + 1)),
             '留下的正好是最大的 DISPLAY_CAP 筆 (不是先遇到的那些)')
        c.eq(rest_size, sum(range(1, n - CAP + 1)),
             '其餘總大小 = 被汰換掉的那些的和')
        c.eq(len(keep) + rest_n, n, '留下的 + 其餘 = 全部')
        c.eq(sum(sizes) + rest_size, n * (n + 1) // 2, '大小總和守恆')

        # 目錄裡混著子目錄時, 子目錄不能被算進檔案帳目
        t.dir('d/sub1')
        t.dir('d/sub2')
        keep2, rest2, _rs2, _tr2 = M.App._dir_files(t.path('d'))
        c.eq(len(keep2) + rest2, n, '子目錄沒有被算成檔案')

    # ---------------------------------------- _dir_files: 提早停止
    M2 = H.load_app('dc_trunc')
    M2.App.SCAN_ON_EXPAND_CAP = 50            # 調小才測得到, 不然要建 2 萬個檔案
    with H.Tree() as t:
        for i in range(200):
            t.file('d/f%03d.dat' % i, i + 1)
        keep, rest_n, rest_size, trunc = M2.App._dir_files(t.path('d'))
        c.ok(trunc, '超過 SCAN_ON_EXPAND_CAP 時回報提早停止')
        c.eq(len(keep) + rest_n, 50,
             '只處理了前 %d 筆 (上限)' % M2.App.SCAN_ON_EXPAND_CAP)
        c.ok(len(keep) <= M2.DISPLAY_CAP, '留下的仍受 DISPLAY_CAP 限制')

    # 空目錄 / 不存在的目錄
    with H.Tree() as t:
        t.dir('empty')
        c.eq(M.App._dir_files(t.path('empty')), ([], 0, 0, False),
             '空目錄回傳空結果')
        c.eq(M.App._dir_files(t.path('nope')), ([], 0, 0, False),
             '不存在的目錄回傳空結果而不是拋例外')

    # ---------------------------------------- _fill: 子資料夾與檔案各自的上限
    with H.Tree() as t:
        n_dirs = CAP + 50
        n_files = CAP + 80
        for i in range(n_dirs):
            t.file('big/sub%04d/x.dat' % i, i + 1)
        for i in range(n_files):
            t.file('big/f%04d.dat' % i, i + 1)
        with H.Session(M) as s:
            app, root = s.app, s.root
            s.scan(t.root, min_size='全部')
            root_iid = app.tv.get_children('')[0]
            big = None
            for k in app.tv.get_children(root_iid):
                m = app.tree_meta.get(k)
                if m and m[0] == 'dir' and m[1].name == 'big':
                    big = k
            c.ok(big is not None, '找到 big 那一列')
            t0 = time.perf_counter()
            app.tv.focus(big)
            app.tv.item(big, open=True)
            app.tv.event_generate('<<TreeviewOpen>>')
            root.update()
            elapsed = time.perf_counter() - t0
            c.ok(elapsed < 2.0, '展開 %d 個子資料夾 + %d 個檔案在 2 秒內 (%.0f ms)'
                 % (n_dirs, n_files, elapsed * 1000))

            rows = app.tv.get_children(big)
            dir_rows = [r for r in rows
                        if app.tree_meta.get(r, ('',))[0] == 'dir']
            file_rows = [r for r in rows
                         if app.tree_meta.get(r, ('',))[0] == 'file']
            notes = [app.tv.item(r, 'text') for r in rows
                     if r not in app.tree_meta]
            c.eq(len(dir_rows), CAP, '子資料夾最多插 DISPLAY_CAP 列')
            c.eq(len(file_rows), CAP, '檔案最多插 DISPLAY_CAP 列')
            c.eq(len(notes), 2, '兩個「另有 N 個未顯示」的提示列')
            joined = ' | '.join(notes)
            c.ok('子資料夾' in joined, '有子資料夾未顯示的提示', joined[:80])
            c.ok('較小檔案' in joined, '有較小檔案未列出的提示', joined[:80])
            c.ok(format(n_dirs - CAP, ',') in joined,
                 '子資料夾的未顯示筆數正確 (%d)' % (n_dirs - CAP), joined[:80])
            c.ok(format(n_files - CAP, ',') in joined,
                 '檔案的未列出筆數正確 (%d)' % (n_files - CAP), joined[:80])

            # 顯示的都要是最大的那些
            shown = sorted(app.tree_meta[r][2] for r in file_rows)
            c.eq(shown, list(range(n_files - CAP + 1, n_files + 1)),
                 '列出的檔案正好是最大的 DISPLAY_CAP 筆')
            dsizes = sorted(app.tree_meta[r][1].size for r in dir_rows)
            c.eq(dsizes, list(range(n_dirs - CAP + 1, n_dirs + 1)),
                 '列出的子資料夾正好是最大的 DISPLAY_CAP 筆')

    # ---------------------------------------- 提早停止時的提示文字
    M3 = H.load_app('dc_trunc_ui')
    M3.App.SCAN_ON_EXPAND_CAP = 30
    with H.Tree() as t:
        for i in range(120):
            t.file('d/f%03d.dat' % i, i + 1)
        with H.Session(M3) as s:
            app, root = s.app, s.root
            s.scan(t.root, min_size='全部')
            root_iid = app.tv.get_children('')[0]
            d = [k for k in app.tv.get_children(root_iid)
                 if app.tree_meta.get(k, ('',))[0] == 'dir']
            c.ok(d, '找到 d 那一列')
            if d:
                app.tv.focus(d[0])
                app.tv.item(d[0], open=True)
                app.tv.event_generate('<<TreeviewOpen>>')
                root.update()
                notes = [app.tv.item(r, 'text')
                         for r in app.tv.get_children(d[0])
                         if r not in app.tree_meta]
                c.ok(any('項目太多' in x for x in notes),
                     '提示列老實說明只讀了前 N 筆', ' | '.join(notes)[:90])
                c.ok(any(format(30, ',') in x for x in notes),
                     '提示列寫出實際的上限值', ' | '.join(notes)[:90])

    # ---------------------------------------- 大小篩選與垃圾檔的互動
    with H.Tree() as t:
        t.file('m/small.txt', 10)
        t.file('m/small.tmp', 10)             # 很小但是垃圾
        t.file('m/big.txt', 5 * 1024 * 1024)
        with H.Session(M) as s:
            app, root = s.app, s.root
            s.scan(t.root, min_size='1 MB')
            root_iid = app.tv.get_children('')[0]
            mdir = [k for k in app.tv.get_children(root_iid)
                    if app.tree_meta.get(k, ('',))[0] == 'dir']
            c.ok(mdir, '找到 m 那一列')
            if mdir:
                app.tv.focus(mdir[0])
                app.tv.item(mdir[0], open=True)
                app.tv.event_generate('<<TreeviewOpen>>')
                root.update()
                names = [app.tv.item(r, 'text').strip()
                         for r in app.tv.get_children(mdir[0])
                         if r in app.tree_meta]
                c.ok('big.txt' in names, '大於門檻的檔案有列出')
                c.ok('small.tmp' in names,
                     '小於門檻但是垃圾的檔案仍然列出 (免得清不到)')
                c.ok('small.txt' not in names,
                     '小於門檻又不是垃圾的檔案被濾掉')

    # ---------------------------------------- 重複 refresh 不會殘留舊列
    with H.Tree() as t:
        for i in range(5):
            t.file('p%d/__pycache__/a.pyc' % i, 500)
        t.file('big.bin', 2 * 1024 * 1024)
        with H.Session(M) as s:
            app, root = s.app, s.root
            s.scan(t.root, min_size='全部')
            counts = []
            for _ in range(3):
                app.refresh_tree()
                app.refresh_clean()
                app.refresh_big()
                app.refresh_ext()
                app.refresh_err()
                root.update()
                counts.append((
                    len(app.tv.get_children(app.tv.get_children('')[0])),
                    len(app.cv.get_children('')),
                    len(app.bv.get_children('')),
                    len(app.ev.get_children('')),
                    len(app.errv.get_children(''))))
            c.eq(counts[0], counts[1], '重複 refresh 列數不變 (沒有累加)')
            c.eq(counts[1], counts[2], '再一次也一樣')

            # tree_meta 的鍵必須跟畫面上真的存在的列一對一
            stack = list(app.tv.get_children(''))
            live = []
            while stack:
                iid = stack.pop()
                stack.extend(app.tv.get_children(iid))
                live.append(iid)
            stale = [k for k in app.tree_meta if k not in set(live)]
            c.eq(stale, [], 'tree_meta 沒有殘留已刪除的列',
                 '%d 個殘留' % len(stale))
            c.ok(len(app.tree_meta) <= len(live),
                 'tree_meta 不多於畫面上的列數 (%d <= %d)'
                 % (len(app.tree_meta), len(live)))
            c.ok(all(app.tv.exists(k) for k in app.tree_meta),
                 'tree_meta 裡每個 iid 都還存在')
    return c.done()


if __name__ == '__main__':
    H.main(run)
