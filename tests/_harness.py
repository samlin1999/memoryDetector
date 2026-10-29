# -*- coding: utf-8 -*-
"""測試共用工具。

設計上的兩個硬限制 (都是踩過才知道的):
  * 一個 process 只能開一個 tkinter root —— 開第二個會踩到
    "Tcl_AsyncDelete: async handler deleted by the wrong thread" 而整個崩掉,
    所以 run_all.py 用子行程跑每個測試檔。
  * Treeview 的 bbox()/see() 只在視窗真的被 map 之後才有幾何資訊, 而且該
    widget 所在的 notebook 分頁必須是選取中的那一頁; 否則 bbox() 回傳空字串。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time

APP_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'disk_cleaner.py')
BS = chr(92)


# ------------------------------------------------------------------ 載入受測程式
def load_app(name='dc'):
    """每次都重新載入一份, 讓模組層級狀態 (例如 _send2trash) 是乾淨的。"""
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ 斷言收集
class Checks(object):
    """收集結果而不是第一個錯就中斷 —— 一次跑完才看得到全貌。"""

    def __init__(self, title):
        self.title = title
        self.failures = []
        self.n = 0
        print('=== %s ===' % title)

    def ok(self, cond, label, detail=''):
        self.n += 1
        if cond:
            print('  ok   %s' % label)
        else:
            self.failures.append((label, detail))
            print('  FAIL %s%s' % (label, ('  ->  %s' % detail)
                                   if detail else ''))
        return bool(cond)

    def eq(self, got, want, label, detail=''):
        extra = (' (%s)' % detail) if detail else ''
        return self.ok(got == want, label,
                       ('got %r, want %r%s' % (got, want, extra))
                       if got != want else '')

    def no_raise(self, label, fn):
        self.n += 1
        try:
            fn()
            print('  ok   %s' % label)
            return True
        except Exception as e:                       # noqa: BLE001
            self.failures.append((label, '%s: %s' % (type(e).__name__, e)))
            print('  FAIL %s  ->  %s: %s' % (label, type(e).__name__, e))
            return False

    def note(self, text):
        print('  --   %s' % text)

    def done(self):
        print('%s: %d checks, %d failures'
              % (self.title, self.n, len(self.failures)))
        return 1 if self.failures else 0


# ------------------------------------------------------------------ 檔案樹
class Tree(object):
    """暫存目錄樹; 全程用 \\?\\ 長路徑, 深樹才建得起來也刪得掉。"""

    def __init__(self, tag='dcleantest'):
        self.root = tempfile.mkdtemp(prefix=tag + '_')

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def dir(self, rel):
        p = self.path(*rel.split('/')) if rel else self.root
        os.makedirs(_long(p), exist_ok=True)
        return p

    def file(self, rel, size=100, data=None):
        parts = rel.split('/')
        p = self.path(*parts)
        os.makedirs(_long(os.path.dirname(p)), exist_ok=True)
        with open(_long(p), 'wb') as f:
            f.write(data if data is not None else (b'x' * size))
        return p

    def close(self):
        shutil.rmtree(_long(self.root), ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _long(p):
    r"""Windows: 一律加 \\?\ 前綴, 免得 MAX_PATH 擋住測試自己的 setup。"""
    if os.name != 'nt':
        return p
    p = os.path.normpath(os.path.abspath(p))
    if p.startswith(BS + BS + '?' + BS):
        return p
    if p.startswith(BS + BS):
        return BS + BS + '?' + BS + 'UNC' + BS + p[2:]
    return BS + BS + '?' + BS + p


# ------------------------------------------------------------------ 掃描
def scan(M, path, workers=None, stop_after=None, timeout=300):
    """跑一次掃描, 回傳 ScanResult; 失敗或沒回報就拋例外。"""
    import queue
    old = M.Scanner.WORKERS
    if workers is not None:
        M.Scanner.WORKERS = workers
    try:
        q = queue.Queue()
        stop = threading.Event()
        sc = M.Scanner(path, q, stop, 1)
        sc.start()
        if stop_after is not None:
            time.sleep(stop_after)
            stop.set()
        sc.join(timeout)
        if sc.is_alive():
            raise AssertionError('scanner thread never finished')
        res = err = None
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            if msg[0] == 'done':
                res = msg[2]
            elif msg[0] == 'error':
                err = msg[2]
        if err is not None:
            raise AssertionError('scan reported error: %s' % err)
        if res is None:
            raise AssertionError('scan posted neither done nor error')
        return res
    finally:
        M.Scanner.WORKERS = old


def junk_map(res, root):
    """{相對路徑: (是否目錄, 分類, 風險, 說明)}"""
    out = {}
    for j in res.junk:
        out[os.path.relpath(j['path'], root)] = (
            j['is_dir'], j['category'], j['risk'], j['note'])
    return out


def walk_nodes(root):
    order = [root]
    i = 0
    while i < len(order):
        order.extend(order[i].children)
        i += 1
    return order


# ------------------------------------------------------------------ GUI
def stub_dialogs(M):
    """把所有 messagebox 換成記錄器, 確認全部回答 yes。回傳記錄 list。"""
    from tkinter import messagebox
    log = []
    for nm in ('showerror', 'showwarning', 'showinfo'):
        setattr(messagebox, nm,
                lambda title, msg='', _n=nm, **kw: log.append((_n, title, msg)))
    messagebox.askyesno = lambda *a, **k: True
    messagebox.askokcancel = lambda *a, **k: True
    M.messagebox = messagebox
    M.filedialog = type('F', (), {
        'askdirectory': staticmethod(lambda **k: '')})()
    return log


def stub_explorer(M):
    """別讓測試真的開出一堆檔案總管視窗。"""
    log = []
    M.subprocess = type('S', (), {
        'Popen': staticmethod(lambda *a, **k: log.append(a))})()
    return log


def protect_recycle_bin(M):
    """測試絕對不能動到使用者真正的資源回收桶。

    * _trash_windows 換成「直接永久刪除」 —— 受測的流程 (逐筆事後驗證、
      per_entry 帳目、UI 更新) 一模一樣, 只是東西不會堆進回收桶。
    * empty_recycle_bin 換成記錄器 —— 真的跑下去會把使用者原本丟在回收桶裡
      的東西全部永久刪掉。
    回傳 (送出的批次, 清空的次數)。真的要測那條路徑請用 --trash。
    """
    batches = []
    emptied = []

    def fake_trash(paths):
        if isinstance(paths, str):
            paths = [paths]
        batches.append(list(paths))
        for p in paths:
            real = M.to_ext(os.path.abspath(M.from_ext(p)))
            try:
                if os.path.isdir(real):
                    shutil.rmtree(_long(real))
                else:
                    os.remove(real)
            except OSError:
                raise OSError('資源回收桶操作失敗 (code=32)')

    M._trash_windows = fake_trash
    M.empty_recycle_bin = lambda: emptied.append(True)
    return batches, emptied


def stub_popups(app):
    """tk_popup() 會抓取輸入並阻塞到有人關掉選單 —— 自動測試裡永遠不會返回。

    換成記錄器: 受測的 handler 邏輯 (identify_row / selection /
    entryconfigure) 仍然真的執行。
    """
    log = []
    for menu in (app.menu_tree, app.menu_clean):
        menu.tk_popup = lambda x, y, _m=menu: log.append((_m, x, y))
    return log


class Session(object):
    """真的 map 出來的視窗 + App; bbox()/see() 需要它被 map 才有幾何資訊。"""

    def __init__(self, M, geometry='1100x730+40+40', visible=True,
                 real_recycle_bin=False):
        import tkinter as tk
        self.M = M
        self.root = tk.Tk()
        self.app = M.App(self.root)
        if visible:
            self.root.geometry(geometry)
            self.root.update()
        else:
            self.root.withdraw()
        self.dialogs = stub_dialogs(M)
        self.explorer = stub_explorer(M)
        self.popups = stub_popups(self.app)
        self.trash_batches, self.bin_emptied = ([], [])
        if not real_recycle_bin:
            self.trash_batches, self.bin_emptied = protect_recycle_bin(M)

    def pump(self, seconds=0.1, until=None):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            if until is not None and until():
                return True
            time.sleep(0.004)
        return until is None or bool(until and until())

    def scan(self, path, min_size='全部', timeout=180):
        self.app.path_var.set(path)
        self.app.minsize_var.set(min_size)
        self.app.start_scan()
        ok = self.pump(timeout, lambda: (self.app.mode == 'idle'
                                         and self.app.result is not None))
        if not ok:
            raise AssertionError('scan did not finish within %ss' % timeout)
        self.pump(0.2)
        return self.app.result

    def select_tab(self, index):
        """widget 所在分頁必須是選取中的那一頁, 否則 bbox() 回傳空字串。"""
        self.app.nb.select(index)
        self.root.update()

    def row_y(self, tv, iid):
        parent = tv.parent(iid)
        if parent:
            tv.item(parent, open=True)      # 點箭頭可能已經把它收合了
        tv.see(iid)
        self.root.update()
        bb = tv.bbox(iid)
        if not bb:
            raise AssertionError('row %s has no geometry: window mapped? '
                                 'right notebook tab selected?' % iid)
        return bb[1] + bb[3] // 2

    def close(self):
        """收乾淨再拆掉視窗。

        App 一直用 after(100, _pump) 自我排程; 直接 destroy() 的話那個排程還在,
        Tcl 會對著已經消失的 command 名字噴 'invalid command name ..._pump'。
        背景執行緒也先叫停 —— 一個 process 開過第二個 root 之後, 還在跑的執行
        緒配上被拆掉的 interpreter 就是 Tcl_AsyncDelete 崩潰的組合。
        """
        try:
            self.app.stop_evt.set()
            self.app.del_stop.set()
        except Exception:                            # noqa: BLE001
            pass
        try:
            for aid in self.root.tk.call('after', 'info'):
                try:
                    self.root.after_cancel(aid)
                except Exception:                    # noqa: BLE001
                    pass
        except Exception:                            # noqa: BLE001
            pass
        try:
            self.root.update()
        except Exception:                            # noqa: BLE001
            pass
        try:
            self.root.destroy()
        except Exception:                            # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(fn):
    """測試檔的統一進入點: 回傳 failure 數當結束碼。"""
    try:
        rc = fn()
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 1
    sys.exit(1 if rc else 0)
