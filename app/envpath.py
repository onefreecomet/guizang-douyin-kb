"""把外部依赖目录补进 PATH。

为什么需要这一步：从桌面快捷方式启动时，进程继承的是系统 PATH，
里面可能没有 node（yt-dlp 解抖音 JS 挑战要用），也没有飞书妙记的 CLI。
界面就会显示「node 不可用」、下载直接失败。

查找顺序见 `paths.py`：**本目录自带 → 环境变量 → 系统 PATH**。
所以只要把 node.exe 放进安装目录的 `bin/node/`，就再也不依赖别的软件。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

MARK = "DY_PATH_AUGMENTED"


def candidate_dirs() -> list:
    """要补进 PATH 的目录，按优先级从高到低。"""
    out = []

    node = paths.find_node()
    if node:
        out.append(str(node.parent))

    lark = paths.find_lark()
    if lark:
        out.append(str(Path(lark).parent))

    if paths.BIN_DIR.is_dir():
        out.append(str(paths.BIN_DIR))

    local_bin = Path.home() / ".local" / "bin"
    if local_bin.is_dir():
        out.append(str(local_bin))

    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def augment_path() -> list:
    """把缺失的目录补到 PATH 最前面，返回被补进去的目录。幂等。"""
    if os.environ.get(MARK) == "1":
        return []

    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    added = []
    for d in reversed(candidate_dirs()):
        if d not in parts:
            path = d + os.pathsep + path if path else d
            added.append(d)
    os.environ["PATH"] = path
    os.environ[MARK] = "1"
    return added


if __name__ == "__main__":
    added = augment_path()
    print("已补充:", added or "无需补充")
    node = paths.find_node()
    print("node:", node or "未找到")
    print("lark-cli:", paths.find_lark() or "未找到（转写会跳过）")
