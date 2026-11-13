"""
给抖音的唤起协议注册一个静默接收器，彻底消除「无法打开此 bytedance 链接」弹窗。

只写 HKCU（当前用户），不需要管理员，随时可撤销：
    python register_protocols.py --remove

以后再装抖音客户端时，请先跑上面的撤销命令，让客户端的注册生效。
"""
import sys
import winreg
from pathlib import Path

APP = Path(__file__).resolve().parent
PYW = Path(sys.executable).with_name("pythonw.exe")
SWALLOW = APP / "proto_swallow.py"

SCHEMES = ["bytedance", "snssdk1128", "douyin", "aweme"]
BASE = r"Software\Classes"


def register():
    if not PYW.exists():
        print("找不到 pythonw.exe:", PYW)
        return 1
    if not SWALLOW.exists():
        print("找不到 proto_swallow.py:", SWALLOW)
        return 1

    cmd = f'"{PYW}" "{SWALLOW}" "%1"'
    for s in SCHEMES:
        root = f"{BASE}\\{s}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"URL:{s} Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
        print(f"已注册  {s}://")
    print("\n处理命令:", cmd)
    return 0


def remove():
    for s in SCHEMES:
        for sub in (rf"{BASE}\{s}\shell\open\command",
                    rf"{BASE}\{s}\shell\open",
                    rf"{BASE}\{s}\shell",
                    rf"{BASE}\{s}"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  {sub}: {e}")
        print(f"已移除  {s}://")
    return 0


if __name__ == "__main__":
    sys.exit(remove() if "--remove" in sys.argv else register())
