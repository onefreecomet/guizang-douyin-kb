"""
桌面启动器：起服务，再开一个无地址栏的独立窗口。

双击桌面快捷方式时由 pythonw.exe 调用，所以不会出现命令行黑窗口。
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent
PORT = int(os.environ.get("DY_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"

# 快捷方式启动的环境里没有 node 目录，先把 PATH 补好再拉起服务
sys.path.insert(0, str(APP))
try:
    from envpath import augment_path
    augment_path()
except Exception:
    pass

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def alive() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def find_edge():
    p = shutil.which("msedge")
    if p:
        return p
    for c in EDGE_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def main():
    if not alive():
        flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
        subprocess.Popen(
            [sys.executable, str(APP / "server.py"), "--port", str(PORT)],
            cwd=str(APP), creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if alive():
                break
            time.sleep(0.25)

    edge = find_edge()
    if edge:
        subprocess.Popen([edge,
                          f"--app={URL}",
                          "--window-size=1320,900",
                          "--window-position=120,60"])
    else:
        webbrowser.open(URL)


if __name__ == "__main__":
    main()
