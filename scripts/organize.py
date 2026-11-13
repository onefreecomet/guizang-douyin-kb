"""
把已经下载好的目录整理成可读的名字。

规则（与下载脚本一致）：
    文件夹  01_标题前16字
    视频    标题前50字.mp4

用法：
    python organize.py [目录]              # 整理 <用户主目录>\抖音视频下载
    python organize.py [目录] --dry-run    # 只打印要改什么，不动手
"""
import re
import sys
from pathlib import Path

from naming import safe_name, clean_title

DEFAULT_BASE = Path.home() / "抖音视频下载"


def read_title(info_file: Path) -> str:
    try:
        text = info_file.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = re.search(r"标题:\s*\n(.+)", text, re.S)
    return m.group(1).strip() if m else ""


def order_key(p: Path):
    head = p.name.split("_")[0]
    return (0, int(head), p.name) if head.isdigit() else (1, 0, p.name)


def organize(base: Path, dry_run=False, log=print):
    if not base.exists():
        log(f"目录不存在: {base}")
        return 0
    changed = 0

    for date_dir in sorted([d for d in base.iterdir() if d.is_dir()]):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name):
            continue
        video_dirs = sorted([d for d in date_dir.iterdir() if d.is_dir()], key=order_key)

        plan = []
        for idx, vdir in enumerate(video_dirs, 1):
            title = clean_title(read_title(vdir / "视频信息.txt"))
            pretty_dir = safe_name(title, 16)
            new_dir_name = f"{idx:02d}_{pretty_dir}" if pretty_dir else f"{idx:02d}"

            # 1. 先把视频文件改名
            mp4s = list(vdir.glob("*.mp4"))
            if mp4s and title:
                old = mp4s[0]
                pretty_file = safe_name(title, 50)
                if pretty_file and old.stem != pretty_file:
                    new_file = vdir / f"{pretty_file}{old.suffix}"
                    if new_file.exists():
                        log(f"  跳过（目标已存在）: {vdir.name}/{new_file.name}")
                    else:
                        log(f"  视频 {old.name}  ->  {new_file.name}")
                        if not dry_run:
                            try:
                                old.rename(new_file)
                                changed += 1
                            except Exception as e:
                                log(f"    失败: {e}")

            # 2. 目录改名留到最后，避免路径失效
            if new_dir_name != vdir.name:
                plan.append((vdir, new_dir_name))

        for old_dir, new_name in plan:
            target = date_dir / new_name
            if target.exists():
                log(f"  跳过目录（已存在）: {new_name}")
                continue
            log(f"  目录 {old_dir.name}  ->  {new_name}")
            if not dry_run:
                try:
                    old_dir.rename(target)
                    changed += 1
                except Exception as e:
                    log(f"    失败: {e}")

    log(f"\n完成，共处理 {changed} 项" + ("（演练模式，未实际修改）" if dry_run else ""))
    return changed


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = Path(args[0]) if args else DEFAULT_BASE
    organize(base, dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
