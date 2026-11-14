"""归藏体检：一条命令看清这台机器还差什么。

装完、换机器、下载突然失败时先跑这个：

    python app/doctor.py

它只读不改：不启动服务、不写配置、不联网。
退出码 0 表示关键项都过了，1 表示还有必须解决的。

    python app/doctor.py --json   # 给脚本/安装器读
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

def _pick_marks() -> tuple:
    """Windows 控制台默认是 GBK，打不出 ✔ ✘ —，那就退回 ASCII，别让体检本身崩掉。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "✔✘—".encode(encoding)
        return OK_UNICODE, BAD_UNICODE, SKIP_UNICODE
    except (UnicodeEncodeError, LookupError):
        return "[OK]", "[!!]", "[--]"


OK_UNICODE, BAD_UNICODE, SKIP_UNICODE = "✔", "✘", "—"
OK, BAD, SKIP = _pick_marks()

# 关键项：缺了就跑不起来；可选：缺了只是少一个功能
CRITICAL = {"python", "node", "ytdlp", "data_dir", "browser", "cookies", "vault"}
OPTIONAL = {"lark", "ai_key", "notify"}


def load_config() -> dict:
    cfg = paths.data_dir() / "config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _masked(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-2:]


def checks(cfg: dict | None = None) -> list:
    """返回 [(key, 名称, 状态, 说明)]，状态取 ok / bad / skip。"""
    cfg = load_config() if cfg is None else cfg
    rows = []

    py = sys.version_info
    rows.append((
        "python", "Python",
        "ok" if py >= (3, 10) else "bad",
        f"{py.major}.{py.minor}.{py.micro} @ {sys.executable}",
    ))

    node = paths.find_node()
    node_ok, node_ver = paths.probe_node(node)
    rows.append((
        "node", "node（下载抖音视频要用）",
        "ok" if node_ok else "bad",
        f"{node_ver} @ {node}" if node_ok else f"未找到或跑不起来（{node_ver}）",
    ))

    ytdlp_ok, ytdlp_ver = paths.probe_ytdlp()
    rows.append((
        "ytdlp", "yt-dlp（下载器本体）",
        "ok" if ytdlp_ok else "bad",
        ytdlp_ver,
    ))

    data = paths.data_dir()
    writable = paths._writable(data)
    rows.append((
        "data_dir", "数据目录",
        "ok" if writable else "bad",
        str(data) if writable else f"{data}（不可写）",
    ))

    edge = paths.find_edge()
    rows.append((
        "browser", "浏览器窗口壳（Edge）",
        "ok" if edge else "skip",
        str(edge) if edge else "未找到，会退化成系统默认浏览器打开",
    ))

    cookies = Path(cfg.get("cookies") or (Path.home() / "抖音视频下载" / "cookies.txt"))
    rows.append((
        "cookies", "抖音 cookies",
        "ok" if cookies.is_file() else "bad",
        str(cookies) if cookies.is_file() else f"{cookies}（不存在：登录抖音后导出 cookies.txt 放到这里）",
    ))

    vault = Path(cfg.get("vault_path") or (Path.home() / "Obsidian" / "Vault"))
    base = Path(cfg.get("base_dir") or (Path.home() / "抖音视频下载"))
    rows.append((
        "vault", "Obsidian 库",
        "ok" if vault.is_dir() else "bad",
        str(vault) if vault.is_dir() else f"{vault}（不存在：在控制台「设置」里选你的库）",
    ))
    rows.append((
        "base_dir", "下载目录",
        "ok" if base.is_dir() else "skip",
        str(base) if base.is_dir() else f"{base}（还不存在，首次下载会自动创建）",
    ))

    lark_ok, lark_ver = paths.probe_lark()
    rows.append((
        "lark", "飞书妙记 CLI（可选，做语音转写）",
        "ok" if lark_ok else "skip",
        lark_ver if lark_ok else "未找到：会跳过语音转写，其余流程照常",
    ))

    key = cfg.get("ai_api_key") or ""
    rows.append((
        "ai_key", "AI 密钥（可选，做摘要加工）",
        "ok" if key else "skip",
        f"{_masked(key)} @ {cfg.get('ai_model') or '未配置模型'}" if key else "未填：会跳过 AI 加工，仍保存转写原文",
    ))

    notify = cfg.get("notify_receive_id") or ""
    rows.append((
        "notify", "飞书通知（可选）",
        "ok" if notify and cfg.get("notify_enabled") else "skip",
        "已配置" if notify and cfg.get("notify_enabled") else "未启用",
    ))

    return rows


def render(rows) -> str:
    width = max(len(name) for _, name, _, _ in rows)
    lines = []
    for _, name, state, detail in rows:
        mark = {"ok": OK, "bad": BAD, "skip": SKIP}[state]
        lines.append(f"{mark} {name.ljust(width)}  {detail}")

    bad = [r for r in rows if r[2] == "bad"]
    critical_bad = [r for r in bad if r[0] in CRITICAL]
    lines.append("")
    if critical_bad:
        lines.append(f"还有 {len(critical_bad)} 个关键项没过：")
        for key, name, _, detail in critical_bad:
            lines.append(f"  · {name}：{detail}")
    else:
        lines.append("关键项全部通过，可以用了。")
    skipped = [r[1] for r in rows if r[2] == "skip" and r[0] in OPTIONAL]
    if skipped:
        lines.append("可选功能未启用：" + "、".join(skipped))
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    rows = checks()
    if "--json" in argv:
        print(json.dumps(
            [{"key": k, "name": n, "state": s, "detail": d} for k, n, s, d in rows],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(render(rows))
    return 0 if not [r for r in rows if r[2] == "bad" and r[0] in CRITICAL] else 1


if __name__ == "__main__":
    raise SystemExit(main())
