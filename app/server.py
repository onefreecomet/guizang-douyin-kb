"""
归藏 · 抖音收藏知识库 · 本地控制台后端
零依赖，只用 Python 标准库。
用法: python server.py [--port 8765] [--open]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SKILL_DIR = APP_DIR.parent
SCRIPTS = SKILL_DIR / "scripts"
WORK_DIR = SKILL_DIR / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)
# The watchdog respects this marker so "退出服务" is never fought by an auto restart.
WATCHDOG_PAUSE = WORK_DIR / "watchdog.pause"


def watchdog_pause(reason):
    try:
        WATCHDOG_PAUSE.write_text(str(reason), encoding="utf-8")
    except OSError:
        pass


def watchdog_resume():
    try:
        WATCHDOG_PAUSE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
sys.path.insert(0, str(SCRIPTS))
from task_state import atomic_text, downloaded_ids, EVENT_PREFIX, load_state, find_video
from process_tree import ProcessTree
from command_runner import run_command

# One shared lane for manual downloads, scans and the automatic pipeline.
OPERATION_LOCK = threading.Lock()
AUTO_LOCK = threading.Lock()
AUTO = None
SETTINGS = None
NEXT_AUTO_REMOTE = 0.0

# 从桌面快捷方式启动时系统 PATH 里没有 node，这里补上
sys.path.insert(0, str(APP_DIR))
try:
    from envpath import augment_path
    _ADDED_PATHS = augment_path()
except Exception:
    _ADDED_PATHS = []

DEFAULT_BASE = Path.home() / "抖音视频下载"
DEFAULT_COOKIES = DEFAULT_BASE / "cookies.txt"
PORT = 8765

# Windows 上 lark-cli 是 .cmd，必须用完整路径调用
LARK_BIN = os.environ.get("LARK_CLI") or shutil.which("lark-cli") or "lark-cli"

LOCK = threading.Lock()
PROC = None
PROC_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WORKER_THREAD = None
QUITTING = False
LAST_JOB = WORK_DIR / "last_job.json"
JOB = {
    "state": "idle",          # idle | running | done | error | stopped
    "total": 0,
    "current": 0,
    "ok": 0,
    "fail": 0,
    "logs": deque(maxlen=4000),
    "started_at": None,
    "ended_at": None,
    "output_dir": "",
    "phase": "",              # 下载中 / 转写中
    "items": [],
    "request_items": [],
    "base_dir": "",
    "cookies": "",
    "transcribe": True,
}
LOG_SEQ = {"n": 0}

URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。、；：！？）】\"']+")
VIDEO_ID_RE = re.compile(r"/video/(\d{6,})")
SHORT_RE = re.compile(r"v\.douyin\.com/([A-Za-z0-9]+)")
TITLE_RE = re.compile(r"【(.+?)】")
def persist_job():
    atomic_text(LAST_JOB, json.dumps({k: v for k, v in JOB.items() if k != "logs"},
                                   ensure_ascii=False, indent=2))


def restore_job():
    if not LAST_JOB.exists():
        return
    try:
        saved = json.loads(LAST_JOB.read_text(encoding="utf-8"))
        JOB.update({k: v for k, v in saved.items() if k in JOB and k != "logs"})
        if JOB["state"] in ("running", "stopping"):
            JOB.update(state="stopped", phase="上次任务中断，可重试未完成项", ended_at=time.time())
    except Exception as exc:
        JOB.update(state="error", phase=f"任务记录读取失败: {exc}")


restore_job()


def log(line: str):
    line = line.rstrip("\r\n")
    if not line.strip():
        return
    with LOCK:
        LOG_SEQ["n"] += 1
        JOB["logs"].append({"n": LOG_SEQ["n"], "t": time.strftime("%H:%M:%S"), "m": line})


def process_event(line):
    event = json.loads(line[len(EVENT_PREFIX):])
    with LOCK:
        if event.get("phase") == "summary":
            return
        row = next((x for x in JOB["items"] if x["video_id"] == str(event.get("video_id"))), None)
        if row is None:
            return
        phase = event.get("phase")
        if phase not in ("download", "transcribe"):
            return
        row[phase] = event["status"]
        if event.get("directory"):
            row["directory"] = event["directory"]
        row["error"] = event.get("error", "")
        if "terminal" in event:
            row["terminal"] = bool(event["terminal"])
        if 'rate_limited' in event:
            row['rate_limited'] = bool(event['rate_limited'])
        row["status"] = ("error" if "error" in (row["download"], row["transcribe"]) else
                         "done" if row["download"] == "done" and
                         (not JOB["transcribe"] or row["transcribe"] == "done") else "pending")
        JOB["ok"] = sum(x["status"] == "done" for x in JOB["items"])
        JOB["fail"] = sum(x["status"] == "error" for x in JOB["items"])
        JOB["current"] = JOB["ok"] + JOB["fail"]
        if JOB["state"] != "stopping":
            JOB["phase"] = "下载中" if phase == "download" else "转写中"
        persist_job()
    if AUTO and AUTO.thread and AUTO.thread.is_alive():
        AUTO._update(stage='download' if phase == 'download' else 'transcribe')
        with AUTO.lock:
            auto_row = next((x for x in AUTO.state['items'] if x['video_id'] == str(event.get('video_id'))), None)
            if auto_row:
                AUTO._row_update(auto_row, phase=phase)


def resolve_short(url: str) -> str:
    """短链跟随重定向，拿最终 URL"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl() or url
    except Exception:
        return url


def parse_input(text: str):
    """把粘贴的文本切成视频条目"""
    items, seen = [], set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        urls = URL_RE.findall(line)
        if not urls:
            continue
        url = urls[0].rstrip(".,;)")
        vid = None
        m = VIDEO_ID_RE.search(url)
        if m:
            vid = m.group(1)
        elif SHORT_RE.search(url):
            final = resolve_short(url)
            m2 = VIDEO_ID_RE.search(final)
            if m2:
                vid = m2.group(1)
                url = final
            else:
                vid = SHORT_RE.search(url).group(1)
        else:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        tm = TITLE_RE.search(line)
        title = tm.group(1).strip() if tm else ""
        if not title:
            title = line.replace(url, "").strip(" ，,。.、")
        items.append({"video_id": vid, "url": url, "title": title[:120], "author": ""})
    return items


def local_downloaded_ids(base: Path) -> set:
    return downloaded_ids(base)


def deps_status(config=None):
    cfg = config or get_settings().get()
    base = Path(cfg.get("base_dir") or DEFAULT_BASE)
    cookies = Path(cfg.get("cookies") or DEFAULT_COOKIES)

    def run(cmd):
        try:
            r = run_command(cmd, capture_output=True, text=True, timeout=25,
                               encoding="utf-8", errors="replace")
            first = (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else ""
            return first if r.returncode == 0 else f"不可用: {first}"
        except Exception as e:
            return f"不可用: {e}"

    ytdlp = run([sys.executable, "-m", "yt_dlp", "--version"])
    node = run(["node", "--version"])
    lark = run([LARK_BIN, "--version"])

    user = ""
    try:
        r = run_command([LARK_BIN, "auth", "status", "--json"], capture_output=True,
                           text=True, timeout=25, encoding="utf-8", errors="replace")
        d = json.loads(r.stdout)
        u = d.get("identities", {}).get("user", {})
        user = u.get("userName", "") if u.get("available") else ""
    except Exception:
        user = ""

    return {
        "ytdlp": ytdlp,
        "node": node,
        "lark": lark,
        "lark_user": user,
        "cookies_exists": cookies.exists(),
        "cookies_path": str(cookies),
        "default_base": str(base),
    }


def worker(items, base_dir: Path, cookies: Path, transcribe: bool, rebuild_minutes=False, owns_lane=False):
    global PROC
    tree = None
    try:
        date_dir = base_dir / time.strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        list_file = date_dir / "video_list.json"
        atomic_text(list_file, json.dumps(items, ensure_ascii=False, indent=2))
        with LOCK:
            JOB["output_dir"] = str(date_dir)
            persist_job()
        log(f"📋 视频列表已生成: {list_file}")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        env["SKILL_SKIP_TRANSCRIBE"] = "0" if transcribe else "1"
        env["DY_REBUILD_FAILED_MINUTES"] = "1" if rebuild_minutes else "0"
        cmd = [sys.executable, str(SCRIPTS / "download_with_cookies.py"),
               str(list_file), str(cookies), str(base_dir)]
        with PROC_LOCK:
            if not STOP_EVENT.is_set():
                tree = ProcessTree(cmd, env=env, cwd=SKILL_DIR)
                PROC = tree
        if tree is not None:
            for line in tree.proc.stdout:
                if line.startswith(EVENT_PREFIX):
                    process_event(line)
                else:
                    log(line)
            tree.proc.wait()
        with LOCK:
            JOB["ended_at"] = time.time()
            if STOP_EVENT.is_set():
                JOB["state"] = "stopped"
            else:
                for row in JOB["items"]:
                    if row["status"] == "pending":
                        row.update(status="error", error="任务异常结束，当前阶段未完成")
                JOB["ok"] = sum(x["status"] == "done" for x in JOB["items"])
                JOB["fail"] = sum(x["status"] == "error" for x in JOB["items"])
                JOB["state"] = ("done" if tree and tree.proc.returncode == 0 and not JOB["fail"]
                                else "partial" if JOB["ok"] else "error")
            JOB["current"] = JOB["ok"] + JOB["fail"]
            JOB["phase"] = ""
            persist_job()
        log("⏹ 任务结束")
    except Exception as exc:
        with LOCK:
            JOB.update(state="stopped" if STOP_EVENT.is_set() else "error", ended_at=time.time(), phase="")
            for row in JOB["items"]:
                if row["status"] != "done":
                    row.update(status="error", error=str(exc))
            JOB["fail"] = sum(x["status"] == "error" for x in JOB["items"])
            persist_job()
        log(f"❌ 任务异常: {exc}")
    finally:
        with PROC_LOCK:
            if tree is not None:
                if tree.proc.poll() is None:
                    tree.terminate()
                tree.close()
                if tree.proc.stdout:
                    tree.proc.stdout.close()
            PROC = None
        if owns_lane:
            OPERATION_LOCK.release()


def start_job(items, base, cookies, transcribe, rebuild_minutes=False, autopilot=False):
    global WORKER_THREAD
    # Reservation and thread creation are atomic against concurrent /start calls.
    owns_lane = not autopilot
    if owns_lane and not OPERATION_LOCK.acquire(blocking=False):
        return False
    with LOCK:
        if QUITTING or (WORKER_THREAD and WORKER_THREAD.is_alive()) or JOB["state"] in ("running", "stopping"):
            if owns_lane:
                OPERATION_LOCK.release()
            return False
        STOP_EVENT.clear()
        JOB["logs"].clear()
        LOG_SEQ["n"] = 0
        JOB.update(state="running", total=len(items), current=0, ok=0, fail=0,
                   started_at=time.time(), ended_at=None, output_dir="", phase="准备中",
                   request_items=items, base_dir=str(base), cookies=str(cookies), transcribe=transcribe,
                   items=[dict(video_id=str(x["video_id"]), title=x.get("title", ""),
                               download="pending", transcribe="pending", status="pending", error="") for x in items])
        persist_job()
        WORKER_THREAD = threading.Thread(target=worker, args=(items, base, cookies, transcribe, rebuild_minutes, owns_lane), daemon=True)
        WORKER_THREAD.start()
    return True


def stop_job():
    with LOCK:
        active = JOB["state"] in ("running", "stopping")
        if not active:
            return False
        STOP_EVENT.set()
        JOB.update(state="stopping", phase="正在停止并清理任务…")
        persist_job()
    with PROC_LOCK:
        if PROC:
            PROC.terminate()
    return True


def shutdown_server(server):
    if AUTO:
        AUTO.close()
    stop_job()
    if WORKER_THREAD:
        WORKER_THREAD.join(timeout=20)
        if WORKER_THREAD.is_alive():
            raise RuntimeError("任务仍在清理，服务未退出")
    server.shutdown()


def get_settings():
    global SETTINGS
    from autopilot import Settings
    with AUTO_LOCK:
        if SETTINGS is None:
            SETTINGS = Settings(WORK_DIR / 'config.json')
        return SETTINGS


def knowledge_base(config=None):
    from knowledge import KnowledgeBase
    config = config or get_settings().get()
    return KnowledgeBase(config['vault_path'], config['kb_subdir'], WORK_DIR / 'knowledge.db')


ENRICHER = None
# 推理型模型的 completion token 里有 reasoning，探测用的上限必须留足余量；
# 上限太小会出现 choices 为空但没有报错的响应。
PROBE_MAX_TOKENS = 512


def get_enricher():
    global ENRICHER
    from enrich import Enricher
    with AUTO_LOCK:
        if ENRICHER is None:
            ENRICHER = Enricher(WORK_DIR)
        return ENRICHER


def ai_enrich(config, item, transcript, topic_names):
    return get_enricher().enrich(config, item, transcript, topic_names)


def knowledge_answer(config, question):
    """知识库问答：让 AI 读相关笔记后回答。"""
    import chat
    return chat.answer(get_enricher(), config or get_settings().get(), knowledge_base(config), question)


def ai_probe(config, key_override=None):
    """A real minimal chat call, so the button proves the pipeline's own path."""
    key = str(key_override or config.get('ai_api_key') or '').strip()
    if not key:
        raise ValueError('尚未设置 AI 密钥，请先填写并保存')
    payload = {'model': config['ai_model'],
               'messages': [{'role': 'user', 'content': '只返回 JSON：{"ok": true}'}],
               'temperature': 0, 'max_tokens': PROBE_MAX_TOKENS,
               'response_format': {'type': 'json_object'}}
    request = urllib.request.Request(
        str(config['ai_base_url']).rstrip('/') + '/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError('AI 接口拒绝请求，请检查地址、模型与密钥（HTTP %s）' % exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError('AI 接口连接失败，请检查网络与接口地址')
    if not isinstance(body, dict):
        raise RuntimeError('AI 接口返回格式不符合预期，请确认地址指向 OpenAI 兼容服务')
    if not body.get('choices'):
        usage = body.get('usage')
        details = usage.get('completion_tokens_details') if isinstance(usage, dict) else None
        reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
        if reasoning:
            raise RuntimeError('AI 接口连通，但模型只输出了思考过程没有结果；'
                               '如果反复出现，请换用非推理型模型')
        raise RuntimeError('AI 接口返回格式不符合预期，请确认地址指向 OpenAI 兼容服务')
    model = str(body.get('model') or config['ai_model'])
    return {'ok': True, 'model': model, 'message': '连通正常，模型 ' + model}


def enrich_missing(config, video_id=None, limit=3):
    """Backfill AI fields for already-published notes; one entry per failure reason."""
    from autopilot import Autopilot
    kb = knowledge_base(config)
    auto = get_autopilot()
    enricher = get_enricher()
    with auto.lock:
        rows = [dict(x) for x in auto.state['items'] if x['status'] == 'done']
    if video_id:
        rows = [x for x in rows if str(x.get('video_id')) == str(video_id)]
    items, failed, names = [], [], None
    for row in rows:
        if len(items) + len(failed) >= limit:
            break
        try:
            path = row.get('note_path') or ''
            if not path:
                raise ValueError('缺少笔记路径')
            markdown = kb.note(path)['markdown']
            if 'ai_summary:' in markdown:
                continue
            if re.search(r'^status:[ \t]*(已定稿|冻结)', markdown, re.M):
                failed.append({'video_id': row['video_id'], 'error': '已定稿冻结，跳过 AI 加工'})
                continue
            directory = Path(row.get('directory') or '')
            if not directory.is_dir():
                raise ValueError('找不到下载目录，无法读取转写')
            transcript = Autopilot._transcript(directory)
            if not transcript:
                raise ValueError('转写正文为空')
            if names is None:
                names = kb.topic_names()
            ai = enricher.enrich(config, row, transcript, names)
            note = kb.publish(row, directory, ai)
            with auto.lock:
                target = next((x for x in auto.state['items']
                               if str(x['video_id']) == str(row['video_id'])), None)
                if target is not None:
                    target.update(ai=True, warning='', note_path=note['path'], phase='published')
                    auto._save()
            items.append({'video_id': row['video_id'], 'path': note['path'],
                          'status': str(note.get('status') or '已加工')})
        except Exception as exc:
            failed.append({'video_id': row.get('video_id', ''), 'error': str(exc)})
    return {'items': items, 'failed': failed}


def send_notification(config, message):
    receive_id = config['notify_receive_id'].strip()
    kind = config['notify_receive_id_type']
    if not receive_id or (kind == 'open_id' and not receive_id.startswith('ou_')) or (kind == 'chat_id' and not receive_id.startswith('oc_')):
        raise ValueError('请填写有效的飞书 open_id 或 chat_id')
    errors = []
    for identity in ('bot', 'user'):
        args = [LARK_BIN, 'im', '+messages-send', '--as', identity,
                '--chat-id' if kind == 'chat_id' else '--user-id', receive_id,
                '--text', message, '--json']
        result = run_command(args, capture_output=True, text=True, timeout=30,
                             encoding='utf-8', errors='replace')
        try:
            body = json.loads(result.stdout or '')
        except ValueError:
            body = None
        if body and body.get('ok') is not False and body.get('code', 0) == 0 and not body.get('error'):
            return
        if body and isinstance(body.get('error'), dict):
            errors.append(str(body['error'].get('subtype') or body['error'].get('type') or identity))
        else:
            errors.append(identity)
    detail = '、'.join(errors)
    if 'missing_scope' in detail or 'authorization' in detail:
        raise RuntimeError('飞书缺少发消息权限：请让应用具备 im:message 权限，或重新授权用户身份后重试')
    raise RuntimeError('飞书通知发送失败，请检查登录、发送权限与接收人')


def auto_execute(item, config, stop):
    global NEXT_AUTO_REMOTE
    from task_state import local_index
    from batch_transcribe import _needs_remote, INTERVAL_BETWEEN_VIDEOS
    existing = item.get('directory') or local_index(Path(config['base_dir'])).get(str(item['video_id']))
    remote = not existing or _needs_remote(Path(existing))
    if remote and NEXT_AUTO_REMOTE > time.monotonic():
        if AUTO:
            AUTO._update(phase='等待飞书转写间隔', stage='transcribe')
        if stop.wait(max(0, NEXT_AUTO_REMOTE - time.monotonic())):
            return {'status': 'pending'}
    if stop.is_set():
        return {'status': 'pending'}
    if not start_job([item], Path(config['base_dir']), Path(config['cookies']), True, autopilot=True):
        raise RuntimeError('下载工作线程仍在运行')
    while WORKER_THREAD and WORKER_THREAD.is_alive():
        if stop.wait(0.25):
            stop_job()
        WORKER_THREAD.join(timeout=0.25)
    if remote:
        NEXT_AUTO_REMOTE = time.monotonic() + INTERVAL_BETWEEN_VIDEOS
    with LOCK:
        return dict(JOB['items'][0])


def get_autopilot():
    global AUTO
    config = get_settings()
    from autopilot import Autopilot
    def scan(cfg, manual, stop):
        from scan_favorites import scan
        # A partial scan still tells us about real new videos; the pipeline reports
        # "收藏夹没有扫描完" instead of throwing the whole run away.
        return scan(Path(cfg['cookies']), favorite=cfg['favorite'], headless=False,
                    log=log, on_manual=manual, stop_event=stop, allow_partial=True)
    with AUTO_LOCK:
        if AUTO is None:
            AUTO = Autopilot(WORK_DIR, config, scan, auto_execute, knowledge_base,
                             send_notification,
                             reserve=lambda: not QUITTING and OPERATION_LOCK.acquire(blocking=False),
                             release=OPERATION_LOCK.release, stop_current=stop_job,
                             enrich=ai_enrich)
        return AUTO


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > 2 * 1024 * 1024:
            self.close_connection = True
            raise ValueError('请求体过大或长度无效')
        if not n:
            return {}
        self.connection.settimeout(10)
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        finally:
            self.connection.settimeout(None)

    def _local_request(self, mutation=False):
        host = urllib.parse.urlsplit('//' + self.headers.get('Host', '')).hostname
        if host not in ('127.0.0.1', 'localhost', '::1'):
            self._json({'ok': False, 'error': '仅允许本机控制台访问'}, 403)
            return False
        origin = self.headers.get('Origin')
        if mutation and origin and origin != 'http://' + self.headers.get('Host', ''):
            self._json({'ok': False, 'error': '拒绝跨站操作'}, 403)
            return False
        if mutation and self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self._json({'ok': False, 'error': '拒绝跨站操作'}, 403)
            return False
        return True

    def do_GET(self):
        if not self._local_request():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/ping':
            # Cheap liveness probe for the watchdog; never touches the pipeline.
            return self._json({'ok': True, 'pid': os.getpid(),
                               'state': (AUTO.state['state'] if AUTO else 'starting')})
        if path in ('/api/settings', '/api/autopilot', '/api/queue', '/api/kb/tree', '/api/kb/search', '/api/kb/note',
                    '/api/kb/topics', '/api/kb/topic', '/api/kb/series', '/api/kb/series/note', '/api/ai/usage'):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if path == '/api/settings':
                    return self._json(get_settings().get(public=True))
                if path == '/api/autopilot':
                    snap = get_autopilot().snapshot()
                    snap.pop('items', None)
                    return self._json(snap)
                if path == '/api/queue':
                    return self._json({'items': get_autopilot().snapshot()['items']})
                if path == '/api/ai/usage':
                    usage = dict(get_enricher().usage())
                    config = get_settings().get()
                    usage['limit'] = config.get('ai_daily_limit', usage.get('limit', 200))
                    usage['ai_key_set'] = bool(str(config.get('ai_api_key') or '').strip())
                    return self._json(usage)
                kb = knowledge_base()
                if path == '/api/kb/tree':
                    out = kb.tree()
                    out['topics'] = kb.topics()['items']
                    out['series'] = kb.series()['items']
                    return self._json(out)
                if path == '/api/kb/topics':
                    return self._json(kb.topics())
                if path == '/api/kb/series':
                    return self._json(kb.series())
                if path == '/api/kb/topic':
                    return self._json(kb.topic_note((q.get('name') or [''])[0]))
                if path == '/api/kb/series/note':
                    return self._json(kb.series_note((q.get('name') or [''])[0]))
                if path == '/api/kb/search':
                    return self._json(kb.search((q.get('q') or [''])[0]))
                return self._json(kb.note((q.get('path') or [''])[0]))
            except (ValueError, FileNotFoundError) as exc:
                return self._json({'ok': False, 'error': str(exc)}, 400)
            except Exception:
                return self._json({'ok': False, 'error': '知识库或配置读取失败，请检查路径、文件权限与配置格式'}, 500)
        if path in ("/", "/index.html"):
            f = APP_DIR / "index.html"
            return self._send(200, f.read_bytes(), MIME[".html"])

        if path == "/api/status":
            return self._json(deps_status(get_settings().get()))

        if path == "/api/job":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            since = int((q.get("since") or ["0"])[0])
            with LOCK:
                logs = [x for x in JOB["logs"] if x["n"] > since]
                snap = {k: v for k, v in JOB.items() if k != "logs"}
                snap["logs"] = logs
                snap["last_n"] = LOG_SEQ["n"]
                if snap["started_at"] and snap["state"] in ("running", "stopping"):
                    snap["elapsed"] = int(time.time() - snap["started_at"])
                elif snap["started_at"] and snap["ended_at"]:
                    snap["elapsed"] = int(snap["ended_at"] - snap["started_at"])
                else:
                    snap["elapsed"] = 0
            return self._json(snap)

        if path == "/api/usage":
            try:
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                import usage
                return self._json(usage.summary())
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path == "/api/files":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            d = Path((q.get("dir") or [""])[0] or JOB["output_dir"] or DEFAULT_BASE)
            today = d / time.strftime("%Y-%m-%d")
            if today.is_dir():
                d = today
            out = []
            if q.get("job") == ["1"]:
                with LOCK:
                    directories = list(dict.fromkeys(row["directory"] for row in JOB["items"] if row.get("directory")))
                subs = [Path(directory) for directory in directories]
            else:
                subs = sorted(d.iterdir(), key=lambda p: p.name)[:200] if d.is_dir() else []
            if subs:
                for sub in subs:
                    if not sub.is_dir():
                        continue
                    vf = find_video(sub)
                    mp4 = [vf] if vf else []
                    checkpoint = load_state(sub)
                    out.append({
                        "name": sub.name,
                        "path": str(sub),
                        "size": round(sum(f.stat().st_size for f in mp4) / 1024 / 1024, 1) if mp4 else 0,
                        "video": mp4[0].name if mp4 else "",
                        "transcript": (sub / "语音转文字.txt").exists(),
                        "copy": checkpoint.get("copy_status") == "done" and (sub / "视频文案.txt").exists(),
                        "error": checkpoint.get("transcript_error") or checkpoint.get("download_error") or "",
                    })
            return self._json({"dir": str(d), "items": out})

        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # Consume a bounded body before a rejection, avoiding HTTP/1.1 unread-body
        # desynchronization and Windows resets that can discard the 403 response.
        try:
            data = self._body()
        except (ValueError, OSError):
            self.close_connection = True
            return self._json({'ok': False, 'error': '请求必须是有效 JSON，且不超过2MB'}, 400)
        if not self._local_request(mutation=True):
            return
        path = urllib.parse.urlparse(self.path).path
        if not isinstance(data, dict):
            return self._json({'ok': False, 'error': '请求必须是 JSON 对象'}, 400)
        if path in ('/api/settings', '/api/autopilot/run', '/api/autopilot/pause', '/api/autopilot/resume',
                    '/api/queue/retry', '/api/kb/reindex', '/api/notify/test', '/api/kb/refresh',
                    '/api/kb/topic/merge', '/api/kb/enrich', '/api/ai/test', '/api/chat'):
            try:
                if path == '/api/settings':
                    if AUTO and AUTO.thread and AUTO.thread.is_alive():
                        raise ValueError('同步运行中，请暂停后再更改设置')
                    changes = dict(data)
                    if not str(changes.get('ai_api_key') or '').strip():
                        changes.pop('ai_api_key', None)
                    return self._json(dict(get_settings().update(changes), ok=True))
                if path == '/api/kb/reindex':
                    return self._json(dict(knowledge_base().reindex(), ok=True))
                if path == '/api/kb/refresh':
                    return self._json(dict(knowledge_base().refresh(), ok=True))
                if path == '/api/kb/topic/merge':
                    source = str(data.get('source') or '').strip()
                    target = str(data.get('target') or '').strip()
                    if not source or not target:
                        raise ValueError('请选择要合并的主题和目标主题')
                    return self._json(dict(knowledge_base().merge_topics(source, target), ok=True))
                if path == '/api/kb/enrich':
                    return self._json(dict(enrich_missing(get_settings().get(), data.get('video_id')), ok=True))
                if path == '/api/ai/test':
                    return self._json(ai_probe(get_settings().get(), data.get('ai_api_key')))
                if path == '/api/chat':
                    return self._json(dict(knowledge_answer(get_settings().get(), data.get('question')), ok=True))
                if path == '/api/notify/test':
                    send_notification(get_settings().get(), '归藏通知测试：连接正常，之后将在需要人工验证或任务失败时通知你。')
                    return self._json({'ok': True})
                auto = get_autopilot()
                if path == '/api/autopilot/pause':
                    return self._json(auto.pause())
                return self._json(auto.start(retry_id=data.get('video_id'), retry_only=path == '/api/queue/retry'))
            except ValueError as exc:
                return self._json({'ok': False, 'error': str(exc)}, 400)
            except RuntimeError as exc:
                if path in ('/api/notify/test', '/api/ai/test', '/api/chat'):
                    return self._json({'ok': False, 'error': str(exc)}, 502)
                raise
            except Exception:
                return self._json({'ok': False, 'error': '操作失败，请检查配置、飞书登录及文件权限'}, 500)

        if path == "/api/parse":
            items = parse_input(data.get("text", ""))
            return self._json({"items": items, "count": len(items)})

        if path == "/api/scan":
            cookies = Path(data.get("cookies") or DEFAULT_COOKIES)
            base = Path(data.get("base_dir") or DEFAULT_BASE)
            if not cookies.exists():
                return self._json({"ok": False, "error": f"cookies 文件不存在: {cookies}"}, 400)
            if not OPERATION_LOCK.acquire(blocking=False):
                return self._json({'ok': False, 'error': '已有任务在运行'}, 409)
            try:
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                import scan_favorites
                log("🔍 开始检查收藏夹…")
                found = scan_favorites.scan(cookies, headless=False,
                                            log=lambda m: log("🔍 " + m))
                have = local_downloaded_ids(base)
                new = [i for i in found if i["video_id"] not in have]
                log(f"🔍 收藏夹 {len(found)} 条 / 本地已有 {len(have)} 条 / 新增 {len(new)} 条")
                return self._json({"ok": True, "total": len(found),
                                   "downloaded": len(have), "new": new,
                                   "new_count": len(new)})
            except Exception as e:
                log(f"❌ 检查收藏夹失败: {e}")
                return self._json({"ok": False, "error": str(e)}, 500)
            finally:
                OPERATION_LOCK.release()

        if path in ("/api/start", "/api/retry"):
            if path == "/api/retry":
                with LOCK:
                    failed = {x["video_id"] for x in JOB["items"] if x["status"] != "done"}
                    items = [dict(x) for x in JOB["request_items"] if str(x["video_id"]) in failed]
                    base = Path(JOB["base_dir"] or DEFAULT_BASE)
                    cookies = Path(JOB["cookies"] or DEFAULT_COOKIES)
                    transcribe = JOB["transcribe"]
                    needs_rebuild = any(x.get("terminal") for x in JOB["items"] if x["video_id"] in failed)
                if needs_rebuild and not data.get("rebuild_minutes"):
                    return self._json({"ok": False, "needs_confirmation": True,
                                       "error": "部分妙记已失败或创建结果未知。请先在飞书核对；重新创建可能重复消耗转写额度，确认继续？"}, 409)
            else:
                items = data.get("items") or []
                base = Path(data.get("base_dir") or DEFAULT_BASE)
                cookies = Path(data.get("cookies") or DEFAULT_COOKIES)
                transcribe = bool(data.get("transcribe", True))
            if not isinstance(items, list) or not items:
                return self._json({"ok": False, "error": "没有可重试或下载的视频"}, 400)
            checked = {}
            for item in items:
                if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(item.get("video_id", ""))):
                    return self._json({"ok": False, "error": "视频 ID 无效"}, 400)
                url = item.get("url", "")
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ("http", "https") or not (parsed.hostname == "douyin.com" or (parsed.hostname or "").endswith(".douyin.com")):
                    return self._json({"ok": False, "error": "请输入有效抖音链接"}, 400)
                checked[str(item["video_id"])] = item
            if not start_job(list(checked.values()), base, cookies, transcribe,
                             rebuild_minutes=path == "/api/retry" and bool(data.get("rebuild_minutes"))):
                return self._json({"ok": False, "error": "已有任务在运行或停止中"}, 409)
            return self._json({"ok": True})

        if path == "/api/stop":
            try:
                if stop_job():
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "当前没有运行中的任务"}, 400)
            except Exception as exc:
                return self._json({"ok": False, "error": f"停止任务失败: {exc}"}, 500)

        if path == "/api/open":
            target = Path(data.get("path") or JOB["output_dir"] or DEFAULT_BASE)
            # 有今天的下载目录就直接进当天，省得再点一层
            try:
                today = target / time.strftime("%Y-%m-%d")
                if today.is_dir() and any(today.iterdir()):
                    target = today
            except Exception:
                pass
            if not target.exists():
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return self._json({"ok": False, "error": f"目录打不开: {e}"}, 400)
            os.startfile(str(target))  # noqa: 仅 Windows
            return self._json({"ok": True, "path": str(target)})

        if path == "/api/reset":
            with LOCK:
                if JOB["state"] in ("running", "stopping") or (WORKER_THREAD and WORKER_THREAD.is_alive()):
                    return self._json({"ok": False, "error": "任务运行中不能重置"}, 409)
                JOB["logs"].clear()
                LOG_SEQ["n"] = 0
                JOB.update(state="idle", total=0, current=0, ok=0, fail=0,
                           started_at=None, ended_at=None, phase="", output_dir="", items=[], request_items=[])
                persist_job()
            return self._json({"ok": True})

        if path == "/api/quit":
            global QUITTING
            with LOCK:
                QUITTING = True
            watchdog_pause('用户退出服务')
            try:
                if AUTO:
                    AUTO.close()
                stop_job()
            except Exception as exc:
                with LOCK:
                    QUITTING = False
                return self._json({"ok": False, "error": f"任务清理失败，未退出: {exc}"}, 500)
            self._json({"ok": True})
            threading.Thread(target=shutdown_server, args=(self.server,), daemon=True).start()
            return

        return self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    global PORT
    args = sys.argv[1:]
    if "--port" in args:
        PORT = int(args[args.index("--port") + 1])

    # A manual or launcher start means the user wants the service running again.
    watchdog_resume()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"抖音下载控制台已启动: {url}")
    print("按 Ctrl+C 停止")
    if "--open" in args:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if '--no-scheduler' not in args:
        try:
            get_autopilot().start_scheduler()
        except Exception:
            log('自动调度未启动：请检查配置和检查点文件，手动下载仍可使用')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        if AUTO:
            AUTO.close()
        stop_job()
        if WORKER_THREAD:
            WORKER_THREAD.join(timeout=20)
        srv.server_close()


if __name__ == "__main__":
    main()
