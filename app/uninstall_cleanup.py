"""卸载前清理：把还在跑的托盘 / 守护 / 服务停掉。

Windows 不允许删除正在被进程使用的文件，卸载程序会因此卡在“runtime\\python\\python.exe 正被占用”。
这个脚本由 Inno Setup 的 UninstallRun 调用，只杀“命令行里带着本安装目录”的那些 python 进程，
不动你自己开的其它 python。

用户数据（work 目录、下载目录、Obsidian 库）一律不碰。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
INSTALL_DIR = APP_DIR.parent
CREATE_NO_WINDOW = 0x08000000

QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
    "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
)


def select_targets(rows, install_dir, own_pid=None, exclude=()):
    """从 Win32_Process 的结果里挑出「属于本安装目录」的进程号。

    只认命令行里带安装目录的，避免误杀用户别的 python。
    自己和调用者（父进程）永远排除。
    """
    marker = str(install_dir).lower()
    drop = {int(p) for p in ([own_pid] if own_pid else []) + list(exclude)}
    picked = []
    for row in rows or []:
        cmd = (row.get("CommandLine") or "").lower()
        if marker and marker not in cmd:
            continue
        pid = row.get("ProcessId")
        if not isinstance(pid, int) or pid in drop:
            continue
        picked.append(pid)
    return picked


def list_processes() -> list:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", QUERY],
            capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = (out.stdout or "").strip()
    if not text:
        return []
    try:
        import json

        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def kill(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       timeout=15, creationflags=CREATE_NO_WINDOW)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    rows = list_processes()
    targets = select_targets(rows, INSTALL_DIR, own_pid=os.getpid(), exclude=[os.getppid()])
    if not targets:
        print("没有找到还在运行的归藏进程")
        return 0
    for pid in targets:
        print(f"停止进程 {pid}")
        kill(pid)
    print(f"已停止 {len(targets)} 个进程；用户数据留在 {INSTALL_DIR}\\work，未删除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
