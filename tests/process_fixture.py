"""Offline worker fixture used only by lifecycle regression tests."""
import os
from pathlib import Path
import subprocess
import sys
import time

if os.environ.get('DY_START_GATE') == '1':
    if sys.stdin.readline().strip() != 'GO':
        sys.exit(1)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')
print('READY', flush=True)
time.sleep(120)
