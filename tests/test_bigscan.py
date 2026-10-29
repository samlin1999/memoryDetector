# -*- coding: utf-8 -*-
"""真實大樹的掃描 (只在 --slow 時跑)。

合成的樹沒有 junction、沒有權限失敗、沒有奇怪檔名、也沒有幾十萬個目錄。
這裡不做跨次比對 (活的資料夾兩次掃描之間本來就會變, 那樣的測試會變成
時好時壞), 只驗不變量、峰值記憶體、以及畫面不能凍住。

    DC_TEST_BIG=C:\\  python tests/run_all.py --slow test_bigscan
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _harness as H                                            # noqa: E402

MAX_RSS_MB = 400.0          # 100 萬檔實測約 74MB, 200 萬檔約 170MB
MAX_STALL_S = 1.5           # 掃描結束那一下 Tk 要為幾千列做 layout


class PMC(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]


def peak_mb():
    if os.name != 'nt':
        return 0.0
    fn = (getattr(ctypes.windll.kernel32, 'K32GetProcessMemoryInfo', None)
          or ctypes.windll.psapi.GetProcessMemoryInfo)
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    fn.restype = wintypes.BOOL
    m = PMC()
    m.cb = ctypes.sizeof(PMC)
    fn(wintypes.HANDLE(-1), ctypes.byref(m), m.cb)
    return m.PeakWorkingSetSize / 1e6


def run():
    c = H.Checks('bigscan')
    if not os.environ.get('DC_TEST_SLOW'):
        c.note('跳過 (要跑就加 --slow)')
        return c.done()

    target = os.environ.get('DC_TEST_BIG') or os.path.expanduser('~')
    if not os.path.isdir(target):
        c.note('跳過: 找不到 %s' % target)
        return c.done()
    c.note('目標: %s' % target)

    M = H.load_app()
    base = peak_mb()
    with H.Session(M) as s:
        app, root = s.app, s.root
        gaps = []
        last = [None]
        stamped = []
        t0 = [None]

        def tick():
            now = time.perf_counter()
            if last[0] is not None:
                d = now - last[0]
                gaps.append(d)
                if d > 0.3:
                    stamped.append((now - t0[0], d))
            last[0] = now
            root.after(16, tick)

        app.path_var.set(target)
        app.minsize_var.set('1 MB')
        t0[0] = time.perf_counter()
        app.start_scan()
        root.after(16, tick)
        ok = s.pump(1800, lambda: app.mode == 'idle'
                    and app.result is not None)
        wall = time.perf_counter() - t0[0]
        s.pump(1.0)
        c.ok(ok, '大樹掃描完成')
        if not ok:
            return c.done()

        res = app.result
        rss = peak_mb()
        c.note('%.1fs  dirs=%s files=%s size=%s errors=%s links=%s junk=%s'
               % (wall, format(res.dirs, ','), format(res.root.nfiles, ','),
                  M.human(res.root.size), format(res.errors, ','),
                  format(res.links, ','), format(len(res.junk), ',')))
        c.note('峰值 RSS %.1f MB (掃描前 %.1f MB)' % (rss, base))
        c.ok(res.dirs > 1000, '這棵樹夠大 (%s 個資料夾)' % format(res.dirs, ','))
        c.ok(rss < MAX_RSS_MB, '峰值記憶體低於 %.0f MB 的上限' % MAX_RSS_MB,
             '%.1f MB' % rss)

        nodes = H.walk_nodes(res.root)
        c.eq(len(nodes), res.dirs, '節點數 = 回報的資料夾數')
        bad = [n.name for n in nodes
               if n.size < sum(k.size for k in n.children)]
        c.eq(bad[:3], [], '每個節點的 size >= 子節點總和')
        bad = [n.name for n in nodes
               if n.nfiles < sum(k.nfiles for k in n.children)]
        c.eq(bad[:3], [], '每個節點的 nfiles >= 子節點總和')
        c.eq(sum(v[0] for v in res.ext_stats.values()), res.root.nfiles,
             'ext_stats 的檔案數加總 = 檔案總數')
        c.eq(sum(v[1] for v in res.ext_stats.values()), res.root.size,
             'ext_stats 的大小加總 = 總大小')
        rp = os.path.normcase(os.path.abspath(target))
        outside = [j['path'] for j in res.junk
                   if not os.path.normcase(
                       os.path.abspath(j['path'])).startswith(rp)]
        c.eq(outside[:3], [], '所有 junk 路徑都在目標底下')
        c.ok(all(j['size'] >= 0 for j in res.junk), '沒有負的 junk 大小')
        c.eq(len(res.error_paths) <= M.Scanner.MAX_ERR_PATHS, True,
             '失敗路徑數受上限控制')

        # 系統目錄裡的東西必須被降級, 不能自動勾選
        sysjunk = [j for j in res.junk if M.in_system_dir(j['path'])]
        notdown = [j['path'] for j in sysjunk if j['risk'] != M.RISK_CHECK]
        c.eq(notdown[:3], [], '系統目錄裡的項目全部降為「需確認」',
             '%d/%d' % (len(notdown), len(sysjunk)))
        # 版本控制資料不能出現在待刪清單裡
        vcs = [j['path'] for j in res.junk if M.in_protected_dir(j['path'])]
        c.eq(vcs[:3], [], '沒有任何版本控制內部資料被列為可清理')
        # 內含 repo 的目錄不能是 safe
        risky = [j['path'] for j in res.junk
                 if M.VCS_WARN in j['note'] and j['risk'] != M.RISK_CHECK]
        c.eq(risky[:3], [], '內含 repo 的目錄一律不是 safe')

        # UI 流暢度
        g = sorted(gaps)
        c.note('主迴圈: %d ticks 中位數 %.1fms p99 %.1fms 最大 %.0fms'
               % (len(g), g[len(g) // 2] * 1000,
                  g[min(len(g) - 1, int(len(g) * .99))] * 1000,
                  g[-1] * 1000))
        for at, d in stamped:
            c.note('  停頓 %.0fms 發生在 t=%.1fs (掃描共 %.1fs)'
                   % (d * 1000, at, wall))
        c.ok(g[-1] < MAX_STALL_S, '沒有超過 %.1f 秒的停頓' % MAX_STALL_S,
             '最大 %.0f ms' % (g[-1] * 1000))
        mid = [d for d in gaps[:int(len(gaps) * 0.9)] if d > 0.3]
        c.eq(len(mid), 0, '掃描途中沒有超過 300ms 的停頓',
             '%d 次' % len(mid))

        # 自動勾選的一定只有 safe
        risks = {app.entry_meta[i].risk for i in app.checked}
        c.ok(risks <= {M.RISK_SAFE}, '自動勾選的只有「安全」等級', str(risks))
        c.note('自動勾選: %s' % app.sel_lbl.cget('text'))

        # 展開最大的幾個資料夾, 不能卡住
        root_iid = app.tv.get_children('')[0]
        worst = 0.0
        for k in list(app.tv.get_children(root_iid))[:6]:
            if app.tree_meta.get(k, ('',))[0] != 'dir':
                continue
            t = time.perf_counter()
            app.tv.focus(k)
            app.tv.item(k, open=True)
            app.tv.event_generate('<<TreeviewOpen>>')
            root.update()
            worst = max(worst, time.perf_counter() - t)
        c.ok(worst < 1.0, '展開最上層的大資料夾都在 1 秒內 (最慢 %.0f ms)'
             % (worst * 1000))
    return c.done()


if __name__ == '__main__':
    H.main(run)
