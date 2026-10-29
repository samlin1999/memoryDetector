#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
磁碟空間分析與暫存檔清理工具 (Disk Space Analyzer & Junk Cleaner)

功能:
  1. 掃描指定資料夾, 樹狀顯示每個子資料夾/檔案佔用的空間 (依大小排序)
  2. 自動辨識「可安全刪除」的暫存檔/快取/編譯產物, 並標示風險等級
  3. 勾選後一鍵刪除 (預設丟到「資源回收桶」, 可還原), 過程可中止
  4. 最大檔案排行 / 副檔名類型統計 / 讀取失敗清單

設計要點:
  * 掃描與刪除都在背景執行緒, 所有 GUI 更新一律由主執行緒經 queue 處理
  * 掃描採「明確堆疊」而非遞迴, 不受 recursion limit 限制
  * Windows 上自動使用 \\?\ 前綴處理超過 260 字元的長路徑
  * 樹上每個目錄只留總計數字, 不留檔案明細也不留完整路徑 (展開那一層時才
    重讀該目錄); 畫面插入筆數另有上限, 避免掃大磁碟時記憶體與 UI 爆掉
  * 送資源回收桶時整批一次呼叫 SHFileOperationW; shell 往返很貴, 逐筆刪
    幾萬個小檔案會慢上幾十分鐘

執行: python disk_cleaner.py     (不需安裝任何第三方套件)
"""

import os
import re
import sys
import time
import stat as statmod
import heapq
import queue
import shutil
import fnmatch
import threading
import subprocess
from collections import defaultdict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

IS_WINDOWS = (os.name == 'nt')

# ---------------------------------------------------------------- 風險等級
RISK_SAFE = 'safe'         # 可安全刪除, 程式會自動重建
RISK_REBUILD = 'rebuild'   # 可刪, 但需重新下載/重新編譯才能還原
RISK_CHECK = 'check'       # 請自行確認, 可能含有你要的東西

RISK_LABEL = {
    RISK_SAFE: '安全',
    RISK_REBUILD: '可重建',
    RISK_CHECK: '需確認',
}
RISK_COLOR = {
    RISK_SAFE: '#1a7f37',
    RISK_REBUILD: '#9a6700',
    RISK_CHECK: '#b42318',
}
RISK_ORDER = (RISK_SAFE, RISK_REBUILD, RISK_CHECK)

# ---------------------------------------------------------------- 垃圾規則
# 資料夾規則: (名稱樣式 tuple(全小寫), 分類, 風險, 說明)
DIR_RULES = [
    (('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
      '.ipynb_checkpoints', '.tox'),
     'Python 快取', RISK_SAFE, '下次執行或測試時自動重建'),

    (('.cache', 'cache', 'caches', 'cache2', 'gpucache', 'code cache',
      'cachedata', 'shadercache', 'dawncache', 'graphitedawncache',
      'webcache', 'thumbnails', 'crashpad', 'crashdumps',
      'component_crx_cache', 'crashreports', 'reportqueue'),
     '應用程式／瀏覽器快取', RISK_SAFE, '快取資料, 程式會自行重新產生'),

    (('temp', 'tmp', '.temp', '.tmp'),
     '暫存資料夾', RISK_SAFE, '暫存目錄, 刪除不影響程式運行'),

    (('logs', 'log'),
     '日誌資料夾', RISK_SAFE, '執行紀錄, 刪除不影響程式運行'),

    (('node_modules',),
     'node_modules', RISK_REBUILD, '可用 npm / yarn install 重新下載'),

    (('.venv', 'venv', 'virtualenv'),
     'Python 虛擬環境', RISK_REBUILD, '可用 requirements.txt 重建'),

    (('.gradle', '.cxx', 'intermediates', 'cmakefiles', 'cmake-build-*',
      '.vs', 'obj', '.dart_tool', '.parcel-cache', '.nuxt', '.svelte-kit',
      '.turbo', '.angular', '.next'),
     '編譯中間檔', RISK_REBUILD, '重新編譯即可還原'),

    (('build', 'dist', 'out', 'target', 'coverage', 'htmlcov'),
     '建置輸出目錄', RISK_CHECK, '編譯產物; 請確認內無你手動放的檔案'),
]

# 這些分類的資料夾是「容器」: 清空內容但保留資料夾本身。
# 例如整個刪掉 %LOCALAPPDATA%\Temp, 某些程式會因為找不到暫存目錄而出錯。
RECREATE_DIR_CATEGORIES = {
    '暫存資料夾',
    '日誌資料夾',
    '應用程式／瀏覽器快取',
}
_ALL_DIR_CATS = {r[1] for r in DIR_RULES}
assert RECREATE_DIR_CATEGORIES <= _ALL_DIR_CATS, (
    '分類名稱與 DIR_RULES 不一致: %s'
    % (RECREATE_DIR_CATEGORIES - _ALL_DIR_CATS))


# 副檔名規則: (副檔名 tuple(全小寫, 含點), 分類, 風險, 說明)
FILE_EXT_RULES = [
    (('.tmp', '.temp', '.chk', '.gid', '.$$$', '.~tmp'),
     '暫存檔', RISK_SAFE, '典型暫存檔'),
    (('.log',),
     '日誌檔', RISK_SAFE, '執行紀錄'),
    (('.dmp', '.mdmp', '.hdmp', '.stackdump'),
     '當機轉存檔', RISK_SAFE, '程式崩潰時的記憶體轉存'),
    (('.swp', '.swo', '.swn'),
     '編輯器暫存', RISK_SAFE, 'vim 等編輯器殘留'),
    (('.pyc', '.pyo'),
     'Python 快取', RISK_SAFE, '自動重新產生'),
    (('.o', '.obj', '.ilk', '.exp', '.ncb', '.sdf', '.idb', '.tlog'),
     '編譯中間檔', RISK_REBUILD, '重新編譯即可還原'),
    (('.crdownload', '.part', '.partial', '.download', '.opdownload'),
     '未完成的下載', RISK_CHECK, '下載中斷的殘檔'),
    (('.bak', '.old', '.orig', '.backup'),
     '備份檔', RISK_CHECK, '請先確認是否還需要'),
]

# 檔名樣式規則: (fnmatch 樣式 tuple(全小寫), 分類, 風險, 說明)
FILE_NAME_RULES = [
    (('thumbs.db', 'ehthumbs.db', 'ehthumbs_vista.db', 'iconcache.db',
      '.ds_store'),
     '系統縮圖快取', RISK_SAFE, '作業系統自動重建'),
    # 注意比對順序是「副檔名 -> 精確檔名 -> 樣式」, 所以樣式不要以某個已經在
    # FILE_EXT_RULES 裡的副檔名結尾 —— 那種樣式永遠不會生效。('*.log.old'
    # 就是這樣被 '.old' 遮住的, 已移除; tests/test_rules.py 會盯著這件事。)
    (('~$*', '*.tmp.*', '*~', '*.log.[0-9]*', '*.old.[0-9]*',
      'core.[0-9]*'),
     '暫存／備份殘留', RISK_SAFE, '編輯或輪替後留下的殘檔'),
]


# 版本控制的內部資料: 一律不當成垃圾。
# 例如 .git\logs 是 reflog, 刪掉 repo 還能用, 但 git reset --hard 之後
# 就再也救不回來 —— 省下的空間微不足道, 風險卻不可逆。
PROTECTED_DIRS = frozenset(('.git', '.svn', '.hg', '.bzr', 'cvs', '_darcs'))


def in_protected_dir(path):
    """路徑是否位於版本控制的內部資料夾底下。"""
    parts = os.path.normpath(path).split(os.sep)
    return any(p.lower() in PROTECTED_DIRS for p in parts[:-1]) or (
        parts[-1].lower() in PROTECTED_DIRS if parts else False)


VCS_WARN = '｜⚠ 內含 .git 等版本控制資料, 刪除會毀掉版本歷史'


def grade_vcs(cat, risk, note):
    """垃圾目錄底下藏著版本控制資料時, 降級為「需確認」並警告。

    PROTECTED_DIRS 的保護只往下 (.git 底下的東西不判垃圾), 不往上 —— 名字剛好
    命中規則的資料夾照樣會被當垃圾, 而刪除是遞迴的。像 cache / temp / logs 這幾
    類還是 RISK_SAFE (會自動勾選) 且屬於 RECREATE_DIR_CATEGORIES (清空內容),
    所以一個放在 temp\\ 底下的 repo 會被連根抹掉。這裡沿用 _grade() 對系統目錄
    的做法: 不從清單裡拿掉 (使用者可能真的要刪), 但絕不自動勾選。
    """
    if risk == RISK_CHECK and VCS_WARN in note:
        return cat, risk, note
    return cat, RISK_CHECK, note + VCS_WARN


def _system_prefixes():
    """需要特別小心的系統目錄 (正規化後的小寫前綴)。"""
    out = set()
    for env in ('SystemRoot', 'windir', 'ProgramFiles', 'ProgramFiles(x86)',
                'ProgramW6432'):
        v = os.environ.get(env)
        if v:
            out.add(os.path.normcase(os.path.normpath(v)) + os.sep)
    return tuple(sorted(out))


SYSTEM_PREFIXES = _system_prefixes()


def in_system_dir(path):
    """路徑是否位於 Windows 或 Program Files 底下。"""
    if not SYSTEM_PREFIXES:
        return False
    return os.path.normcase(os.path.normpath(path)).startswith(SYSTEM_PREFIXES)


def _has_glob(p):
    return '*' in p or '?' in p or '[' in p


# 規則預處理: 精確名稱走 dict/set (O(1)), 只有樣式才跑 fnmatch
_DIR_EXACT = {}
_DIR_GLOB = []
for _pats, _cat, _risk, _note in DIR_RULES:
    for _p in _pats:
        (_DIR_GLOB.append((_p, (_cat, _risk, _note))) if _has_glob(_p)
         else _DIR_EXACT.setdefault(_p, (_cat, _risk, _note)))

_EXT_MAP = {}
for _exts, _cat, _risk, _note in FILE_EXT_RULES:
    for _e in _exts:
        _EXT_MAP.setdefault(_e, (_cat, _risk, _note))

_NAME_EXACT = {}
_NAME_GLOB = []
for _pats, _cat, _risk, _note in FILE_NAME_RULES:
    for _p in _pats:
        (_NAME_GLOB.append((_p, (_cat, _risk, _note))) if _has_glob(_p)
         else _NAME_EXACT.setdefault(_p, (_cat, _risk, _note)))


def _build_glob_re(rules):
    """把一組 glob 規則併成單一 regex, 回傳 (regex, 依序對應的值)。

    這是掃描時的最熱路徑: 絕大多數檔名 (.py / .dll / .jpg …) 精確名稱與副檔名
    都不中, 於是每個檔案都要把樣式表整個跑一遍。併成一條 alternation 後只需
    比對一次, 再用 lastgroup 取回是哪一條規則中的 —— 實測快 2.9 倍。
    每個分支都以 fnmatch.translate() 產生 (結尾自帶 \\Z), 語意與逐條
    fnmatchcase() 完全相同。
    """
    if not rules:
        return None, []
    pat = '|'.join('(?P<g%d>%s)' % (i, fnmatch.translate(p))
                   for i, (p, _v) in enumerate(rules))
    return re.compile(pat), [v for _p, v in rules]


_NAME_GLOB_RE, _NAME_GLOB_VALS = _build_glob_re(_NAME_GLOB)
_DIR_GLOB_RE, _DIR_GLOB_VALS = _build_glob_re(_DIR_GLOB)


def _match_glob_re(low, regex, vals):
    if regex is None:
        return None
    m = regex.match(low)
    return vals[int(m.lastgroup[1:])] if m else None


def match_dir_rule(name):
    """判斷資料夾名稱是否符合垃圾規則, 回傳 (分類, 風險, 說明) 或 None。"""
    low = name.lower()
    hit = _DIR_EXACT.get(low)
    if hit:
        return hit
    return _match_glob_re(low, _DIR_GLOB_RE, _DIR_GLOB_VALS)


def match_file_rule(name, low=None, ext=None):
    """判斷檔名是否符合垃圾規則, 回傳 (分類, 風險, 說明) 或 None。

    low / ext 讓呼叫端把已經算好的小寫檔名與副檔名傳進來 (掃描時本來就要
    算一次做副檔名統計), 免得每個檔案重複做 lower() 與 rfind('.')。
    """
    if low is None:
        low = name.lower()
        dot = low.rfind('.')
        ext = low[dot:] if dot > 0 else None
    if ext:
        hit = _EXT_MAP.get(ext)
        if hit:
            return hit
    hit = _NAME_EXACT.get(low)
    if hit:
        return hit
    return _match_glob_re(low, _NAME_GLOB_RE, _NAME_GLOB_VALS)


# ---------------------------------------------------------------- 工具函式
def human(n):
    n = float(n)
    for unit, div in (('TB', 1024 ** 4), ('GB', 1024 ** 3),
                      ('MB', 1024 ** 2), ('KB', 1024)):
        if n >= div:
            return '%s %s' % (format(n / div, ',.2f'), unit)
    return '%d B' % int(n)


def bar(frac, width=10):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return '█' * filled + '·' * (width - filled)


# ----------------------------------------- Windows 長路徑 (>260 字元) 支援
LONG_PATH_THRESHOLD = 230


def to_ext(path):
    r"""需要時加上 \\?\ 前綴, 讓 os 呼叫能處理超長路徑。"""
    if not IS_WINDOWS or path.startswith('\\\\?\\'):
        return path
    if len(path) < LONG_PATH_THRESHOLD:
        return path
    p = os.path.abspath(path)
    if p.startswith('\\\\'):                      # UNC: \\server\share
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def force_long(path):
    r"""Windows: 無條件轉成 \\?\ 長路徑形式 (刪除整棵樹時必須)。"""
    if not IS_WINDOWS:
        return path
    p = os.path.normpath(os.path.abspath(from_ext(path)))
    if p.startswith('\\\\'):                          # UNC
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def from_ext(path):
    r"""去掉 \\?\ 前綴, 用於顯示。"""
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        return path[4:]
    return path


REPARSE_FLAG = getattr(statmod, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
DIR_FLAG = getattr(statmod, 'FILE_ATTRIBUTE_DIRECTORY', 0x10)


def is_reparse_stat(st):
    """由 stat 結果判斷是否為 reparse point (junction / symlink / 雲端佔位)。"""
    return bool(getattr(st, 'st_file_attributes', 0) & REPARSE_FLAG)


def probe_path(p):
    """一次 lstat 取得 (存在, 是否目錄, 是否連結)。

    刪除迴圈裡每個項目原本要跑 isdir + islink + isjunction + lstat, 一項就是
    4 個 syscall (實測 17.7us); 改成單次 lstat 後由屬性直接判斷。Windows 上
    看 st_file_attributes 的 DIRECTORY / REPARSE_POINT 旗標, 拿到的是「連結
    本身」的性質而不是它指向的目標, 正好是刪除時要的資訊。
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False, False, False
    if IS_WINDOWS:
        attrs = getattr(st, 'st_file_attributes', 0)
        return True, bool(attrs & DIR_FLAG), bool(attrs & REPARSE_FLAG)
    return True, statmod.S_ISDIR(st.st_mode), statmod.S_ISLNK(st.st_mode)


def reveal_in_explorer(path):
    path = from_ext(path)
    try:
        if IS_WINDOWS:
            if os.path.isdir(to_ext(path)):
                subprocess.Popen(['explorer', os.path.normpath(path)])
            else:
                subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(path)])
    except Exception:
        pass


def disk_usage_of(path):
    """回傳 (free, total); 失敗回傳 None。"""
    try:
        u = shutil.disk_usage(to_ext(os.path.abspath(path)))
        return u.free, u.total
    except (OSError, ValueError):
        return None


def free_bytes_of(path):
    u = disk_usage_of(path)
    return u[0] if u else None


def recycle_bin_info():
    """回傳全部磁碟的資源回收桶 (大小, 項目數); 失敗回傳 None。"""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD),
                ('i64Size', ctypes.c_int64),
                ('i64NumItems', ctypes.c_int64),
            ]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if hr != 0:
            return None
        return int(info.i64Size), int(info.i64NumItems)
    except Exception:
        return None


def empty_recycle_bin():
    """清空所有磁碟的資源回收桶 (永久刪除)。"""
    if not IS_WINDOWS:
        raise OSError('此平台不支援')
    import ctypes
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOSOUND = 0x4
    hr = ctypes.windll.shell32.SHEmptyRecycleBinW(
        None, None, SHERB_NOCONFIRMATION | SHERB_NOSOUND)
    # 0 = 成功; 回收桶本來就空的時候會回 E_UNEXPECTED, 視為成功
    if hr not in (0, -2147418113):
        raise OSError('清空回收桶失敗 (hr=0x%08X)' % (hr & 0xFFFFFFFF))


# ------------------------------------------------- 刪除 (優先送資源回收桶)
# 環境剛好有裝 send2trash 就優先用。這個 import 一定要在模組載入時做完:
# 失敗的 import 不會被 sys.modules 快取, 放在刪除迴圈裡等於每個項目都重走一次
# sys.path —— 實測 121us/次 (成功的 import 只要 0.72us), 10 萬個項目就是 12 秒。
try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None


def com_init():
    """在背景執行緒使用 Shell API 前初始化 COM。回傳是否需要 Uninitialize。"""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        COINIT_APARTMENTTHREADED = 0x2
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        return hr in (0, 1)          # S_OK / S_FALSE
    except Exception:
        return False


def com_uninit():
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


class PartialDelete(Exception):
    """資料夾只刪掉一部分, 其餘被程式佔用。"""

    def __init__(self, remaining):
        Exception.__init__(self, '仍有 %s 個項目被佔用'
                           % format(remaining, ','))
        self.remaining = remaining


def _count_children(p):
    """只數第一層, 用來判斷資料夾是否已清空 (避免整棵樹重走)。"""
    try:
        with os.scandir(p) as it:
            n = 0
            for _e in it:
                n += 1
                if n >= 1000:
                    break
            return n
    except OSError:
        return 0


TRASH_BATCH = 64            # 一次送幾個路徑給 SHFileOperationW
TRASH_BATCH_CHARS = 8000    # pFrom 緩衝區總長度上限, 不宜開太大
TRASH_PATH_LIMIT = 250      # 超過這個長度就送不進回收桶 (Shell 不吃長路徑)


def _trash_windows(paths):
    """用 SHFileOperationW 把一批項目送到資源回收桶 (可還原)。

    pFrom 本來就吃「NUL 分隔、雙 NUL 結尾」的路徑清單, 所以整批一次呼叫就行。
    每次呼叫的 shell 往返成本很高 —— 實測單檔逐筆刪要 22~28ms, 64 個一批
    降到 4ms/檔 (快約 6 倍); 幾萬個小暫存檔的差距是幾十分鐘。
    """
    import ctypes
    from ctypes import wintypes

    if isinstance(paths, str):        # 防呆: 傳字串進來會被逐字元拆開
        paths = [paths]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ('hwnd', wintypes.HWND),
            ('wFunc', wintypes.UINT),
            ('pFrom', wintypes.LPCWSTR),
            ('pTo', wintypes.LPCWSTR),
            ('fFlags', ctypes.c_uint16),
            ('fAnyOperationsAborted', wintypes.BOOL),
            ('hNameMappings', ctypes.c_void_p),
            ('lpszProgressTitle', wintypes.LPCWSTR),
        ]

    joined = '\0'.join(os.path.abspath(from_ext(p)).rstrip('\\')
                       for p in paths)
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = joined + '\0\0'            # 必須以兩個 NUL 結尾
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0:
        raise OSError('資源回收桶操作失敗 (code=%s)' % res)
    if op.fAnyOperationsAborted:
        raise OSError('操作被中止')


def delete_path(path, to_trash=True, recreate=False):
    """刪除檔案或資料夾。

    to_trash  -- True 送資源回收桶 (可還原), False 永久刪除
    recreate  -- 資料夾刪除後重建一個空的 (Temp / Cache 這類容器)

    成功回傳 'trash' 或 'perm';
    資料夾只清掉一部分 (其餘被佔用) 拋 PartialDelete;
    完全失敗拋 OSError。
    """
    state = _pre_delete(path)
    mode = _delete_once(path, to_trash, state[1], state[2])
    return _post_delete(state, recreate, mode)


def _pre_delete(path):
    """刪除前記下狀態 (單次 lstat), 交給 _post_delete 事後驗證。"""
    chk = to_ext(path)
    _exists, is_dir, is_link = probe_path(chk)
    was_dir = is_dir and not is_link              # 連結不當資料夾處理
    return (chk, is_dir, is_link, was_dir,
            _count_children(chk) if was_dir else 0)


def _post_delete(state, recreate, mode):
    """事後檢查: 檔案被程式鎖住時, Shell 會靜默跳過而不回報錯誤。"""
    chk, _is_dir, _is_link, was_dir, before = state
    if not os.path.exists(chk):
        if was_dir and recreate:
            try:
                os.makedirs(chk, exist_ok=True)   # 保留容器資料夾本身
            except OSError:
                pass
        return mode
    if not was_dir:
        raise OSError('項目仍存在, 可能被程式佔用')
    remaining = _count_children(chk)
    if remaining == 0:
        return mode          # 只剩空殼 (資料夾本身被開著), 內容已清掉
    if remaining >= before:
        raise OSError('資料夾無法刪除 (權限不足或被程式佔用)')
    raise PartialDelete(remaining)


def can_batch_trash(to_trash):
    """這批刪除能不能用 SHFileOperationW 整批送回收桶。"""
    return bool(to_trash and IS_WINDOWS and _send2trash is None)


def _try_delete(path, to_trash, recreate):
    """刪一筆; 成功回傳 None, 失敗回傳 Exception (不外拋)。"""
    try:
        delete_path(path, to_trash, recreate)
    except Exception as e:                        # noqa: BLE001
        return e
    return None


def _verify_delete(state, recreate, mode='trash'):
    """事後驗證一筆; 成功回傳 None, 失敗回傳 Exception。"""
    try:
        _post_delete(state, recreate, mode)
    except Exception as e:                        # noqa: BLE001
        return e
    return None


def chunk_by_budget(items, path_of, size=TRASH_BATCH,
                    chars=TRASH_BATCH_CHARS):
    """把待刪清單切成一次 shell 呼叫吃得下的批次。

    除了筆數上限, 總字元數也要設限 —— SHFileOperationW 的 pFrom 是一整塊
    緩衝區, 一次塞太多路徑進去並不安全。單筆就超過預算時自成一批。
    """
    buf, n = [], 0
    for it in items:
        length = len(path_of(it)) + 1
        if buf and (len(buf) >= size or n + length > chars):
            yield buf
            buf, n = [], 0
        buf.append(it)
        n += length
    if buf:
        yield buf


def delete_many(jobs, to_trash=True):
    """刪除一批 (路徑, 是否重建空資料夾); 回傳等長的 [None 或 Exception]。

    每一筆的成敗都由 _post_delete() 逐筆判定, 不看批次的回傳碼 —— Shell 本來
    就會靜默跳過被鎖住的檔案, 這份程式一開始就不信它的回傳值, 所以批次化不會
    讓判定變寬鬆。

    批次大小在這裡自己夾住而不是靠呼叫端: 組 pFrom 緩衝區的是這個函式, 上限就
    該由它保證, 免得哪天有人一次餵進上萬筆而組出一塊超大的緩衝區。
    """
    if len(jobs) < 2 or not can_batch_trash(to_trash):
        return [_try_delete(p, to_trash, r) for p, r in jobs]

    chunks = list(chunk_by_budget(jobs, lambda j: j[0]))
    if len(chunks) > 1:
        out = []
        for ch in chunks:
            out.extend(delete_many(ch, to_trash))
        return out

    states, send = [], []
    for path, _recreate in jobs:
        if len(os.path.abspath(from_ext(path))) >= TRASH_PATH_LIMIT:
            states.append(None)                   # 太長, 送不進回收桶
        else:
            states.append(_pre_delete(path))
            send.append(path)

    batch_err = None
    if send:
        try:
            _trash_windows(send)
        except OSError as e:
            batch_err = e        # 哪幾筆真的沒了仍由 _post_delete 認定

    out = []
    for (path, recreate), state in zip(jobs, states):
        if state is None:
            out.append(OSError('路徑過長, 無法送資源回收桶 (請改用永久刪除)'))
            continue
        err = _verify_delete(state, recreate)
        if err is not None and batch_err is not None:
            # Shell 一撞到被鎖住的檔案就會放掉整批剩下的項目 (實測 12 個裡第 6
            # 個被鎖, 後面 6 個全沒被處理)。所以批次回報失敗時剩下的逐筆再試
            # 一次, 免得一個被佔用的檔案拖著同批無辜的項目一起被判失敗。
            err = _try_delete(path, True, recreate)
        out.append(err)
    return out


def _delete_once(path, to_trash, is_dir=None, is_link=None):
    if is_dir is None or is_link is None:
        _exists, is_dir, is_link = probe_path(to_ext(path))
    if to_trash:
        abs_path = os.path.abspath(from_ext(path))
        if _send2trash is not None:            # 環境剛好有裝就優先用
            _send2trash(abs_path)
            return 'trash'
        if not IS_WINDOWS:
            raise OSError('此平台未支援資源回收桶, 請改用永久刪除')
        if len(abs_path) >= TRASH_PATH_LIMIT:
            raise OSError('路徑過長, 無法送資源回收桶 (請改用永久刪除)')
        _trash_windows([path])
        return 'trash'

    # 永久刪除一律用長路徑形式: 根目錄雖短, 底下仍可能有超過 260 字元的子項
    p = force_long(path)

    # 連結/junction 只移除連結本身, 絕不遞迴刪到它指向的真實資料
    if is_link:
        if os.path.isdir(p):
            os.rmdir(p)
        else:
            os.remove(p)
        return 'perm'

    if is_dir:
        def on_err(func, target, _exc):
            # 清掉唯讀屬性後重試一次; 仍失敗就跳過, 由 delete_path 事後判定
            _clear_readonly(target)
            try:
                func(target)
            except OSError:
                pass

        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=on_err)
        else:
            shutil.rmtree(p, onerror=lambda f, t, ei: on_err(f, t, ei[1]))
    else:
        try:
            os.remove(p)
        except PermissionError:
            _clear_readonly(p)
            os.remove(p)                           # 清掉唯讀後重試
    return 'perm'


def _clear_readonly(p):
    try:
        os.chmod(p, statmod.S_IWRITE)
    except OSError:
        pass


# ---------------------------------------------------------------- 掃描核心
NO_CHILDREN = ()            # 葉目錄共用; 絕對不要 append 到 Node.children


class Node(object):
    """樹狀圖的一個目錄。

    刻意只存 name + parent 而不存完整路徑: 掃 100 萬個檔案時完整路徑要
    2000 萬個字元 (實測 30.5MB), 而 path 完全可以沿 parent 串回來。
    也刻意不存目錄內的檔案明細: 那份資料只有「使用者展開的那一層」用得到,
    展開時重掃那一個目錄只要不到 1ms, 卻能省下 70MB 以上 (實測)。
    """

    __slots__ = ('name', 'parent', 'size', 'nfiles', 'children', 'junk',
                 'protected', 'has_vcs')

    def __init__(self, name, parent=None):
        self.name = name     # 根節點放完整路徑, 其餘放單層名稱
        self.parent = parent
        # 走訪期間 size/nfiles 只含「自己這一層」的檔案, 掃完由 _aggregate()
        # 後序加上子樹 —— 這樣 worker 之間不必共用任何節點狀態。
        self.size = 0
        self.nfiles = 0
        # 葉目錄佔多數, 共用一個空 tuple 省下每個節點一個 list 物件 (實測
        # 光是這些空 list 就 10.4MB)。有子目錄時由掃描器整份指派進來。
        self.children = NO_CHILDREN
        self.junk = None     # (分類, 風險, 說明) 若本身是垃圾目錄
        self.protected = False   # 位於 .git 等版本控制資料內, 不做垃圾判定
        self.has_vcs = False     # 底下藏有 .git 等版本控制資料 (由 _aggregate 標)

    @property
    def path(self):
        parts = []
        n = self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        if not parts:
            return n.name
        parts.append(n.name)
        parts.reverse()
        return os.path.join(*parts)


class ScanResult(object):
    def __init__(self):
        self.root = None
        self.junk = []          # list[dict]
        self.top_files = []     # list[(size, path)]
        self.ext_stats = {}     # ext -> [count, size]
        self.errors = 0
        self.error_paths = []   # 前 N 筆讀取失敗的路徑
        self.junk_omitted = [0, 0]   # 超過上限未列出的垃圾檔 [筆數, 大小]
        self.links = 0          # 跳過的連結/junction 數量
        self.dirs = 0
        self.elapsed = 0.0
        self.cancelled = False


class _Acc(object):
    """單一 worker 的區域累計值。

    走訪期間 worker 只寫自己的 _Acc 與自己負責的那個 Node, 彼此不共用可寫
    狀態; 全部跑完才由 _merge() 合併。共用的只有工作佇列與垃圾檔筆數上限。
    """

    __slots__ = ('junk', 'ext_stats', 'heap', 'errors', 'error_paths',
                 'links', 'dirs')

    def __init__(self):
        self.junk = []
        self.ext_stats = {}
        self.heap = []
        self.errors = 0
        self.error_paths = []
        self.links = 0
        self.dirs = 0


class Scanner(threading.Thread):
    """背景掃描執行緒, 透過 queue 回報進度與結果。

    不遞迴 (用明確工作佇列), 因此不受 recursion limit 與 stack 大小限制。
    目錄列舉由數個 worker 並行做: 掃描是 I/O bound (實測純列舉就佔 76% 的
    時間), 而 os.scandir 在 syscall 期間會放掉 GIL, 所以能真的重疊。
    """

    TOP_N = 500             # 最大檔案排行保留筆數
    MAX_ERR_PATHS = 200
    MAX_JUNK_FILES = 100000  # 個別垃圾「檔案」最多記錄筆數 (限制記憶體)
    # 實測 (100 萬檔的使用者資料夾, 各跑 5 次取中位數): 1 個 17.7s、3 個
    # 13.2s (1.34x)、6 個 18.1s —— 再多只會退化。每個檔案的 Python 處理
    # (副檔名統計、規則比對) 仍受 GIL 限制不會平行, 所以上限就在這附近。
    WORKERS = 3

    def __init__(self, root_path, out_q, stop_evt, gen):
        threading.Thread.__init__(self)
        self.daemon = True
        self.root_path = os.path.abspath(root_path)
        self.q = out_q
        self.stop = stop_evt
        self.gen = gen               # 掃描世代, 用來丟棄過期訊息
        self.res = ScanResult()
        self._last_report = 0.0
        self._junk_files = 0
        self._cond = threading.Condition()
        self._queue = []             # [(node, path, in_junk)] 待處理目錄 (LIFO)
        self._outstanding = 0        # 已派出但還沒處理完的目錄數
        self._accs = []
        self._worker_err = []

    def _report(self, path):
        """進度回報 (節流, 避免刷爆 GUI)。

        多個 worker 都會呼叫; _last_report 的競爭是無害的 (最壞就是多回報
        幾次), 所以不為它加鎖。
        """
        now = time.monotonic()
        if now - self._last_report > 0.12:
            self._last_report = now
            self.q.put(('progress', self.gen,
                        sum(a.dirs for a in self._accs), path))

    @staticmethod
    def _is_link_entry(e):
        """DirEntry 是否為連結目錄。Windows 的 junction 用 reparse 旗標判斷。"""
        try:
            if e.is_symlink():
                return True
            if IS_WINDOWS:
                return is_reparse_stat(e.stat(follow_symlinks=False))
        except OSError:
            return False
        return False

    @staticmethod
    def _grade(path, hit):
        """系統目錄裡的東西一律降級為「需確認」, 避免被自動勾選。"""
        cat, risk, note = hit
        if risk != RISK_CHECK and in_system_dir(path):
            return cat, RISK_CHECK, note + '｜位於系統目錄, 請自行確認'
        return cat, risk, note

    def _err(self, acc, path):
        acc.errors += 1
        if len(acc.error_paths) < self.MAX_ERR_PATHS:
            acc.error_paths.append(path)

    def _add_junk_file(self, acc, fpath, name, size, hit):
        """登錄一筆垃圾檔。筆數上限是全域的, 所以這裡要拿鎖。

        只有命中規則時才會走到 (百萬檔的掃描約一兩萬次), 拿鎖的成本可忽略;
        _grade() 會做 normpath/normcase, 刻意放在鎖外面。
        """
        with self._cond:
            if self._junk_files >= self.MAX_JUNK_FILES:
                # 超過上限只累計, 不保留路徑 (避免吃光記憶體)
                self.res.junk_omitted[0] += 1
                self.res.junk_omitted[1] += size
                return
            self._junk_files += 1
        cat, risk, note = self._grade(fpath, hit)
        acc.junk.append(dict(path=fpath, name=name, size=size, is_dir=False,
                             category=cat, risk=risk, note=note))

    def run(self):
        """一定會回報 done 或 error。

        主視窗在收到其中之一以前會一直停在「掃描中」, 按鈕全是 disabled ——
        所以這裡連 MemoryError 這種 BaseException 也要接下來變成 error 訊息,
        絕不能讓執行緒默默死掉。
        """
        t0 = time.monotonic()
        try:
            root = Node(self.root_path)        # 根節點的 name 就是完整路徑
            # 直接掃 .git 或它的子目錄時, 整棵樹都受保護
            root.protected = in_protected_dir(self.root_path)
            self._queue.append((root, self.root_path, False))
            self._outstanding = 1
            self._accs = [_Acc() for _ in range(max(1, self.WORKERS))]
            workers = [threading.Thread(target=self._work, args=(a,),
                                        daemon=True) for a in self._accs]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            if self._worker_err:
                raise self._worker_err[0]
            self._merge()
            self.res.root = self._aggregate(root)
            self.res.elapsed = time.monotonic() - t0
            self.res.cancelled = self.stop.is_set()
            msg = ('done', self.gen, self.res)
        except BaseException as e:                   # noqa: BLE001
            msg = ('error', self.gen, '%s: %s' % (type(e).__name__, e))
        try:
            self.q.put(msg)
        except Exception:                            # noqa: BLE001
            pass

    def _work(self, acc):
        """worker: 從共用佇列取目錄, 只寫自己的 acc 與自己取到的那個 Node。"""
        cond = self._cond
        try:
            while True:
                with cond:
                    while not self._queue and self._outstanding > 0:
                        cond.wait(0.05)
                    if not self._queue:
                        return                   # 沒工作也沒人在做 -> 收工
                    node, path, in_junk = self._queue.pop()

                kids = ()
                if not self.stop.is_set():
                    sub_in_junk = in_junk or (node.junk is not None)
                    names = self._read_dir(node, path, in_junk, acc)
                    if names:
                        kids = []
                        children = []
                        for name in names:
                            child = Node(name, node)
                            child.protected = (node.protected or
                                               name.lower() in PROTECTED_DIRS)
                            if not sub_in_junk and not child.protected:
                                child.junk = match_dir_rule(name)
                            children.append(child)
                            kids.append((child, os.path.join(path, name),
                                         sub_in_junk))
                        node.children = children

                with cond:
                    self._outstanding += len(kids) - 1
                    if kids:
                        self._queue.extend(kids)
                    # 只有真的新增了工作、或全部做完時才叫醒別人 (葉目錄佔
                    # 多數, 每個目錄都 notify 會把時間全花在喚醒上)
                    if kids or self._outstanding <= 0:
                        cond.notify_all()
        except BaseException as e:                   # noqa: BLE001
            with cond:
                self._worker_err.append(e)
                self._outstanding = 0                # 讓其他 worker 收工
                cond.notify_all()

    def _merge(self):
        """把各 worker 的累計值併回 self.res。"""
        res = self.res
        ext = res.ext_stats
        heap = []
        for a in self._accs:
            res.dirs += a.dirs
            res.links += a.links
            res.errors += a.errors
            res.junk.extend(a.junk)
            heap.extend(a.heap)
            for k, v in a.ext_stats.items():
                s = ext.get(k)
                if s is None:
                    ext[k] = v
                else:
                    s[0] += v[0]
                    s[1] += v[1]
            for p in a.error_paths:
                if len(res.error_paths) >= self.MAX_ERR_PATHS:
                    break
                res.error_paths.append(p)
        # 每個 worker 各留自己的前 TOP_N 名, 全域前 TOP_N 名一定在聯集裡
        heap.sort(reverse=True)
        res.top_files = heap[:self.TOP_N]

    def _aggregate(self, root):
        """後序把子樹的大小加總上去, 並登錄垃圾目錄。

        單執行緒做這一步, 所以走訪期間 worker 完全不必共用節點狀態。垃圾目錄
        也得等這裡才能登錄 —— 它要的是整棵子樹的最終大小。
        掃描被中止時這一步照樣正確: 沒掃到的子樹就是 0, 祖先反映已掃到的部分。

        43 萬個目錄要跑約 155ms 且是純 Python (拿著 GIL)。試過每 2 萬個節點
        time.sleep(0) 讓出 GIL, 各跑 3 次的結果 (687/567/715ms vs
        472/753/651ms) 落在雜訊裡, 看不出好處, 所以不留那段複雜度 ——
        掃描結束那一下的停頓主要來自 Tk 給約 3700 列做 layout, 不是這裡。
        """
        order = [root]
        i = 0
        while i < len(order):                # 先展成 BFS 順序
            order.extend(order[i].children)
            i += 1
        junk = self.res.junk
        for node in reversed(order):         # 反向 BFS: 子節點必定先處理完
            parent = node.parent
            if parent is not None:
                parent.size += node.size
                parent.nfiles += node.nfiles
            # 「底下有版本控制資料」往上傳: 走訪順序保證子節點都標好了
            if node.name.lower() in PROTECTED_DIRS:
                node.has_vcs = True
            if node.has_vcs and parent is not None:
                parent.has_vcs = True
            # junk 只在「祖先都不是垃圾目錄」時才會被設起來, 所以不用再查一次
            if node.junk is not None and node.size > 0:
                path = node.path
                cat, risk, note = self._grade(path, node.junk)
                if node.has_vcs:
                    cat, risk, note = grade_vcs(cat, risk, note)
                junk.append(dict(path=path, name=node.name, size=node.size,
                                 is_dir=True, category=cat, risk=risk,
                                 note=note))
        return root

    # -- 讀取單一目錄: 統計其中的檔案, 回傳子目錄名稱清單
    def _read_dir(self, node, path, in_junk, acc):
        acc.dirs += 1
        self._report(path)

        sub_in_junk = in_junk or (node.junk is not None)
        judge = not sub_in_junk and not node.protected   # 迴圈外先判一次
        subdirs = []
        ext_stats = acc.ext_stats
        heap = acc.heap
        top_n = self.TOP_N
        size_total = 0
        nfiles = 0

        try:
            it = os.scandir(to_ext(path))
        except OSError:
            self._err(acc, path)
            return subdirs

        try:
            with it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        self._err(acc, path + os.sep + e.name)
                        continue

                    if is_dir:
                        # 連結/junction 不進入: 否則會重複計算, 甚至無限迴圈
                        # (例: AppData\Local\Application Data 指回自己)
                        if self._is_link_entry(e):
                            acc.links += 1
                            continue
                        subdirs.append(e.name)
                        continue

                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        self._err(acc, path + os.sep + e.name)
                        continue

                    size_total += size
                    nfiles += 1

                    # 副檔名統計 (low / ext 下面判垃圾檔還要用, 只算一次)
                    low = e.name.lower()
                    dot = low.rfind('.')
                    ext = low[dot:] if dot > 0 else None
                    key = ext if ext is not None else '(無副檔名)'
                    s = ext_stats.get(key)
                    if s is None:
                        ext_stats[key] = [1, size]
                    else:
                        s[0] += 1
                        s[1] += size

                    # 最大檔案排行 (固定大小的最小堆)。組路徑不便宜 (實測每檔
                    # 2.1us), 所以只有真的要進榜時才組。
                    if len(heap) < top_n:
                        heapq.heappush(heap, (size, os.path.join(path, e.name)))
                    elif size > heap[0][0]:
                        heapq.heapreplace(heap,
                                          (size, os.path.join(path, e.name)))

                    # 垃圾檔判定 (垃圾目錄底下不重複列; 版本控制內不判定)
                    if judge:
                        fh = match_file_rule(e.name, low, ext)
                        if fh:
                            self._add_junk_file(
                                acc, os.path.join(path, e.name), e.name,
                                size, fh)
        except OSError:
            self._err(acc, path)

        node.size = size_total
        node.nfiles = nfiles
        return subdirs



# ------------------------------------------------- 可清理清單 (合併小項目)
MAX_PER_CATEGORY = 300      # 每個分類最多列幾筆, 其餘合併成一列


class Entry(object):
    """清理清單中的一列; 可能代表多個路徑 (小項目合併)。"""
    __slots__ = ('label', 'items', 'size', 'category', 'risk', 'note',
                 'count', 'is_dir')

    def __init__(self, label, items, size, category, risk, note, is_dir):
        self.label = label
        self.items = items          # list[(path, size)]
        self.size = size
        self.category = category
        self.risk = risk
        self.note = note
        self.count = len(items)
        self.is_dir = is_dir


def build_entries(junk_items, root):
    """把掃描到的垃圾項目依分類整理成顯示用的群組。"""
    by_cat = defaultdict(list)
    for j in junk_items:
        # 只用 (分類, 風險) 當鍵: 同一分類的資料夾規則與副檔名規則說明不同,
        # 若把說明也算進鍵裡, 同一個分類會被拆成兩列 (例: Python 快取)
        by_cat[(j['category'], j['risk'])].append(j)

    groups = []
    for (cat, risk), items in by_cat.items():
        items.sort(key=lambda x: x['size'], reverse=True)
        note = items[0]['note']            # 以最大項目的說明為代表
        entries = []
        for j in items[:MAX_PER_CATEGORY]:
            try:
                rel = os.path.relpath(j['path'], root)
            except ValueError:
                rel = j['path']
            entries.append(Entry(rel, [(j['path'], j['size'])], j['size'],
                                 cat, risk, note, j['is_dir']))
        rest = items[MAX_PER_CATEGORY:]
        if rest:
            tot = sum(x['size'] for x in rest)
            entries.append(Entry('（其他 %d 個較小項目）' % len(rest),
                                 [(x['path'], x['size']) for x in rest],
                                 tot, cat, risk, note, False))
        groups.append((cat, risk, note, sum(x['size'] for x in items),
                       len(items), entries))

    # 先按風險 (安全優先), 再按可釋放空間排序
    groups.sort(key=lambda g: (RISK_ORDER.index(g[1]), -g[3]))
    return groups


# ---------------------------------------------------------------- 主視窗
DISPLAY_CAP = 600           # 樹狀圖每層最多插入幾列


class App(object):

    # 展開一層時最多讀幾筆目錄項目 (見 _dir_files)。實測 scandir 2.7us/筆,
    # 所以 2 萬筆約 54ms —— 感覺不出來; 不設限的話幾十萬檔的目錄會凍住一秒以上。
    SCAN_ON_EXPAND_CAP = 20000

    def __init__(self, root):
        self.root = root
        root.title('磁碟空間分析 / 暫存檔清理工具')
        root.geometry('1100x730')
        root.minsize(920, 560)

        self.q = queue.Queue()
        self.stop_evt = threading.Event()      # 掃描中止
        self.del_stop = threading.Event()      # 刪除中止
        self.scanner = None
        self.result = None
        self.gen = 0                           # 掃描世代編號
        self.tree_meta = {}     # iid -> ('dir', Node) / ('file', path, size)
        self.entry_meta = {}    # iid -> Entry
        self.group_leaves = {}  # 群組 iid -> [葉 iid]
        self.checked = set()
        self.mode = 'idle'      # idle | scan | delete
        self.stale = False      # 資料已因刪除而過期
        self._disk_busy = False    # 磁碟/回收桶查詢中 (見 _update_disk_label)
        self._disk_pending = False
        self._rb_info = None       # 背景查到的 (回收桶大小, 項目數) 快取

        self._build_style()
        self._build_top()
        self._build_tabs()
        self._build_status()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.after(100, self._pump)

    # ------------------------------------------------------------ 外觀
    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use('vista' if IS_WINDOWS else 'clam')
        except tk.TclError:
            pass
        st.configure('Treeview', rowheight=22)
        st.configure('Big.TButton', padding=(12, 4))

    def _build_top(self):
        f = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        f.pack(fill='x')

        ttk.Label(f, text='目標資料夾：').pack(side='left')
        self.path_var = tk.StringVar(value=os.path.expanduser('~'))
        self.path_entry = ttk.Entry(f, textvariable=self.path_var)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self.path_entry.bind('<Return>', lambda e: self.start_scan())

        ttk.Button(f, text='瀏覽…', command=self.browse).pack(side='left')
        self.btn_scan = ttk.Button(f, text='開始掃描', style='Big.TButton',
                                   command=self.start_scan)
        self.btn_scan.pack(side='left', padx=(6, 0))
        self.btn_stop = ttk.Button(f, text='停止', command=self.stop_current,
                                   state='disabled')
        self.btn_stop.pack(side='left', padx=(4, 0))

        f2 = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        f2.pack(fill='x')
        self.prog = ttk.Progressbar(f2, mode='determinate', maximum=100)
        self.prog.pack(side='left', fill='x', expand=True)
        self.prog_lbl = ttk.Label(f2, text='就緒', width=48, anchor='w')
        self.prog_lbl.pack(side='left', padx=(8, 0))

    def _build_tabs(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill='both', expand=True, padx=10, pady=(0, 4))
        self._tab_tree()
        self._tab_clean()
        self._tab_big()
        self._tab_ext()
        self._tab_err()

    # -------- Tab 1: 資料夾大小
    def _tab_tree(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='  資料夾大小  ')

        bar_f = ttk.Frame(tab, padding=(0, 6))
        bar_f.pack(fill='x')
        ttk.Label(bar_f, text='只顯示大於：').pack(side='left')
        self.minsize_var = tk.StringVar(value='1 MB')
        cb = ttk.Combobox(bar_f, textvariable=self.minsize_var, width=8,
                          state='readonly',
                          values=('全部', '100 KB', '1 MB', '10 MB', '100 MB'))
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda e: self.refresh_tree())
        ttk.Label(bar_f, text='   (點三角形展開；右鍵可開啟位置或直接刪除)',
                  foreground='#666').pack(side='left', padx=6)

        body = ttk.Frame(tab)
        body.pack(fill='both', expand=True)
        cols = ('size', 'pct', 'items', 'flag')
        self.tv = ttk.Treeview(body, columns=cols, show='tree headings')
        self.tv.heading('#0', text='名稱')
        self.tv.heading('size', text='大小')
        self.tv.heading('pct', text='佔比')
        self.tv.heading('items', text='檔案數')
        self.tv.heading('flag', text='可清理')
        self.tv.column('#0', width=440, minwidth=220)
        self.tv.column('size', width=110, anchor='e', stretch=False)
        self.tv.column('pct', width=160, anchor='w', stretch=False)
        self.tv.column('items', width=80, anchor='e', stretch=False)
        self.tv.column('flag', width=190, anchor='w', stretch=False)

        sb = ttk.Scrollbar(body, orient='vertical', command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        for r, c in RISK_COLOR.items():
            self.tv.tag_configure(r, foreground=c)
        self.tv.tag_configure('dim', foreground='#888')

        self.tv.bind('<<TreeviewOpen>>', self._on_open)
        self.tv.bind('<Button-3>', self._popup_tree)
        self.tv.bind('<Button-2>', self._popup_tree)      # macOS

        self.menu_tree = tk.Menu(self.root, tearoff=0)
        self.menu_tree.add_command(label='在檔案總管中開啟',
                                   command=lambda: self._tree_action('open'))
        self.menu_tree.add_command(label='複製完整路徑',
                                   command=lambda: self._tree_action('copy'))
        self.menu_tree.add_separator()
        self.menu_tree.add_command(label='刪除（送資源回收桶）',
                                   command=lambda: self._tree_action('del'))

    # -------- Tab 2: 可清理項目
    def _tab_clean(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='  可清理項目  ')

        top = ttk.Frame(tab, padding=(0, 6))
        top.pack(fill='x')
        self.btn_safe = ttk.Button(top, text='只選「安全」項目',
                                   command=lambda: self.select_risk(RISK_SAFE))
        self.btn_safe.pack(side='left')
        self.btn_all = ttk.Button(top, text='全選',
                                  command=lambda: self.select_all(True))
        self.btn_all.pack(side='left', padx=4)
        self.btn_none = ttk.Button(top, text='全部取消',
                                   command=lambda: self.select_all(False))
        self.btn_none.pack(side='left')
        self.trash_var = tk.BooleanVar(value=True)
        self.chk_trash = ttk.Checkbutton(top, text='刪除到資源回收桶（可還原）',
                                         variable=self.trash_var)
        self.chk_trash.pack(side='left', padx=(14, 0))
        self.btn_del = ttk.Button(top, text='刪除選取項目', style='Big.TButton',
                                  command=self.do_delete, state='disabled')
        self.btn_del.pack(side='right')
        self.btn_rb = ttk.Button(top, text='清空回收桶…',
                                 command=self.do_empty_recycle_bin)
        self.btn_rb.pack(side='right', padx=(0, 8))
        self.sel_lbl = ttk.Label(top, text='已選 0 項 / 0 B',
                                 font=('', 10, 'bold'))
        self.sel_lbl.pack(side='right', padx=10)

        body = ttk.Frame(tab)
        body.pack(fill='both', expand=True)
        cols = ('size', 'risk', 'note')
        self.cv = ttk.Treeview(body, columns=cols, show='tree headings')
        self.cv.heading('#0', text='項目（點名稱可勾選 / 空白鍵切換）')
        self.cv.heading('size', text='大小')
        self.cv.heading('risk', text='風險')
        self.cv.heading('note', text='說明')
        self.cv.column('#0', width=500, minwidth=260)
        self.cv.column('size', width=110, anchor='e', stretch=False)
        self.cv.column('risk', width=80, anchor='center', stretch=False)
        self.cv.column('note', width=310, anchor='w')

        sb = ttk.Scrollbar(body, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        self.cv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        for r, c in RISK_COLOR.items():
            self.cv.tag_configure(r, foreground=c)
        self.cv.tag_configure('dim', foreground='#888')

        self.cv.bind('<Button-1>', self._on_click_clean)
        self.cv.bind('<space>', self._on_space_clean)
        self.cv.bind('<Double-1>', self._on_dbl_clean)
        self.cv.bind('<Button-3>', self._popup_clean)
        self.cv.bind('<Button-2>', self._popup_clean)

        self.menu_clean = tk.Menu(self.root, tearoff=0)
        self.menu_clean.add_command(label='在檔案總管中開啟',
                                    command=self._clean_open)
        self.menu_clean.add_command(label='複製完整路徑',
                                    command=self._clean_copy)

    # -------- Tab 3: 最大檔案
    def _tab_big(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='  最大檔案  ')
        self.bv = ttk.Treeview(tab, columns=('size', 'path'), show='headings')
        self.bv.heading('size', text='大小')
        self.bv.heading('path', text='完整路徑（雙擊開啟位置）')
        self.bv.column('size', width=110, anchor='e', stretch=False)
        self.bv.column('path', width=900, anchor='w')
        sb = ttk.Scrollbar(tab, orient='vertical', command=self.bv.yview)
        self.bv.configure(yscrollcommand=sb.set)
        self.bv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.bv.bind('<Double-1>', lambda e: self._big_open())

    # -------- Tab 4: 類型統計
    def _tab_ext(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='  類型統計  ')
        self.ev = ttk.Treeview(tab, columns=('ext', 'size', 'count', 'pct'),
                               show='headings')
        for c, t, w, a in (('ext', '副檔名', 140, 'w'),
                           ('size', '總大小', 120, 'e'),
                           ('count', '檔案數', 100, 'e'),
                           ('pct', '佔比', 300, 'w')):
            self.ev.heading(c, text=t)
            self.ev.column(c, width=w, anchor=a, stretch=(c == 'pct'))
        sb = ttk.Scrollbar(tab, orient='vertical', command=self.ev.yview)
        self.ev.configure(yscrollcommand=sb.set)
        self.ev.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

    # -------- Tab 5: 讀取失敗
    def _tab_err(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='  讀取失敗  ')
        ttk.Label(tab, padding=(0, 6), foreground='#666',
                  text='下列項目因權限不足、被程式鎖定或路徑過長而無法讀取，'
                       '其大小未列入統計：').pack(fill='x')
        self.errv = ttk.Treeview(tab, columns=('path',), show='headings')
        self.errv.heading('path', text='路徑')
        self.errv.column('path', width=1000, anchor='w')
        sb = ttk.Scrollbar(tab, orient='vertical', command=self.errv.yview)
        self.errv.configure(yscrollcommand=sb.set)
        self.errv.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

    def _build_status(self):
        bar_f = ttk.Frame(self.root)
        bar_f.pack(fill='x', side='bottom')
        self.status = ttk.Label(bar_f, anchor='w', padding=(10, 4),
                                relief='sunken',
                                text='選擇資料夾後按「開始掃描」')
        self.status.pack(side='left', fill='x', expand=True)
        self.disk_lbl = ttk.Label(bar_f, anchor='e', padding=(10, 4),
                                  relief='sunken', width=52)
        self.disk_lbl.pack(side='right')
        self._update_disk_label()

    def _update_disk_label(self):
        """顯示目標磁碟的可用空間與回收桶大小 (使用者真正在意的數字)。

        查詢一律丟背景: SHQueryRecycleBinW(NULL) 會走過每個磁碟的
        $Recycle.Bin, 實測回收桶只有 13 個項目就要 296ms, 而這個函式在
        啟動、選資料夾、掃描前後、刪除完成後都會被呼叫 —— 尤其「剛把幾萬個
        項目丟進回收桶之後」正是它最慢的時候, 放在主執行緒會直接凍住視窗。
        """
        if self._disk_busy:
            self._disk_pending = True        # 併掉重複請求, 不疊執行緒
            return
        self._disk_busy = True
        path = self.path_var.get().strip().strip('"') or os.getcwd()
        threading.Thread(target=self._disk_worker, args=(path,),
                         daemon=True).start()

    def _disk_worker(self, path):
        """一定會回報 disk_info, 否則 _disk_busy 永遠是 True, 狀態列就再也不更新。"""
        drive = path
        usage = rb = None
        need_uninit = False
        try:
            drive = os.path.splitdrive(os.path.abspath(path))[0] or path
            need_uninit = com_init()         # 背景執行緒用 Shell API 前先初始化
            usage, rb = disk_usage_of(path), recycle_bin_info()
        except BaseException:                # noqa: BLE001
            pass                             # 純顯示用的數字, 查不到就留空
        finally:
            if need_uninit:
                com_uninit()
        try:
            self.q.put(('disk_info', usage, drive, rb))
        except Exception:                    # noqa: BLE001
            pass

    def _render_disk_label(self, usage, drive, rb):
        parts = []
        if usage:
            free, total = usage
            parts.append('%s 可用 %s / %s' % (drive, human(free), human(total)))
        if rb and rb[0] > 0:
            parts.append('回收桶 %s' % human(rb[0]))
        try:
            self.disk_lbl.configure(text='    '.join(parts) if parts else '')
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 忙碌狀態
    @property
    def busy(self):
        return self.mode != 'idle'

    def _set_mode(self, mode):
        """統一切換按鈕可用狀態, 避免操作與背景工作互相踩到。"""
        self.mode = mode
        idle = (mode == 'idle')
        self.btn_scan.configure(state='normal' if idle else 'disabled')
        self.btn_stop.configure(state='disabled' if idle else 'normal')
        for w in (self.btn_safe, self.btn_all, self.btn_none,
                  self.chk_trash, self.btn_del, self.btn_rb):
            w.configure(state='normal' if idle else 'disabled')
        if idle and self.result is None:
            self.btn_del.configure(state='disabled')

    # ------------------------------------------------------------ 掃描流程
    def browse(self):
        d = filedialog.askdirectory(title='選擇要分析的資料夾',
                                    initialdir=self.path_var.get() or '/')
        if d:
            self.path_var.set(os.path.normpath(d))
            self._update_disk_label()

    def start_scan(self):
        if self.busy:
            return
        path = self.path_var.get().strip().strip('"')
        if not path:
            return
        path = os.path.expanduser(os.path.expandvars(path))
        if not os.path.isdir(to_ext(path)):
            messagebox.showerror('路徑錯誤', '找不到資料夾：\n%s' % path)
            return
        self._clear_all()
        self.result = None
        self.stale = False
        self._update_disk_label()
        self.gen += 1
        self.stop_evt = threading.Event()
        self._set_mode('scan')
        self.prog.configure(mode='indeterminate')
        self.prog.start(12)
        self.prog_lbl.configure(text='掃描中…')
        self.scanner = Scanner(path, self.q, self.stop_evt, self.gen)
        self.scanner.start()

    def stop_current(self):
        if self.mode == 'scan':
            self.stop_evt.set()
            self.prog_lbl.configure(text='正在停止掃描…')
        elif self.mode == 'delete':
            self.del_stop.set()
            self.prog_lbl.configure(text='正在停止刪除…')

    def on_close(self):
        if self.mode == 'delete':
            self.del_stop.set()
            if not messagebox.askyesno(
                    '刪除進行中',
                    '刪除尚未完成。強制關閉可能留下只刪一半的資料夾。\n\n'
                    '確定要立刻關閉嗎？（按「否」可等它結束）'):
                return
        self.stop_evt.set()
        self.del_stop.set()
        self.root.destroy()

    def _pump(self):
        """主執行緒定時取出背景訊息並更新畫面。

        任何處理失敗都不能中斷輪詢, 否則整個 UI 會停止更新。
        """
        try:
            for _ in range(2000):                # 單次最多處理筆數, 保持順暢
                try:
                    msg = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(msg)
                except tk.TclError:
                    return                       # 視窗已關閉
                except Exception as e:           # noqa: BLE001
                    try:
                        self.status.configure(text='內部錯誤: %s' % e)
                    except tk.TclError:
                        return
        finally:
            try:
                self.root.after(100, self._pump)
            except tk.TclError:
                pass

    def _handle(self, msg):
        kind = msg[0]
        if kind == 'progress':
            _, gen, dirs, path = msg
            if gen != self.gen or self.mode != 'scan':
                return                           # 過期訊息, 丟棄
            p = path if len(path) < 62 else '…' + path[-59:]
            self.prog_lbl.configure(text='已掃 %s 個資料夾 | %s'
                                         % (format(dirs, ','), p))
        elif kind == 'done':
            _, gen, res = msg
            if gen != self.gen:
                return
            self._finish(res)
        elif kind == 'error':
            _, gen, text = msg
            if gen != self.gen:
                return
            self._scan_end()
            messagebox.showerror('掃描失敗', text)
        elif kind == 'del_progress':
            _, i, n, name = msg
            self.prog.configure(value=i * 100.0 / max(1, n))
            self.prog_lbl.configure(text='刪除中 %s/%s：%s'
                                         % (format(i, ','), format(n, ','),
                                            name[:36]))
        elif kind == 'del_done':
            self._delete_finished(*msg[1:])
        elif kind == 'rb_done':
            self._rb_finished(*msg[1:])
        elif kind == 'disk_info':
            self._disk_busy = False
            self._rb_info = msg[3]           # 給 do_empty_recycle_bin 用
            self._render_disk_label(*msg[1:])
            if self._disk_pending:           # 查詢期間又有人要求更新
                self._disk_pending = False
                self._update_disk_label()

    def _scan_end(self):
        self.prog.stop()
        self.prog.configure(mode='determinate', value=0)
        self._set_mode('idle')

    def _finish(self, res):
        self._scan_end()
        if res.root is None:
            self.prog_lbl.configure(text='掃描未完成')
            return
        self.result = res
        self.refresh_tree()
        self.refresh_clean()
        self.refresh_big()
        self.refresh_ext()
        self.refresh_err()
        junk_total = sum(j['size'] for j in res.junk)
        self.prog_lbl.configure(
            text='掃描%s（%.1f 秒）' % ('中止' if res.cancelled else '完成',
                                       res.elapsed))
        self.status.configure(
            text='總計 %s ／ %s 個檔案 ／ %s 個資料夾    |    '
                 '偵測到可清理 %s（%d 項）    |    無法讀取 %s 項%s'
                 % (human(res.root.size), format(res.root.nfiles, ','),
                    format(res.dirs, ','), human(junk_total),
                    len(res.junk), format(res.errors, ','),
                    ('    |    略過連結/junction %s 個（避免重複計算）'
                     % format(res.links, ','))
                    if res.links else ''))
        self.btn_del.configure(state='normal')
        self._update_disk_label()

    def _clear_all(self):
        for tv in (self.tv, self.cv, self.bv, self.ev, self.errv):
            kids = tv.get_children('')
            if kids:
                tv.delete(*kids)
        self.tree_meta.clear()
        self.entry_meta.clear()
        self.group_leaves.clear()
        self.checked.clear()
        self._update_sel_label()

    # ------------------------------------------------------------ Tab1 樹
    def _min_bytes(self):
        return {'全部': 0, '100 KB': 100 * 1024, '1 MB': 1024 ** 2,
                '10 MB': 10 * 1024 ** 2,
                '100 MB': 100 * 1024 ** 2}.get(self.minsize_var.get(), 0)

    def refresh_tree(self):
        kids = self.tv.get_children('')
        if kids:
            self.tv.delete(*kids)
        self.tree_meta.clear()
        if not self.result or not self.result.root:
            return
        r = self.result.root
        iid = self.tv.insert('', 'end', text='  %s' % r.path,
                             values=(human(r.size), bar(1.0) + ' 100%',
                                     format(r.nfiles, ','), ''),
                             open=True)
        self.tree_meta[iid] = ('dir', r)
        self._fill(iid, r)

    @staticmethod
    def _dir_files(path):
        """讀一個目錄的檔案明細 (只有展開某一層時才會呼叫)。

        掃描時刻意不保留這份資料 (見 Node), 展開時才來讀。
        回傳 (最大的 DISPLAY_CAP 筆, 其餘筆數, 其餘總大小, 是否提早停止)。

        兩道上限都是為了不讓視窗卡住:
          * 用固定大小的最小堆而不是整份清單 —— 8 萬檔的目錄整份讀進來要
            9.6MB, 幾十萬檔就是幾十 MB, 而畫面反正只顯示 DISPLAY_CAP 列。
          * 掃到 SCAN_ON_EXPAND_CAP 筆就停 —— 實測光是 scandir 8 萬筆就要
            214ms, 幾十萬筆會超過一秒, 在主執行緒做等於畫面凍住。
        """
        keep = []
        rest_n = 0
        rest_size = 0
        seen = 0
        truncated = False
        try:
            with os.scandir(to_ext(path)) as it:
                for e in it:
                    if seen >= App.SCAN_ON_EXPAND_CAP:
                        truncated = True
                        break
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            continue
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if len(keep) < DISPLAY_CAP:
                        keep.append((size, e.name))
                        if len(keep) == DISPLAY_CAP:
                            heapq.heapify(keep)
                    else:
                        rest_n += 1
                        if size > keep[0][0]:
                            rest_size += heapq.heapreplace(
                                keep, (size, e.name))[0]
                        else:
                            rest_size += size
        except OSError:
            pass
        keep.sort(reverse=True)
        return keep, rest_n, rest_size, truncated

    def _fill(self, parent_iid, node):
        """插入 node 的下一層; 子資料夾放佔位符以支援延遲展開。

        每層插入筆數設上限, 避免單一目錄有十萬個項目時 UI 凍結。
        """
        total = float(self.result.root.size) or 1.0
        lim = self._min_bytes()
        insert = self.tv.insert
        node_path = node.path         # property 會沿 parent 串, 只取一次

        shown = 0
        skipped_n = 0
        skipped_size = 0
        for c in sorted(node.children, key=lambda n: n.size, reverse=True):
            if c.size < lim and c.junk is None:
                continue
            if shown >= DISPLAY_CAP:
                skipped_n += 1
                skipped_size += c.size
                continue
            shown += 1
            flag, tags = '', []
            if c.junk:
                cat, risk, note = Scanner._grade(
                    os.path.join(node_path, c.name), c.junk)
                if c.has_vcs:            # 與可清理清單的判定保持一致
                    cat, risk, note = grade_vcs(cat, risk, note)
                flag = '%s｜%s%s' % (RISK_LABEL[risk], cat,
                                     '｜⚠ 內含版本控制資料' if c.has_vcs else '')
                tags.append(risk)
            iid = insert(parent_iid, 'end', text='  \U0001F4C1 %s' % c.name,
                         values=(human(c.size),
                                 '%s %3d%%' % (bar(c.size / total),
                                               round(100 * c.size / total)),
                                 format(c.nfiles, ','), flag),
                         tags=tuple(tags))
            self.tree_meta[iid] = ('dir', c)
            if c.children or c.nfiles:
                insert(iid, 'end', text='…', tags=('__ph__',))
        if skipped_n:
            insert(parent_iid, 'end', tags=('dim',),
                   text='  … 另有 %s 個子資料夾未顯示' % format(skipped_n, ','),
                   values=(human(skipped_size), '', '', ''))

        keep, skipped_n, skipped_size, truncated = self._dir_files(node_path)
        for size, name in keep:              # keep 本身已經受 DISPLAY_CAP 限制
            hit = None if node.protected else match_file_rule(name)
            if size < lim and not hit:
                continue
            fpath = os.path.join(node_path, name)
            flag, tags = '', []
            if hit:
                cat, risk, _note = Scanner._grade(fpath, hit)
                flag = '%s｜%s' % (RISK_LABEL[risk], cat)
                tags.append(risk)
            iid = insert(parent_iid, 'end', text='     %s' % name,
                         values=(human(size),
                                 '%s %3d%%' % (bar(size / total),
                                               round(100 * size / total)),
                                 '', flag),
                         tags=tuple(tags))
            self.tree_meta[iid] = ('file', fpath, size)

        if skipped_n or truncated:
            more = (('（此資料夾項目太多, 只讀了前 %s 筆）'
                     % format(self.SCAN_ON_EXPAND_CAP, ',')) if truncated
                    else '')
            insert(parent_iid, 'end', tags=('dim',),
                   text='  … 另有 %s 個較小檔案未列出%s'
                        % (format(skipped_n, ','), more),
                   values=(human(skipped_size), '', '', ''))

    def _on_open(self, _evt):
        iid = self.tv.focus()
        meta = self.tree_meta.get(iid)
        if not meta or meta[0] != 'dir':
            return
        kids = self.tv.get_children(iid)
        if len(kids) == 1 and '__ph__' in self.tv.item(kids[0], 'tags'):
            self.tv.delete(kids[0])
            self._fill(iid, meta[1])

    def _popup_tree(self, evt):
        iid = self.tv.identify_row(evt.y)
        if iid and iid in self.tree_meta:
            self.tv.selection_set(iid)
            self.tv.focus(iid)
            state = 'disabled' if self.busy else 'normal'
            self.menu_tree.entryconfigure(3, state=state)
            self.menu_tree.tk_popup(evt.x_root, evt.y_root)

    def _tree_action(self, what):
        meta = self.tree_meta.get(self.tv.focus())
        if not meta:
            return
        p = meta[1].path if meta[0] == 'dir' else meta[1]
        if what == 'open':
            reveal_in_explorer(p)
        elif what == 'copy':
            self.root.clipboard_clear()
            self.root.clipboard_append(p)
        elif what == 'del':
            if self.busy:
                return
            norm = os.path.normpath(os.path.abspath(p))
            root_path = (os.path.normpath(os.path.abspath(self.result.root.path))
                         if self.result and self.result.root else None)
            if norm == root_path or os.path.dirname(norm) == norm:
                messagebox.showwarning(
                    '不允許刪除',
                    '為了安全, 不能從這裡刪除「掃描的根資料夾」或磁碟根目錄：'
                    '\n\n%s' % p)
                return
            warn = ''
            if in_protected_dir(p):
                warn = ('\n\n⚠ 這是版本控制（.git 等）的內部資料。\n'
                        '刪除 .git\\logs 會讓 git reflog 失效, '
                        '之後無法從誤操作中還原；\n'
                        '刪除 .git\\objects 會直接毀掉版本歷史。')
            elif meta[0] == 'dir' and meta[1].has_vcs:
                warn = ('\n\n⚠ 這個資料夾底下有 .git 等版本控制資料。\n'
                        '刪除是遞迴的 —— 整個版本歷史會跟著一起被刪掉。')
            if not messagebox.askyesno(
                    '確認刪除',
                    '要把以下項目送到資源回收桶嗎？\n\n%s%s' % (p, warn),
                    icon='warning' if warn else 'question'):
                return
            try:
                delete_path(p, to_trash=True)
            except Exception as e:                 # noqa: BLE001
                messagebox.showerror('刪除失敗', '%s\n\n%s' % (p, e))
                return
            iid = self.tv.focus()
            self._forget_subtree(iid)
            self.tv.delete(iid)
            self._mark_stale('已刪除：%s' % p)

    def _forget_subtree(self, iid):
        """從 tree_meta 移除該節點與其已展開的子節點, 避免殘留參照。

        用明確堆疊而不是遞迴: 目錄可以嵌得很深, 遞迴會撞到 recursion limit。
        """
        stack = [iid]
        while stack:
            cur = stack.pop()
            stack.extend(self.tv.get_children(cur))
            self.tree_meta.pop(cur, None)

    def _mark_stale(self, msg):
        self.stale = True
        self.status.configure(text='%s    |    ⚠ 統計已過期, 請重新掃描' % msg)

    # ------------------------------------------------------------ Tab2 清理
    def refresh_clean(self):
        kids = self.cv.get_children('')
        if kids:
            self.cv.delete(*kids)
        self.entry_meta.clear()
        self.group_leaves.clear()
        self.checked.clear()
        if not self.result:
            self._update_sel_label()
            return
        groups = build_entries(self.result.junk, self.result.root.path)
        if not groups:
            self.cv.insert('', 'end', text='  沒有偵測到可清理的暫存檔',
                           values=('', '', ''))
            self._update_sel_label()
            return
        for cat, risk, note, total, cnt, entries in groups:
            gid = self.cv.insert(
                '', 'end', open=(risk == RISK_SAFE),
                text='☐  %s（%s 項）' % (cat, format(cnt, ',')),
                values=(human(total), RISK_LABEL[risk], note),
                tags=(risk,))
            leaves = []
            for en in entries:
                iid = self.cv.insert(gid, 'end',
                                     text='☐  %s' % en.label,
                                     values=(human(en.size), '', ''),
                                     tags=(risk,))
                self.entry_meta[iid] = en
                leaves.append(iid)
            self.group_leaves[gid] = leaves
        if self.result.junk_omitted[0]:
            self.cv.insert('', 'end', tags=('dim',),
                           text='   ⓘ 另有 %s 個小型暫存檔未列出（合計 %s）,'
                                ' 清完上面的項目後可再掃一次'
                                % (format(self.result.junk_omitted[0], ','),
                                   human(self.result.junk_omitted[1])),
                           values=('', '', ''))
        self._select_risk(RISK_SAFE)

    def _leaves_of(self, iid):
        if iid in self.group_leaves:
            return self.group_leaves[iid]
        if iid in self.entry_meta:
            return (iid,)
        return ()

    def _set_check(self, iid, on):
        txt = self.cv.item(iid, 'text')
        body = txt[1:] if txt[:1] in ('☐', '☑') else txt
        self.cv.item(iid, text=('☑' if on else '☐') + body)

    def _apply(self, leaves, on):
        """勾選/取消一批葉節點, 最後才同步群組與標籤 (減少 Tk 呼叫)。"""
        checked = self.checked
        for lf in leaves:
            was = lf in checked
            if was == on:
                continue
            if on:
                checked.add(lf)
            else:
                checked.discard(lf)
            self._set_check(lf, on)
        self._sync_groups()
        self._update_sel_label()

    def _sync_groups(self):
        for gid, leaves in self.group_leaves.items():
            self._set_check(gid, bool(leaves) and
                            all(x in self.checked for x in leaves))

    def _toggle(self, iid):
        if self.busy or not iid:
            return
        leaves = self._leaves_of(iid)
        if not leaves:
            return
        self._apply(leaves, not all(x in self.checked for x in leaves))

    def _on_click_clean(self, evt):
        """點名稱 = 切換勾選; 點展開箭頭 = 交給 Tk 展開或收合。"""
        if self.cv.identify_region(evt.x, evt.y) != 'tree':
            return None
        try:
            elem = str(self.cv.identify_element(evt.x, evt.y))
        except tk.TclError:
            elem = 'text'
        if 'indicator' in elem:
            return None            # 讓 Tk 處理展開/收合
        iid = self.cv.identify_row(evt.y)
        if not iid:
            return None
        self._toggle(iid)
        return 'break'

    def _on_space_clean(self, _evt):
        self._toggle(self.cv.focus())
        return 'break'

    def _on_dbl_clean(self, _evt):
        self._clean_open()
        return 'break'

    def _popup_clean(self, evt):
        iid = self.cv.identify_row(evt.y)
        if iid and iid in self.entry_meta:
            self.cv.selection_set(iid)
            self.cv.focus(iid)
            self.menu_clean.tk_popup(evt.x_root, evt.y_root)

    def _clean_open(self):
        en = self.entry_meta.get(self.cv.focus())
        if en and en.items:
            reveal_in_explorer(en.items[0][0])

    def _clean_copy(self):
        en = self.entry_meta.get(self.cv.focus())
        if en and en.items:
            self.root.clipboard_clear()
            self.root.clipboard_append('\n'.join(p for p, _s in en.items))

    def select_all(self, on):
        if self.busy:
            return
        all_leaves = [lf for lv in self.group_leaves.values() for lf in lv]
        self._apply(all_leaves, on)

    def select_risk(self, risk):
        if self.busy:
            return
        self._select_risk(risk)

    def _select_risk(self, risk):
        want, drop = [], []
        for gid, leaves in self.group_leaves.items():
            (want if risk in self.cv.item(gid, 'tags') else drop).extend(leaves)
        self._apply(drop, False)
        self._apply(want, True)

    def _update_sel_label(self):
        n = size = 0
        for iid in self.checked:
            en = self.entry_meta.get(iid)
            if en:
                n += en.count
                size += en.size
        self.sel_lbl.configure(text='已選 %s 項 / %s'
                                    % (format(n, ','), human(size)))

    # ------------------------------------------------------ 清空回收桶
    def do_empty_recycle_bin(self):
        if self.busy:
            return
        if not IS_WINDOWS:
            messagebox.showwarning('不支援', '此平台不支援清空資源回收桶。')
            return
        # 用背景查來的快取數字, 不在這裡現查: SHQueryRecycleBinW 要走過每個
        # 磁碟的 $Recycle.Bin, 剛丟進幾萬個項目之後可能要好幾秒。
        rb = self._rb_info
        if rb and rb[1] <= 0:
            messagebox.showinfo('回收桶是空的', '資源回收桶目前沒有東西。')
            self._update_disk_label()
            return
        size, items = rb if rb else (0, 0)
        detail = ('資源回收桶內有 %s 個項目, 共 %s。\n\n'
                  % (format(items, ','), human(size)) if rb
                  else '即將清空資源回收桶。\n\n')
        if not messagebox.askyesno(
                '清空資源回收桶',
                detail +
                '★ 清空後這些檔案將永久刪除, 無法還原 ★\n'
                '（包含你先前自己丟進回收桶的東西, 不只是本工具刪的）\n\n'
                '確定要清空嗎？',
                icon='warning'):
            return
        self._set_mode('delete')
        self.prog.configure(mode='indeterminate')
        self.prog.start(12)
        self.prog_lbl.configure(text='清空資源回收桶中…')
        before = free_bytes_of(self.path_var.get().strip() or os.getcwd())
        threading.Thread(target=self._rb_worker,
                         args=(size, items, before), daemon=True).start()

    def _rb_worker(self, size, items, before):
        """一定會回報 rb_done, 否則主視窗會永遠停在「清空回收桶中」。"""
        need_uninit = False
        err = ''
        try:
            need_uninit = com_init()
            empty_recycle_bin()
        except BaseException as e:                  # noqa: BLE001
            err = '%s: %s' % (type(e).__name__, e)
        finally:
            if need_uninit:
                com_uninit()
        try:
            self.q.put(('rb_done', size, items, before, err))
        except Exception:                           # noqa: BLE001
            pass

    def _rb_finished(self, size, items, before, err):
        self.prog.stop()
        self.prog.configure(mode='determinate', value=0)
        self._set_mode('idle')
        self.prog_lbl.configure(text='回收桶已清空' if not err else '清空失敗')
        self._update_disk_label()
        if err:
            messagebox.showerror('清空失敗', err)
            return
        after = free_bytes_of(self.path_var.get().strip() or os.getcwd())
        extra = ''
        if before is not None and after is not None and after > before:
            extra = '\n本磁碟可用空間增加 %s' % human(after - before)
        self.status.configure(text='已清空資源回收桶（%s 個項目, %s）'
                                   % (format(items, ','), human(size)))
        messagebox.showinfo('完成', '已清空資源回收桶：%s 個項目, %s%s'
                            % (format(items, ','), human(size), extra))

    # ------------------------------------------------------------ 刪除
    def do_delete(self):
        if self.busy:
            return
        sel = [(iid, self.entry_meta[iid]) for iid in self.checked
               if iid in self.entry_meta]
        if not sel:
            messagebox.showinfo('沒有選取', '請先勾選要刪除的項目。')
            return

        jobs = []               # (entry_iid, path, 大小, 是否重建空資料夾)
        for iid, en in sel:
            recreate = en.category in RECREATE_DIR_CATEGORIES
            for p, s in en.items:
                jobs.append((iid, p, s, recreate))
        size = sum(en.size for _i, en in sel)
        risks = {en.risk for _i, en in sel}
        to_trash = self.trash_var.get()

        msg = ['即將刪除 %s 個項目, 合計 %s。'
               % (format(len(jobs), ','), human(size)), '']
        if to_trash:
            msg += ['刪除方式：送到資源回收桶（可還原）',
                    '※ 注意：移到回收桶「還不會」真正釋放磁碟空間,',
                    '　 要按「清空回收桶」後才會空出來。']
        else:
            msg += ['刪除方式：★ 永久刪除, 無法還原 ★']
        extra = [RISK_LABEL[r] for r in RISK_ORDER
                 if r in risks and r != RISK_SAFE]
        if extra:
            msg += ['', '注意：選取項目含「%s」等級, 請確認裡面沒有你需要的檔案。'
                    % '／'.join(extra)]
        msg += ['', '確定繼續嗎？']
        if not messagebox.askyesno('確認刪除', '\n'.join(msg), icon='warning'):
            return

        self.del_stop = threading.Event()
        self._free_before = free_bytes_of(
            self.result.root.path if self.result and self.result.root
            else self.path_var.get())
        self._to_trash_used = to_trash
        self._set_mode('delete')
        self.prog.configure(mode='determinate', value=0)
        threading.Thread(target=self._delete_worker,
                         args=(jobs, to_trash, self.del_stop),
                         daemon=True).start()

    @staticmethod
    def _job_chunks(jobs, batched):
        """把待刪清單切成一次呼叫的批次 (不批次時就是每筆一批)。

        用的是跟 delete_many() 同一套預算規則 (chunk_by_budget), 這裡切的目的
        是讓進度回報有合理的顆粒度; 真正保證 pFrom 不會過大的是 delete_many
        自己。
        """
        if not batched:
            for j in jobs:
                yield [j]
            return
        for ch in chunk_by_budget(jobs, lambda j: j[1]):
            yield ch

    def _delete_worker(self, jobs, to_trash, stop_evt):
        """背景刪除。只透過 queue 與 GUI 溝通, 不碰任何 widget/共用狀態。

        一定會回報 del_done —— 主視窗在收到它以前會一直停在「刪除中」, 連
        「停止」都救不回來 (mode 不會變), 使用者只剩關掉程式一途。
        """
        need_uninit = False
        ok = freed = partial = 0
        fails = []
        per_entry = defaultdict(lambda: [0, 0])      # iid -> [成功, 失敗]
        n = len(jobs)
        done = 0
        last = 0.0
        try:
            need_uninit = com_init() if to_trash else False
            for chunk in self._job_chunks(jobs, can_batch_trash(to_trash)):
                if stop_evt.is_set():
                    break
                now = time.monotonic()
                if now - last > 0.1 or done + len(chunk) >= n:   # 節流
                    last = now
                    first = chunk[0][1]
                    self.q.put(('del_progress', done + len(chunk), n,
                                os.path.basename(first) or first))
                results = delete_many([(p, r) for _i, p, _s, r in chunk],
                                      to_trash)
                for (iid, path, size, _recreate), err in zip(chunk, results):
                    if err is None:
                        ok += 1
                        freed += size            # 用掃描時的大小, 不重走目錄
                        per_entry[iid][0] += 1
                        continue
                    per_entry[iid][1] += 1
                    if isinstance(err, PartialDelete):
                        # 大部分已清掉, 只有被佔用的留著 -> 保留該列供重試
                        partial += 1
                        if len(fails) < 500:
                            fails.append('%s  →  部分刪除, %s' % (path, err))
                    elif len(fails) < 500:
                        fails.append('%s  →  %s' % (path, err))
                done += len(chunk)               # 結果記完才算處理過
        except BaseException as e:                   # noqa: BLE001
            if len(fails) < 500:
                fails.append('刪除中斷：%s: %s' % (type(e).__name__, e))
        finally:
            if need_uninit:
                com_uninit()
        for iid, _p, _s, _r in jobs[done:]:
            per_entry[iid][1] += 1                   # 沒處理到的一律算未完成
        try:
            self.q.put(('del_done', ok, freed, fails, dict(per_entry),
                        stop_evt.is_set(), partial))
        except Exception:                            # noqa: BLE001
            pass

    def _delete_finished(self, ok, freed, fails, per_entry, cancelled,
                         partial=0):
        self.prog.configure(value=0)
        self._set_mode('idle')
        self.prog_lbl.configure(text='刪除中止' if cancelled else '刪除完成')

        # 只移除「完全刪除成功」的列, 未完成的留在清單上讓使用者處理
        for iid, (n_ok, n_fail) in per_entry.items():
            if n_fail == 0 and n_ok > 0 and self.cv.exists(iid):
                self.cv.delete(iid)
                self.entry_meta.pop(iid, None)
                self.checked.discard(iid)
            elif n_fail:
                self.checked.discard(iid)
                if self.cv.exists(iid):
                    self._set_check(iid, False)
                    self._trim_entry(iid)
        for gid, leaves in list(self.group_leaves.items()):
            self.group_leaves[gid] = [x for x in leaves if self.cv.exists(x)]
            if not self.group_leaves[gid] and self.cv.exists(gid):
                self.cv.delete(gid)
                del self.group_leaves[gid]
        self._sync_groups()
        self._update_sel_label()
        self._mark_stale('已處理 %s 個項目（%s）；未完成 %s 個'
                         % (format(ok, ','), human(freed),
                            format(len(fails), ',')))
        self._update_disk_label()

        to_trash = getattr(self, '_to_trash_used', True)
        if to_trash:
            text = ('已把 %s 個項目移到資源回收桶（合計 %s, 可還原）\n'
                    '※ 磁碟空間要等「清空回收桶」後才會真正空出來。'
                    % (format(ok, ','), human(freed)))
        else:
            text = ('已永久刪除 %s 個項目\n釋放空間約 %s'
                    % (format(ok, ','), human(freed)))
        after = free_bytes_of(
            self.result.root.path if self.result and self.result.root
            else self.path_var.get())
        before = getattr(self, '_free_before', None)
        if before is not None and after is not None:
            diff = after - before
            text += ('\n\n磁碟實際可用空間：%s（%s%s）'
                     % (human(after), '+' if diff >= 0 else '-',
                        human(abs(diff))))
        if cancelled:
            text = '（已中止）\n' + text
        if partial:
            text += ('\n\n另有 %s 個資料夾只清掉一部分（少數檔案正被程式使用），'
                     '實際釋放的空間會比上面更多。關掉相關程式後可再試一次。'
                     % format(partial, ','))
        if fails:
            text += '\n\n%s 個項目未能完全刪除（正被程式使用、權限不足或路徑過長）：\n\n%s' \
                    % (format(len(fails), ','), '\n'.join(fails[:10]))
            if len(fails) > 10:
                text += '\n…（其餘 %s 筆省略）' % format(len(fails) - 10, ',')
            messagebox.showwarning('部分完成', text)
        else:
            messagebox.showinfo('刪除完成', text + '\n\n建議重新掃描以更新統計。')

    def _trim_entry(self, iid):
        """把已成功刪掉的路徑從保留下來的列移除, 免得重試永遠失敗。"""
        en = self.entry_meta.get(iid)
        if en is None or len(en.items) > 5000:
            return
        left = [(p, s) for p, s in en.items if os.path.exists(to_ext(p))]
        if len(left) == len(en.items):
            return
        if not left:
            self.cv.delete(iid)
            self.entry_meta.pop(iid, None)
            return
        en.items = left
        en.count = len(left)
        en.size = sum(s for _p, s in left)
        if en.count > 1:
            en.label = '（其他 %d 個較小項目）' % en.count
        txt = self.cv.item(iid, 'text')
        mark = txt[:1] if txt[:1] in ('☐', '☑') else '☐'
        self.cv.item(iid, text='%s  %s' % (mark, en.label),
                     values=(human(en.size), '', ''))

    # ------------------------------------------------------------ Tab3~5
    def refresh_big(self):
        kids = self.bv.get_children('')
        if kids:
            self.bv.delete(*kids)
        if not self.result:
            return
        for size, path in self.result.top_files[:400]:
            self.bv.insert('', 'end', values=(human(size), path))

    def _big_open(self):
        sel = self.bv.focus()
        if sel:
            reveal_in_explorer(self.bv.item(sel, 'values')[1])

    def refresh_ext(self):
        kids = self.ev.get_children('')
        if kids:
            self.ev.delete(*kids)
        if not self.result:
            return
        total = float(self.result.root.size) or 1.0
        rows = sorted(self.result.ext_stats.items(),
                      key=lambda kv: kv[1][1], reverse=True)
        for ext, (cnt, size) in rows[:200]:
            self.ev.insert('', 'end', values=(
                ext, human(size), format(cnt, ','),
                '%s %5.1f%%' % (bar(size / total, 16), 100 * size / total)))

    def refresh_err(self):
        kids = self.errv.get_children('')
        if kids:
            self.errv.delete(*kids)
        if not self.result:
            return
        for p in self.result.error_paths:
            self.errv.insert('', 'end', values=(p,))
        left = self.result.errors - len(self.result.error_paths)
        if left > 0:
            self.errv.insert('', 'end',
                             values=('… 另有 %s 項未列出' % format(left, ','),))


def main():
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1:
        app.path_var.set(os.path.abspath(sys.argv[1]))
        if os.path.isdir(to_ext(sys.argv[1])):
            root.after(300, app.start_scan)
    root.mainloop()


if __name__ == '__main__':
    main()
