# -*- coding: utf-8 -*-
"""跑完 tests/ 底下所有 test_*.py。

    python tests/run_all.py              # 一般 (約 1~2 分鐘)
    python tests/run_all.py --slow       # 加上大型掃描, 會跑幾分鐘
    python tests/run_all.py --trash      # 加上真的送資源回收桶的測試
    python tests/run_all.py test_rules   # 只跑指定幾個

每個測試檔跑在自己的子行程裡: tkinter 一個 process 只能有一個 root,
第二個會踩到 Tcl_AsyncDelete 而整個崩掉。
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    args = sys.argv[1:]
    slow = '--slow' in args
    trash = '--trash' in args
    names = [a for a in args if not a.startswith('--')]

    files = sorted(glob.glob(os.path.join(HERE, 'test_*.py')))
    if names:
        files = [f for f in files
                 if any(n in os.path.basename(f) for n in names)]
    if not files:
        print('no test files matched')
        return 1

    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    if slow:
        env['DC_TEST_SLOW'] = '1'
    if trash:
        env['DC_TEST_TRASH'] = '1'

    results = []
    t_all = time.time()
    for f in files:
        name = os.path.basename(f)[:-3]
        print('\n' + '-' * 72)
        t = time.time()
        p = subprocess.run([sys.executable, '-u', f], env=env)
        d = time.time() - t
        results.append((name, p.returncode, d))

    print('\n' + '=' * 72)
    bad = 0
    for name, rc, d in results:
        bad += 1 if rc else 0
        print('  %-22s %-6s %5.1fs' % (name, 'PASS' if rc == 0 else 'FAIL', d))
    print('%d/%d passed in %.1fs%s'
          % (len(results) - bad, len(results), time.time() - t_all,
             '' if slow else '   (--slow for the big-tree checks)'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
