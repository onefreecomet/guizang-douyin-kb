"""
桌面启动器：起服务，再开一个无地址栏的独立窗口。

双击桌面快捷方式时由 pythonw.exe 调用，所以不会出现命令行黑窗口。
窗口关掉不代表服务停了：托盘图标常驻右下角，点一下就能把窗口叫回来。
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

try:
    import paths as _paths
except Exception:
    _paths = None


def alive() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def find_edge():
    """查找顺序：GUIZANG_BROWSER → 注册表 → 两个常见路径 → PATH。"""
    if _paths is not None:
        found = _paths.find_edge()
        return str(found) if found else None
    p = shutil.which("msedge")
    if p:
        return p
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(c).exists():
            return c
    return None


def start_server():
    if alive():
        return
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


def open_window():
    edge = find_edge()
    if edge:
        subprocess.Popen([edge,
                          f"--app={URL}",
                          "--window-size=1320,900",
                          "--window-position=120,60"])
    else:
        webbrowser.open(URL)


def main():
    start_server()
    try:
        from tray import activate_console, ensure_tray
    except Exception:
        activate_console = ensure_tray = None
    # 窗口已经开着就只把它叫到前面，不再开第二个
    if activate_console and activate_console():
        if ensure_tray:
            ensure_tray()
        return
    open_window()
    if ensure_tray:
        ensure_tray()


if __name__ == "__main__":
    main()
