# -*- coding: utf-8 -*-
"""掃描進行中檔案樹被人亂改。

目錄在走訪途中消失、檔案被新增/刪除/改名、整棵子樹在父層 scandir 之後才被砍。
這是真實世界的常態, 而 test_parallel 的等價比對用的是靜態的樹, 蓋不到。
"""
import os
import random
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def build(t, depth=4, width=4, files=5):
    made = [t.root]
    dirs = [t.root]
    for _d in range(depth):
        nxt = []
        for p in dirs:
            for w in range(width):
                q = os.path.join(p, 'd%d' % w)
                os.makedirs(H._long(q), exist_ok=True)
                nxt.append(q)
                made.append(q)
        dirs = nxt
    for p in made:
        for i in range(files):
            ext = ('.tmp', '.log', '.py', '.pyc', '.bak')[i % 5]
            try:
                with open(H._long(os.path.join(p, 'f%d%s' % (i, ext))),
                          'wb') as f:
                    f.write(b'x' * (64 * (i + 1)))
            except OSError:
                pass
    return made


def run():
    M = H.load_app()
    c = H.Checks('churn')
    reps = 12 if os.environ.get('DC_TEST_SLOW') else 6

    with H.Tree() as t:
        made = build(t)
        c.note('建了 %d 個資料夾當靶' % len(made))
        stats = [0, 0, 0, 0]
        stop = threading.Event()

        def churn(seed):
            rnd = random.Random(seed)
            while not stop.is_set():
                try:
                    p = rnd.choice(made)
                    op = rnd.randrange(4)
                    if op == 0:
                        with open(H._long(os.path.join(
                                p, 'n%d.tmp' % rnd.randrange(9999))),
                                'wb') as f:
                            f.write(b'y' * 128)
                        stats[0] += 1
                    elif op == 1:
                        names = [x for x in os.listdir(H._long(p))
                                 if os.path.isfile(
                                     H._long(os.path.join(p, x)))]
                        if names:
                            os.remove(H._long(os.path.join(
                                p, rnd.choice(names))))
                            stats[1] += 1
                    elif op == 2:
                        subs = [x for x in os.listdir(H._long(p))
                                if os.path.isdir(
                                    H._long(os.path.join(p, x)))]
                        if subs:
                            shutil.rmtree(H._long(os.path.join(
                                p, rnd.choice(subs))), ignore_errors=True)
                            stats[2] += 1
                    else:
                        subs = [x for x in os.listdir(H._long(p))
                                if os.path.isdir(
                                    H._long(os.path.join(p, x)))]
                        if subs:
                            src = os.path.join(p, rnd.choice(subs))
                            os.rename(H._long(src),
                                      H._long(src + '_r%d'
                                              % rnd.randrange(999)))
                            stats[3] += 1
                except OSError:
                    pass
                except Exception:                    # noqa: BLE001
                    pass

        for rep in range(reps):
            if rep and rep % 3 == 0:
                shutil.rmtree(H._long(t.root), ignore_errors=True)
                os.makedirs(H._long(t.root), exist_ok=True)
                made = build(t)
            stop.clear()
            threads = [threading.Thread(target=churn, args=(rep * 7 + i,),
                                        daemon=True) for i in range(3)]
            for th in threads:
                th.start()
            time.sleep(0.03)
            try:
                res = H.scan(M, t.root, workers=3 if rep % 2 else 1)
                failed = None
            except AssertionError as e:
                res, failed = None, str(e)
            stop.set()
            for th in threads:
                th.join(5)

            if failed:
                c.ok(False, 'rep %d 掃描正常結束' % rep, failed)
                continue
            c.ok(True, 'rep %d 掃描正常結束 (dirs=%d files=%d errors=%d)'
                 % (rep, res.dirs, res.root.nfiles, res.errors))
            nodes = H.walk_nodes(res.root)
            bad = [n.name for n in nodes
                   if n.size < sum(k.size for k in n.children)]
            c.eq(bad, [], 'rep %d 大小自洽' % rep)
            c.ok(res.root.size >= 0 and res.root.nfiles >= 0,
                 'rep %d 統計非負' % rep)
            c.ok(all(j['size'] >= 0 for j in res.junk),
                 'rep %d 沒有負的 junk 大小' % rep)
            rp = os.path.normcase(os.path.abspath(t.root))
            outside = [j['path'] for j in res.junk
                       if not os.path.normcase(
                           os.path.abspath(j['path'])).startswith(rp)]
            c.eq(outside, [], 'rep %d 所有 junk 路徑都在根目錄內' % rep)
            c.eq(len(nodes), res.dirs, 'rep %d 節點數 = 回報的資料夾數' % rep)

        c.note('churn 操作: 新增=%d 刪檔=%d 刪整棵=%d 改名=%d' % tuple(stats))
        c.ok(sum(stats) > 50, '真的有製造出足夠的干擾 (%d 次操作)'
             % sum(stats))
    return c.done()


if __name__ == '__main__':
    H.main(run)
