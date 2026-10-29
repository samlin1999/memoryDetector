# -*- coding: utf-8 -*-
"""刪除的核心行為。

預設一律用「永久刪除」, 不去動使用者的資源回收桶。送回收桶那條路徑改用
攔截 _trash_windows 的方式驗證 (連 Shell 撞到被鎖檔案就放掉整批剩下項目
的行為都模擬進去), 只有 DC_TEST_TRASH=1 時才真的丟東西進回收桶。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

BS = H.BS


def run():
    M = H.load_app()
    c = H.Checks('delete')

    # ------------------------------------------------ 永久刪除的各種情形
    with H.Tree() as t:
        f = t.file('plain.tmp', 100)
        c.eq(M._try_delete(f, False, False), None, '刪一個普通檔案')
        c.ok(not os.path.exists(f), '檔案真的不見了')

        t.dir('tree/sub')
        t.file('tree/a.tmp', 100)
        t.file('tree/sub/b.tmp', 100)
        c.eq(M._try_delete(t.path('tree'), False, False), None, '刪整棵子樹')
        c.ok(not os.path.exists(t.path('tree')), '子樹真的不見了')

        t.file('container/x.tmp', 50)
        c.eq(M._try_delete(t.path('container'), False, True), None,
             '刪除後重建空容器')
        c.ok(os.path.isdir(t.path('container'))
             and not os.listdir(t.path('container')),
             '容器還在而且是空的')

        missing = t.path('never-existed')
        err = M._try_delete(missing, False, False)
        c.ok(isinstance(err, OSError), '刪不存在的路徑會回報錯誤',
             repr(err))

        ro = t.file('readonly.tmp', 100)
        os.chmod(ro, 0o444)
        c.eq(M._try_delete(ro, False, False), None,
             '唯讀檔案會先清掉屬性再刪')
        c.ok(not os.path.exists(ro), '唯讀檔案真的不見了')

        locked_path = t.file('locked.tmp', 100)
        holder = open(locked_path, 'rb')
        err = M._try_delete(locked_path, False, False)
        c.ok(err is not None, '被佔用的檔案會回報錯誤', repr(err))
        c.ok(os.path.exists(locked_path), '被佔用的檔案還在')
        holder.close()

        # 資料夾裡有一個被鎖住 -> PartialDelete
        t.file('partial/keep.tmp', 100)
        t.file('partial/gone.tmp', 100)
        holder = open(t.path('partial', 'keep.tmp'), 'rb')
        err = M._try_delete(t.path('partial'), False, False)
        c.ok(isinstance(err, M.PartialDelete),
             '只刪掉一部分時回報 PartialDelete', repr(err))
        if isinstance(err, M.PartialDelete):
            c.eq(err.remaining, 1, 'PartialDelete 記下剩幾個項目')
        c.ok(not os.path.exists(t.path('partial', 'gone.tmp')),
             '沒被鎖的那個已經刪掉')
        holder.close()

    # ------------------------------------------------ 連結只刪連結本身
    if os.name == 'nt':
        with H.Tree() as t:
            t.file('target/keep.txt', 500)
            import subprocess
            r = subprocess.run(['cmd', '/c', 'mklink', '/J',
                                t.path('link'), t.path('target')],
                               capture_output=True)
            if r.returncode == 0:
                c.eq(M._try_delete(t.path('link'), False, False), None,
                     '刪 junction')
                c.ok(not os.path.lexists(t.path('link')), 'junction 不見了')
                c.ok(os.path.exists(t.path('target', 'keep.txt')),
                     'junction 指向的真實資料沒被動到')
            else:
                c.note('跳過 junction 測試 (mklink 失敗)')

    # ------------------------------------------------ 長路徑
    with H.Tree() as t:
        p = 'deep'
        for i in range(60):
            p += '/d%02d' % i
        f = t.file(p + '/x.tmp', 100)
        c.ok(len(f) > 300, '測試路徑真的超過 MAX_PATH (%d 字元)' % len(f))
        c.eq(M._try_delete(t.path('deep'), False, False), None,
             '永久刪除吃得下超長路徑')
        c.ok(not os.path.exists(H._long(t.path('deep'))), '深樹不見了')

    # ------------------------------------------------ delete_many 批次語意
    with H.Tree() as t:
        paths = [t.file('m/f%03d.tmp' % i, 10) for i in range(150)]
        holder = open(paths[7], 'rb')
        jobs = [(p, False) for p in paths]
        out = M.delete_many(jobs, to_trash=False)
        c.eq(len(out), len(jobs), 'delete_many 回傳與輸入等長')
        oks = [p for p, e in zip(paths, out) if e is None]
        bad = [(p, e) for p, e in zip(paths, out) if e is not None]
        c.eq(len(bad), 1, 'delete_many: 只有被鎖的那一個失敗')
        c.eq([os.path.basename(p) for p, _e in bad], ['f007.tmp'],
             'delete_many: 失敗的正是被鎖的那一個')
        c.ok(all(not os.path.exists(p) for p in oks),
             '回報成功的都真的不在了')
        c.ok(all(os.path.exists(p) for p, _e in bad),
             '回報失敗的都還在')
        holder.close()
        c.eq(M.delete_many([], to_trash=False), [],
             'delete_many 吃得下空清單')

    # ------------------------------- 送回收桶: 攔下 shell 呼叫來驗證邏輯
    with H.Tree() as t:
        paths = [t.file('b/f%03d.tmp' % i, 10) for i in range(80)]
        locked_idx = 40
        holder = open(paths[locked_idx], 'rb')
        batches = []

        def fake_trash(plist):
            """模擬 SHFileOperationW: 撞到被鎖的就放掉整批剩下的項目。"""
            if isinstance(plist, str):
                plist = [plist]
            batches.append(list(plist))
            for p in plist:
                real = os.path.abspath(M.from_ext(p))
                try:
                    os.remove(M.to_ext(real))
                except OSError:
                    raise OSError('資源回收桶操作失敗 (code=32)')

        M._trash_windows = fake_trash
        M.IS_WINDOWS = True
        M._send2trash = None
        c.ok(M.can_batch_trash(True), 'can_batch_trash(True) 在這個組態下成立')
        c.ok(not M.can_batch_trash(False), 'can_batch_trash(False) 為否')

        out = M.delete_many([(p, False) for p in paths], to_trash=True)
        oks = [p for p, e in zip(paths, out) if e is None]
        bad = [(p, e) for p, e in zip(paths, out) if e is not None]
        c.ok(len(batches) > 1, '真的走了批次路徑 (%d 次 shell 呼叫處理 %d 筆)'
             % (len(batches), len(paths)))
        c.ok(max(len(b) for b in batches) <= M.TRASH_BATCH,
             '每批不超過 TRASH_BATCH')
        c.eq(len(bad), 1, '整批被 Shell 放掉之後, 只有被鎖的那筆算失敗')
        c.eq([os.path.basename(p) for p, _e in bad],
             [os.path.basename(paths[locked_idx])],
             '失敗的正是被鎖的那一筆 (其餘由逐筆重試救回)')
        c.ok(all(not os.path.exists(p) for p in oks),
             '回報成功的都真的不在了')
        c.eq(len(oks), len(paths) - 1, '其他 %d 筆全部成功' % (len(paths) - 1))
        holder.close()

    # 路徑太長送不進回收桶 -> 要明確回報, 不能默默算成功
    M2 = H.load_app()
    with H.Tree() as t:
        p = 'x'
        while len(t.path(p)) < 300:
            p += '/dddddddddd'
        f = t.file(p + '/f.tmp', 10)
        called = []
        M2._trash_windows = lambda pl: called.append(pl)
        M2.IS_WINDOWS = True
        M2._send2trash = None
        out = M2.delete_many([(f, False), (f, False)], to_trash=True)
        c.ok(all(e is not None for e in out),
             '超長路徑送回收桶會回報失敗 (%d 字元)' % len(f))
        c.ok(all('過長' in str(e) for e in out), '錯誤訊息說明是路徑過長',
             str(out[0]))
        c.eq(called, [], '超長路徑不會被塞進 shell 呼叫')

    # ------------------------------------------------ send2trash 分支
    import types
    fake = types.ModuleType('send2trash')
    hits = []
    fake.send2trash = lambda p: (hits.append(p), os.remove(M.to_ext(p)))[0]
    sys.modules['send2trash'] = fake
    try:
        M3 = H.load_app('dc_s2t')
        c.ok(M3._send2trash is not None, 'send2trash 有裝時會在載入時抓到')
        c.ok(not M3.can_batch_trash(True),
             '有 send2trash 時不批次 (交給它逐筆處理)')
        with H.Tree() as t:
            paths = [t.file('s/f%d.tmp' % i, 10) for i in range(5)]
            out = M3.delete_many([(p, False) for p in paths], to_trash=True)
            c.ok(all(e is None for e in out), 'send2trash 路徑全部成功')
            c.eq(len(hits), 5, 'send2trash 被呼叫 5 次')
            c.ok(all(not os.path.exists(p) for p in paths), '檔案都不見了')

        boom = types.ModuleType('send2trash')

        def raiser(_p):
            raise OSError('in use by another process')
        boom.send2trash = raiser
        sys.modules['send2trash'] = boom
        M4 = H.load_app('dc_s2t_err')
        with H.Tree() as t:
            f = t.file('s/f.tmp', 10)
            out = M4.delete_many([(f, False)], to_trash=True)
            c.ok(out[0] is not None, 'send2trash 拋錯時會回報失敗',
                 repr(out[0]))
    finally:
        sys.modules.pop('send2trash', None)

    # ------------------------------- 真的送回收桶 (只在 DC_TEST_TRASH=1)
    if os.environ.get('DC_TEST_TRASH'):
        M5 = H.load_app('dc_real_trash')
        with H.Tree() as t:
            paths = [t.file('r/DCTEST_%02d.tmp' % i, 32) for i in range(8)]
            holder = open(paths[3], 'rb')
            need = M5.com_init()
            try:
                out = M5.delete_many([(p, False) for p in paths],
                                     to_trash=True)
            finally:
                if need:
                    M5.com_uninit()
            bad = [os.path.basename(p)
                   for p, e in zip(paths, out) if e is not None]
            c.eq(bad, [os.path.basename(paths[3])],
                 '真的送回收桶: 只有被鎖的那一筆失敗')
            holder.close()
            c.note('注意: 7 個 DCTEST_*.tmp 已經被丟進你的資源回收桶')
    else:
        c.note('跳過真的送回收桶的測試 (要跑就加 --trash)')
    return c.done()


if __name__ == '__main__':
    H.main(run)
