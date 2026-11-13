"""创建桌面快捷方式（带图标），以后换 Python 版本重跑一次即可"""
import os
import sys
from pathlib import Path

import win32com.client

APP = Path(__file__).resolve().parent
PYW = Path(sys.executable).with_name("pythonw.exe")
LNK = Path(os.path.expanduser("~")) / "Desktop" / "DouyinDownloader.lnk"


def main():
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(LNK))
    sc.TargetPath = str(PYW)
    sc.Arguments = f'"{APP / "launcher.py"}"'
    sc.WorkingDirectory = str(APP)
    sc.IconLocation = f"{APP / 'icon.ico'},0"
    sc.Description = "Douyin Favorites Downloader"
    sc.WindowStyle = 7
    sc.Save()
    print("快捷方式:", LNK, "存在:", LNK.exists())


if __name__ == "__main__":
    main()
