"""归藏 —— 运行时路径与外部依赖定位。

把"东西在哪儿"的判断集中到这一个文件，别的地方只调函数、不自己拼路径。

查找顺序（每一类都一样）：**本目录自带 → 环境变量 → 系统 PATH**。
自带优先，是因为"解压即用"的包里带的版本应该压过用户机器上的旧版本；
环境变量次之，是给高级用户临时改道用的。

不依赖任何第三方库。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
INSTALL_DIR = APP_DIR.parent
BIN_DIR = INSTALL_DIR / "bin"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 环境变量名集中在这里，文档和体检页都引用同一份
ENV_HOME = "GUIZANG_HOME"
ENV_NODE = "GUIZANG_NODE"
ENV_LARK = "GUIZANG_LARK"
ENV_BROWSER = "GUIZANG_BROWSER"


# ---------------------------------------------------------------- 目录

def data_dir() -> Path:
    """工作目录：配置、数据库、日志放这里。

    默认仍是程序旁边的 work/（和以前一致，升级不会搬家），
    但允许用 GUIZANG_HOME 指到别处——安装包将来会把它指到 %LOCALAPPDATA%。
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    return INSTALL_DIR / "work"


def ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_bin_dir() -> Path:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    return BIN_DIR


# ---------------------------------------------------------------- 通用

def _first_file(candidates) -> Path | None:
    for c in candidates:
        if not c:
            continue
        p = Path(c) if not isinstance(c, Path) else c
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _newest_node_version_dir(base: Path) -> Path | None:
    """~/.workbuddy/binaries/node/versions/ 下取版本号最大的一个（只在旧路径兜底时用）。"""
    try:
        dirs = [d for d in base.iterdir() if (d / "node.exe").is_file()]
    except OSError:
        return None
    if not dirs:
        return None

    def key(d: Path):
        # 版本目录名长这样：22.22.2-6 / v20.11.0 / 18.0.0，带不带 v、带不带 -N 都要能排队
        parts = []
        for piece in d.name.lstrip("vV").replace("-", ".").split("."):
            digits = "".join(ch for ch in piece if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        return parts

    return sorted(dirs, key=key)[-1]


# ---------------------------------------------------------------- node

def find_node() -> Path | None:
    """yt-dlp 解抖音 JS 挑战要用的 node.exe。

    注意最后那个 ~/.workbuddy 兜底只是给"从旧环境迁移过来"的机器用的，
    新装的机器应该靠 bin/node/node.exe。
    """
    env = os.environ.get(ENV_NODE)
    if env:
        p = _first_file([Path(env).expanduser()])
        if p:
            return p
    p = _first_file([BIN_DIR / "node" / "node.exe", BIN_DIR / "node.exe"])
    if p:
        return p
    legacy = _newest_node_version_dir(Path.home() / ".workbuddy" / "binaries" / "node" / "versions")
    if legacy:
        return legacy / "node.exe"
    which = shutil.which("node")
    return Path(which) if which else None


def find_lark() -> str | None:
    """飞书妙记 CLI（语音转写用，可选依赖）。找不到不代表程序坏了，只代表不能转写。"""
    env = os.environ.get(ENV_LARK)
    if env:
        return env
    p = _first_file([BIN_DIR / "lark-cli.cmd", BIN_DIR / "lark-cli.exe", BIN_DIR / "lark-cli"])
    if p:
        return str(p)
    # 老环境里 lark-cli 和 node 是装在一起的，跟着 node 找一遍
    node = find_node()
    if node:
        p = _first_file([node.parent / "lark-cli.cmd", node.parent / "lark-cli.exe", node.parent / "lark-cli"])
        if p:
            return str(p)
    # 我们自己带了 bin/node 之后，node 旁边就没有 lark-cli 了；
    # 但"从旧环境迁移过来"的机器上它跟旧 node 装在一起，仍值得找一遍。
    legacy = _newest_node_version_dir(Path.home() / ".workbuddy" / "binaries" / "node" / "versions")
    if legacy:
        p = _first_file([legacy / "lark-cli.cmd", legacy / "lark-cli.exe", legacy / "lark-cli"])
        if p:
            return str(p)
    which = shutil.which("lark-cli")
    if which:
        return which
    return None


# ---------------------------------------------------------------- 浏览器

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _edge_from_registry() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
    )
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                val, _ = winreg.QueryValueEx(k, "")
        except OSError:
            continue
        if val:
            p = _first_file([val.strip('"')])
            if p:
                return p
    return None


def find_edge() -> Path | None:
    env = os.environ.get(ENV_BROWSER)
    if env:
        p = _first_file([Path(env).expanduser()])
        if p:
            return p
    p = _edge_from_registry()
    if p:
        return p
    p = _first_file([Path(c) for c in EDGE_PATHS])
    if p:
        return p
    which = shutil.which("msedge")
    return Path(which) if which else None


# ---------------------------------------------------------------- Python

RUNTIME_PYTHON_DIR = INSTALL_DIR / "runtime" / "python"


def bundled_pythonw() -> Path | None:
    """便携版自带的解释器（免安装，用户机器上不需要装 Python）。"""
    return _first_file([RUNTIME_PYTHON_DIR / "pythonw.exe"])


def bundled_python() -> Path | None:
    return _first_file([RUNTIME_PYTHON_DIR / "python.exe"])


def venv_pythonw() -> Path | None:
    """后台进程用的无窗口解释器：便携版优先，其次 .venv。"""
    return _first_file([
        RUNTIME_PYTHON_DIR / "pythonw.exe",
        INSTALL_DIR / ".venv" / "Scripts" / "pythonw.exe",
    ])


def venv_python() -> Path | None:
    """跑脚本、`-m yt_dlp` 用的解释器：便携版优先，其次 .venv。"""
    return _first_file([
        RUNTIME_PYTHON_DIR / "python.exe",
        INSTALL_DIR / ".venv" / "Scripts" / "python.exe",
    ])


def pythonw() -> Path:
    """拉起后台进程时用的解释器：优先便携版/venv 的 pythonw，避免弹黑窗口。"""
    p = venv_pythonw()
    if p:
        return p
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return cand if cand.is_file() else exe


# ---------------------------------------------------------------- 命令

def _run(cmd) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    text = (out.stdout or out.stderr or "").strip()
    return out.returncode == 0, text.splitlines()[0] if text else ""


def probe_node(exe: Path | None = None) -> tuple[bool, str]:
    exe = exe or find_node()
    if not exe:
        return False, "未找到"
    ok, text = _run([str(exe), "--version"])
    return (ok and text.startswith("v")), text or "无法执行"


def probe_ytdlp(python: Path | None = None) -> tuple[bool, str]:
    py = python or venv_python() or Path(sys.executable)
    ok, text = _run([str(py), "-m", "yt_dlp", "--version"])
    return ok, text or "未安装"


def probe_lark() -> tuple[bool, str]:
    bin_path = find_lark()
    if not bin_path:
        return False, "未找到"
    # .cmd 要走 cmd.exe 才能稳；直接用 CreateProcess 有时会 193
    cmd = ["cmd", "/c", bin_path, "--version"] if bin_path.lower().endswith((".cmd", ".bat")) else [bin_path, "--version"]
    ok, text = _run(cmd)
    return ok, text or "存在但无法执行"


# ---------------------------------------------------------------- 汇总

def snapshot() -> dict:
    """给体检页 / doctor 用的一份现状快照。"""
    node_exe = find_node()
    node_ok, node_ver = probe_node(node_exe)
    ytdlp_ok, ytdlp_ver = probe_ytdlp()
    lark_ok, lark_ver = probe_lark()
    edge = find_edge()
    return {
        "install_dir": str(INSTALL_DIR),
        "app_dir": str(APP_DIR),
        "data_dir": str(data_dir()),
        "data_dir_writable": _writable(data_dir()),
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "pythonw": str(pythonw()),
        "node": str(node_exe) if node_exe else None,
        "node_ok": node_ok,
        "node_version": node_ver,
        "node_bundled": bool(node_exe and BIN_DIR in node_exe.parents),
        "ytdlp_ok": ytdlp_ok,
        "ytdlp_version": ytdlp_ver,
        "lark": find_lark(),
        "lark_ok": lark_ok,
        "lark_version": lark_ver,
        "edge": str(edge) if edge else None,
        "home_override": os.environ.get(ENV_HOME, ""),
    }


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    import json

    print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
