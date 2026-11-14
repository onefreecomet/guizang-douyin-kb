"""
静默接收器：吞掉抖音的唤起协议（bytedance:// 等），避免系统反复弹「需要一个新应用」。

由 pythonw.exe 调用，不显示任何窗口，收到链接后记录一行日志就退出。
"""
import datetime
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
try:  # 数据目录与其它模块保持一致（GUIZANG_HOME 可改），拿不到就退回老位置
    import paths as _paths

    WORK = _paths.data_dir()
except Exception:  # pragma: no cover - 只在 paths 坏掉时走
    WORK = APP.parent / "work"
LOG = WORK / "swallowed_protocols.log"
MAX_BYTES = 200_000


def main():
    try:
        url = sys.argv[1] if len(sys.argv) > 1 else ""
        LOG.parent.mkdir(parents=True, exist_ok=True)
        # 抖音会在一秒内连发几十次，同一秒只记一条，避免日志和磁盘 IO 爆炸
        now = datetime.datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        if LOG.exists():
            try:
                with LOG.open("rb") as f:
                    f.seek(max(0, LOG.stat().st_size - 200))
                    tail = f.read().decode("utf-8", "ignore")
                if tail.strip().splitlines() and tail.strip().splitlines()[-1].startswith(stamp):
                    return
            except Exception:
                pass
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{url}\n")
        if LOG.stat().st_size > MAX_BYTES:
            lines = LOG.read_text(encoding="utf-8").splitlines()[-200:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    main()
