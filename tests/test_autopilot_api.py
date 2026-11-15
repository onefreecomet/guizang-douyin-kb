"""Offline integration of the new APIs over a real ephemeral loopback server.

The pipeline and publisher are real; external scans/downloads/notifications are
replaced, and every config, checkpoint, download and note lives in our fixture.
"""
from contextlib import ExitStack
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import unittest
from urllib.parse import urlencode
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import server
import scan_favorites
from autopilot import Settings


class AutopilotApiTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.stack = ExitStack()
        self.http = None
        self.serving = None
        self.auto = None
        self.release_gate = threading.Event()
        self.addCleanup(self.cleanup)
        self.work, self.vault, self.downloads = (self.root / name for name in ('work', 'vault', 'downloads'))
        for directory in (self.work, self.vault, self.downloads):
            directory.mkdir()
        self.cookies = self.root / 'fixture-cookies.txt'
        self.cookies.write_text('fixture only; not a real credential', encoding='utf-8')
        self.settings = Settings(self.work / 'config.json')
        self.settings.update(dict(enabled=False, base_dir=str(self.downloads),
                                  cookies=str(self.cookies), vault_path=str(self.vault), notify_enabled=False,
                                  retry_attempts=1))
        isolated_job = copy.deepcopy(server.JOB)
        isolated_job.update(state='idle', items=[], request_items=[])
        replacements = dict(WORK_DIR=self.work, LAST_JOB=self.work / 'last_job.json',
                            SETTINGS=self.settings, AUTO=None, JOB=isolated_job,
                            WORKER_THREAD=None, QUITTING=False, PROC=None, ENRICHER=None,
                            STOP_EVENT=threading.Event(), OPERATION_LOCK=threading.Lock(),
                            AUTO_LOCK=threading.Lock())
        for name, value in replacements.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.items = [dict(video_id='12345678', title='本机HTTP素材', author='离线作者',
                           saved_at='2026-11-12', url='https://www.douyin.com/video/12345678')]
        self.scan_calls = []
        self.execute_calls = []
        self.notifications = []
        self.scan_impl = lambda cookies, **kwargs: copy.deepcopy(self.items)
        self.execute_impl = self.successful_execute

        def fake_scan(cookies, **kwargs):
            self.assertEqual(Path(cookies), self.cookies)
            self.assertEqual(kwargs['favorite'], self.settings.get()['favorite'])
            self.scan_calls.append(str(cookies))
            return self.scan_impl(cookies, **kwargs)

        def fake_execute(item, config, stop):
            self.assertEqual(Path(config['base_dir']), self.downloads)
            self.execute_calls.append(item['video_id'])
            return self.execute_impl(item, config, stop)

        self.stack.enter_context(patch.object(scan_favorites, 'scan', side_effect=fake_scan))
        self.stack.enter_context(patch.object(server, 'auto_execute', side_effect=fake_execute))
        self.stack.enter_context(patch.object(server, 'send_notification',
                                             side_effect=lambda config, message: self.notifications.append(message)))
        self.stack.enter_context(patch.object(server, 'run_command', side_effect=AssertionError('external command forbidden')))
        self.stack.enter_context(patch.object(server.urllib.request, 'urlopen', side_effect=AssertionError('external HTTP forbidden')))
        self.auto = server.get_autopilot()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        self.serving = threading.Thread(target=self.http.serve_forever,
                                        kwargs={'poll_interval': 0.02}, daemon=True)
        self.serving.start()
        self.port = self.http.server_address[1]

    def cleanup(self):
        self.release_gate.set()
        try:
            if self.auto is not None:
                self.auto.close()
            if self.http is not None:
                self.http.shutdown()
                if self.serving is not None:
                    self.serving.join(timeout=5)
                self.http.server_close()
        finally:
            self.stack.close()
            expected = (ROOT / 'tests' / '.runs').resolve()
            assert self.root.resolve().parent == expected and len(self.root.name) == 32
            shutil.rmtree(self.root)

    def request(self, route, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        request_headers = {'Host': f'127.0.0.1:{self.port}', 'Connection': 'close'}
        request_headers.update(headers or {})
        body = None
        method = 'GET'
        if data is not None:
            method = 'POST'
            # An empty POST is the API's documented equivalent of {}. Avoid
            # unread payload bytes when Host/Origin rejects before body parsing.
            body = b'' if isinstance(data, dict) and not data else json.dumps(data, ensure_ascii=False).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, route, body, request_headers)
            response = connection.getresponse()
            content = response.read().decode('utf-8')
            return response.status, json.loads(content)
        finally:
            connection.close()

    def successful_execute(self, item, config, stop):
        directory = self.downloads / '2026-11-12' / item['video_id']
        directory.mkdir(parents=True, exist_ok=True)
        (directory / '视频信息.txt').write_text(
            '视频ID: ' + item['video_id'] + '\n标题:\n' + item['title'], encoding='utf-8')
        (directory / '语音转文字.txt').write_text(
            'Duration: 2min 30s\nKeywords: 冰山 / 本机测试\nSpeaker 1 00:00:01.200\n真实HTTP正文冰山词。', encoding='utf-8')
        (directory / '视频文案.txt').write_text('真实HTTP正文冰山词。', encoding='utf-8')
        return {'status': 'done', 'directory': str(directory)}

    def wait_finished(self):
        self.assertIsNotNone(self.auto.thread)
        self.auto.thread.join(timeout=10)
        self.assertFalse(self.auto.thread.is_alive(), 'pipeline did not finish')
        return self.request('/api/autopilot')[1]

    def test_ping_reports_liveness_without_touching_the_queue(self):
        status, response = self.request('/api/ping')
        self.assertEqual(status, 200, response)
        self.assertTrue(response['ok'])
        self.assertEqual(response['pid'], os.getpid())
        self.assertIn('state', response)
        self.assertEqual(self.request('/api/queue')[1]['items'], [])

    def test_settings_secret_never_echoed_and_invalid_updates_do_not_persist(self):
        secret = 'fixture-key-not-a-secret-0000'
        status, response = self.request('/api/settings', {'ai_api_key': secret})
        self.assertEqual(status, 200, response)
        self.assertTrue(response['ok'])
        self.assertTrue(response['ai_key_set'])
        self.assertNotIn('ai_api_key', response)
        self.assertNotIn(secret, json.dumps(response))
        status, response = self.request('/api/settings')
        self.assertEqual(status, 200)
        self.assertTrue(response['ai_key_set'])
        self.assertNotIn(secret, json.dumps(response))
        persisted = json.loads((self.work / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['ai_api_key'], secret)
        before = (self.work / 'config.json').read_bytes()
        for invalid in ({'kb_subdir': '../private'}, {'kb_subdir': ''},
                        {'favorite': '   '}, {'schedule_time': '25:00'}, {'schedule_time': ':30'},
                        {'base_dir': 'relative'}, {'ai_daily_limit': 0},
                        {'ai_daily_limit': 2001}, {'ai_enabled': 'yes'},
                        {'skin': 'neon'}, {'skin': 7},
                        {'unknown': 'value'}, []):
            status, error = self.request('/api/settings', invalid)
            self.assertEqual(status, 400, error)
            self.assertFalse(error['ok'])
            self.assertNotIn(secret, json.dumps(error))
            self.assertEqual((self.work / 'config.json').read_bytes(), before)
        self.assertEqual(self.request('/api/settings', {'ai_model': 'fixture-model'})[0], 200)
        self.assertEqual(self.settings.get()['ai_api_key'], secret)

    def test_skin_setting_round_trips_over_http(self):
        status, response = self.request('/api/settings')
        self.assertEqual(status, 200)
        self.assertEqual(response['skin'], 'paper')
        status, response = self.request('/api/settings', {'skin': 'ink'})
        self.assertEqual(status, 200, response)
        self.assertEqual(response['skin'], 'ink')
        self.assertEqual(self.request('/api/settings')[1]['skin'], 'ink')
        self.assertEqual(self.settings.get()['skin'], 'ink')
        persisted = json.loads((self.work / 'config.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['skin'], 'ink')

    def test_run_queue_persistence_and_knowledge_http_end_to_end(self):
        status, response = self.request('/api/autopilot/run', {})
        self.assertEqual((status, response), (200, {'ok': True}))
        state = self.wait_finished()
        self.assertEqual(state['state'], 'done', state)
        self.assertNotIn('items', state)
        self.assertIsNone(state['next_run'])
        status, queue = self.request('/api/queue')
        self.assertEqual(status, 200)
        row = queue['items'][0]
        self.assertEqual((row['status'], row['phase']), ('done', 'published'))
        self.assertEqual(row['attempt'], 1)
        note_path = row['note_path']
        self.assertTrue((self.vault / note_path).is_file())
        status, tree = self.request('/api/kb/tree')
        self.assertEqual((status, tree['count']), (200, 1))
        status, note = self.request('/api/kb/note?' + urlencode({'path': note_path}))
        self.assertEqual(status, 200, note)
        self.assertIn('00:00:01.200', note['markdown'])
        self.assertIn('真实HTTP正文冰山词。', note['markdown'])
        self.assertTrue(note['obsidian_url'].startswith('obsidian://open?'))
        status, search = self.request('/api/kb/search?' + urlencode({'q': '冰山词'}))
        self.assertEqual((status, search['count']), (200, 1))
        self.assertEqual(search['items'][0]['path'], note_path)
        self.assertEqual(self.request('/api/kb/reindex', {})[1]['count'], 1)
        connection = sqlite3.connect(self.work / 'pipeline.db')
        try:
            saved = json.loads(connection.execute('SELECT body FROM checkpoint WHERE id=1').fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(saved['items'][0]['note_path'], note_path)
        self.auto.close()
        server.AUTO = None
        self.auto = server.get_autopilot()
        self.assertEqual(self.request('/api/queue')[1], queue)
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        self.assertEqual(self.execute_calls, ['12345678'], 'published ID must not execute twice')
        self.assertEqual(self.request('/api/kb/tree')[1]['count'], 1)

    def test_raw_markdown_search_and_cache_rebuild_never_create_html(self):
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        path = self.request('/api/queue')[1]['items'][0]['note_path']
        payload = '<script>alert("fixture-xss")</script>'
        with (self.vault / path).open('a', encoding='utf-8') as note:
            note.write('\n## 我的手写笔记\n' + payload + '\n')
        status, raw = self.request('/api/kb/note?' + urlencode({'path': path}))
        self.assertEqual(status, 200)
        self.assertIn(payload, raw['markdown'])
        self.assertNotIn('html', raw)
        cache = self.work / 'knowledge.db'
        self.assertTrue(cache.is_file())
        self.assertEqual(cache.resolve().parent, self.work.resolve())
        cache.unlink()
        status, result = self.request('/api/kb/search?' + urlencode({'q': 'fixture-xss'}))
        self.assertEqual((status, result['count']), (200, 1))
        self.assertIn(payload, result['items'][0]['snippet'])
        self.assertNotIn('html', result['items'][0])
        self.assertTrue(cache.is_file(), 'search must rebuild the disposable index')
        self.assertEqual(self.request('/api/kb/reindex', {})[1]['count'], 1)

    def test_paths_outside_controlled_root_are_rejected_over_http(self):
        private = self.vault / '私人笔记.md'
        private.write_text('私密内容不能泄漏', encoding='utf-8')
        for path in ('../私人笔记.md', '90 - 抖音精选/../私人笔记.md', str(private),
                     '/etc/passwd', '素材\\..\\私人笔记.md', '素材/test.txt'):
            with self.subTest(path=path):
                status, response = self.request('/api/kb/note?' + urlencode({'path': path}))
                self.assertEqual(status, 400, response)
                self.assertFalse(response['ok'])
                self.assertNotIn('私密内容不能泄漏', json.dumps(response, ensure_ascii=False))
        self.assertEqual(private.read_text(encoding='utf-8'), '私密内容不能泄漏')

    def test_host_and_cross_site_mutations_are_forbidden(self):
        for headers in ({'Host': 'evil.example'}, {'Host': '127.0.0.1.evil.example'},
                        {'Origin': 'https://evil.example'}, {'Origin': 'null'},
                        {'Sec-Fetch-Site': 'cross-site'},
                        {'Origin': 'http://127.0.0.1:1'}):
            with self.subTest(headers=headers):
                status, response = self.request('/api/autopilot/run', {'ignored': 'nonempty-body'}, headers)
                self.assertEqual(status, 403, response)
                self.assertFalse(response['ok'])
        self.assertEqual(self.request('/api/settings', headers={'Host': 'evil.example'})[0], 403)
        self.assertIsNone(self.auto.thread)
        self.assertEqual(self.scan_calls, [])
        same_origin = {'Origin': f'http://127.0.0.1:{self.port}', 'Sec-Fetch-Site': 'same-origin'}
        self.assertEqual(self.request('/api/settings', {}, same_origin)[0], 200)

    def test_notify_test_surfaces_permission_error_instead_of_generic_500(self):
        message = '飞书缺少发消息权限：请让应用具备 im:message 权限，或重新授权用户身份后重试'
        with patch.object(server, 'send_notification', side_effect=RuntimeError(message)):
            status, response = self.request('/api/notify/test', {})
        self.assertEqual(status, 502, response)
        self.assertFalse(response['ok'])
        self.assertEqual(response['error'], message)
        self.assertEqual(self.notifications, [])

    def test_busy_manual_lane_prevents_automatic_start(self):
        self.assertTrue(server.OPERATION_LOCK.acquire(blocking=False))
        try:
            status, response = self.request('/api/autopilot/run', {})
            self.assertEqual(status, 400, response)
            self.assertFalse(response['ok'])
            self.assertIn('运行', response['error'])
            self.assertIsNone(self.auto.thread)
            self.assertEqual(self.scan_calls, [])
        finally:
            server.OPERATION_LOCK.release()
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        self.assertTrue(server.OPERATION_LOCK.acquire(blocking=False), 'completed automatic run must release lane')
        server.OPERATION_LOCK.release()

    def test_running_auto_blocks_manual_start_scan_and_settings_update(self):
        entered = threading.Event()
        def blocked_execute(item, cfg, stop):
            entered.set()
            if not self.release_gate.wait(5):
                raise AssertionError('fixture execute gate timed out')
            return self.successful_execute(item, cfg, stop)
        self.execute_impl = blocked_execute
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertTrue(entered.wait(5))
        try:
            status, response = self.request('/api/start', dict(items=self.items, base_dir=str(self.downloads),
                                                              cookies=str(self.cookies), transcribe=True))
            self.assertEqual(status, 409, response)
            status, response = self.request('/api/scan', {'cookies': str(self.cookies), 'base_dir': str(self.downloads)})
            self.assertEqual(status, 409, response)
            status, response = self.request('/api/settings', {'ai_model': 'must-not-write'})
            self.assertEqual(status, 400, response)
            self.assertNotEqual(self.settings.get()['ai_model'], 'must-not-write')
        finally:
            self.release_gate.set()
        self.assertEqual(self.wait_finished()['state'], 'done')
        self.assertEqual(len(self.scan_calls), 1)
        self.assertFalse((self.work / 'last_job.json').exists(), 'manual worker must never have launched')

    def test_failure_retry_restores_queue_and_publishes_without_rescan(self):
        self.execute_impl = lambda item, cfg, stop: {'status': 'error', 'error': 'fixture transcription failure'}
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'partial')
        queue = self.request('/api/queue')[1]
        self.assertEqual(queue['items'][0]['status'], 'error')
        self.assertEqual(self.request('/api/kb/tree')[1]['count'], 0)
        self.auto.close()
        server.AUTO = None
        self.auto = server.get_autopilot()
        self.assertEqual(self.request('/api/queue')[1], queue)
        self.execute_impl = self.successful_execute
        status, response = self.request('/api/queue/retry', {'video_id': '12345678'})
        self.assertEqual(status, 200, response)
        self.assertEqual(self.wait_finished()['state'], 'done')
        row = self.request('/api/queue')[1]['items'][0]
        self.assertEqual((row['status'], row['attempt']), ('done', 2))
        self.assertEqual(len(self.scan_calls), 1, 'retry must not rescan favorites')
        self.assertEqual(self.request('/api/kb/search?' + urlencode({'q': '冰山词'}))[1]['count'], 1)
        self.assertEqual(self.request('/api/queue/retry', {'video_id': 'does-not-exist'})[0], 400)
        self.assertTrue(server.OPERATION_LOCK.acquire(blocking=False), 'failed retry validation must release lane')
        server.OPERATION_LOCK.release()

    def test_pause_waiting_scan_then_resume_via_http(self):
        entered = threading.Event()
        def waiting_scan(cookies, **kwargs):
            entered.set()
            kwargs['stop_event'].wait(5)
            return []
        self.scan_impl = waiting_scan
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertTrue(entered.wait(5))
        status, response = self.request('/api/autopilot/pause', {})
        self.assertEqual((status, response), (200, {'ok': True}))
        self.assertEqual(self.wait_finished()['state'], 'paused')
        self.assertEqual(self.request('/api/queue')[1]['items'], [])
        self.scan_impl = lambda cookies, **kwargs: copy.deepcopy(self.items)
        self.assertEqual(self.request('/api/autopilot/resume', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        self.assertEqual(self.request('/api/kb/tree')[1]['count'], 1)


    def test_ai_and_page_endpoints_over_http(self):
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        row = self.request('/api/queue')[1]['items'][0]
        note = self.vault / row['note_path']
        self.assertIn('status: 待加工', note.read_text(encoding='utf-8'))

        class FakeEnricher:
            def __init__(self):
                self.calls = []

            def enrich(self, config, item, transcript, topic_names):
                self.calls.append((item['video_id'], transcript, tuple(topic_names)))
                return {'summary': '补加工摘要', 'key_points': ['要点甲', '要点乙'],
                        'topics': ['离线主题', '冰山'], 'series': '离线系列', 'series_index': 1,
                        'model': 'fixture-model', 'at': '2026-11-13T15:00:00+08:00'}

            def usage(self):
                return {'date': '2026-11-13', 'used': 1, 'limit': 200}

        fake = FakeEnricher()
        with patch.object(server, 'get_enricher', lambda: fake):
            status, response = self.request('/api/kb/enrich', {})
            self.assertEqual(status, 200, response)
            self.assertEqual([x['video_id'] for x in response['items']], ['12345678'])
            self.assertEqual(response['failed'], [])
            self.assertEqual(self.request('/api/kb/enrich', {})[1]['items'], [],
                             'notes that already carry AI fields must be skipped')
            status, usage = self.request('/api/ai/usage')
        self.assertEqual(status, 200, usage)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], '12345678')
        self.assertIn('真实HTTP正文冰山词', fake.calls[0][1])
        self.assertEqual(usage['limit'], 200)
        self.assertFalse(usage['ai_key_set'])
        text = note.read_text(encoding='utf-8')
        self.assertIn('ai_summary: "补加工摘要"', text)
        self.assertIn('status: 已加工', text)
        self.assertIn('ai_model: "fixture-model"', text)

        status, topics = self.request('/api/kb/topics')
        self.assertEqual(status, 200, topics)
        self.assertEqual(topics['count'], 2)
        self.assertEqual({x['name'] for x in topics['items']}, {'离线主题', '冰山'})
        status, series = self.request('/api/kb/series')
        self.assertEqual([x['name'] for x in series['items']], ['离线系列'])
        series_note = self.request('/api/kb/series/note?' + urlencode({'name': '离线系列'}))[1]
        self.assertIn('## 分集', series_note['markdown'])
        topic_note = self.request('/api/kb/topic?' + urlencode({'name': '离线主题'}))[1]
        self.assertIn('## 相关视频（1）', topic_note['markdown'])
        tree = self.request('/api/kb/tree')[1]
        self.assertEqual((tree['count'], len(tree['topics']), len(tree['series'])), (1, 2, 1))
        refresh = self.request('/api/kb/refresh', {})[1]
        self.assertEqual((refresh['topics'], refresh['series'], refresh['notes']), (2, 1, 1))
        status, merged = self.request('/api/kb/topic/merge', {'source': '冰山', 'target': '离线主题'})
        self.assertEqual(status, 200, merged)
        self.assertEqual((merged['moved'], merged['frozen'], merged['kept_page']), (1, 0, True))
        self.assertEqual(self.request('/api/kb/topics')[1]['count'], 1)
        self.assertEqual(self.request('/api/kb/topic/merge', {'source': '', 'target': '目标'})[0], 400)

        with patch.object(server, 'ai_probe',
                          side_effect=RuntimeError('AI 接口连接失败，请检查网络与接口地址')):
            status, error = self.request('/api/ai/test', {})
        self.assertEqual(status, 502, error)
        self.assertEqual(error['error'], 'AI 接口连接失败，请检查网络与接口地址')
        with patch.object(server, 'ai_probe', return_value={'ok': True, 'model': 'fixture-model',
                                                            'message': '连通正常，模型 fixture-model'}):
            status, response = self.request('/api/ai/test', {'ai_api_key': 'fixture-only'})
        self.assertEqual((status, response['ok']), (200, True))

    def test_chat_answers_from_published_notes_over_http(self):
        self.assertEqual(self.request('/api/autopilot/run', {})[0], 200)
        self.assertEqual(self.wait_finished()['state'], 'done')
        note_path = self.request('/api/queue')[1]['items'][0]['note_path']

        class FakeChatEnricher:
            def __init__(self):
                self.messages = []

            def chat(self, config, messages):
                self.messages.append(messages)
                return {'text': '冰山词来自本机测试素材。', 'model': 'fixture-chat', 'tokens': 42}

        fake = FakeChatEnricher()
        with patch.object(server, 'get_enricher', lambda: fake):
            status, response = self.request('/api/chat', {'question': '冰山词是什么'})
            self.assertEqual(status, 200, response)
            self.assertTrue(response['ok'])
            self.assertEqual(response['answer'], '冰山词来自本机测试素材。')
            self.assertEqual(response['model'], 'fixture-chat')
            self.assertEqual([item['path'] for item in response['sources']], [note_path])
            self.assertIn('用户问题：冰山词是什么', fake.messages[0][1]['content'])
            self.assertIn('归藏', fake.messages[0][0]['content'])
            status, error = self.request('/api/chat', {'question': '   '})
            self.assertEqual(status, 400, error)
            self.assertIn('请输入要问的问题', error['error'])

        with patch.object(server, 'get_enricher',
                          lambda: (_ for _ in ()).throw(RuntimeError('今日 AI 调用已达上限，明天继续'))):
            status, error = self.request('/api/chat', {'question': '冰山词是什么'})
        self.assertEqual(status, 502, error)
        self.assertEqual(error['error'], '今日 AI 调用已达上限，明天继续')


class FakeApiResponse:
    """Minimal context-manager stand-in for urllib's HTTP response."""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.payload


class AiProbeTests(unittest.TestCase):
    """The probe must survive reasoning-only replies from reasoning models."""

    config = {'ai_model': 'fixture-model', 'ai_api_key': 'fixture-only',
              'ai_base_url': 'https://example.invalid/v1'}

    def probe(self, payload):
        seen = {}

        def fake_urlopen(request, *args, **kwargs):
            seen['body'] = json.loads(request.data.decode('utf-8'))
            return FakeApiResponse(payload)

        with patch.object(server.urllib.request, 'urlopen', side_effect=fake_urlopen):
            try:
                return server.ai_probe(self.config), seen
            except RuntimeError as exc:
                return exc, seen

    def test_reasoning_only_reply_explains_thinking_instead_of_format_error(self):
        result, seen = self.probe({'choices': [],
                                   'usage': {'completion_tokens': 64,
                                             'completion_tokens_details': {'reasoning_tokens': 63}}})
        self.assertIsInstance(result, RuntimeError)
        message = str(result)
        self.assertIn('只输出了思考过程', message)
        self.assertNotIn('格式不符合预期', message)
        self.assertEqual(seen['body']['max_tokens'], server.PROBE_MAX_TOKENS)
        self.assertGreaterEqual(server.PROBE_MAX_TOKENS, 256)

    def test_normal_reply_reports_model(self):
        result, seen = self.probe({'model': 'fixture-model',
                                   'choices': [{'message': {'content': '{"ok": true}'}}]})
        self.assertEqual(result['ok'], True)
        self.assertIn('fixture-model', result['message'])
        self.assertEqual(seen['body']['max_tokens'], server.PROBE_MAX_TOKENS)

    def test_missing_key_is_a_value_error(self):
        with self.assertRaises(ValueError):
            server.ai_probe(dict(self.config, ai_api_key=''))


class DependenciesStatusTests(unittest.TestCase):
    """状态栏回报的路径必须来自设置，而不是打包时的默认值。"""

    def test_status_reports_configured_paths(self):
        root = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        root.mkdir(parents=True)
        try:
            cookies = root / 'cookies.txt'
            cookies.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
            config = {'base_dir': str(root), 'cookies': str(cookies)}
            stub = type('Result', (), {'stdout': '{}', 'stderr': ''})()
            with patch.object(server, 'run_command', lambda *a, **k: stub):
                data = server.deps_status(config)
            self.assertEqual(data['default_base'], str(root))
            self.assertEqual(data['cookies_path'], str(cookies))
            self.assertTrue(data['cookies_exists'])

            cookies.unlink()
            with patch.object(server, 'run_command', lambda *a, **k: stub):
                data = server.deps_status(config)
            self.assertFalse(data['cookies_exists'])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
