# -*- coding: utf-8 -*-
"""多 worker 的結果必須跟單執行緒逐項相同, 而且中止一定收得回來。

這是整套測試裡最重要的一個: 掃描核心是並行的, races 不會自己現形。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def build(t):
    """故意做成不平均 + 有各種特例, 讓 worker 之間的切分不對稱。"""
    # 一支很肥的子樹 (逼出負載不均)
    for i in range(40):
        for j in range(4):
            t.file('heavy/mod%02d/sub%d/a.py' % (i, j), 500 + i)
            t.file('heavy/mod%02d/sub%d/__pycache__/a.pyc' % (i, j), 100 + j)
    # 幾支很淺的
    for i in range(6):
        t.file('light%d/x.log' % i, 300 + i)
    # 一支很深的
    p = 'deep'
    for i in range(30):
        p += '/d%02d' % i
        t.file(p + '/f.tmp', 10 + i)
    # 保護 / 降級 / 連結相關
    t.file('proj/.git/logs/HEAD', 77)
    t.file('proj/.git/objects/x', 88)
    t.file('cachevcs/cache/.git/HEAD', 99)
    t.file('plaincache/cache/data.bin', 4096)
    t.dir('emptydir')
    t.file('big.bin', 3 * 1024 * 1024)
    for i in range(120):                       # 夠多檔案讓 top_files 換血
        t.file('many/f%03d.dat' % i, (i + 1) * 97)


def fingerprint(res, M):
    """UI 會讀到的每一件事, 都攤成可比對的形式。"""
    tree = sorted(
        (n.path, n.size, n.nfiles, len(n.children), n.junk, n.protected,
         n.has_vcs) for n in H.walk_nodes(res.root))
    return dict(
        total=(res.root.size, res.root.nfiles),
        dirs=res.dirs, links=res.links, errors=res.errors,
        tree=tree,
        junk=sorted((j['path'], j['size'], j['is_dir'], j['category'],
                     j['risk'], j['note']) for j in res.junk),
        ext=sorted(res.ext_stats.items()),
        top=sorted(res.top_files),
        omitted=list(res.junk_omitted),
        error_paths=sorted(res.error_paths),
    )


KEYS = ('total', 'dirs', 'links', 'errors', 'tree', 'junk', 'ext', 'top',
        'omitted', 'error_paths')


def run():
    M = H.load_app()
    c = H.Checks('parallel')
    reps = 2 if os.environ.get('DC_TEST_SLOW') else 1

    with H.Tree() as t:
        build(t)
        base = fingerprint(H.scan(M, t.root, workers=1), M)
        c.note('baseline (1 worker): dirs=%d files=%d junk=%d ext=%d top=%d'
               % (base['dirs'], base['total'][1], len(base['junk']),
                  len(base['ext']), len(base['top'])))
        c.ok(base['dirs'] > 200, '測試樹夠大, 值得平行 (%d 個資料夾)'
             % base['dirs'])

        for w in (2, 3, 4, 8):
            for rep in range(reps):
                f = fingerprint(H.scan(M, t.root, workers=w), M)
                diff = [k for k in KEYS if f[k] != base[k]]
                detail = ''
                if diff:
                    k = diff[0]
                    if k == 'tree':
                        sb = set(map(repr, base[k]))
                        sf = set(map(repr, f[k]))
                        detail = ('only-in-1w: %s | only-in-%dw: %s'
                                  % (list(sb - sf)[:2], w, list(sf - sb)[:2]))
                    else:
                        detail = '%s: %r vs %r' % (k, base[k], f[k])
                c.eq(diff, [], '%d workers (rep %d) 與單執行緒逐項相同'
                     % (w, rep), detail)

        # 中止: 一定要收得回來, 而且資料要自洽
        for w in (1, 3):
            for delay in (0.0, 0.02, 0.1):
                res = H.scan(M, t.root, workers=w, stop_after=delay)
                nodes = H.walk_nodes(res.root)
                bad = [n.name for n in nodes
                       if n.size < sum(k.size for k in n.children)]
                c.eq(bad, [], '%d workers 中止 %.2fs: 大小仍自洽 (%d 節點)'
                     % (w, delay, len(nodes)))
                c.ok(res.root.size >= 0 and res.root.nfiles >= 0,
                     '%d workers 中止 %.2fs: 統計非負' % (w, delay))

        # WORKERS 設成奇怪的值也不能壞
        for w in (0, -1, 1):
            res = H.scan(M, t.root, workers=w)
            c.eq(res.dirs, base['dirs'],
                 'WORKERS=%d 仍掃到全部資料夾 (至少開一個 worker)' % w)
    return c.done()


if __name__ == '__main__':
    H.main(run)
