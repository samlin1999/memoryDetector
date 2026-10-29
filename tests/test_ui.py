# -*- coding: utf-8 -*-
"""每一個 UI 事件處理函式都要真的被執行過一次。

注意兩個 harness 陷阱 (見 _harness.py):
  * bbox()/see() 需要視窗被 map, 而且該 widget 的 notebook 分頁要是選取中的。
  * tk_popup() 會抓取輸入並阻塞到有人關掉選單, 所以被換成記錄器。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

BS = H.BS


def build(t):
    for i in range(3):
        for k in range(3):
            t.file('mod%d/__pycache__/a%d.pyc' % (i, k), 2048)
    t.file('logs/app.log', 4096)
    t.file('tmp/t.tmp', 2048)
    t.file('old.bak', 1024)
    t.file('big.bin', 3 * 1024 * 1024)
    t.file('nested/deep/deeper/x.log', 512)


def ev(**kw):
    return type('E', (), kw)()


def run():
    M = H.load_app()
    c = H.Checks('ui')

    with H.Tree() as t:
        build(t)
        with H.Session(M) as s:
            app, root = s.app, s.root
            res = s.scan(t.root)
            c.note('scan: dirs=%d files=%d junk=%d 可清理列=%d'
                   % (res.dirs, res.root.nfiles, len(res.junk),
                      len(app.entry_meta)))
            c.ok(len(app.entry_meta) > 0, '掃到可清理項目')
            c.ok(len(app.tv.get_children('')) == 1, '樹狀圖有一個根列')

            # ---------------------------------------------- 分頁1: 右鍵選單
            tree_root = app.tv.get_children('')[0]
            kid = app.tv.get_children(tree_root)[0]
            app.tv.focus(kid)
            app.tv.selection_set(kid)
            c.no_raise('_tree_action("open") -> 開啟位置',
                       lambda: app._tree_action('open'))
            c.ok(len(s.explorer) == 1, '真的呼叫了檔案總管 (被攔下來)')
            c.no_raise('_tree_action("copy") -> 複製路徑',
                       lambda: app._tree_action('copy'))
            c.ok(root.clipboard_get().startswith(t.root),
                 '剪貼簿內容是完整路徑')
            c.no_raise('<Button-3> 觸發 _popup_tree',
                       lambda: app.tv.event_generate('<Button-3>',
                                                     x=40, y=30))
            c.ok(len(s.popups) >= 1, '選單真的被彈出 (被攔下來)')
            c.no_raise('_popup_tree 點在空白處',
                       lambda: app._popup_tree(ev(x=5, y=9999, x_root=10,
                                                  y_root=10)))
            c.no_raise('_on_open 在檔案列上', lambda: app._on_open(None))
            c.no_raise('_tree_action 沒有選取任何列',
                       lambda: (app.tv.focus(''), app._tree_action('copy'),
                                app._tree_action('open')))

            # ---------------------------------------------- 分頁1: 展開
            target = None
            for k in app.tv.get_children(tree_root):
                m = app.tree_meta.get(k)
                if m and m[0] == 'dir' and m[1].name == 'nested':
                    target = k
            c.ok(target is not None, '找到 nested 目錄那一列')
            if target:
                app.tv.focus(target)
                app.tv.item(target, open=True)
                app.tv.event_generate('<<TreeviewOpen>>')
                root.update()
                c.ok(len(app.tv.get_children(target)) >= 1,
                     '展開後插進了子列')

            # ---------------------------------------------- 分頁1: 真的刪除
            victim = None
            for k in app.tv.get_children(tree_root):
                m = app.tree_meta.get(k)
                if m and m[0] == 'dir' and m[1].name == 'logs':
                    victim = k
            c.ok(victim is not None, '找到 logs 目錄那一列')
            if victim:
                app.tv.focus(victim)
                app.tv.selection_set(victim)
                c.no_raise('_tree_action("del") 送回收桶',
                           lambda: app._tree_action('del'))
                c.eq(len(s.trash_batches), 1,
                     '走的是送回收桶的路徑 (被攔下來改成直接刪)')
                c.ok(not os.path.exists(t.path('logs')), 'logs 真的被刪掉')
                c.ok(app.stale, '刪除後標記統計已過期')
                c.ok(not app.tv.exists(victim), '該列從樹狀圖移除')

            app.tv.focus(tree_root)
            app.tv.selection_set(tree_root)
            n_before = len(s.dialogs)
            c.no_raise('_tree_action("del") 對根目錄',
                       lambda: app._tree_action('del'))
            c.ok(os.path.exists(t.root), '掃描根目錄沒有被刪掉')
            c.ok(any(d[1] == '不允許刪除'
                     for d in s.dialogs[n_before:]),
                 '對根目錄刪除會出現「不允許刪除」')

            # ---------------------------------------------- 分頁2: 勾選
            s.select_tab(1)
            gid = app.cv.get_children('')[0]
            leaf = app.cv.get_children(gid)[0]
            n0 = len(app.checked)
            c.no_raise('_on_click_clean 點群組列',
                       lambda: app._on_click_clean(
                           ev(x=30, y=s.row_y(app.cv, gid))))
            c.no_raise('_on_click_clean 點葉節點',
                       lambda: app._on_click_clean(
                           ev(x=30, y=s.row_y(app.cv, leaf))))
            c.no_raise('_on_click_clean 點在樹欄之外',
                       lambda: app._on_click_clean(ev(x=700, y=5)))
            app.cv.focus(leaf)
            c.no_raise('_on_space_clean', lambda: app._on_space_clean(None))
            c.no_raise('_on_dbl_clean -> 開啟位置',
                       lambda: app._on_dbl_clean(None))
            c.no_raise('_clean_copy', lambda: app._clean_copy())
            c.no_raise('_popup_clean',
                       lambda: app._popup_clean(
                           ev(x=30, y=s.row_y(app.cv, leaf),
                              x_root=10, y_root=10)))
            c.no_raise('_toggle 不存在的 iid',
                       lambda: app._toggle('nonexistent'))
            c.no_raise('_toggle 空 iid', lambda: app._toggle(''))
            c.ok(isinstance(n0, int), '勾選狀態仍可讀取')

            # 全選 / 全不選 / 只選安全
            app.select_all(True)
            root.update()
            all_leaves = [x for lv in app.group_leaves.values() for x in lv]
            c.eq(len(app.checked), len(all_leaves), '全選勾到每一列')
            total = sum(app.entry_meta[i].size for i in app.checked)
            c.ok(M.human(total) in app.sel_lbl.cget('text'),
                 '已選標籤顯示正確的總大小', app.sel_lbl.cget('text'))
            app.select_all(False)
            root.update()
            c.eq(len(app.checked), 0, '全部取消')
            c.ok('0 B' in app.sel_lbl.cget('text'), '已選標籤歸零')
            app.select_risk(M.RISK_SAFE)
            root.update()
            safe_leaves = [x for gid2, lv in app.group_leaves.items()
                           for x in lv
                           if M.RISK_SAFE in app.cv.item(gid2, 'tags')]
            c.eq(sorted(app.checked), sorted(safe_leaves),
                 '「只選安全」勾到的正好是安全群組的列')

            # 群組列的核取記號要跟子列同步
            for gid2, leaves in app.group_leaves.items():
                mark = app.cv.item(gid2, 'text')[:1]
                allon = all(x in app.checked for x in leaves)
                c.eq(mark, '☑' if allon else '☐',
                     '群組核取記號與子列同步 (%s)'
                     % app.cv.item(gid2, 'text')[3:15])

            # ---------------------------------------------- 分頁3/4/5
            s.select_tab(2)
            c.ok(len(app.bv.get_children('')) > 0, '最大檔案分頁有資料')
            app.bv.focus(app.bv.get_children('')[0])
            c.no_raise('_big_open', lambda: app._big_open())
            app.bv.focus('')
            c.no_raise('_big_open 沒有選取任何列', lambda: app._big_open())
            s.select_tab(3)
            c.ok(len(app.ev.get_children('')) > 0, '類型統計分頁有資料')
            s.select_tab(4)
            c.no_raise('讀取失敗分頁可以渲染', lambda: app.refresh_err())

            # ---------------------------------------------- 大小篩選重繪
            for v in ('全部', '100 KB', '1 MB', '10 MB', '100 MB'):
                app.minsize_var.set(v)
                c.no_raise('大小篩選 %s 重繪樹狀圖' % v,
                           lambda: (app.refresh_tree(), root.update()))
            app.minsize_var.set('全部')
            app.refresh_tree()

            # ---------------------------------------------- _trim_entry
            s.select_tab(1)
            app.refresh_clean()
            root.update()
            target = None
            for iid, e in app.entry_meta.items():
                if e.items and all(os.path.isfile(p) for p, _s in e.items):
                    target = (iid, e)
                    break
            c.ok(target is not None, '找到一列純檔案的項目')
            if target:
                iid, e = target
                e.items = e.items + [(t.path('tmp', 'never-existed.tmp'), 10)]
                e.count = len(e.items)
                e.size += 10
                os.remove(e.items[0][0])       # 假裝第一個真的被刪掉了
                c.no_raise('_trim_entry', lambda: app._trim_entry(iid))
                after = app.entry_meta.get(iid)
                c.ok(after is None or after.count < e.count
                     or not app.cv.exists(iid),
                     '_trim_entry 把已消失的路徑清掉')
                c.no_raise('_trim_entry 再跑一次 (幂等)',
                           lambda: app._trim_entry(iid))

            # ---------------------------------------------- 其他入口
            c.no_raise('browse (對話框取消)', lambda: app.browse())
            c.no_raise('stop_current 在閒置時', lambda: app.stop_current())
            app.mode = 'scan'
            c.no_raise('stop_current 在掃描時', lambda: app.stop_current())
            app.mode = 'delete'
            c.no_raise('stop_current 在刪除時', lambda: app.stop_current())
            app.mode = 'idle'
            app._rb_info = (12345, 7)        # 假裝背景查到回收桶裡有東西
            c.no_raise('do_empty_recycle_bin (用快取的數量)',
                       lambda: app.do_empty_recycle_bin())
            c.ok(s.pump(30, lambda: app.mode == 'idle'),
                 '清空回收桶之後回到閒置')
            c.eq(len(s.bin_emptied), 1,
                 '真的走到清空回收桶那一步 (但被攔下來, 沒動使用者的資料)')
            app._rb_info = (0, 0)
            n_before = len(s.dialogs)
            c.no_raise('do_empty_recycle_bin 在回收桶是空的時候',
                       lambda: app.do_empty_recycle_bin())
            c.ok(any(d[1] == '回收桶是空的' for d in s.dialogs[n_before:]),
                 '回收桶是空的時候直接告知, 不啟動工作')
            c.eq(len(s.bin_emptied), 1, '沒有再多清一次')
            c.no_raise('busy 中不接受新的掃描',
                       lambda: (setattr(app, 'mode', 'delete'),
                                app.start_scan(),
                                setattr(app, 'mode', 'idle')))
            c.no_raise('on_close', lambda: app.on_close())
    return c.done()


if __name__ == '__main__':
    H.main(run)
