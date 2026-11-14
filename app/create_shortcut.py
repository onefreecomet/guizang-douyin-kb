"""创建桌面快捷方式（带图标）。

早先这个脚本 `import win32com.client`，但 venv 里根本没装 pywin32——
也就是说它是靠别的 Python 跑起来的，属于没人记得的隐式依赖。
现在改成**纯标准库**：拼一段 PowerShell 交给系统的 COM 组件去做，
不需要任何第三方包，装在干净机器上也能直接跑。

用法：
    python app/create_shortcut.py            # 桌面图标
    python app/create_shortcut.py --startup  # 顺带建开机自启（守护进程）
"""
import base64
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

APP = paths.APP_DIR
ICON = APP / "icon.ico"
DESCRIPTION = "归藏 · 抖音收藏知识库 — by 冰临城下"


def desktop_dir() -> Path:
    """优先问系统要桌面路径（可能是 OneDrive 重定向过的）。"""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as k:
                val, _ = winreg.QueryValueEx(k, "Desktop")
            return Path(os.path.expandvars(val))
        except OSError:
            pass
    return Path.home() / "Desktop"


def startup_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def shortcut_spec(kind: str = "desktop") -> dict:
    """生成一份快捷方式定义（纯数据，方便测试）。

    命名与安装包（tools/installer.iss 的 [Icons]）保持一致：
    桌面「归藏.lnk」、启动「归藏守护.lnk」。旧版叫 DouyinDownloader.lnk /
    GuizangWatchdog.lnk，与安装包混用会在开始菜单/启动项里出现两个入口。
    """
    pythonw = paths.pythonw()
    if kind == "startup":
        return {
            "path": startup_dir() / "归藏守护.lnk",
            "target": str(pythonw),
            "arguments": f'"{APP / "watchdog.py"}" --interval 30',
            "workdir": str(APP),
            "icon": f"{ICON},0" if ICON.is_file() else "",
            "description": f"{DESCRIPTION}（开机自启）",
            "window_style": 7,
        }
    return {
        "path": desktop_dir() / "归藏.lnk",
        "target": str(pythonw),
        "arguments": f'"{APP / "launcher.py"}"',
        "workdir": str(APP),
        "icon": f"{ICON},0" if ICON.is_file() else "",
        "description": DESCRIPTION,
        "window_style": 7,
    }


def _ps_quote(value: str) -> str:
    """PowerShell 单引号字符串：内部的 ' 要写成 ''。"""
    return "'" + value.replace("'", "''") + "'"


def ps_script(spec: dict) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$sc = $shell.CreateShortcut({_ps_quote(str(spec['path']))})",
        f"$sc.TargetPath = {_ps_quote(spec['target'])}",
        f"$sc.Arguments = {_ps_quote(spec['arguments'])}",
        f"$sc.WorkingDirectory = {_ps_quote(spec['workdir'])}",
    ]
    if spec.get("icon"):
        lines.append(f"$sc.IconLocation = {_ps_quote(spec['icon'])}")
    if spec.get("description"):
        lines.append(f"$sc.Description = {_ps_quote(spec['description'])}")
    lines += [
        f"$sc.WindowStyle = {int(spec.get('window_style', 7))}",
        "$sc.Save()",
    ]
    return ";\n".join(lines)


def create(kind: str = "desktop") -> Path:
    spec = shortcut_spec(kind)
    script = ps_script(spec)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    cmd = [
        str(exe if exe.is_file() else "powershell"),
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"创建快捷方式失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return spec["path"]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    made = [create("desktop")]
    if "--startup" in argv:
        made.append(create("startup"))
    for p in made:
        print(f"快捷方式: {p}  存在: {p.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
