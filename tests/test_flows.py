# -*- coding: utf-8 -*-
"""使用者操作流程上的分支: 確認對話框選「否」、沒有選取、關窗時還在刪除、
沒有可清理項目、PartialDelete 走完整條 UI 等等。

這些分支只在特定回答/特定狀態下才會走到, 平常的測試都繞過去了。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def answer(M, value):
    """把確認對話框固定回答成 value, 並記錄被問了幾次。"""
    from tkinter import messagebox
    asked = []

    def ask(title, msg='', **kw):
        asked.append((title, msg))
        return value
    messagebox.askyesno = ask
    M.messagebox = messagebox
    return asked


def run():
    c = H.Checks('flows')

    # ------------------------------------------- 確認刪除時選「否」
    M = H.load_app('dc_no')
    with H.Tree() as t:
        for i in range(3):
            t.file('mod%d/__pycache__/a.pyc' % i, 500)
        t.file('x.tmp', 300)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            s.app.select_all(True)
            s.root.update()
            n_checked = len(s.app.checked)
            n_rows = len(s.app.entry_meta)
            asked = answer(M, False)
            s.app.do_delete()
            s.pump(1.0)
            c.ok(len(asked) == 1, '有彈出確認對話框')
            c.eq(s.app.mode, 'idle', '選「否」之後維持閒置, 沒有開始刪除')
            c.eq(len(s.app.entry_meta), n_rows, '清單一列都沒少')
            c.eq(len(s.app.checked), n_checked, '勾選狀態保持不變')
            c.ok(os.path.exists(t.path('x.tmp')), '檔案還在')
            c.ok(not s.app.stale, '沒有被標記為過期')

    # ------------------------------------------- 沒有選取任何項目就按刪除
    M = H.load_app('dc_nosel')
    with H.Tree() as t:
        t.file('x.tmp', 300)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            s.app.select_all(False)
            s.root.update()
            n = len(s.dialogs)
            s.app.do_delete()
            s.pump(0.5)
            c.ok(any(d[1] == '沒有選取' for d in s.dialogs[n:]),
                 '提示「沒有選取」')
            c.eq(s.app.mode, 'idle', '沒有進入刪除狀態')
            c.ok(os.path.exists(t.path('x.tmp')), '檔案還在')

    # ------------------------------------------- 完全沒有可清理項目
    M = H.load_app('dc_nojunk')
    with H.Tree() as t:
        t.file('src/main.py', 1000)
        t.file('README.md', 200)
        with H.Session(M, visible=False) as s:
            res = s.scan(t.root)
            c.eq(len(res.junk), 0, '這棵樹沒有垃圾')
            rows = s.app.cv.get_children('')
            c.eq(len(rows), 1, '可清理分頁只有一列提示')
            c.ok('沒有偵測到' in s.app.cv.item(rows[0], 'text'),
                 '那一列是「沒有偵測到可清理的暫存檔」',
                 s.app.cv.item(rows[0], 'text'))
            c.eq(len(s.app.checked), 0, '沒有東西被勾選')
            c.ok('0 B' in s.app.sel_lbl.cget('text'), '已選標籤是 0')
            n = len(s.dialogs)
            s.app.do_delete()
            s.pump(0.5)
            c.ok(any(d[1] == '沒有選取' for d in s.dialogs[n:]),
                 '此時按刪除只會提示沒有選取')

    # ------------------------------------------- 關窗時還在刪除
    M = H.load_app('dc_close')
    with H.Session(M, visible=False) as s:
        s.app.mode = 'delete'
        asked = answer(M, False)
        alive = [True]
        s.root.destroy = lambda: alive.__setitem__(0, False)
        s.app.on_close()
        c.ok(len(asked) == 1, '刪除中關窗會先問一次')
        c.ok(alive[0], '選「否」時視窗不關')
        c.ok(s.app.del_stop.is_set(), '不管選什麼都先叫停刪除')
        answer(M, True)
        s.app.on_close()
        c.ok(not alive[0], '選「是」才真的關閉')

    # ------------------------------------------- 樹狀圖右鍵刪除選「否」
    M = H.load_app('dc_treeno')
    with H.Tree() as t:
        t.file('logs/a.log', 400)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            root_iid = s.app.tv.get_children('')[0]
            victim = None
            for k in s.app.tv.get_children(root_iid):
                m = s.app.tree_meta.get(k)
                if m and m[0] == 'dir' and m[1].name == 'logs':
                    victim = k
            c.ok(victim is not None, '找到 logs 那一列')
            if victim:
                s.app.tv.focus(victim)
                asked = answer(M, False)
                s.app._tree_action('del')
                c.ok(len(asked) == 1, '有彈出確認')
                c.ok(os.path.exists(t.path('logs')),
                     '選「否」時資料夾還在')
                c.ok(s.app.tv.exists(victim), '那一列還在樹上')
                c.ok(not s.app.stale, '沒有標記為過期')

    # ------------------------------------------- 內含 repo 的資料夾要多一段警告
    M = H.load_app('dc_vcswarn')
    with H.Tree() as t:
        t.file('cache/.git/HEAD', 40)
        t.file('cache/data.bin', 5000)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            root_iid = s.app.tv.get_children('')[0]
            target = None
            for k in s.app.tv.get_children(root_iid):
                m = s.app.tree_meta.get(k)
                if m and m[0] == 'dir' and m[1].name == 'cache':
                    target = k
            c.ok(target is not None, '找到 cache 那一列')
            if target:
                flag = s.app.tv.item(target, 'values')[3]
                c.ok('版本控制' in flag,
                     '樹狀圖的可清理欄有標出內含版本控制資料', flag)
                s.app.tv.focus(target)
                asked = answer(M, False)
                s.app._tree_action('del')
                c.ok(asked and '版本控制' in asked[0][1],
                     '右鍵刪除的確認訊息帶上版本控制警告',
                     asked[0][1][:80] if asked else '')

    # ------------------------------------------- PartialDelete 走完整條 UI
    M = H.load_app('dc_partial')
    with H.Tree() as t:
        for i in range(4):
            t.file('proj%d/__pycache__/a.pyc' % i, 500)
            t.file('proj%d/__pycache__/b.pyc' % i, 500)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            s.app.select_all(True)
            s.root.update()
            n_rows = len(s.app.entry_meta)
            holder = open(t.path('proj1', '__pycache__', 'a.pyc'), 'rb')
            s.app.trash_var.set(False)
            s.app.do_delete()
            done = s.pump(60, lambda: s.app.mode == 'idle')
            holder.close()
            c.ok(done, '刪除結束')
            c.ok(any(d[1] == '部分完成' for d in s.dialogs),
                 '回報「部分完成」')
            msg = [d[2] for d in s.dialogs if d[1] == '部分完成']
            c.ok(msg and '部分' in msg[-1],
                 '訊息裡提到有資料夾只清掉一部分', (msg[-1][:120]
                                                if msg else ''))
            c.eq(len(s.app.entry_meta), 1,
                 '只留下那一列沒清完的 (原本 %d 列)' % n_rows)
            c.ok(os.path.isdir(t.path('proj1', '__pycache__')),
                 '被佔用的資料夾還在')
            c.ok(not os.path.exists(t.path('proj1', '__pycache__', 'b.pyc')),
                 '同一個資料夾裡沒被佔用的檔案已經清掉')
            for i in (0, 2, 3):
                c.ok(not os.path.exists(t.path('proj%d' % i, '__pycache__')),
                     'proj%d 的快取整個清掉' % i)

    # ------------------------------------------- 刪除到回收桶時的提醒文字
    M = H.load_app('dc_trashmsg')
    with H.Tree() as t:
        t.file('x.tmp', 300)
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            s.app.select_all(True)
            s.root.update()
            s.app.trash_var.set(True)
            asked = answer(M, False)
            s.app.do_delete()
            c.ok(asked, '有彈出確認')
            if asked:
                body = asked[0][1]
                c.ok('資源回收桶' in body, '確認訊息說明是送回收桶', body[:60])
                c.ok('還不會' in body or '清空回收桶' in body,
                     '並提醒空間要清空回收桶才會真的釋放')
            s.app.trash_var.set(False)
            asked2 = answer(M, False)
            s.app.do_delete()
            c.ok(asked2 and '永久刪除' in asked2[0][1],
                 '永久刪除的確認訊息有強調無法還原',
                 asked2[0][1][:60] if asked2 else '')

    # ------------------------------------------- 含非 safe 等級時要額外提醒
    M = H.load_app('dc_riskmsg')
    with H.Tree() as t:
        t.file('old.bak', 500)               # 需確認
        t.file('a.tmp', 300)                 # 安全
        with H.Session(M, visible=False) as s:
            s.scan(t.root)
            s.app.select_all(True)
            s.root.update()
            asked = answer(M, False)
            s.app.do_delete()
            c.ok(asked and '需確認' in asked[0][1],
                 '選到「需確認」等級時確認訊息會點名',
                 asked[0][1][:120] if asked else '')
    return c.done()


if __name__ == '__main__':
    H.main(run)
