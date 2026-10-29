# -*- coding: utf-8 -*-
"""垃圾判定規則。

不跟「改動前的版本」比 —— 那個參照點已經是歷史了。改成兩種自足的驗證:
  1. 一份人工挑選、明確寫出期望的案例表 (規則本身改了就會亮)。
  2. 一個寫在測試裡的樸素 fnmatch 迴圈當 oracle, 跟受測程式的
     單一 alternation regex 對跑大量隨機檔名 —— 守住那個加速改寫。
"""
import fnmatch
import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

# (檔名, 期望分類 or None)
FILE_CASES = [
    ('a.tmp', '暫存檔'), ('A.TMP', '暫存檔'), ('x.temp', '暫存檔'),
    ('run.log', '日誌檔'), ('RUN.LOG', '日誌檔'),
    ('core.dmp', '當機轉存檔'), ('x.stackdump', '當機轉存檔'),
    ('.x.swp', '編輯器暫存'),
    ('m.pyc', 'Python 快取'), ('m.pyo', 'Python 快取'),
    ('o.obj', '編譯中間檔'), ('a.o', '編譯中間檔'),
    ('f.crdownload', '未完成的下載'), ('g.part', '未完成的下載'),
    ('n.bak', '備份檔'), ('n.old', '備份檔'), ('n.orig', '備份檔'),
    ('thumbs.db', '系統縮圖快取'), ('Thumbs.db', '系統縮圖快取'),
    ('.DS_Store', '系統縮圖快取'), ('iconcache.db', '系統縮圖快取'),
    ('~$report.docx', '暫存／備份殘留'),
    ('doc~', '暫存／備份殘留'),
    ('app.log.3', '暫存／備份殘留'),
    ('data.old.12', '暫存／備份殘留'),
    ('core.1234', '暫存／備份殘留'),
    ('build.tmp.lock', '暫存／備份殘留'),
    ('~', '暫存／備份殘留'),          # '*~' 也吃得下只有一個 ~ 的檔名
    # 比對順序是「副檔名 -> 精確檔名 -> 樣式」, 所以 .old 這個副檔名規則
    # 會先命中, 分類是較保守的「備份檔／需確認」而不是「殘留／安全」
    ('app.log.old', '備份檔'),
    # 不該中的
    ('main.py', None), ('lib.dll', None), ('photo.jpg', None),
    ('readme', None), ('.gitignore', None), ('notes.txt', None),
    ('core.abc', None), ('app.log2', None), ('tmp', None),
    ('a.tmpx', None), ('mybak', None),
    ('thumbs.dbx', None), ('archive.tar.gz', None),
]

DIR_CASES = [
    ('__pycache__', 'Python 快取'), ('.pytest_cache', 'Python 快取'),
    ('.mypy_cache', 'Python 快取'), ('.tox', 'Python 快取'),
    ('cache', '應用程式／瀏覽器快取'), ('Cache', '應用程式／瀏覽器快取'),
    ('cache2', '應用程式／瀏覽器快取'),
    ('GPUCache', '應用程式／瀏覽器快取'),
    ('code cache', '應用程式／瀏覽器快取'),
    ('temp', '暫存資料夾'), ('tmp', '暫存資料夾'), ('.tmp', '暫存資料夾'),
    ('logs', '日誌資料夾'), ('log', '日誌資料夾'), ('LOGS', '日誌資料夾'),
    ('node_modules', 'node_modules'),
    ('.venv', 'Python 虛擬環境'), ('venv', 'Python 虛擬環境'),
    ('.gradle', '編譯中間檔'), ('intermediates', '編譯中間檔'),
    ('CMakeFiles', '編譯中間檔'),
    ('cmake-build-debug', '編譯中間檔'),      # glob 樣式
    ('cmake-build-', '編譯中間檔'),
    ('build', '建置輸出目錄'), ('dist', '建置輸出目錄'),
    ('out', '建置輸出目錄'), ('target', '建置輸出目錄'),
    # 不該中的
    ('src', None), ('my_cache_dir', None), ('logs2', None),
    ('temporary', None), ('cmake', None), ('cmake-buildx', None),
    ('.git', None), ('builds', None), ('outer', None),
]


def naive_file_rule(name, M):
    """oracle: 完全按規則表逐條比對, 不用受測程式的 regex 捷徑。"""
    low = name.lower()
    dot = low.rfind('.')
    if dot > 0:
        for exts, cat, risk, note in M.FILE_EXT_RULES:
            if low[dot:] in exts:
                return cat, risk, note
    for pats, cat, risk, note in M.FILE_NAME_RULES:
        for p in pats:
            if not M._has_glob(p):
                if low == p:
                    return cat, risk, note
    for pats, cat, risk, note in M.FILE_NAME_RULES:
        for p in pats:
            if M._has_glob(p) and fnmatch.fnmatchcase(low, p):
                return cat, risk, note
    return None


def naive_dir_rule(name, M):
    low = name.lower()
    for pats, cat, risk, note in M.DIR_RULES:
        for p in pats:
            if not M._has_glob(p) and low == p:
                return cat, risk, note
    for pats, cat, risk, note in M.DIR_RULES:
        for p in pats:
            if M._has_glob(p) and fnmatch.fnmatchcase(low, p):
                return cat, risk, note
    return None


def run():
    M = H.load_app()
    c = H.Checks('rules')

    bad = []
    for name, want in FILE_CASES:
        got = M.match_file_rule(name)
        cat = got[0] if got else None
        if cat != want:
            bad.append((name, cat, want))
    c.eq(bad, [], '%d 個人工檔名案例的分類都符合期望' % len(FILE_CASES))

    bad = []
    for name, want in DIR_CASES:
        got = M.match_dir_rule(name)
        cat = got[0] if got else None
        if cat != want:
            bad.append((name, cat, want))
    c.eq(bad, [], '%d 個人工資料夾案例的分類都符合期望' % len(DIR_CASES))

    # low/ext 快速路徑必須跟自己的慢路徑一致
    bad = []
    for name, _want in FILE_CASES:
        low = name.lower()
        dot = low.rfind('.')
        ext = low[dot:] if dot > 0 else None
        if M.match_file_rule(name) != M.match_file_rule(name, low, ext):
            bad.append(name)
    c.eq(bad, [], 'match_file_rule(name) 與 (name, low, ext) 結果相同')

    # regex 改寫 vs 樸素 fnmatch oracle: 大量隨機檔名
    random.seed(20240101)
    alpha = string.ascii_letters + '.$~-_0123456789 '
    names = [n for n, _w in FILE_CASES] + [n for n, _w in DIR_CASES]
    names += [''.join(random.choice(alpha)
                      for _ in range(random.randint(0, 18)))
              for _ in range(200000)]
    fmis = dmis = 0
    fex = dex = None
    for n in names:
        if M.match_file_rule(n) != naive_file_rule(n, M):
            fmis += 1
            fex = fex or n
        if M.match_dir_rule(n) != naive_dir_rule(n, M):
            dmis += 1
            dex = dex or n
    c.eq(fmis, 0, 'match_file_rule 對 %s 個檔名等於樸素 oracle'
         % format(len(names), ','), 'e.g. %r' % fex)
    c.eq(dmis, 0, 'match_dir_rule 對 %s 個名稱等於樸素 oracle'
         % format(len(names), ','), 'e.g. %r' % dex)

    # 沒有樣式被副檔名規則整個遮蔽 (那種樣式永遠不會生效)
    shadowed = []
    for pat, val in M._NAME_GLOB:
        if pat.endswith(('*', '?', ']')):
            continue                     # 結尾是萬用字元, 副檔名不固定
        dot = pat.rfind('.')
        if dot <= 0:
            continue
        ext = pat[dot:]
        if not M._has_glob(ext) and ext in M._EXT_MAP:
            shadowed.append((pat, ext, M._EXT_MAP[ext][0]))
    c.eq(shadowed, [],
         '沒有檔名樣式被 FILE_EXT_RULES 遮蔽 (比對順序: 副檔名先)',
         '這些樣式永遠不會生效')

    # regex 組出來的群組數要跟規則數一致, 否則 lastgroup 取值會錯位
    if M._NAME_GLOB_RE is not None:
        c.eq(M._NAME_GLOB_RE.groups, len(M._NAME_GLOB),
             '檔名 glob regex 的群組數 = 規則數 (lastgroup 才對得上)')
    if M._DIR_GLOB_RE is not None:
        c.eq(M._DIR_GLOB_RE.groups, len(M._DIR_GLOB),
             '資料夾 glob regex 的群組數 = 規則數')

    # 奇怪但合法的檔名不能讓比對爆掉
    weird = ['movie \U0001F600 .mp4', chr(0x6a94) + '.txt',
             'inv' + chr(0x202E) + 'cod.exe', 'e' + chr(0x0301) + '.log',
             'bad' + chr(0xD800) + '.tmp', 'a' + chr(1) + 'b.tmp',
             'two' + chr(10) + 'lines.tmp', 'a{b}[c].tmp',
             'a' + H.BS + 'b.tmp', '${HOME}.tmp', 'x' * 5000 + '.tmp', '']
    c.no_raise('奇怪檔名 (emoji/CJK/控制字元/lone surrogate/超長) 不會讓比對爆掉',
               lambda: [(M.match_file_rule(w), M.match_dir_rule(w))
                        for w in weird])
    bad = [w for w in weird
           if M.match_file_rule(w) != naive_file_rule(w, M)]
    c.eq(bad, [], '奇怪檔名的比對結果也等於 oracle')
    return c.done()


if __name__ == '__main__':
    H.main(run)
