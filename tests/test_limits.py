# -*- coding: utf-8 -*-
"""各種上限、世代守衛、以及訊息處理的邊界。

這些路徑平常很難碰到 (要幾十萬筆垃圾檔、要一堆讀取失敗、要連按兩次掃描),
但它們算的是會顯示給使用者的數字, 算錯了不會有人發現。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def run():
    c = H.Checks('limits')

    # ------------------------------------------- MAX_JUNK_FILES 與 omitted
    for workers in (1, 3):
        M = H.load_app('dc_junkcap_%d' % workers)
        M.Scanner.MAX_JUNK_FILES = 50            # 調小才測得到
        with H.Tree() as t:
            total = 200
            sizes = {}
            for i in range(total):
                p = t.file('j/f%03d.tmp' % i, 10 + i)
                sizes[p] = 10 + i
            res = H.scan(M, t.root, workers=workers)
            listed = [j for j in res.junk if not j['is_dir']]
            n_om, sz_om = res.junk_omitted
            c.eq(len(listed), 50,
                 '%d workers: 垃圾檔記錄數剛好等於上限' % workers)
            c.eq(len(listed) + n_om, total,
                 '%d workers: 列出的 + 省略的 = 全部 %d 筆'
                 % (workers, total))
            listed_sz = sum(j['size'] for j in listed)
            c.eq(listed_sz + sz_om, sum(sizes.values()),
                 '%d workers: 列出的大小 + 省略的大小 = 總大小' % workers)
            c.ok(n_om > 0 and sz_om > 0,
                 '%d workers: 真的有記到省略的統計 (%d 筆 / %d bytes)'
                 % (workers, n_om, sz_om))

            # UI 要把「另有 N 個未列出」那一列畫出來
            with H.Session(M, visible=False) as s:
                s.app.result = res
                s.app.refresh_clean()
                s.root.update()
                texts = [s.app.cv.item(i, 'text')
                         for i in s.app.cv.get_children('')]
                c.ok(any('未列出' in x for x in texts),
                     '%d workers: 可清理清單有顯示「另有 N 個未列出」'
                     % workers)
                c.ok(any(format(n_om, ',') in x for x in texts),
                     '%d workers: 那一列顯示正確的筆數' % workers)

    # ------------------------------------------------- MAX_ERR_PATHS 與合併
    for workers in (1, 3):
        M = H.load_app('dc_errcap_%d' % workers)
        M.Scanner.MAX_ERR_PATHS = 5
        with H.Tree() as t:
            for i in range(40):
                t.file('d%03d/x.txt' % i, 10)
            real_scandir = os.scandir
            fails = {t.path('d%03d' % i) for i in range(30)}

            def flaky_scandir(p=None, *a, **k):
                real = M.from_ext(p) if isinstance(p, str) else p
                if isinstance(real, str) and os.path.normcase(
                        os.path.abspath(real)) in {
                            os.path.normcase(os.path.abspath(x))
                            for x in fails}:
                    raise PermissionError(5, 'injected')
                return real_scandir(p, *a, **k)

            M.os = type('OS', (), {})()
            for attr in dir(os):
                if not attr.startswith('__'):
                    try:
                        setattr(M.os, attr, getattr(os, attr))
                    except (AttributeError, TypeError):
                        pass
            M.os.scandir = flaky_scandir
            try:
                res = H.scan(M, t.root, workers=workers)
            finally:
                M.os = os
            c.eq(res.errors, 30,
                 '%d workers: 讀取失敗的總數完整計入' % workers)
            c.ok(len(res.error_paths) <= 5,
                 '%d workers: 保留的失敗路徑受上限控制 (%d 筆)'
                 % (workers, len(res.error_paths)))
            c.ok(len(res.error_paths) > 0,
                 '%d workers: 至少留了一些失敗路徑' % workers)

            with H.Session(M, visible=False) as s:
                s.app.result = res
                s.app.refresh_err()
                s.root.update()
                rows = s.app.errv.get_children('')
                c.eq(len(rows), len(res.error_paths) + 1,
                     '%d workers: 失敗分頁列出保留的路徑 + 一列「另有 N 項」'
                     % workers)
                last = s.app.errv.item(rows[-1], 'values')[0]
                c.ok('另有' in last and format(
                    res.errors - len(res.error_paths), ',') in last,
                     '%d workers: 最後一列說明還有幾項沒列出' % workers, last)

    # ----------------------------------------------------- 世代守衛
    M = H.load_app('dc_gen')
    with H.Tree() as t:
        t.file('a/x.tmp', 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            res1 = s.scan(t.root)
            gen1 = app.gen
            t.file('b/y.tmp', 200)
            res2 = s.scan(t.root)
            c.ok(app.gen > gen1, '第二次掃描的世代編號有往前')
            c.ok(app.result is res2, '畫面上的結果是第二次的')
            # 塞一個過期的 done 進去, 不能覆蓋現在的結果
            app.q.put(('done', gen1, res1))
            s.pump(0.5)
            c.ok(app.result is res2, '過期的 done 訊息被丟棄')
            app.q.put(('progress', gen1, 999, 'stale'))
            app.q.put(('error', gen1, 'stale error'))
            n = len(s.dialogs)
            s.pump(0.5)
            c.eq(len(s.dialogs), n, '過期的 error 訊息不會彈出對話框')
            c.eq(app.mode, 'idle', '過期訊息不會改變忙碌狀態')

            # 非掃描期間的 progress 本來就該被忽略 (掃完了才收到就是過期的)
            before = app.prog_lbl.cget('text')
            app.q.put(('progress', app.gen, 123, 'not scanning'))
            s.pump(0.5)
            c.eq(app.prog_lbl.cget('text'), before,
                 'mode 不是 scan 時 progress 被忽略')

            # 不認識的訊息種類不能讓輪詢死掉。用 disk_info 當探針:
            # 它是唯一不看 gen/mode 的分支。
            app.q.put(('no_such_kind', 1, 2, 3))
            app.q.put((None,))
            s.pump(0.5)
            app._rb_info = None
            app.q.put(('disk_info', (1, 2), 'X:', (42, 7)))
            s.pump(1.0, lambda: app._rb_info == (42, 7))
            c.eq(app._rb_info, (42, 7),
                 '不認識/畸形的訊息之後輪詢仍在運作')

            # 一次灌大量訊息: 輪詢要撐得住, 而且最後要追上
            app.mode = 'scan'                 # progress 只在掃描中才會顯示
            try:
                for i in range(5000):
                    app.q.put(('progress', app.gen, i, 'flood%d' % i))
                s.pump(5.0, lambda: app.q.empty())
                c.ok(app.q.empty(), '5000 筆訊息在幾輪之內處理完')
                c.ok('flood4999' in app.prog_lbl.cget('text'),
                     '處理到最後一筆', app.prog_lbl.cget('text'))
            finally:
                app.mode = 'idle'

    # ----------------------------------------------------- _finish 沒有結果
    M = H.load_app('dc_noroot')
    with H.Session(M, visible=False) as s:
        empty = M.ScanResult()
        c.no_raise('_finish(root=None) 不會爆',
                   lambda: s.app._finish(empty))
        c.eq(s.app.mode, 'idle', '  並且回到閒置')
        c.ok('未完成' in s.app.prog_lbl.cget('text'),
             '  進度標籤說明掃描未完成', s.app.prog_lbl.cget('text'))
        c.eq(str(s.app.btn_del.cget('state')), 'disabled',
             '  刪除按鈕保持不可按')

    # ----------------------------------------------------- 磁碟查詢併發合併
    M = H.load_app('dc_diskq')
    with H.Session(M, visible=False) as s:
        started = []
        real = M.disk_usage_of

        def slow(p):
            started.append(p)
            return real(p)
        M.disk_usage_of = slow
        s.app._disk_busy = False
        s.app._disk_pending = False
        for _ in range(6):
            s.app._update_disk_label()
        c.ok(s.app._disk_busy, '第一次呼叫之後標記為忙碌')
        c.ok(s.app._disk_pending, '後續呼叫被併成一次待處理')
        c.ok(s.pump(20, lambda: not s.app._disk_busy
                    and not s.app._disk_pending),
             '待處理的那次跑完之後兩個旗標都清掉')
        c.ok(len(started) <= 3,
             '6 次連續請求只真的查了 %d 次 (併掉重複)' % len(started))

    # ----------------------------------------------------- 中止後的狀態列
    M = H.load_app('dc_cancel')
    with H.Tree() as t:
        for i in range(60):
            t.file('d%02d/x%d.tmp' % (i, i), 100)
        with H.Session(M, visible=False) as s:
            app = s.app
            app.path_var.set(t.root)
            app.start_scan()
            app.stop_evt.set()
            c.ok(s.pump(30, lambda: app.mode == 'idle'), '中止後回到閒置')
            c.ok(app.result is not None, '中止仍然產生了結果')
            c.ok(app.result.cancelled, '結果標記為已中止')
            c.ok('中止' in app.prog_lbl.cget('text'),
                 '進度標籤顯示「中止」', app.prog_lbl.cget('text'))
            c.eq(str(app.btn_del.cget('state')), 'normal',
                 '中止後仍可以刪除已經掃到的部分')
    return c.done()


if __name__ == '__main__':
    H.main(run)
