"""Shared durable video state, legacy discovery and conservative MP4 validation."""
import json
import os
import re
import struct
import uuid
from pathlib import Path

EVENT_PREFIX = '@@DY_EVENT@@'
STATE_FILE = 'task_state.json'


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_state(video_dir):
    path = Path(video_dir) / STATE_FILE
    if not path.exists():
        return {}
    # Corrupt checkpoints must not silently trigger a second paid upload.
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'任务状态格式无效: {path}')
    return data


def save_state(video_dir, **updates):
    data = load_state(video_dir)
    data.update(updates)
    atomic_text(Path(video_dir) / STATE_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def emit_event(**data):
    print(EVENT_PREFIX + json.dumps(data, ensure_ascii=False), flush=True)


def read_video_id(video_dir):
    video_dir = Path(video_dir)
    state = load_state(video_dir)
    if state.get('video_id'):
        return str(state['video_id'])
    info = video_dir / '视频信息.txt'
    if info.exists():
        m = re.search(r'视频ID\s*[:：]\s*([A-Za-z0-9_-]+)', info.read_text(encoding='utf-8-sig'))
        if m:
            return m.group(1)
    for path in video_dir.glob('video_*.mp4'):
        m = re.fullmatch(r'video_([A-Za-z0-9_-]+)\.mp4', path.name)
        if m:
            return m.group(1)
    return ''


def _boxes(f, start, end):
    pos = start
    while pos < end:
        if end - pos < 8:
            raise ValueError('truncated box header')
        f.seek(pos)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            raw = f.read(8)
            if len(raw) != 8:
                raise ValueError('truncated extended size')
            size = struct.unpack('>Q', raw)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError('truncated or invalid box')
        yield kind, pos + header, pos + size
        pos += size


def valid_video(path):
    """Check complete ISO BMFF boxes, movie header, video track and media payload.

    This catches HTTP bodies and truncated files; it is not a full codec decoder.
    """
    try:
        path = Path(path)
        size = path.stat().st_size
        if size < 32:
            return False
        found = set()
        video_track = False
        movie_header = False
        with path.open('rb') as f:
            for kind, start, end in _boxes(f, 0, size):
                if kind == b'ftyp' and end - start >= 8:
                    found.add('ftyp')
                elif kind == b'mdat' and end > start:
                    found.add('mdat')
                elif kind == b'moov':
                    for sub, s, e in _boxes(f, start, end):
                        if sub == b'mvhd' and e - s >= 20:
                            f.seek(s)
                            version = f.read(1)[0]
                            off = 20 if version == 1 else 12
                            width = 8 if version == 1 else 4
                            if e - s < off + 4 + width:
                                continue
                            f.seek(s + off)
                            scale = int.from_bytes(f.read(4), 'big')
                            duration = int.from_bytes(f.read(width), 'big')
                            movie_header = scale > 0 and duration > 0
                        if sub == b'trak':
                            for track, ts, te in _boxes(f, s, e):
                                if track != b'mdia':
                                    continue
                                for media, ms, me in _boxes(f, ts, te):
                                    if media == b'hdlr' and me - ms >= 12:
                                        f.seek(ms + 8)
                                        video_track |= f.read(4) == b'vide'
        return found == {'ftyp', 'mdat'} and movie_header and video_track
    except (OSError, ValueError, struct.error, IndexError):
        return False


def find_video(video_dir):
    video_dir = Path(video_dir)
    state = load_state(video_dir)
    name = state.get('video_file')
    if name and Path(name).name == name and valid_video(video_dir / name):
        return video_dir / name
    return next((p for p in sorted(video_dir.glob('*.mp4')) if valid_video(p)), None)


def local_index(base):
    """Include failed directories for stable retry paths and renamed legacy videos."""
    index = {}
    for directory in sorted(Path(base).glob('*/*')):
        if not directory.is_dir():
            continue
        vid = read_video_id(directory)
        if vid and (vid not in index or (find_video(directory) and not find_video(index[vid]))):
            index[vid] = directory
    return index


def downloaded_ids(base):
    return {vid for vid, directory in local_index(base).items() if find_video(directory)}
