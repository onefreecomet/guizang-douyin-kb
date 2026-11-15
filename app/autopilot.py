"""Durable single-lane P1 pipeline. No scheduler or network side effects on import."""
import copy
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_state import atomic_text, local_index, load_state

CST = timezone(timedelta(hours=8))
DEFAULTS = {
    'enabled': True, 'favorite': '有帮助', 'schedule_time': '04:00',
    'base_dir': str(Path.home() / '抖音视频下载'),
    'cookies': str(Path.home() / '抖音视频下载' / 'cookies.txt'),
    'vault_path': str(Path.home() / 'Obsidian' / 'Vault'),
    'kb_subdir': '90 - 抖音精选',
    'notify_enabled': True, 'notify_receive_id': '', 'notify_receive_id_type': 'open_id',
    'ai_base_url': 'https://amr-link.open-design.ai/v1', 'ai_model': 'deepseek-v4.1-flash',
    'ai_api_key': '', 'ai_enabled': True, 'ai_daily_limit': 200,
    'retry_attempts': 3, 'retry_wait': 120,
    'skin': 'paper',
}


_SCHEDULE = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])$')


def schedule_clock(value):
    """把 HH:MM 配置解析成 (hour, minute)；非法值已由 Settings.validate 拦截。"""
    match = _SCHEDULE.match(str(value or ''))
    if not match:
        return 4, 0
    return int(match.group(1)), int(match.group(2))


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = dict(DEFAULTS)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('配置文件格式无效')
            self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        self.validate(self.data)

    @staticmethod
    def validate(d):
        for key, label in (('favorite', '收藏夹'), ('kb_subdir', '知识库子目录')):
            value = d[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(label + '不能为空')
            if value != value.strip():
                raise ValueError(label + '不能有首尾空格')
            if len(value) > 50 or any(c in value for c in '\r\n\t'):
                raise ValueError(label + '最长 50 字，且不能包含换行或制表符')
        subdir = d['kb_subdir']
        if any(c in subdir for c in '\\/:*?"<>|') or subdir in ('.', '..'):
            raise ValueError('知识库子目录必须是 vault 下的一层目录名，不能包含路径分隔符')
        if not isinstance(d['schedule_time'], str) or not _SCHEDULE.match(d['schedule_time']):
            raise ValueError('每日运行时间必须是 24 小时制的 HH:MM')
        for k in ('enabled', 'notify_enabled', 'ai_enabled'):
            if type(d[k]) is not bool:
                raise ValueError(k + ' 必须是开关')
        for k in DEFAULTS:
            if k in ('enabled', 'notify_enabled', 'ai_enabled', 'ai_daily_limit',
                     'retry_attempts', 'retry_wait'):
                continue
            if not isinstance(d[k], str):
                raise ValueError(k + ' 必须是文本')
        if type(d['ai_daily_limit']) is not int or not 1 <= d['ai_daily_limit'] <= 2000:
            raise ValueError('AI 每日上限必须是 1 到 2000 的整数')
        if type(d['retry_attempts']) is not int or not 1 <= d['retry_attempts'] <= 5:
            raise ValueError('自动重试次数必须是 1 到 5 的整数')
        if type(d['retry_wait']) is not int or not 30 <= d['retry_wait'] <= 3600:
            raise ValueError('重试等待必须是 30 到 3600 秒的整数')
        for k in ('base_dir', 'cookies', 'vault_path'):
            if not d[k].strip() or not Path(d[k]).is_absolute():
                raise ValueError(k + ' 必须是绝对路径')
        if d['notify_receive_id_type'] not in ('open_id', 'chat_id'):
            raise ValueError('不支持的飞书接收标识类型')
        if len(d['notify_receive_id']) > 256 or any(c in d['notify_receive_id'] for c in '\r\n'):
            raise ValueError('飞书接收标识无效')
        if not d['ai_base_url'].startswith('https://'):
            raise ValueError('AI 接口地址必须使用 HTTPS')
        # 外观只允许内置的三套皮肤，避免前端拿到未知值后套不上样式。
        if d['skin'] not in ('paper', 'ink', 'warm'):
            raise ValueError('外观只能是纸白、夜色或暖纸')

    def get(self, public=False):
        with self.lock:
            d = dict(self.data)
        if public:
            d['ai_key_set'] = bool(d.pop('ai_api_key', ''))
        return d

    def update(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
            raise ValueError('包含未知配置项')
        with self.lock:
            d = dict(self.data)
            d.update(changes)
            self.validate(d)
            atomic_text(self.path, json.dumps(d, ensure_ascii=False, indent=2))
            self.data = d
        return self.get(public=True)


class PipelinePaused(RuntimeError):
    pass


class Autopilot:
    def __init__(self, work, settings, scan, execute, kb_factory, notify,
                 reserve=lambda: True, release=lambda: None, stop_current=lambda: None,
                 enrich=None):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.scan = scan
        self.execute = execute
        self.kb_factory = kb_factory
        self.notify_send = notify
        self.enrich = enrich
        self.reserve, self.release, self.stop_current = reserve, release, stop_current
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        self.scheduler = None
        self.db = self.work / 'pipeline.db'
        self.state = {'state': 'idle', 'phase': '', 'message': '', 'last_run': None,
                      'last_schedule_day': datetime.now(CST).date().isoformat(), 'notification_error': '', 'items': []}
        # 本次运行属于哪一天：定时触发时取调度日，手动触发时取今天。
        # manual() 用它记 manual_day —— 若直接用系统时间，把 tick 时间注入后
        # 就会算出「今天的日期不等于被测试的日期」这种自相矛盾的记录。
        self.current_day = datetime.now(CST).date().isoformat()
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            row = db.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()
            if row:
                self.state.update(json.loads(row[0]))
        self.interrupted = self.state['state'] in ('running', 'pausing', 'interrupted')
        if self.interrupted:
            self.state.update(state='interrupted', message='上次运行中断，将继续未完成阶段')
        for row in self.state['items']:
            if row['status'] == 'running':
                row['status'] = 'pending'
        self._save()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db, timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self):
        # Caller holds self.lock once threads are running.
        with self._connection() as db:
            db.execute('INSERT OR REPLACE INTO checkpoint(id,body) VALUES(1,?)',
                       (json.dumps(self.state, ensure_ascii=False),))

    def _update(self, **fields):
        with self.lock:
            self.state.update(fields)
            self._save()

    def snapshot(self):
        with self.lock:
            d = copy.deepcopy(self.state)
        cfg = self.settings.get()
        now = datetime.now(CST)
        hour, minute = schedule_clock(cfg['schedule_time'])
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= due:
            due += timedelta(days=1)
        d.update(enabled=cfg['enabled'], next_run=due.isoformat() if cfg['enabled'] else None)
        if cfg['notify_enabled'] and not cfg['notify_receive_id']:
            d['notification_error'] = '飞书通知尚未配置接收人，请在设置填写 open_id 或 chat_id'
        return d

    def notify(self, message):
        cfg = self.settings.get()
        if not cfg['notify_enabled']:
            return
        try:
            if not cfg['notify_receive_id']:
                raise ValueError('飞书通知尚未配置接收人，请到设置填写并发送测试通知')
            self.notify_send(cfg, message)
            self._update(notification_error='')
        except Exception as exc:
            # Do not store subprocess output, tokens or receiver identifiers.
            self._update(notification_error=str(exc) if isinstance(exc, ValueError) else '飞书通知发送失败，请检查登录与接收人设置')

    def manual(self, message):
        # Remember which day needed the human so the scheduler can retry on a
        # later day without re-triggering the captcha over and over today.
        self._update(state='needs_manual', phase='等待人工验证', message=message,
                     manual_day=self.current_day)
        self.notify('归藏需要人工处理：' + message + '。请打开电脑控制台查看，完成后继续同步。')

    def start(self, retry_id=None, retry_only=False, scheduled_day=None):
        with self.lock:
            if self.closed.is_set() or (self.thread and self.thread.is_alive()):
                raise ValueError('同步正在运行或等待人工验证')
            if not self.reserve():
                raise ValueError('已有下载或收藏夹检查正在运行')
            try:
                self.stop_event.clear()
                self.current_day = scheduled_day or datetime.now(CST).date().isoformat()
                if retry_only:
                    matches = [x for x in self.state['items'] if (not retry_id or x['video_id'] == retry_id) and x['status'] != 'done']
                    if not matches:
                        raise ValueError('没有可重试的任务')
                    if any(x.get('terminal') for x in matches):
                        raise ValueError('飞书已确认转写失败，需在下载页明确确认后重建妙记；自动队列不会重复付费创建')
                    for row in matches:
                        row.update(status='pending', error='') if row.get('status') != 'ignored' else None
                self.state.update(state='running', phase='准备同步', message='', last_run=datetime.now(CST).isoformat())
                if scheduled_day:
                    self.state['last_schedule_day'] = scheduled_day
                self._save()
                self.thread = threading.Thread(target=self._run, args=(retry_only, retry_id), daemon=True)
                self.thread.start()
            except Exception:
                self.release()
                raise
        return {'ok': True}

    def pause(self):
        with self.lock:
            self.stop_event.set()
            self.state.update(state='pausing' if self.thread and self.thread.is_alive() else 'paused', message='已请求暂停，完成清理后可继续')
            self._save()
            if self.thread and self.thread.is_alive():
                self.stop_current()
        return {'ok': True}

    def ignore(self, video_id=None, all_failed=False):
        with self.lock:
            changed = 0
            for row in self.state['items']:
                if all_failed:
                    if row.get('status') != 'error':
                        continue
                elif video_id is None or str(row.get('video_id')) != str(video_id):
                    continue
                row.update(status='ignored', phase='ignored', error=row.get('error') or '')
                changed += 1
            if changed:
                self._save()
            return {'ignored': changed}

    def clear_history(self):
        with self.lock:
            keep = [row for row in self.state['items'] if row.get('status') not in ('done', 'ignored')]
            removed = len(self.state['items']) - len(keep)
            self.state['items'] = keep
            if removed:
                self._save()
            return {'removed': removed}
    def _row_update(self, row, **fields):
        with self.lock:
            row.update(fields)
            self._save()

    def _run(self, retry_only, retry_id):
        try:
            cfg = self.settings.get()
            kb = self.kb_factory(cfg)
            incomplete = ''
            # Resume prior queue even when scan later fails. Import downloaded legacy
            # entries as pending so a valid MP4 alone never suppresses transcription.
            with self.lock:
                known = {x['video_id'] for x in self.state['items']}
            if not retry_only:
                legacy = local_index(Path(cfg['base_dir']))
                for vid, directory in legacy.items():
                    if vid not in known:
                        state = load_state(directory)
                        self._add(dict(video_id=vid, title=state.get('title') or directory.name,
                                       url=state.get('url') or 'https://www.douyin.com/video/' + vid,
                                       author='', directory=str(directory)))
                        known.add(vid)
            # Queue first: restarting an interrupted run must not require a successful
            # new scan before resuming paid work from its saved checkpoints.
            self._process(cfg, kb, retry_id if retry_only else None)
            if self.stop_event.is_set():
                return
            if not retry_only:
                self._update(phase='扫描收藏夹', stage='scan', state='running')
                found = self.scan(cfg, self.manual, self.stop_event)
                incomplete = str(getattr(found, 'incomplete', '') or '')
                if self.stop_event.is_set():
                    return
                self._update(state='running', phase='整理新增', message='')
                for item in found:
                    if not re.fullmatch(r'\d{6,}', str(item.get('video_id', ''))):
                        raise ValueError('收藏夹返回无效视频 ID')
                    if str(item['video_id']) not in known:
                        self._add(item)
                        known.add(str(item['video_id']))
                self._process(cfg, kb)
            if self.stop_event.is_set():
                return
            snap = self.snapshot()
            failed = sum(x['status'] == 'error' for x in snap['items'])
            notes = []
            if failed:
                notes.append('存在失败任务，可在队列重试')
            if incomplete:
                notes.append('收藏夹没有扫描完：' + incomplete + '（已先处理本次发现的条目，下次会继续扫）')
            self._update(state='partial' if (failed or incomplete) else 'done', phase='',
                         message='；'.join(notes) if notes else '本次同步已完成',
                         finished_at=datetime.now(CST).isoformat())
            self.notify('归藏同步完成：累计已入库 ' + str(sum(x['status'] == 'done' for x in snap['items'])) + ' 条，待处理失败 ' + str(failed) + ' 条。')
        except Exception as exc:
            if not self.stop_event.is_set():
                from scan_favorites import ManualRequired
                state = ('needs_manual' if isinstance(exc, ManualRequired) else
                         'paused' if isinstance(exc, PipelinePaused) else 'error')
                extra = ({'manual_day': self.current_day}
                         if state == 'needs_manual' else {})
                self._update(state=state, phase='', message=str(exc), **extra)
                self.notify('归藏同步暂停：' + str(exc))
        finally:
            with self.lock:
                if self.stop_event.is_set():
                    self._update(state='paused', phase='', stage='', message='已暂停，可从检查点继续')
                self.release()

    def _add(self, item):
        with self.lock:
            self.state['items'].append(dict(item, video_id=str(item['video_id']),
                status='pending', phase='discovered', error='', note_path='', attempt=0,
                warning='', ai=False))
            self._save()

    @staticmethod
    def _transcript(directory):
        """Read the best available transcript text; AI truncation happens later."""
        directory = Path(directory)
        for name in ('语音转文字.txt', '视频文案.txt'):
            path = directory / name
            if path.is_file():
                text = path.read_text(encoding='utf-8-sig').strip()
                if text:
                    return text
        return ''

    def _enrich(self, cfg, kb, row, directory):
        """Return (ai_result, warning). AI failure degrades the note, never the pipeline."""
        if self.enrich is None or not cfg.get('ai_enabled', True):
            return None, ''
        if not str(cfg.get('ai_api_key') or '').strip():
            return None, ''
        title = row.get('title') or row['video_id']
        self._update(stage='enrich', phase='AI 加工：' + title)
        try:
            transcript = self._transcript(directory)
            if not transcript:
                raise RuntimeError('转写正文为空')
            names = kb.topic_names() if kb is not None else []
            return self.enrich(cfg, dict(row), transcript, names), ''
        except Exception as exc:
            return None, 'AI 加工未完成：' + str(exc)

    def _attempt(self, cfg, kb, row):
        """One full attempt for a single row. Returns an error text, or None on success."""
        self._update(phase='处理：' + (row.get('title') or row['video_id']), stage='download')
        self._row_update(row, status='running', attempt=row.get('attempt', 0) + 1)
        try:
            result = self.execute(dict(row), cfg, self.stop_event)
            if result.get('directory'):
                self._row_update(row, directory=result['directory'])
            if self.stop_event.is_set():
                self._row_update(row, status='pending')
                return None
            if result.get('status') != 'done':
                self._row_update(row, terminal=bool(result.get('terminal')))
                if result.get('rate_limited'):
                    raise PipelinePaused('飞书限流，已暂停整条流水线；请稍后手动继续并重试失败项')
                raise RuntimeError(result.get('error') or '下载或转写未完成')
            self._row_update(row, phase='transcribed')
            # 笔记 frontmatter 记录实际扫描的收藏夹名，换过收藏夹也不会写错。
            if not str(row.get('favorite') or '').strip() and cfg.get('favorite'):
                row['favorite'] = cfg['favorite']
            ai, warning = self._enrich(cfg, kb, row, result['directory'])
            self._update(stage='publish', phase='写入笔记：' + (row.get('title') or row['video_id']))
            note = (kb.publish(row, Path(result['directory']), ai) if ai
                    else kb.publish(row, Path(result['directory'])))
            self._row_update(row, status='done', phase='published', error='',
                             note_path=note['path'], warning=warning, ai=bool(ai))
            return None
        except Exception as exc:
            self._row_update(row, status='pending' if self.stop_event.is_set() else 'error', error=str(exc))
            if isinstance(exc, PipelinePaused):
                raise
            return str(exc)

    def _process(self, cfg, kb, only=None):
        with self.lock:
            rows = [x for x in self.state['items'] if x['status'] == 'pending' and (not only or x['video_id'] == only)]
        attempts = max(1, int(cfg.get('retry_attempts', 1)))
        wait = max(0, int(cfg.get('retry_wait', 0)))
        for row in rows:
            for attempt in range(1, attempts + 1):
                if self.stop_event.is_set():
                    break
                error = self._attempt(cfg, kb, row)
                # A confirmed terminal failure (for example a paid minute that will not
                # be rebuilt automatically) must never be retried inside this run.
                if error is None or row.get('terminal') or self.stop_event.is_set():
                    break
                if attempt >= attempts:
                    break
                self._update(phase='等待重试：' + (row.get('title') or row['video_id']),
                             stage='download', message='第 ' + str(attempt) + ' 次失败，' + str(wait) + ' 秒后重试')
                if self.stop_event.wait(wait):
                    break
            if self.stop_event.is_set():
                # A row interrupted by the user must stay retryable, not look failed.
                if row.get('status') == 'error':
                    self._row_update(row, status='pending')
                break

    def tick(self, now=None):
        now = now or datetime.now(CST)
        cfg = self.settings.get()
        if not cfg['enabled']:
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            day = now.date().isoformat()
            hour, minute = schedule_clock(cfg['schedule_time'])
            # 到点后当天只跑一次；改过运行时间也不会在同一天重复触发。
            slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            due = now >= slot and self.state['last_schedule_day'] != day
            resume = self.interrupted
            if not (due or resume):
                return
            if self.state['state'] == 'paused':
                # A user pause is a deliberate stop: keep it until the user resumes.
                return
            if (self.state['state'] == 'needs_manual'
                    and self.state.get('manual_day') == day):
                # Waiting for a human is not a terminal state, but retrying it
                # again today would only re-trigger the captcha; the next
                # scheduled day picks it up automatically.
                return
        if due or resume:
            try:
                self.start(scheduled_day=day if due else None)
                self.interrupted = False
            except ValueError:
                pass  # Busy lane: retry next scheduler tick, do not consume the day.

    def start_scheduler(self):
        if self.scheduler and self.scheduler.is_alive():
            return
        def loop():
            while not self.closed.is_set():
                try:
                    self.tick()
                except Exception:
                    self._update(state='error', message='调度检查失败，请检查配置与检查点文件')
                self.closed.wait(20)
        self.scheduler = threading.Thread(target=loop, daemon=True)
        self.scheduler.start()

    def close(self):
        self.closed.set()
        active = self.thread and self.thread.is_alive()
        if active:
            self.pause()
        if self.thread:
            self.thread.join(timeout=25)
            if self.thread.is_alive():
                raise RuntimeError('自动同步尚未完成清理')
        if active:
            self._update(state='interrupted', message='服务关闭，下次启动从检查点继续')
        if self.scheduler:
            self.scheduler.join(timeout=2)
