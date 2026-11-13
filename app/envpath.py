"""
把 WorkBuddy 自带的 node / lark-cli 目录补进 PATH。

从桌面快捷方式启动时，进程继承的是系统 PATH，里面没有 node 目录，
会导致界面显示「node 不可用」、下载时找不到 node。
"""
import os
from pathlib import Path

MARK = "DY_PATH_AUGMENTED"


def augment_path() -> list:
    """返回被补进去的目录"""
    if os.environ.get(MARK) == "1":
        return []

    added = []
    candidates = []

    node_base = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if node_base.is_dir():
        for d in sorted(node_base.iterdir(), reverse=True):
            if (d / "node.exe").exists():
                candidates.append(d)
                break

    portable = Path(__file__).resolve().parent.parent / "bin" / "node"
    if (portable / "node.exe").exists():
        candidates.append(portable)

    for c in (Path.home() / ".local" / "bin",):
        if c.is_dir():
            candidates.append(c)

    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep)
    for d in reversed(candidates):
        if str(d) not in parts:
            path = str(d) + os.pathsep + path
            added.append(str(d))
    os.environ["PATH"] = path
    os.environ[MARK] = "1"
    return added


if __name__ == "__main__":
    print("已补充:", augment_path() or "无需补充")
