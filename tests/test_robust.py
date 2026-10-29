# -*- coding: utf-8 -*-
"""韌性: 背景工作爆掉時 UI 一定要回得來, 極端路徑也不能倒。

最重要的性質: 主視窗在收到 done/error/del_done/rb_done 之前會一直停在忙碌
狀態、按鈕全部 disabled, 連「停止」都救不回來。所以任何背景執行緒都必須
保證送出完成訊息。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def small_tree(t):
    for i in range(4):
        t.file('mod%d/__pycache__/a.pyc' % i, 500)
        t.file('mod%d/main.py' % i, 800)
    t.file('logs/x.log', 400)
    t.file('tmp/y.tmp', 300)
    t.file('old.bak', 200)


def run():
    c = H.Checks('robust')

    # ---------------------------------------- 1. 掃描 worker 拋 BaseException
    for exc in (MemoryError('injected'), RuntimeError('injected'),
                KeyboardInterrupt()):
        M = H.load_app('dc_scan_%s' % type(exc).__name__)
        with H.Tree() as t:
            small_tree(t)
            with H.Session(M, visible=False) as s:
                def boom(self, node, path, in_junk, acc, _e=exc):
                    raise _e
                M.Scanner._read_dir = boom
                s.app.path_var.set(t.root)
                s.app.start_scan()
                back = s.pump(30, lambda: s.app.mode == 'idle')
                c.ok(back, '掃描 worker 拋 %s 後回到閒置'
                     % type(exc).__name__)
                c.ok(any(d[1] == '掃描失敗' for d in s.dialogs),
                     '  並且回報「掃描失敗」')
                c.eq(str(s.app.btn_scan.cget('state')), 'normal',
                     '  掃描按鈕可以再按')
                c.eq(str(s.app.btn_stop.cget('state')), 'disabled',
                     '  停止按鈕回到不可按')

    # ---------------------------------------- 2. 刪除 worker 中途拋例外
    M = H.load_app('dc_del')
    with H.Tree() as t:
        small_tree(t)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            c.ok(len(s.app.checked) > 0, '掃完有自動勾選項目')
            orig = M.delete_many
            calls = [0]

            def flaky(jobs, to_trash=True):
                calls[0] += 1
                if calls[0] == 2:
                    raise RuntimeError('injected mid-delete')
                return orig(jobs, to_trash)
            M.delete_many = flaky
            s.app.trash_var.set(False)
            s.app.do_delete()
            back = s.pump(60, lambda: s.app.mode == 'idle')
            c.ok(back, '刪除 worker 中途拋例外後回到閒置')
            c.eq(str(s.app.btn_del.cget('state')), 'normal',
                 '  刪除按鈕可以再按')
            c.ok(any(d[1] in ('部分完成', '刪除完成') for d in s.dialogs),
                 '  有向使用者回報結果')
            c.ok('刪除' in s.app.prog_lbl.cget('text'),
                 '  進度標籤已收尾', s.app.prog_lbl.cget('text'))
            M.delete_many = orig

    # ---------------------------------------- 3. 磁碟/回收桶查詢拋例外
    M = H.load_app('dc_disk')
    with H.Session(M, visible=False) as s:
        M.recycle_bin_info = lambda: (_ for _ in ()).throw(OSError('injected'))
        s.app._disk_busy = False
        s.app._update_disk_label()
        c.ok(s.pump(20, lambda: not s.app._disk_busy),
             '回收桶查詢拋例外後 _disk_busy 有被清掉')
        M.recycle_bin_info = lambda: (123, 4)
        s.app._update_disk_label()
        c.ok(s.pump(20, lambda: s.app._rb_info == (123, 4)),
             '之後的查詢仍然正常運作')
        M.disk_usage_of = lambda p: (_ for _ in ()).throw(OSError('injected'))
        s.app._update_disk_label()
        c.ok(s.pump(20, lambda: not s.app._disk_busy),
             '磁碟容量查詢拋例外也能收尾')

    # ---------------------------------------- 4. 清空回收桶拋例外
    M = H.load_app('dc_rb')
    with H.Session(M, visible=False) as s:
        M.empty_recycle_bin = lambda: (_ for _ in ()).throw(
            OSError('injected'))
        s.app._rb_info = (999, 3)
        s.app.do_empty_recycle_bin()
        c.ok(s.pump(30, lambda: s.app.mode == 'idle'),
             '清空回收桶拋例外後回到閒置')
        c.ok(any(d[1] == '清空失敗' for d in s.dialogs),
             '  並且回報「清空失敗」')

    # ---------------------------------------- 5. queue.put 壞掉也不能卡死
    M = H.load_app('dc_q')
    with H.Tree() as t:
        small_tree(t)
        import queue as _q
        bad_q = _q.Queue()
        real_put = bad_q.put
        state = {'n': 0}

        def flaky_put(item, *a, **k):
            state['n'] += 1
            if state['n'] == 1:
                raise RuntimeError('queue exploded')
            return real_put(item, *a, **k)
        bad_q.put = flaky_put
        import threading
        sc = M.Scanner(t.root, bad_q, threading.Event(), 1)
        sc.start()
        sc.join(60)
        c.ok(not sc.is_alive(), 'queue.put 拋例外時掃描執行緒仍然結束')

    # ---------------------------------------- 6. 極深 / 極長路徑
    M = H.load_app('dc_deep')
    with H.Tree() as t:
        p = 'deep'
        for i in range(120):
            p += '/d%03d' % i
        leaf = t.file(p + '/scratch.tmp', 1000)
        t.file('/'.join(p.split('/')[:60]) + '/mid.tmp', 500)
        c.ok(len(leaf) > 500, '葉節點路徑長度 %d 字元' % len(leaf))
        res = H.scan(M, t.root, workers=1)
        c.eq(res.errors, 0, '深樹掃描沒有讀取失敗')
        c.eq(res.dirs, 122, '掃到全部 122 個資料夾')
        c.eq(len(res.junk), 2, '兩個長路徑的垃圾檔都找到')
        nodes = H.walk_nodes(res.root)
        deepest = max(nodes, key=lambda n: len(n.path))
        c.eq(deepest.path, os.path.abspath(os.path.dirname(leaf)),
             'Node.path 沿 parent 重建出正確的超長路徑')

        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            iid = s.app.tv.get_children('')[0]
            depth = 0
            while depth < 200:
                s.app.tv.focus(iid)
                s.app.tv.item(iid, open=True)
                s.app.tv.event_generate('<<TreeviewOpen>>')
                s.root.update()
                kids = [k for k in s.app.tv.get_children(iid)
                        if s.app.tree_meta.get(k, ('',))[0] == 'dir']
                if not kids:
                    break
                iid = kids[0]
                depth += 1
            c.eq(depth, 121, '樹狀圖可以一路展開到底 (%d 層)' % depth)
            c.no_raise('_forget_subtree 不會撞 recursion limit',
                       lambda: s.app._forget_subtree(
                           s.app.tv.get_children('')[0]))
        c.eq(M._try_delete(t.path('deep'), False, False), None,
             '整棵深樹可以永久刪除')
        c.ok(not os.path.exists(H._long(t.path('deep'))), '深樹真的不見了')

    # ---------------------------------------- 7. 掃描不存在/沒權限的路徑
    M = H.load_app('dc_bad')
    with H.Session(M, visible=False) as s:
        for label, val in (('不存在的路徑', 'Z:' + H.BS + 'nope' + H.BS + 'x'),
                           ('空字串', ''), ('純空白', '   '),
                           ('檔案而不是資料夾', H.APP_PATH)):
            s.app.path_var.set(val)
            c.no_raise('start_scan(%s) 不會爆' % label,
                       lambda: s.app.start_scan())
            c.eq(s.app.mode, 'idle', '  並且維持閒置 (%s)' % label)
    return c.done()


if __name__ == '__main__':
    H.main(run)
