# -*- coding: utf-8 -*-
"""純函式與資料表的不變量。跑得很快, 壞掉的話影響面很廣。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402


def run():
    M = H.load_app()
    c = H.Checks('units')
    BS = H.BS

    # ---------------------------------------------------------- human()
    for n, want in ((0, '0 B'), (1, '1 B'), (1023, '1023 B'),
                    (1024, '1.00 KB'), (1024 ** 2, '1.00 MB'),
                    (1024 ** 3, '1.00 GB'), (1024 ** 4, '1.00 TB'),
                    (1536, '1.50 KB')):
        c.eq(M.human(n), want, 'human(%d)' % n)
    c.ok(M.human(1024 ** 5).endswith('TB'), 'human() 超過 TB 就一直用 TB',
         M.human(1024 ** 5))
    c.no_raise('human() 吃得下負數與 float', lambda: (M.human(-5),
                                                    M.human(1.5)))

    # ---------------------------------------------------------- bar()
    c.ok(all(len(M.bar(f)) == 10 for f in (-1, 0, 0.5, 1, 2, 1e9)),
         'bar() 長度固定 = width')
    c.eq(M.bar(0), '·' * 10, 'bar(0) 全空')
    c.eq(M.bar(1), '█' * 10, 'bar(1) 全滿')
    c.eq(M.bar(-5), '·' * 10, 'bar() 負數往下夾')
    c.eq(M.bar(5), '█' * 10, 'bar() 超過 1 往上夾')
    c.eq(len(M.bar(0.5, 16)), 16, 'bar() 自訂寬度')

    # ------------------------------------------------- 長路徑前綴 round-trip
    for p in ('C:' + BS + 'short',
              'C:' + BS + 'a' + BS + ('x' * 300),
              BS + BS + 'server' + BS + 'share' + BS + 'file.txt',
              BS + BS + 'server' + BS + 'share' + BS + ('y' * 300)):
        rt = M.from_ext(M.force_long(p))
        c.eq(rt, os.path.normpath(os.path.abspath(p)),
             'force_long/from_ext round-trip (%d 字元)' % len(p))
    c.eq(M.to_ext('C:' + BS + 'short'), 'C:' + BS + 'short',
         'to_ext() 短路徑不動')
    long_p = 'C:' + BS + 'a' + BS + ('x' * 300)
    c.ok(M.to_ext(long_p).startswith(BS + BS + '?' + BS),
         'to_ext() 長路徑會加前綴')
    c.eq(M.to_ext(M.to_ext(long_p)), M.to_ext(long_p),
         'to_ext() 幂等 (不會疊兩層前綴)')
    unc = BS + BS + 'srv' + BS + 'sh' + BS + ('z' * 300)
    c.ok(M.to_ext(unc).startswith(BS + BS + '?' + BS + 'UNC' + BS),
         'to_ext() UNC 用 \\\\?\\UNC\\ 形式')

    # ---------------------------------------------------- in_protected_dir
    for p, want in (('C:' + BS + 'a' + BS + '.git' + BS + 'logs', True),
                    ('C:' + BS + 'a' + BS + '.git', True),
                    ('C:' + BS + 'a' + BS + '.GIT' + BS + 'x', True),
                    ('C:' + BS + 'a' + BS + '.svn', True),
                    ('C:' + BS + 'a' + BS + 'b', False),
                    ('C:' + BS + 'a' + BS + 'gitfoo', False),
                    ('C:' + BS + 'a' + BS + 'my.git.bak', False)):
        c.eq(M.in_protected_dir(p), want, 'in_protected_dir(...%s)'
             % p.split(BS)[-1])

    # ------------------------------------------------------- 風險表一致性
    risks = set()
    for rules in (M.DIR_RULES, M.FILE_EXT_RULES, M.FILE_NAME_RULES):
        for _pats, _cat, risk, _note in rules:
            risks.add(risk)
    c.eq(risks - set(M.RISK_ORDER), set(),
         '所有規則用的風險等級都在 RISK_ORDER 裡')
    c.eq(risks - set(M.RISK_LABEL), set(), '每個風險等級都有 RISK_LABEL')
    c.eq(risks - set(M.RISK_COLOR), set(), '每個風險等級都有 RISK_COLOR')
    cats = {r[1] for r in M.DIR_RULES}
    c.ok(M.RECREATE_DIR_CATEGORIES <= cats,
         'RECREATE_DIR_CATEGORIES 都是真的分類名',
         str(M.RECREATE_DIR_CATEGORIES - cats))

    # 規則裡不該有重複的樣式 (重複代表後面那條永遠不會生效)
    for name, rules, idx in (('DIR_RULES', M.DIR_RULES, 0),
                             ('FILE_EXT_RULES', M.FILE_EXT_RULES, 0),
                             ('FILE_NAME_RULES', M.FILE_NAME_RULES, 0)):
        seen, dup = set(), []
        for entry in rules:
            for pat in entry[idx]:
                if pat in seen:
                    dup.append(pat)
                seen.add(pat)
        c.eq(dup, [], '%s 沒有重複樣式' % name)
        low = [p for e in rules for p in e[idx] if p != p.lower()]
        c.eq(low, [], '%s 的樣式全是小寫 (比對前會 lower)' % name)

    # ---------------------------------------------------------- _min_bytes
    from tkinter import ttk
    with H.Session(M, visible=False) as s:
        want = {'全部': 0, '100 KB': 100 * 1024, '1 MB': 1024 ** 2,
                '10 MB': 10 * 1024 ** 2, '100 MB': 100 * 1024 ** 2}
        for k, v in want.items():
            s.app.minsize_var.set(k)
            c.eq(s.app._min_bytes(), v, '_min_bytes(%r)' % k)
        s.app.minsize_var.set('沒這個選項')
        c.eq(s.app._min_bytes(), 0, '_min_bytes() 不認識的值 -> 0 (顯示全部)')

        # 下拉選單的選項必須全部被 _min_bytes 認得, 否則篩選會默默失效
        combos = []
        stack = [s.root]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, ttk.Combobox):
                combos.append(w)
        c.eq(len(combos), 1, '找到大小篩選下拉選單')
        if combos:
            values = list(combos[0].cget('values'))
            c.eq(sorted(values), sorted(want),
                 '下拉選單的選項與 _min_bytes 的對照表完全一致')

    # ------------------------------------------------------- build_entries
    junk = []
    for i in range(700):
        junk.append(dict(
            path='C:' + BS + 'r' + BS + 'f%04d.tmp' % i, name='f%d' % i,
            size=(i + 1) * 10, is_dir=False,
            category='暫存檔' if i % 2 else '日誌檔',
            risk=M.RISK_SAFE if i % 3 else M.RISK_CHECK, note='n%d' % (i % 5)))
    groups = M.build_entries(junk, 'C:' + BS + 'r')
    tot_items = sum(g[4] for g in groups)
    tot_size = sum(g[3] for g in groups)
    c.eq(tot_items, len(junk), 'build_entries 沒有漏掉或重複任何項目')
    c.eq(tot_size, sum(j['size'] for j in junk), 'build_entries 總大小守恆')
    for cat, risk, _note, gtotal, gcount, entries in groups:
        c.eq(sum(e.count for e in entries), gcount,
             '群組 %s/%s 的列數加總 = 項目數' % (cat, risk))
        c.eq(sum(e.size for e in entries), gtotal,
             '群組 %s/%s 的列大小加總 = 群組總大小' % (cat, risk))
        c.ok(all(e.size == sum(sz for _p, sz in e.items) for e in entries),
             '群組 %s/%s 每列的大小 = 它所含路徑的大小和' % (cat, risk))
        c.ok(len(entries) <= M.MAX_PER_CATEGORY + 1,
             '群組 %s/%s 列數不超過上限+1 (合併列)' % (cat, risk),
             '%d 列' % len(entries))
    order = [M.RISK_ORDER.index(g[1]) for g in groups]
    c.eq(order, sorted(order), 'build_entries 依風險排序 (安全優先)')
    for i in range(len(groups) - 1):
        if groups[i][1] == groups[i + 1][1]:
            c.ok(groups[i][3] >= groups[i + 1][3],
                 '同風險內依可釋放空間遞減排序')
    c.eq(M.build_entries([], 'C:' + BS + 'r'), [],
         'build_entries 吃得下空清單')

    # ---------------------------------------------------------- _job_chunks
    jobs = ([('i', 'C:' + BS + 'p%03d' % k, 1, False) for k in range(500)]
            + [('i', 'C:' + BS + ('L' * 900), 1, False)]
            + [('i', 'C:' + BS + 'q%03d' % k, 1, False) for k in range(30)])
    for batched in (True, False):
        chunks = list(M.App._job_chunks(jobs, batched))
        flat = [j for ch in chunks for j in ch]
        c.eq(flat, jobs, '_job_chunks(batched=%s) 每筆剛好出現一次且順序不變'
             % batched)
        c.ok(all(ch for ch in chunks),
             '_job_chunks(batched=%s) 不產生空批次' % batched)
        cap = M.TRASH_BATCH if batched else 1
        c.ok(all(len(ch) <= cap for ch in chunks),
             '_job_chunks(batched=%s) 每批不超過 %d 筆' % (batched, cap),
             'max=%d' % max(len(ch) for ch in chunks))
        c.ok(all(sum(len(j[1]) + 1 for j in ch[:-1]) <= M.TRASH_BATCH_CHARS
                 for ch in chunks),
             '_job_chunks(batched=%s) 尊重字元預算' % batched)
    c.eq(list(M.App._job_chunks([], True)), [], '_job_chunks 吃得下空清單')

    # ------------------------------------------------------------ probe_path
    with H.Tree() as t:
        f = t.file('a.txt', 10)
        d = t.dir('sub')
        c.eq(M.probe_path(f), (True, False, False), 'probe_path(檔案)')
        c.eq(M.probe_path(d), (True, True, False), 'probe_path(目錄)')
        c.eq(M.probe_path(t.path('nope')), (False, False, False),
             'probe_path(不存在)')

    # ------------------------------------------------------------ grade_vcs
    cat, risk, note = M.grade_vcs('快取', M.RISK_SAFE, '說明')
    c.eq(risk, M.RISK_CHECK, 'grade_vcs 把 safe 降成 check')
    c.ok(M.VCS_WARN in note, 'grade_vcs 加上警告文字')
    again = M.grade_vcs(cat, risk, note)
    c.eq(again, (cat, risk, note), 'grade_vcs 幂等 (不會疊兩次警告)')

    # ------------------------------------------------------------ _grade
    plain = ('分類', M.RISK_SAFE, '說明')
    c.eq(M.Scanner._grade('C:' + BS + 'work' + BS + 'x', plain), plain,
         '_grade 對一般路徑不動')
    if M.SYSTEM_PREFIXES:
        sysdir = os.path.join(M.SYSTEM_PREFIXES[0], 'x')
        cat2, risk2, note2 = M.Scanner._grade(sysdir, plain)
        c.eq(risk2, M.RISK_CHECK, '_grade 把系統目錄裡的降成 check')
        c.ok('系統目錄' in note2, '_grade 加上系統目錄說明')
    return c.done()


if __name__ == '__main__':
    H.main(run)
