# -*- coding: utf-8 -*-
"""coverage 指出來、其他測試都繞過去的邊角分支。

刪除途中按停止、忙碌中的各種防護、跨磁碟的相對路徑、送回收桶成功的訊息、
釋放空間的計算等等。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

BS = H.BS


def run():
    c = H.Checks('edge')

    # -------------------------------------------- 刪除途中按停止
    M = H.load_app('dc_stopdel')
    with H.Tree() as t:
        for i in range(400):
            t.file('j/f%04d.tmp' % i, 50)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.root)
            app.select_all(True)
            s.root.update()
            n_jobs = sum(app.entry_meta[i].count for i in app.checked)
            c.eq(n_jobs, 400, '待刪 400 筆')

            slow_calls = [0]
            real = M.delete_many

            def slow(jobs, to_trash=True):
                slow_calls[0] += 1
                if slow_calls[0] == 3:
                    app.del_stop.set()          # 第三批之後叫停
                return real(jobs, to_trash)
            M.delete_many = slow
            app.trash_var.set(False)
            app.do_delete()
            done = s.pump(90, lambda: app.mode == 'idle')
            M.delete_many = real
            c.ok(done, '按停止之後刪除流程有結束')
            c.ok('中止' in app.prog_lbl.cget('text'),
                 '進度標籤顯示「刪除中止」', app.prog_lbl.cget('text'))
            left = sum(len(fn) for _dp, _dn, fn in os.walk(t.root))
            c.ok(0 < left < 400,
                 '只刪掉一部分 (磁碟上還剩 %d 個)' % left)
            remaining = sum(e.count for e in app.entry_meta.values())
            c.eq(remaining, left,
                 '清單留下的路徑數 = 磁碟上還剩的檔案數')
            c.ok(all(os.path.exists(p) for e in app.entry_meta.values()
                     for p, _s in e.items),
                 '留下來的路徑都真的還在')
            c.eq(len(app.checked), 0, '勾選已清空')

    # -------------------------------------------- 忙碌中的各種防護
    M = H.load_app('dc_busy')
    with H.Tree() as t:
        t.file('a.tmp', 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.root)
            n_checked = len(app.checked)
            for mode in ('scan', 'delete'):
                app.mode = mode
                before = dict(checked=set(app.checked), stale=app.stale)
                c.no_raise('%s 中 select_all(True) 被忽略' % mode,
                           lambda: app.select_all(True))
                c.no_raise('%s 中 select_all(False) 被忽略' % mode,
                           lambda: app.select_all(False))
                c.no_raise('%s 中 select_risk 被忽略' % mode,
                           lambda: app.select_risk(M.RISK_SAFE))
                c.eq(app.checked, before['checked'],
                     '%s 中勾選狀態沒有被改動' % mode)
                c.no_raise('%s 中 do_delete 被忽略' % mode,
                           lambda: app.do_delete())
                c.eq(app.stale, before['stale'],
                     '%s 中沒有被標記為過期' % mode)
                c.no_raise('%s 中 do_empty_recycle_bin 被忽略' % mode,
                           lambda: app.do_empty_recycle_bin())
                c.eq(len(s.bin_emptied), 0, '%s 中沒有真的去清回收桶' % mode)
                c.no_raise('%s 中 _toggle 被忽略' % mode,
                           lambda: app._toggle(
                               next(iter(app.entry_meta), '')))
                c.eq(app.checked, before['checked'],
                     '%s 中 _toggle 也沒有改動勾選' % mode)
            app.mode = 'idle'
            c.eq(len(app.checked), n_checked, '回到閒置後勾選狀態完好')

    # -------------------------------------------- 跨磁碟的相對路徑
    M = H.load_app('dc_relpath')
    junk = [dict(path='Z:' + BS + 'other' + BS + 'x.tmp', name='x.tmp',
                 size=100, is_dir=False, category='暫存檔',
                 risk=M.RISK_SAFE, note='n'),
            dict(path='C:' + BS + 'root' + BS + 'y.tmp', name='y.tmp',
                 size=200, is_dir=False, category='暫存檔',
                 risk=M.RISK_SAFE, note='n')]
    groups = None
    c.no_raise('build_entries 吃得下跨磁碟的路徑 (relpath 會拋 ValueError)',
               lambda: groups)
    groups = M.build_entries(junk, 'C:' + BS + 'root')
    labels = [e.label for g in groups for e in g[5]]
    c.eq(len(labels), 2, '兩筆都被列出')
    c.ok(any(lb.startswith('Z:') for lb in labels),
         '不同磁碟的那筆退回顯示完整路徑', str(labels))
    c.ok(any(lb == 'y.tmp' for lb in labels),
         '同磁碟的那筆顯示相對路徑', str(labels))

    # -------------------------------------------- 送回收桶成功的訊息
    M = H.load_app('dc_trashmsg2')
    with H.Tree() as t:
        for i in range(4):
            t.file('t/f%d.tmp' % i, 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.root)
            app.select_all(True)
            s.root.update()
            app.trash_var.set(True)         # 走送回收桶 (harness 已攔下)
            app.do_delete()
            c.ok(s.pump(60, lambda: app.mode == 'idle'), '刪除結束')
            c.ok(len(s.trash_batches) >= 1, '真的走了送回收桶那條路徑')
            msgs = [d[2] for d in s.dialogs if d[1] == '刪除完成']
            c.ok(msgs, '出現「刪除完成」')
            if msgs:
                c.ok('資源回收桶' in msgs[-1],
                     '訊息說明是移到回收桶', msgs[-1][:60])
                c.ok('清空回收桶' in msgs[-1],
                     '並提醒要清空才會真的釋放空間', msgs[-1][:80])

    # -------------------------------------------- 清空回收桶成功後的空間計算
    M = H.load_app('dc_rbspace')
    with H.Session(M, visible=False) as s:
        app = s.app
        free = [1000]
        M.free_bytes_of = lambda p: free[0]
        app._rb_info = (5 * 1024 * 1024, 9)
        app.do_empty_recycle_bin()
        free[0] = 1000 + 5 * 1024 * 1024        # 假裝空間變多了
        c.ok(s.pump(30, lambda: app.mode == 'idle'), '清空流程結束')
        c.eq(len(s.bin_emptied), 1, '真的走到清空那一步 (被攔下)')
        msgs = [d[2] for d in s.dialogs if d[1] == '完成']
        c.ok(msgs, '出現「完成」')
        if msgs:
            c.ok('增加' in msgs[-1], '訊息回報可用空間增加了多少',
                 msgs[-1][:80])
            c.ok(M.human(5 * 1024 * 1024) in msgs[-1],
                 '增加的數字正確', msgs[-1][:80])
        c.ok('已清空' in app.status.cget('text'),
             '狀態列更新', app.status.cget('text')[:60])

    # -------------------------------------------- browse 選了資料夾
    M = H.load_app('dc_browse')
    with H.Tree() as t:
        t.dir('picked')
        with H.Session(M, visible=False) as s:
            M.filedialog = type('F', (), {
                'askdirectory': staticmethod(
                    lambda **k: t.path('picked').replace(os.sep, '/'))})()
            s.app.browse()
            c.eq(s.app.path_var.get(), os.path.normpath(t.path('picked')),
                 'browse 選到的路徑被正規化後填進欄位')
            c.ok(s.pump(20, lambda: not s.app._disk_busy),
                 'browse 之後的磁碟查詢也收尾了')

    # -------------------------------------------- _on_open 的各種狀況
    M = H.load_app('dc_onopen')
    with H.Tree() as t:
        t.file('sub/deep/x.tmp', 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.root, min_size='全部')
            root_iid = app.tv.get_children('')[0]
            app.tv.focus('')
            c.no_raise('_on_open 沒有焦點', lambda: app._on_open(None))
            files = [k for k in app.tv.get_children(root_iid)
                     if app.tree_meta.get(k, ('',))[0] == 'file']
            if files:
                app.tv.focus(files[0])
                c.no_raise('_on_open 在檔案列上 (不該做事)',
                           lambda: app._on_open(None))
            dirs = [k for k in app.tv.get_children(root_iid)
                    if app.tree_meta.get(k, ('',))[0] == 'dir']
            if dirs:
                app.tv.focus(dirs[0])
                app.tv.item(dirs[0], open=True)
                app.tv.event_generate('<<TreeviewOpen>>')
                s.root.update()
                n1 = len(app.tv.get_children(dirs[0]))
                app._on_open(None)              # 第二次: 佔位符已經沒了
                s.root.update()
                c.eq(len(app.tv.get_children(dirs[0])), n1,
                     '同一個節點展開兩次不會插重複的列')

    # -------------------------------------------- 單一項目的 stat 失敗
    # 掃描途中某個檔案剛好消失/沒權限: 那一筆要算成讀取失敗, 其他照舊
    class BadEntry(object):
        def __init__(self, name, kind):
            self.name = name
            self._kind = kind

        def is_dir(self, follow_symlinks=True):
            if self._kind == 'isdir':
                raise PermissionError(5, 'injected is_dir')
            return False

        def is_symlink(self):
            return False

        def stat(self, follow_symlinks=True):
            raise PermissionError(5, 'injected stat')

    M = H.load_app('dc_badentry')
    with H.Tree() as t:
        t.file('d/good.txt', 100)
        real_scandir = os.scandir
        target = os.path.normcase(os.path.abspath(t.path('d')))

        class FakeIt(object):
            def __init__(self, real):
                self._real = real

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._real.__exit__(*a)

            def __iter__(self):
                for e in self._real:
                    yield e
                yield BadEntry('vanished.tmp', 'stat')
                yield BadEntry('vanished_dir', 'isdir')

        def patched(p=None, *a, **k):
            real = real_scandir(p, *a, **k)
            raw = M.from_ext(p) if isinstance(p, str) else p
            if isinstance(raw, str) and os.path.normcase(
                    os.path.abspath(raw)) == target:
                return FakeIt(real)
            return real

        saved = M.os
        M.os = type('OS', (), {k: getattr(os, k) for k in dir(os)
                               if not k.startswith('__')})()
        M.os.scandir = patched
        try:
            res = H.scan(M, t.root, workers=1)
        finally:
            M.os = saved
        c.eq(res.errors, 2, '兩個 stat 失敗的項目都算成讀取失敗')
        c.eq(res.root.nfiles, 1, '好的檔案仍然被統計到')
        c.eq(res.root.size, 100, '大小只算得到的那個')
        c.ok(any('vanished' in p for p in res.error_paths),
             '失敗清單記下了那兩筆', str(res.error_paths[:3]))

        # _dir_files 也要能跳過 stat 失敗的項目
        M.os = type('OS', (), {k: getattr(os, k) for k in dir(os)
                               if not k.startswith('__')})()
        M.os.scandir = patched
        try:
            keep, rest_n, _rs, _tr = M.App._dir_files(t.path('d'))
        finally:
            M.os = saved
        c.eq(len(keep) + rest_n, 1,
             '_dir_files 跳過 stat 失敗的項目, 只算得到的那個')

    # -------------------------------------------- 右鍵刪除 .git 內部
    M = H.load_app('dc_gitwarn')
    with H.Tree() as t:
        t.file('.git/logs/HEAD', 100)
        t.file('.git/logs/refs/x', 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.path('.git'), min_size='全部')
            root_iid = app.tv.get_children('')[0]
            logs = [k for k in app.tv.get_children(root_iid)
                    if app.tree_meta.get(k, ('',))[0] == 'dir']
            c.ok(logs, '找到 .git 底下的 logs')
            if logs:
                app.tv.focus(logs[0])
                asked = []
                from tkinter import messagebox

                def ask(title, msg='', **kw):
                    asked.append(msg)
                    return False
                messagebox.askyesno = ask
                M.messagebox = messagebox
                app._tree_action('del')
                c.ok(asked, '有彈出確認')
                if asked:
                    c.ok('reflog' in asked[0],
                         '確認訊息警告刪掉 .git\\logs 會讓 reflog 失效',
                         asked[0][:80])

    # -------------------------------------------- _delete_once 自己補 probe
    M = H.load_app('dc_probe')
    with H.Tree() as t:
        f = t.file('x.tmp', 100)
        c.eq(M._delete_once(f, False), 'perm',
             '_delete_once 沒收到 is_dir/is_link 時自己去 probe')
        c.ok(not os.path.exists(f), '檔案被刪掉')
        d = t.dir('sub')
        t.file('sub/y.tmp', 50)
        c.eq(M._delete_once(d, False), 'perm',
             '_delete_once 對目錄也能自己 probe')
        c.ok(not os.path.exists(d), '目錄被刪掉')

    # -------------------------------------------- 還沒掃描就 refresh
    M = H.load_app('dc_norefresh')
    with H.Session(M, visible=False) as s:
        for name in ('refresh_tree', 'refresh_clean', 'refresh_big',
                     'refresh_ext', 'refresh_err'):
            c.no_raise('%s() 在還沒有結果時直接返回' % name,
                       getattr(s.app, name))
        c.eq(len(s.app.tv.get_children('')), 0, '樹狀圖是空的')
        c.eq(len(s.app.entry_meta), 0, '可清理清單是空的')
        c.ok('0 B' in s.app.sel_lbl.cget('text'), '已選標籤是 0')

    # -------------------------------------------- 失敗超過 10 筆時的省略訊息
    M = H.load_app('dc_manyfail')
    with H.Session(M, visible=False) as s:
        fails = ['C:%sp%03d  ->  boom' % (BS, i) for i in range(25)]
        per_entry = {}
        c.no_raise('_delete_finished 吃得下 25 筆失敗',
                   lambda: s.app._delete_finished(0, 0, fails, per_entry,
                                                  False, 0))
        msgs = [d[2] for d in s.dialogs if d[1] == '部分完成']
        c.ok(msgs, '出現「部分完成」')
        if msgs:
            c.ok('其餘' in msgs[-1] and '省略' in msgs[-1],
                 '訊息說明其餘幾筆被省略', msgs[-1][-60:])
            c.ok(format(15, ',') in msgs[-1],
                 '省略筆數正確 (25 - 10 = 15)', msgs[-1][-60:])

    # -------------------------------------------- _merge 的失敗路徑上限
    M = H.load_app('dc_merge')
    sc = M.Scanner.__new__(M.Scanner)
    sc.res = M.ScanResult()
    sc._accs = []
    for w in range(4):
        a = M._Acc()
        a.dirs, a.links, a.errors = 10, 2, 50
        a.error_paths = ['w%d_p%d' % (w, i)
                         for i in range(M.Scanner.MAX_ERR_PATHS)]
        a.ext_stats = {'.tmp': [3, 300], '.w%d' % w: [1, 100]}
        a.heap = [(i, 'w%d_f%d' % (w, i)) for i in range(10)]
        a.junk = [dict(path='w%d' % w, size=1)]
        sc._accs.append(a)
    sc._merge()
    c.eq(sc.res.dirs, 40, '_merge 把各 worker 的資料夾數加起來')
    c.eq(sc.res.links, 8, '_merge 把連結數加起來')
    c.eq(sc.res.errors, 200, '_merge 把失敗數加起來')
    c.eq(len(sc.res.junk), 4, '_merge 把 junk 串起來')
    c.eq(len(sc.res.error_paths), M.Scanner.MAX_ERR_PATHS,
         '_merge 讓失敗路徑停在上限 (不會 4 個 worker 各塞滿)')
    c.eq(sc.res.ext_stats['.tmp'], [12, 1200],
         '_merge 把同一個副檔名的統計相加')
    c.eq(len(sc.res.ext_stats), 5, '_merge 保留各 worker 獨有的副檔名')
    c.eq(len(sc.res.top_files), min(40, M.Scanner.TOP_N),
         '_merge 合併各 worker 的排行')
    c.eq(sc.res.top_files, sorted(sc.res.top_files, reverse=True),
         '_merge 後的排行由大到小')

    # -------------------------------------------- 沒有 glob 規則時
    c.eq(M._build_glob_re([]), (None, []), '_build_glob_re 吃得下空規則')
    c.eq(M._match_glob_re('anything', None, []), None,
         '_match_glob_re 對 None regex 回 None')

    # -------------------------------------------- _post_delete 的兩個分支
    M = H.load_app('dc_post')
    with H.Tree() as t:
        # 內容清掉了, 只剩空殼 (資料夾本身被開著) -> 算成功
        shell = t.dir('shell')
        state = (shell, True, False, True, 5)      # before=5, 現在 0 個
        c.eq(M._verify_delete(state, False, 'perm'), None,
             '只剩空殼 (內容已清掉) 算成功')
        # 一個都沒刪掉 -> 算失敗, 而不是回報 PartialDelete
        t.file('stuck/a', 10)
        t.file('stuck/b', 10)
        t.file('stuck/c', 10)
        state = (t.path('stuck'), True, False, True, 1)   # before=1, 現在 3 個
        err = M._verify_delete(state, False, 'perm')
        c.ok(isinstance(err, OSError) and not isinstance(err,
                                                         M.PartialDelete),
             '完全沒刪掉時回報 OSError 而不是 PartialDelete', repr(err))
        c.ok('無法刪除' in str(err), '錯誤訊息說明資料夾無法刪除', str(err))
        # recreate 但重建失敗 (父層被刪掉) 不能拋出來
        state = (t.path('gone', 'deeper'), True, False, True, 3)
        c.eq(M._verify_delete(state, True, 'perm'), None,
             'recreate 失敗時默默略過, 不影響刪除結果')

    # -------------------------------------------- _delete_once 超長路徑
    M = H.load_app('dc_once_long')
    M.IS_WINDOWS = True
    M._send2trash = None
    long_path = 'C:' + BS + ('d' * 300)
    err = None
    try:
        M._delete_once(long_path, True, False, False)
    except OSError as e:
        err = e
    c.ok(err is not None and '過長' in str(err),
         '_delete_once 對超長路徑直接拒絕送回收桶', str(err))

    # -------------------------------------------- rmtree 的舊版分支
    M = H.load_app('dc_rmtree_old')
    with H.Tree() as t:
        t.file('tree/a.tmp', 100)
        t.file('tree/sub/b.tmp', 100)
        os.chmod(t.path('tree', 'a.tmp'), 0o444)     # 逼出 on_err 重試
        real_vi = M.sys.version_info
        try:
            M.sys = type('S', (), {k: getattr(sys, k) for k in
                                   ('argv', 'platform', 'modules')})()
            M.sys.version_info = (3, 11, 0)
            err = M._try_delete(t.path('tree'), False, False)
        finally:
            M.sys = sys
        c.eq(err, None, 'Python < 3.12 的 rmtree(onerror=...) 分支可用')
        c.ok(not os.path.exists(t.path('tree')), '整棵樹被刪掉')
        c.ok(real_vi >= (3, 12), '目前跑的是 3.12+ (預設走 onexc 分支)',
             str(real_vi[:2]))

    # -------------------------------------------- _dir_files 的汰換 else 分支
    M = H.load_app('dc_evict')
    with H.Tree() as t:
        cap = M.DISPLAY_CAP
        n = cap + 100
        # 大小遞減: 後面遇到的一定比堆裡的最小值還小 -> 走 else 分支
        for i in range(n):
            t.file('d/f%05d.dat' % i, n - i)
        keep, rest_n, rest_size, _tr = M.App._dir_files(t.path('d'))
        c.eq(len(keep), cap, '仍然只留 DISPLAY_CAP 筆')
        c.eq(rest_n, n - cap, '其餘筆數正確')
        sizes = sorted(s for s, _nm in keep)
        c.eq(sizes, list(range(n - cap + 1, n + 1)),
             '大小遞減時留下的仍然是最大的那些')
        c.eq(rest_size, sum(range(1, n - cap + 1)),
             '大小遞減時「其餘總大小」也正確 (走的是 else 分支)')
        c.eq(sum(sizes) + rest_size, n * (n + 1) // 2, '總和守恆')

    # -------------------------------------------- 沒有系統目錄前綴時
    M = H.load_app('dc_nosys')
    saved = M.SYSTEM_PREFIXES
    M.SYSTEM_PREFIXES = ()
    c.eq(M.in_system_dir('C:' + BS + 'Windows' + BS + 'x'), False,
         '沒有系統目錄前綴時 in_system_dir 一律回 False')
    plain = ('分類', M.RISK_SAFE, '說明')
    c.eq(M.Scanner._grade('C:' + BS + 'Windows' + BS + 'x', plain), plain,
         '  因此 _grade 也不會降級')
    M.SYSTEM_PREFIXES = saved

    # -------------------------------------------- _pump 的內部錯誤保護
    M = H.load_app('dc_pumperr')
    with H.Session(M, visible=False) as s:
        s.app.q.put(('done', s.app.gen, object()))   # 讓 _finish 內部爆掉
        s.pump(1.0)
        c.ok('內部錯誤' in s.app.status.cget('text'),
             '_handle 內部爆掉時狀態列顯示「內部錯誤」而不是整個停擺',
             s.app.status.cget('text')[:60])
        s.app._rb_info = None
        s.app.q.put(('disk_info', (1, 2), 'X:', (7, 1)))
        s.pump(1.0, lambda: s.app._rb_info == (7, 1))
        c.eq(s.app._rb_info, (7, 1), '出過內部錯誤之後輪詢仍然在跑')

    # -------------------------------------------- 點展開箭頭 = 交給 Tk
    M = H.load_app('dc_indicator')
    with H.Tree() as t:
        for i in range(3):
            t.file('p%d/__pycache__/a.pyc' % i, 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            s.scan(t.root)
            gid = app.cv.get_children('')[0]
            app.cv.identify_region = lambda x, y: 'tree'
            app.cv.identify_element = lambda x, y: 'Treeitem.indicator'
            app.cv.identify_row = lambda y: gid
            before = set(app.checked)
            r = app._on_click_clean(type('E', (), {'x': 10, 'y': 10})())
            c.eq(r, None, '點在展開箭頭上時回 None (讓 Tk 自己展開/收合)')
            c.eq(app.checked, before, '  並且不會改動勾選狀態')

    # -------------------------------------------- refresh_err 重複呼叫
    M = H.load_app('dc_err2')
    with H.Session(M, visible=False) as s:
        res = M.ScanResult()
        res.root = M.Node('C:' + BS + 'x')
        res.errors = 7
        res.error_paths = ['C:%sp%d' % (BS, i) for i in range(3)]
        s.app.result = res
        s.app.refresh_err()
        n1 = len(s.app.errv.get_children(''))
        s.app.refresh_err()
        n2 = len(s.app.errv.get_children(''))
        c.eq(n1, 4, 'refresh_err 列出 3 筆路徑 + 一列「另有 4 項」')
        c.eq(n2, n1, '重複呼叫不會累加列數')

    # -------------------------------------------- _count_children 的上限
    M = H.load_app('dc_count')
    with H.Tree() as t:
        for i in range(1200):
            t.file('many/f%04d.tmp' % i, 1)
        n = M._count_children(t.path('many'))
        c.eq(n, 1000, '_count_children 數到 1000 就停 (避免整棵樹重走)')
        c.eq(M._count_children(t.path('nope')), 0,
             '_count_children 對不存在的路徑回 0')
        t.dir('emptyd')
        c.eq(M._count_children(t.path('emptyd')), 0, '空目錄回 0')
    return c.done()


if __name__ == '__main__':
    H.main(run)
