"""
本地转写用量台账。

飞书没有开放妙记额度的查询接口，只能在每次转写后自己记一笔，
按月累计出「已用多少分钟、还剩多少」。

数据结构（work/usage.json）：
{
  "quota_minutes": 300,          # 0 表示不限（商业版/企业版）
  "records": [{month, video_id, title, seconds, at}]
}
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
WORK = SKILL / "work"
FILE = WORK / "usage.json"
DEFAULT_QUOTA = 300  # 飞书基础版：每人每自然月 300 分钟


def load():
    if not FILE.exists():
        return {"quota_minutes": DEFAULT_QUOTA, "records": []}
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        d.setdefault("quota_minutes", DEFAULT_QUOTA)
        d.setdefault("records", [])
        return d
    except Exception as exc:
        raise ValueError(f"用量台账读取失败，保留原文件: {FILE}") from exc


def save(d):
    from task_state import atomic_text
    atomic_text(FILE, json.dumps(d, ensure_ascii=False, indent=2))


def mp4_duration(path):
    """读 mp4 的 mvhd 盒子拿时长（秒），不依赖 ffmpeg"""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            def read_box(pos):
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                box_size = int.from_bytes(hdr[:4], "big")
                box_type = hdr[4:8]
                header = 8
                if box_size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        return None
                    box_size = int.from_bytes(ext, "big")
                    header = 16
                elif box_size == 0:
                    box_size = size - pos
                if box_size < 8:
                    return None
                return box_type, pos + header, pos + box_size

            pos = 0
            while pos < size:
                box = read_box(pos)
                if not box:
                    break
                btype, start, end = box
                if btype == b"moov":
                    p = start
                    while p < end:
                        b = read_box(p)
                        if not b:
                            break
                        t2, s2, e2 = b
                        if t2 == b"mvhd":
                            f.seek(s2)
                            version = f.read(1)[0]
                            f.read(3)
                            if version == 0:
                                f.read(8)
                                timescale = int.from_bytes(f.read(4), "big")
                                duration = int.from_bytes(f.read(4), "big")
                            else:
                                f.read(16)
                                timescale = int.from_bytes(f.read(4), "big")
                                duration = int.from_bytes(f.read(8), "big")
                            return duration / timescale if timescale else None
                        p = e2
                    return None
                pos = end if box[2] > pos else pos + 8
    except Exception:
        return None
    return None


def add_record(video_path, title="", seconds=None, *, record_id=None, video_id=None):
    path = Path(video_path)
    d = load()
    if record_id:
        existing = next((r for r in d["records"] if r.get("record_id") == record_id), None)
        if existing:
            return existing.get("seconds", 0)
    vid = video_id or ""
    if not vid:
        from task_state import read_video_id
        vid = read_video_id(path.parent)
    if seconds is None:
        seconds = mp4_duration(path) or 0
    d["records"].append({
        "record_id": record_id,
        "month": datetime.now().strftime("%Y-%m"),
        "video_id": vid,
        "title": title[:80],
        "seconds": round(float(seconds or 0), 1),
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save(d)
    return seconds


def summary():
    d = load()
    month = datetime.now().strftime("%Y-%m")
    recs = [r for r in d["records"] if r.get("month") == month]
    used_sec = sum(float(r.get("seconds") or 0) for r in recs)
    quota = int(d.get("quota_minutes", DEFAULT_QUOTA) or 0)
    return {
        "month": month,
        "quota_minutes": quota,
        "unlimited": quota == 0,
        "used_minutes": round(used_sec / 60, 1),
        "remaining_minutes": None if quota == 0 else round(max(0.0, quota - used_sec / 60), 1),
        "count": len(recs),
        "recent": sorted(recs, key=lambda r: r.get("at", ""), reverse=True)[:8],
    }


def backfill(base=str(Path.home() / "抖音视频下载")):
    """扫描已经转写完的视频目录，把历史用量补进台账"""
    base = Path(base)
    if not base.exists():
        print("目录不存在:", base)
        return
    d = load()
    known = {(r.get("video_id"), r.get("month")) for r in d["records"]}
    added = 0
    for info in base.glob("*/*/视频信息.txt"):
        vdir = info.parent
        if not (vdir / "语音转文字.txt").exists():
            continue
        mp4 = list(vdir.glob("*.mp4"))
        if not mp4:
            continue
        from task_state import read_video_id
        vid = read_video_id(vdir)
        if not vid:
            continue
        month = vdir.parent.name[:7]
        if (vid, month) in known:
            continue
        sec = mp4_duration(mp4[0]) or 0
        d["records"].append({
            "month": month,
            "video_id": vid,
            "title": vdir.parent.name + "/" + vdir.name,
            "seconds": round(sec, 1),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        known.add((vid, month))
        added += 1
        print(f"补录 {vid}  {sec/60:.1f} 分钟")
    if added:
        save(d)
    print(f"完成，新增 {added} 条")


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        idx = sys.argv.index("--backfill")
        target = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else str(Path.home() / "抖音视频下载")
        backfill(target)
    elif "--set-quota" in sys.argv:
        idx = sys.argv.index("--set-quota")
        d = load()
        d["quota_minutes"] = int(sys.argv[idx + 1])
        save(d)
        print("额度已设为", d["quota_minutes"], "分钟（0 表示不限）")
    else:
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
