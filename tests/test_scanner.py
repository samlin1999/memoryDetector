# -*- coding: utf-8 -*-
"""掃描器: 樹的統計、垃圾分類、保護規則、降級規則、連結處理。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

BS = H.BS


def build(t):
    """一棵刻意做得很刁鑽的樹。"""
    for i in range(3):
        t.file('src/mod%d/a.py' % i, 4000)
        t.file('src/mod%d/__pycache__/a.pyc' % i, 1000)
    t.dir('empty_pycache/__pycache__')                 # 空的垃圾目錄
    t.file('nested/__pycache__/inner/deep.pyc', 500)   # 垃圾目錄內的垃圾檔
    t.file('nested/__pycache__/logs/x.log', 400)       # 垃圾目錄內的垃圾目錄
    t.file('proj/.git/logs/HEAD', 300)                 # 版本控制內部
    t.file('proj/.git/objects/ab/obj', 200)
    t.file('proj/.git/__pycache__/p.pyc', 150)         # 版本控制內的垃圾
    t.file('cache_with_git/cache/.git/HEAD', 90)       # 垃圾目錄內藏 repo
    t.file('cache_with_git/cache/data.bin', 6000)
    t.file('clean_cache/cache/data.bin', 7000)         # 同名但沒有 repo
    t.file('zero/empty.tmp', 0)                        # 0 bytes 垃圾檔
    t.file('top.log', 700)
    t.dir('totally_empty')
    t.file('deepvcs/temp/a/b/c/.svn/wc.db', 2000)      # repo 埋很深
    t.file('node_modules/pkg/index.js', 8000)
    t.file('outdir/out/artifact.bin', 9000)


def run():
    M = H.load_app()
    c = H.Checks('scanner')

    with H.Tree() as t:
        build(t)
        res = H.scan(M, t.root, workers=1)
        jm = H.junk_map(res, t.root)
        c.note('dirs=%d files=%d size=%s junk=%d errors=%d links=%d'
               % (res.dirs, res.root.nfiles, M.human(res.root.size),
                  len(res.junk), res.errors, res.links))
        for k in sorted(jm):
            c.note('  flagged: %-40s dir=%-5s %s/%s'
                   % (k, jm[k][0], jm[k][1], jm[k][2]))

        # ---- 分類 ----
        cases = [
            ('nested' + BS + '__pycache__', True, '垃圾目錄本身要列出'),
            ('nested' + BS + '__pycache__' + BS + 'inner' + BS + 'deep.pyc',
             False, '垃圾目錄內的垃圾檔不重複列'),
            ('nested' + BS + '__pycache__' + BS + 'logs', False,
             '垃圾目錄內的垃圾目錄不重複列'),
            ('proj' + BS + '.git' + BS + 'logs', False, '.git 內部受保護'),
            ('proj' + BS + '.git' + BS + '__pycache__', False,
             '.git 內的垃圾目錄仍受保護'),
            ('cache_with_git' + BS + 'cache', True, '藏著 repo 的快取目錄要列'),
            ('cache_with_git' + BS + 'cache' + BS + '.git', False,
             '垃圾目錄內的 .git 不另外列'),
            ('empty_pycache' + BS + '__pycache__', False,
             '空的 (0 bytes) 垃圾目錄不列'),
            ('zero' + BS + 'empty.tmp', True, '0 bytes 的垃圾檔仍要列'),
            ('top.log', True, '最上層的垃圾檔'),
            ('clean_cache' + BS + 'cache', True, '沒有 repo 的快取目錄要列'),
            ('deepvcs' + BS + 'temp', True, '藏著 repo 的暫存目錄要列'),
            ('node_modules', True, 'node_modules'),
            ('outdir' + BS + 'out', True, '建置輸出目錄'),
            ('src' + BS + 'mod0', False, '一般原始碼目錄不列'),
            ('totally_empty', False, '空目錄不列'),
        ]
        for path, want, why in cases:
            c.eq(path in jm, want, '%s: %s' % (why, path))

        # ---- 風險降級: 藏著版本控制資料的一律降成「需確認」 ----
        for path in ('cache_with_git' + BS + 'cache',
                     'deepvcs' + BS + 'temp'):
            if path in jm:
                c.eq(jm[path][2], M.RISK_CHECK,
                     '%s 因內含 repo 被降級' % path)
                c.ok(M.VCS_WARN in jm[path][3],
                     '%s 帶上版本控制警告' % path)
        if 'clean_cache' + BS + 'cache' in jm:
            c.eq(jm['clean_cache' + BS + 'cache'][2], M.RISK_SAFE,
                 '沒有 repo 的同名目錄維持 safe')
            c.ok(M.VCS_WARN not in jm['clean_cache' + BS + 'cache'][3],
                 '沒有 repo 的同名目錄不加警告')

        # ---- has_vcs 旗標 ----
        by_name = {}
        for n in H.walk_nodes(res.root):
            by_name.setdefault(n.name, []).append(n)
        for name, want in (('cache_with_git', True), ('deepvcs', True),
                           ('proj', True), ('clean_cache', False),
                           ('src', False), ('node_modules', False)):
            nodes = by_name.get(name, [])
            c.eq(all(n.has_vcs for n in nodes) if want
                 else all(not n.has_vcs for n in nodes), True,
                 'has_vcs(%s) == %s' % (name, want))

        # ---- 樹的統計不變量 ----
        nodes = H.walk_nodes(res.root)
        c.eq(len(nodes), res.dirs, '節點數 = 回報的資料夾數')
        bad = [n.name for n in nodes
               if n.size < sum(k.size for k in n.children)]
        c.eq(bad, [], '每個節點的 size >= 子節點 size 總和')
        bad = [n.name for n in nodes
               if n.nfiles < sum(k.nfiles for k in n.children)]
        c.eq(bad, [], '每個節點的 nfiles >= 子節點 nfiles 總和')
        c.ok(res.root.nfiles > 0 and res.root.size > 0, '根節點有統計到東西')

        # 檔案總數要跟磁碟上實際的數量一致
        real_files = real_dirs = 0
        for dp, dn, fn in os.walk(t.root):
            real_dirs += 1
            real_files += len(fn)
        c.eq(res.root.nfiles, real_files, '檔案總數與 os.walk 一致')
        c.eq(res.dirs, real_dirs, '資料夾總數與 os.walk 一致')

        # ---- Node.path 沿 parent 重建 ----
        bad = [n.name for n in nodes if not os.path.exists(M.to_ext(n.path))]
        c.eq(bad, [], '每個 Node.path 都指向真的存在的目錄')
        c.eq(res.root.path, os.path.abspath(t.root), '根節點的 path')

        # ---- top_files / ext_stats ----
        c.ok(len(res.top_files) <= M.Scanner.TOP_N, 'top_files 不超過上限')
        c.eq(res.top_files, sorted(res.top_files, reverse=True),
             'top_files 由大到小排序')
        c.eq(sum(v[0] for v in res.ext_stats.values()), real_files,
             'ext_stats 的檔案數加總 = 檔案總數')
        c.eq(sum(v[1] for v in res.ext_stats.values()), res.root.size,
             'ext_stats 的大小加總 = 總大小')

        # 每個 junk 路徑都必須在掃描根目錄底下
        rp = os.path.normcase(os.path.abspath(t.root))
        outside = [j['path'] for j in res.junk
                   if not os.path.normcase(
                       os.path.abspath(j['path'])).startswith(rp)]
        c.eq(outside, [], '所有 junk 路徑都在掃描根目錄內')

    # ---- 連結/junction 不進入 ----
    if os.name == 'nt':
        with H.Tree() as t:
            t.file('real/inside.txt', 5000)
            t.file('real/deeper/x.tmp', 100)
            import subprocess
            r = subprocess.run(['cmd', '/c', 'mklink', '/J',
                                t.path('link'), t.path('real')],
                               capture_output=True)
            if r.returncode == 0:
                res = H.scan(M, t.root, workers=1)
                c.eq(res.links, 1, 'junction 被跳過並計數')
                c.eq(res.root.nfiles, 2,
                     'junction 指向的檔案沒有被重複計算')
                names = [n.name for n in H.walk_nodes(res.root)]
                c.ok('link' not in names, 'junction 沒有變成樹上的節點')
            else:
                c.note('跳過 junction 測試 (mklink 失敗)')

    # ---- 掃描根目錄本身就在 .git 底下 -> 整棵樹受保護 ----
    with H.Tree() as t:
        t.file('.git/logs/HEAD', 100)
        t.file('.git/logs/x.log', 200)
        t.file('.git/__pycache__/a.pyc', 300)
        res = H.scan(M, t.path('.git'), workers=1)
        c.eq(len(res.junk), 0,
             '直接掃 .git 時整棵樹受保護, 一項都不列')

    # ---- 空目錄 / 單一檔案 ----
    with H.Tree() as t:
        res = H.scan(M, t.root, workers=1)
        c.eq((res.dirs, res.root.nfiles, res.root.size), (1, 0, 0),
             '空目錄: 1 個資料夾 0 個檔案')
        c.eq(len(res.junk), 0, '空目錄沒有可清理項目')
    with H.Tree() as t:
        t.file('only.tmp', 1234)
        res = H.scan(M, t.root, workers=1)
        c.eq((res.dirs, res.root.nfiles, res.root.size), (1, 1, 1234),
             '單一檔案的統計')
        c.eq(len(res.junk), 1, '單一垃圾檔被列出')
    return c.done()


if __name__ == '__main__':
    H.main(run)
