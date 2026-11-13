"""Offline durable scheduler/queue behavior, with fake external services only."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'scripts')]
from autopilot import Autopilot, Settings, CST


class FakeKB:
    def __init__(self):
        self.published = []
        self.ai = []

    def topic_names(self):
        return ['神话']

    def publish(self, item, directory, ai=None):
        self.published.append(item['video_id'])
        self.ai.append(ai)
        return {'path': '90 - 抖音精选/素材/test.md'}


class PipelineBase(unittest.TestCase):
    """夹具与助手：一条流水线、一个假知识库、一条假收藏夹。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / 'config.json')
        self.settings.update({'base_dir': str(self.root/'downloads'), 'cookies': str(self.root/'cookies.txt'),
                              'vault_path': str(self.root/'vault'), 'notify_enabled': False,
                              'retry_attempts': 1})
        self.kb = FakeKB()
        self.executed = []
        self.scans = []
        self.items = [{'video_id': '12345678', 'url': 'https://www.douyin.com/video/12345678', 'title': '测试'}]
        self.lane = threading.Lock()
        self.auto = self.make()

    def make(self, execute=None, scan=None):
        def default_scan(*args):
            self.scans.append(1)
            return self.items
        def default_execute(item, cfg, stop):
            self.executed.append(item['video_id'])
            return {'status': 'done', 'directory': str(self.root/'download')}
        return Autopilot(self.root/'work', self.settings, scan or default_scan, execute or default_execute,
                         lambda cfg: self.kb, lambda cfg, message: None,
                         reserve=lambda: self.lane.acquire(blocking=False), release=self.lane.release)

    def join(self):
        self.auto.thread.join(timeout=10)
        self.assertFalse(self.auto.thread.is_alive())

    def tearDown(self):
        self.auto.close()
        self.temp.cleanup()

class PipelineTests(PipelineBase):
    """基础流水线行为。"""

    def test_idempotent_scan_and_durable_restart(self):
        self.auto.start(); self.join()
        self.assertEqual(self.auto.snapshot()['state'], 'done')
        self.auto.start(); self.join()
        self.assertEqual(self.executed, ['12345678'])
        self.assertEqual(self.kb.published, ['12345678'])
        self.auto.close()
        self.auto = self.make()
        self.auto.start(); self.join()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.auto.snapshot()['items'][0]['note_path'], '90 - 抖音精选/素材/test.md')

    def test_failed_queue_retries_only_selected(self):
        self.auto.execute = lambda *args: {'status': 'error', 'error': '离线失败'}
        self.auto.start(); self.join()
        self.assertEqual(self.auto.snapshot()['state'], 'partial')
        self.auto.execute = lambda *args: {'status': 'done', 'directory': str(self.root/'download')}
        self.auto.start(retry_id='12345678', retry_only=True); self.join()
        self.assertEqual(self.auto.snapshot()['items'][0]['status'], 'done')
        self.assertEqual(len(self.scans), 1)

    def test_terminal_transcription_never_rebuilt_automatically(self):
        self.auto.execute = lambda *args: {'status': 'error', 'error': '妙记失败', 'terminal': True}
        self.auto.start(); self.join()
        with self.assertRaisesRegex(ValueError, '不会重复付费'):
            self.auto.start(retry_only=True)
        self.assertFalse(self.lane.locked())

    def test_pause_stops_processing_and_resume_uses_pending(self):
        started = threading.Event()
        def execute(item, cfg, stop):
            started.set(); stop.wait(5)
            return {'status': 'pending'}
        self.auto.execute = execute
        self.auto.start()
        self.assertTrue(started.wait(3))
        self.auto.pause(); self.join()
        self.assertEqual(self.auto.snapshot()['state'], 'paused')
        self.assertEqual(self.auto.snapshot()['items'][0]['status'], 'pending')
        self.auto.execute = lambda *args: {'status': 'done', 'directory': str(self.root/'download')}
        self.auto.start(); self.join()
        self.assertEqual(self.auto.snapshot()['state'], 'done')

    def test_schedule_catchup_once_beijing_and_busy_not_consumed(self):
        self.auto._update(last_schedule_day='2026-11-12')
        self.auto.tick(datetime(2026, 11, 13, 3, 59, tzinfo=CST))
        self.assertIsNone(self.auto.thread)
        self.lane.acquire()
        self.auto.tick(datetime(2026, 11, 13, 4, 0, tzinfo=CST))
        self.assertEqual(self.auto.snapshot()['last_schedule_day'], '2026-11-12')
        self.lane.release()
        self.auto.tick(datetime(2026, 11, 13, 9, 0, tzinfo=CST)); self.join()
        first = self.auto.thread
        self.auto.tick(datetime(2026, 11, 13, 12, 0, tzinfo=CST))
        self.assertIs(self.auto.thread, first)
        self.auto.pause()
        self.auto.tick(datetime(2026, 11, 14, 4, 0, tzinfo=CST))
        self.assertIs(self.auto.thread, first)

    def test_import_legacy_already_downloaded_still_publishes(self):
        directory = self.root/'downloads'/'2026-11-12'/'01_旧视频'
        directory.mkdir(parents=True)
        (directory/'视频信息.txt').write_text('视频ID: 98765432', encoding='utf-8')
        self.auto.start(); self.join()
        self.assertEqual(set(self.executed), {'12345678', '98765432'})
        self.assertEqual(len(self.auto.snapshot()['items']), 2)

    def test_interrupted_run_recovered_before_new_scan(self):
        self.auto._add(self.items[0])
        self.auto._update(state='running')
        self.auto = self.make()
        self.assertTrue(self.auto.interrupted)
        self.auto.scan = lambda *args: (_ for _ in ()).throw(RuntimeError('扫描离线'))
        self.auto.start(); self.join()
        self.assertEqual(self.auto.snapshot()['items'][0]['status'], 'done')
        self.assertEqual(self.auto.snapshot()['state'], 'error')

    def test_notification_missing_recipient_visible(self):
        self.settings.update({'notify_enabled': True})
        self.auto.notify('测试')
        self.assertIn('尚未配置接收人', self.auto.snapshot()['notification_error'])

    def test_idle_pause_does_not_stop_manual_worker(self):
        called = []
        self.auto.stop_current = lambda: called.append(True)
        self.auto.pause()
        self.assertEqual(called, [])

    def test_rate_limit_pauses_whole_queue(self):
        self.items.append(dict(self.items[0], video_id='98765432'))
        called = []
        def limited(item, cfg, stop):
            called.append(item['video_id'])
            return {'status': 'error', 'rate_limited': True, 'error': 'RateLimitError'}
        self.auto.execute = limited
        self.auto.start(); self.join()
        self.assertEqual(called, ['12345678'])
        snapshot = self.auto.snapshot()
        self.assertEqual(snapshot['state'], 'paused')
        self.assertEqual(snapshot['items'][1]['status'], 'pending')
        self.assertIn('限流', snapshot['message'])

    def test_settings_hide_secret_and_reject_unsafe_changes(self):
        self.settings.update({'ai_api_key': 'fixture-secret'})
        self.assertTrue(self.settings.get(public=True)['ai_key_set'])
        self.assertNotIn('fixture-secret', json.dumps(self.settings.get(public=True)))
        for changes in ({'kb_subdir': '../escape'}, {'schedule_time': '08:00'}, {'enabled': 'false'}, {'unexpected': True}):
            with self.assertRaises(ValueError):
                self.settings.update(changes)
        self.assertEqual(Settings(self.settings.path).get()['ai_api_key'], 'fixture-secret')


class RetryTests(PipelineBase):
    """失败自动重试：退避、上限、终态不重试。"""

    def setUp(self):
        super().setUp()
        # Retry timing is injected so the tests never sleep.
        self.auto.stop_event.wait = lambda timeout=None: False

    def test_transient_failure_retries_within_same_run(self):
        self.settings.update({'retry_attempts': 3, 'retry_wait': 30})
        calls = []
        def flaky(item, cfg, stop):
            calls.append(item['video_id'])
            if len(calls) == 1:
                return {'status': 'error', 'error': '临时失败'}
            return {'status': 'done', 'directory': str(self.root/'download')}
        self.auto.execute = flaky
        self.auto.start(); self.join()
        self.assertEqual(len(calls), 2)
        snapshot = self.auto.snapshot()
        self.assertEqual(snapshot['state'], 'done')
        self.assertEqual(snapshot['items'][0]['status'], 'done')
        self.assertEqual(snapshot['items'][0]['attempt'], 2)

    def test_retry_gives_up_after_limit_and_leaves_manual_retry(self):
        self.settings.update({'retry_attempts': 2, 'retry_wait': 30})
        calls = []
        def always_fail(item, cfg, stop):
            calls.append(1)
            return {'status': 'error', 'error': '离线失败'}
        self.auto.execute = always_fail
        self.auto.start(); self.join()
        self.assertEqual(len(calls), 2)
        snapshot = self.auto.snapshot()
        self.assertEqual(snapshot['state'], 'partial')
        self.assertEqual(snapshot['items'][0]['status'], 'error')
        self.assertEqual(snapshot['items'][0]['attempt'], 2)

    def test_terminal_failure_is_never_retried(self):
        self.settings.update({'retry_attempts': 3, 'retry_wait': 30})
        calls = []
        def terminal(item, cfg, stop):
            calls.append(1)
            return {'status': 'error', 'error': '妙记失败', 'terminal': True}
        self.auto.execute = terminal
        self.auto.start(); self.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.auto.snapshot()['state'], 'partial')

    def test_pause_during_retry_wait_stops_promptly(self):
        self.settings.update({'retry_attempts': 3, 'retry_wait': 30})
        self.auto.execute = lambda *args: {'status': 'error', 'error': '离线失败'}
        def wait(timeout=None):
            self.auto.pause()
            return True
        self.auto.stop_event.wait = wait
        self.auto.start(); self.join()
        snapshot = self.auto.snapshot()
        self.assertEqual(snapshot['state'], 'paused')
        self.assertEqual(snapshot['items'][0]['status'], 'pending')

    def test_retry_settings_are_validated(self):
        for changes in ({'retry_attempts': 0}, {'retry_attempts': 6},
                        {'retry_wait': 29}, {'retry_wait': 3601}, {'retry_attempts': '3'}):
            with self.assertRaises(ValueError):
                self.settings.update(changes)

    def test_skin_setting_is_validated_and_persisted(self):
        self.assertEqual(self.settings.get()['skin'], 'paper')
        for name in ('paper', 'ink', 'warm'):
            self.settings.update({'skin': name})
            self.assertEqual(self.settings.get()['skin'], name)
        for bad in ('neon', 'Paper', '', 3, None):
            with self.assertRaises(ValueError):
                self.settings.update({'skin': bad})
        self.assertEqual(self.settings.get()['skin'], 'warm')
        reloaded = Settings(self.settings.path)
        self.assertEqual(reloaded.get()['skin'], 'warm')

    def test_partial_scan_marks_partial_but_still_publishes(self):
        class Incomplete(list):
            incomplete = '收藏夹扫描不完整：已达 max_scroll=25，列表尚未稳定'
        def partial_scan(*args):
            self.scans.append(1)
            return Incomplete(self.items)
        self.auto = self.make(scan=partial_scan)
        self.auto.start(); self.join()
        snapshot = self.auto.snapshot()
        self.assertEqual(snapshot['state'], 'partial')
        self.assertIn('没有扫描完', snapshot['message'])
        self.assertEqual(snapshot['items'][0]['status'], 'done')
        self.assertEqual(self.kb.published, ['12345678'])

    def test_full_scan_keeps_done_state(self):
        self.auto.start(); self.join()
        self.assertEqual(self.auto.snapshot()['state'], 'done')
        self.assertEqual(self.auto.snapshot()['message'], '本次同步已完成')


class AiStageTests(unittest.TestCase):
    """AI 加工阶段：成功写入、失败降级、关闭或没密钥时完全跳过。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / 'config.json')
        self.settings.update({'base_dir': str(self.root/'downloads'), 'cookies': str(self.root/'cookies.txt'),
                              'vault_path': str(self.root/'vault'), 'notify_enabled': False,
                              'ai_api_key': 'fixture-secret'})
        self.download = self.root / 'download'
        self.download.mkdir()
        (self.download / '语音转文字.txt').write_text('Speaker 1 00:00:01.000\n正文内容。', encoding='utf-8')
        self.kb = FakeKB()
        self.calls = []
        self.lane = threading.Lock()
        self.items = [{'video_id': '12345678', 'url': 'https://www.douyin.com/video/12345678', 'title': '测试'}]
        self.auto = self.make()

    def make(self, enrich=None, work=None):
        def scan(*args):
            return self.items
        def execute(item, cfg, stop):
            return {'status': 'done', 'directory': str(self.download)}
        def fake_enrich(cfg, item, transcript, names):
            self.calls.append((item['video_id'], transcript, list(names)))
            if enrich is not None:
                raise enrich
            return {'summary': '摘要', 'key_points': ['要点一'],
                    'topics': [{'name': '神话', 'parent': ''}], 'series': None,
                    'keywords': ['关键词'], 'model': 'test-model'}
        return Autopilot(work or self.root/'work', self.settings, scan, execute, lambda cfg: self.kb,
                         lambda cfg, message: None,
                         reserve=lambda: self.lane.acquire(blocking=False), release=self.lane.release,
                         enrich=fake_enrich)

    def run_once(self):
        self.auto.start(); self.auto.thread.join(timeout=10)
        self.assertFalse(self.auto.thread.is_alive())

    def tearDown(self):
        self.auto.close()
        self.temp.cleanup()

    def test_ai_success_publishes_enriched_note(self):
        self.run_once()
        row = self.auto.snapshot()['items'][0]
        self.assertEqual(row['status'], 'done')
        self.assertTrue(row['ai'])
        self.assertEqual(row['warning'], '')
        self.assertEqual(self.kb.ai[0]['summary'], '摘要')
        self.assertEqual(self.calls[0][1], 'Speaker 1 00:00:01.000\n正文内容。')

    def test_ai_failure_degrades_but_keeps_note(self):
        self.auto.close()
        self.auto = self.make(enrich=RuntimeError('模型返回格式异常'))
        self.run_once()
        row = self.auto.snapshot()['items'][0]
        self.assertEqual(row['status'], 'done')
        self.assertFalse(row['ai'])
        self.assertIn('AI 加工未完成：模型返回格式异常', row['warning'])
        self.assertIsNone(self.kb.ai[0])

    def test_ai_skipped_when_disabled_or_without_key(self):
        self.settings.update({'ai_enabled': False})
        self.run_once()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.kb.ai[0])
        self.assertFalse(self.auto.snapshot()['items'][0]['ai'])
        self.auto.close()
        self.settings.update({'ai_enabled': True, 'ai_api_key': ''})
        self.auto = self.make(work=self.root / 'work2')
        self.run_once()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.kb.ai[-1])


if __name__ == '__main__':
    unittest.main()
