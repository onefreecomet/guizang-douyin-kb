"""yt-dlp 自动更新。

两种装法对应两条路：
- 便携版（``runtime\\lib`` 里放 yt_dlp 包）：下载官方 wheel，整个 ``yt_dlp`` 目录换掉；
- 开发/venv 版（``.venv\\Lib\\site-packages``）：同样换目录，不需要 pip 也不联网装依赖。

不 import yt_dlp，只把它当子进程用——这样更新出一个坏版本，最多是下载失败，
不会把主程序一起带崩（``--check`` 和回退逻辑都在这层）。

用法::

    python app/ytdlp_update.py --check     # 只看有没有新版，不装
    python app/ytdlp_update.py             # 有新版就装（24 小时内只查一次）
    python app/ytdlp_update.py --force     # 不管新旧、不管时间，立刻查并装
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
INSTALL_DIR = APP_DIR.parent

PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
STATE_NAME = "ytdlp_update.json"
MAX_AGE_HOURS = 24
TIMEOUT = 30
WHEEL_TAG = "py3-none-any"


def _utf8_stdout() -> None:
    """Windows 控制台是 GBK，打印 ✔ 之类会炸，统一改成 replace。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 位置

def site_dir() -> Path | None:
    """yt_dlp 包应该待的目录：便携版优先，其次 .venv。"""
    runtime_lib = INSTALL_DIR / "runtime" / "lib"
    if (runtime_lib / "yt_dlp").is_dir():
        return runtime_lib
    venv_site = INSTALL_DIR / ".venv" / "Lib" / "site-packages"
    if (venv_site / "yt_dlp").is_dir():
        return venv_site
    return None


def state_path() -> Path:
    try:
        import paths

        return paths.data_dir() / STATE_NAME
    except Exception:  # paths 不可用时退到项目自己的 work/
        return INSTALL_DIR / "work" / STATE_NAME


# ---------------------------------------------------------------- 版本

def local_version() -> str | None:
    """当前能跑起来的 yt-dlp 版本；跑不起来就 None。"""
    try:
        import paths

        ok, text = paths.probe_ytdlp()
        return text if ok else None
    except Exception:
        return None


def _read_json(url: str, timeout: int = TIMEOUT) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _stream_to(url: str, dest: Path, timeout: int = TIMEOUT) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as response, dest.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def latest_release(fetch=_read_json) -> tuple[str, str]:
    """返回 (版本号, wheel 地址)。取不到就抛异常，调用方负责兜底。"""
    data = fetch(PYPI_JSON)
    version = (data.get("info") or {}).get("version")
    if not version:
        raise ValueError("PyPI 返回里没有版本号")
    for entry in (data.get("releases") or {}).get(version) or []:
        name = entry.get("filename") or ""
        if name.endswith(".whl") and WHEEL_TAG in name:
            url = entry.get("url")
            if url:
                return version, url
    raise ValueError(f"{version} 没有可用的 wheel")


def version_tuple(text: str | None) -> tuple:
    """'2026.10.26' -> (2026, 10, 26)；带后缀的脏数据也不炸。"""
    if not text:
        return ()
    parts = []
    for piece in text.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str | None) -> bool:
    if not current:
        return True
    return version_tuple(candidate) > version_tuple(current)


# ---------------------------------------------------------------- 安装

def install_wheel(url: str, target: Path | None = None, download=_stream_to) -> Path:
    """把 wheel 里的 yt_dlp 包换进目标目录。先解到临时目录，再整体替换。"""
    target = target or site_dir()
    if target is None:
        raise RuntimeError("找不到 yt_dlp 该装到哪里（既没有 runtime/lib 也没有 .venv）")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        whl = Path(tmp) / "yt_dlp.whl"
        download(url, whl)
        unpack = Path(tmp) / "unpack"
        with zipfile.ZipFile(whl) as archive:
            archive.extractall(unpack)
        source = unpack / "yt_dlp"
        if not source.is_dir():
            raise ValueError("wheel 里没有 yt_dlp 目录")
        final = target / "yt_dlp"
        stale = target / ".yt_dlp.old"
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
        if final.exists():
            final.rename(stale)
        shutil.copytree(source, final)
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)
    return final


def _load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def fresh_enough(hours: int = MAX_AGE_HOURS) -> bool:
    """上一次检查是不是还在有效期内（避免守护每轮都去问 PyPI）。"""
    checked = _load_state().get("checked_at")
    if not isinstance(checked, (int, float)):
        return False
    return (time.time() - checked) < hours * 3600


def maybe_update(force: bool = False, hours: int = MAX_AGE_HOURS) -> dict:
    """检查并按需更新。返回一份结果字典，永不抛异常。"""
    result = {"checked": False, "updated": False, "from": local_version(), "to": None, "error": None}
    if not force and fresh_enough(hours):
        result["skipped"] = "recently-checked"
        return result
    state = _load_state()
    state["checked_at"] = time.time()
    result["checked"] = True
    try:
        version, url = latest_release()
        result["to"] = version
        if is_newer(version, result["from"]):
            install_wheel(url)
            result["from"] = result["from"]
            result["now"] = local_version()
            result["updated"] = True
            state["installed"] = version
        state["latest"] = version
        state["error"] = None
    except (urllib.error.URLError, OSError, ValueError, RuntimeError, KeyError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        state["error"] = result["error"]
    _save_state(state)
    return result


def render(result: dict) -> str:
    if result.get("skipped"):
        return f"yt-dlp 最近已检查过（{result['skipped']}），跳过"
    if result.get("error"):
        return f"yt-dlp 更新检查失败：{result['error']}（继续用 {result.get('from') or '现有版本'}）"
    if result.get("updated"):
        return f"yt-dlp 已更新：{result.get('from')} -> {result.get('now') or result.get('to')}"
    return f"yt-dlp 已是最新：{result.get('from') or result.get('to')}"


def main(argv=None) -> int:
    _utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    check_only = "--check" in argv
    if check_only:
        try:
            version, _url = latest_release()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"检查失败：{exc}")
            return 1
        current = local_version()
        newer = is_newer(version, current)
        print(f"当前 {current or '不可用'} / 最新 {version} / {'有新版本' if newer else '已是最新'}")
        return 0 if not newer else 2
    result = maybe_update(force=force)
    print(render(result))
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
