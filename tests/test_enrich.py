"""P2 · app/enrich.py 离线测试。

全部走本地 127.0.0.1 假 OpenAI 服务或 unittest.mock 打桩，绝不访问真实网络。
覆盖：截断、提示词、结果校验各分支、新主题上限、每日上限与跨日归零、缺密钥、
5xx 重试一次、返回非 JSON、用量台账原子写、错误消息不含密钥。
"""
import json
import os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import enrich
from enrich import AiError, Enricher, build_messages, parse_result, truncate

KEY = 'test-key-not-a-secret-0123456789'
TODAY = datetime.now(enrich.CST).date().isoformat()
YESTERDAY = (datetime.now(enrich.CST).date() - timedelta(days=1)).isoformat()
LEDGER = 'ai_usage.json'


def valid_payload(**overrides):
    payload = {'summary': '一条测试素材的摘要。',
               'key_points': ['第一个要点', '第二个要点'],
               'topics': [{'name': '已有主题', 'parent': ''},
                          {'name': '新主题', 'parent': '已有主题'}],
               'series': None,
               'keywords': ['测试', '#视频']}
    payload.update(overrides)
    return payload


def envelope(payload=None, tokens=321):
    """OpenAI 兼容响应体；payload 为 dict 时转成 content 字符串。"""
    content = payload if isinstance(payload, str) else json.dumps(
        valid_payload() if payload is None else payload, ensure_ascii=False)
    body = {'id': 'chatcmpl-test', 'model': 'fake-model',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': content}}]}
    if tokens is not None:
        body['usage'] = {'prompt_tokens': 10, 'completion_tokens': 20,
                         'total_tokens': tokens}
    return body


def reply(payload=None, tokens=321, status=200):
    return {'status': status, 'body': envelope(payload, tokens)}


class FakeApi:
    """本地假 OpenAI 兼容服务；按脚本顺序返回响应，请求全部记录在 requests。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                try:
                    body = json.loads(raw.decode('utf-8')) if raw else {}
                except ValueError:
                    body = {}
                outer.requests.append({'path': self.path,
                                       'headers': {k.lower(): v for k, v in self.headers.items()},
                                       'body': body})
                if len(outer.responses) > 1:
                    spec = outer.responses.pop(0)
                elif outer.responses:
                    spec = outer.responses[0]
                else:
                    spec = {'status': 500, 'body': {'error': 'no scripted response'}}
                status = int(spec.get('status', 200))
                payload = spec.get('body', '')
                if not isinstance(payload, (str, bytes)):
                    payload = json.dumps(payload, ensure_ascii=False)
                data = payload.encode('utf-8') if isinstance(payload, str) else payload
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_address[1]) + '/v1'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)


def no_network():
    """断言不会发起任何网络请求。"""
    return patch.object(urllib.request, 'urlopen',
                        side_effect=AssertionError('测试不应发起网络请求'))


def no_sleep():
    return patch.object(enrich.time, 'sleep', lambda *_: None)


class TruncateTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        text = '字' * enrich.TRANSCRIPT_LIMIT
        self.assertEqual(truncate(text), text)
        self.assertEqual(truncate(''), '')
        self.assertEqual(truncate(None), '')

    def test_long_text_keeps_head_tail_and_marker(self):
        text = 'H' * 15000 + 'M' * 5000 + 'T' * 15000
        result = truncate(text)
        self.assertTrue(result.startswith('H' * 12000))
        self.assertTrue(result.endswith('T' * 12000))
        self.assertEqual(sum(1 for line in result.splitlines() if '省略' in line), 1)
        self.assertIn('中间省略 11000 字', result)
        self.assertLess(len(result), len(text))

    def test_small_custom_limit_does_not_duplicate_content(self):
        text = '内容' * 2500  # 5000 字：超过 limit 100，但不超过首尾保留总长
        self.assertEqual(truncate(text, 100), text)


class BuildMessagesTests(unittest.TestCase):
    def test_two_messages_with_roles(self):
        messages = build_messages({'title': '标题'}, '正文', [])
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertIn('JSON', messages[0]['content'])
        self.assertTrue(messages[1]['content'].startswith('以下是待整理的抖音收藏视频素材'))

    def test_user_message_carries_item_and_transcript(self):
        item = {'title': '视频标题', 'author': '作者名', 'saved_at': '2026-11-12 10:00',
                'url': 'https://www.douyin.com/video/123', 'duration': '03:20'}
        content = build_messages(item, '这里是转写正文', ['历史', '神话'])[1]['content']
        for expected in ('视频标题', '作者名', '2026-11-12 10:00', '03:20',
                         'https://www.douyin.com/video/123', '这里是转写正文',
                         '共 2 个', '- 历史', '- 神话'):
            self.assertIn(expected, content)

    def test_topic_names_are_capped_at_200(self):
        names = ['主题%03d' % index for index in range(250)]
        content = build_messages({}, '正文', names)[1]['content']
        self.assertIn('主题199', content)
        self.assertNotIn('主题200', content)
        self.assertIn('共 200 个', content)

    def test_topic_names_are_cleaned(self):
        content = build_messages({}, '正文', ['', '  ', '甲', '甲', '乙'])[1]['content']
        self.assertIn('共 2 个', content)
        self.assertIn('- 甲', content)
        self.assertIn('- 乙', content)

    def test_long_transcript_is_truncated_in_prompt(self):
        content = build_messages({'title': '标题'}, '长' * 50000, [])[1]['content']
        self.assertIn('中间省略', content)
        self.assertLess(len(content), 40000)

    def test_missing_fields_and_empty_transcript(self):
        content = build_messages({}, '', [])[1]['content']
        self.assertIn('无转写原文', content)
        self.assertIn('暂无', content)
        content = build_messages(None, None, None)[1]['content']
        self.assertIn('无转写原文', content)


class ParseResultTests(unittest.TestCase):
    def test_valid_payload_is_normalized(self):
        payload = valid_payload(summary='  摘要\n第二行  ',
                                keywords=['#测试', ' 测试 ', '视频', '', 5])
        result = parse_result(payload, ['已有主题'])
        self.assertEqual(result, {
            'summary': '摘要 第二行',
            'key_points': ['第一个要点', '第二个要点'],
            'topics': [{'name': '已有主题', 'parent': ''},
                       {'name': '新主题', 'parent': '已有主题'}],
            'series': None,
            'keywords': ['测试', '视频']})

    def test_payload_must_be_object(self):
        for payload in ([], '文字', 3, None, True):
            with self.assertRaises(AiError):
                parse_result(payload, [])

    def test_summary_is_required_and_bounded(self):
        for summary in (None, 123, '', '   ', '长' * 401):
            with self.assertRaises(AiError) as ctx:
                parse_result(valid_payload(summary=summary), [])
            self.assertIn('摘要', str(ctx.exception))
        self.assertEqual(len(parse_result(valid_payload(summary='长' * 400), [])['summary']), 400)

    def test_key_points_are_required(self):
        for points in ('文字', None, [], ['', '  '], [1, None, True]):
            with self.assertRaises(AiError) as ctx:
                parse_result(valid_payload(key_points=points), [])
            self.assertIn('要点', str(ctx.exception))

    def test_key_points_are_cleaned_and_capped(self):
        points = ['  第%d条\n要点  ' % index for index in range(1, 10)]
        result = parse_result(valid_payload(key_points=points), [])
        self.assertEqual(len(result['key_points']), 8)
        self.assertEqual(result['key_points'][0], '第1条 要点')
        long_point = parse_result(valid_payload(key_points=['长' * 250]), [])['key_points'][0]
        self.assertEqual(len(long_point), 200)

    def test_topics_are_required(self):
        for topics in (None, '主题', [], [{'name': ''}], [{'name': '长' * 41}],
                       [{'parent': '父'}], [1, 2]):
            with self.assertRaises(AiError) as ctx:
                parse_result(valid_payload(topics=topics), [])
            self.assertEqual(str(ctx.exception), 'AI 未返回可用主题')

    def test_new_topic_is_limited_to_one(self):
        payload = valid_payload(topics=[{'name': '已有A', 'parent': ''},
                                        {'name': '新B', 'parent': '已有A'},
                                        {'name': '新C', 'parent': '已有A'},
                                        {'name': '新D', 'parent': ''}])
        result = parse_result(payload, ['已有A'])
        self.assertEqual([topic['name'] for topic in result['topics']], ['已有A', '新B'])

    def test_all_new_topics_keep_first(self):
        payload = valid_payload(topics=[{'name': '甲', 'parent': ''},
                                        {'name': '乙', 'parent': ''}])
        self.assertEqual([t['name'] for t in parse_result(payload, [])['topics']], ['甲'])

    def test_topics_are_capped_at_four_and_deduped(self):
        known = ['A', 'B', 'C', 'D', 'E']
        payload = valid_payload(topics=[{'name': name, 'parent': ''}
                                        for name in ('A', 'B', 'C', 'D', 'E', 'A')])
        result = parse_result(payload, known)
        self.assertEqual([topic['name'] for topic in result['topics']],
                         ['A', 'B', 'C', 'D'])

    def test_topic_entries_are_cleaned(self):
        payload = valid_payload(topics=[{'name': '  已有  A ', 'parent': 7},
                                        {'name': '已有A', 'parent': '父'},
                                        {'name': '长' * 41, 'parent': ''},
                                        '字符串主题'])
        result = parse_result(payload, ['已有 A'])
        self.assertEqual([topic['name'] for topic in result['topics']], ['已有 A', '已有A'])
        self.assertEqual(result['topics'][0]['parent'], '')
        self.assertEqual(result['topics'][1]['parent'], '父')

    def test_series_null_or_valid(self):
        for raw in (None, '', {}):
            self.assertIsNone(parse_result(valid_payload(series=raw), [])['series'])
        self.assertEqual(parse_result(valid_payload(series={'name': ' 第一季 ', 'index': '3'}), [])['series'],
                         {'name': '第一季', 'index': 3})
        self.assertEqual(parse_result(valid_payload(series={'name': '系列', 'index': 999}), [])['series'],
                         {'name': '系列', 'index': 999})

    def test_series_is_validated(self):
        for raw in ({'name': '', 'index': 1}, {'name': '长' * 61, 'index': 1},
                    {'name': '系列', 'index': 0}, {'name': '系列', 'index': 1000},
                    {'name': '系列', 'index': True}, {'name': '系列', 'index': 'x'},
                    {'name': '系列'}, '系列', [1]):
            with self.assertRaises(AiError) as ctx:
                parse_result(valid_payload(series=raw), [])
            self.assertEqual(str(ctx.exception), 'AI 返回的系列信息不符合要求')

    def test_keywords_are_cleaned(self):
        result = parse_result(valid_payload(keywords=['#历史', ' 历史 ', '神话', '',
                                                      '长' * 50, 7]), [])
        self.assertEqual(result['keywords'][:2], ['历史', '神话'])
        self.assertEqual(len(result['keywords']), 3)
        self.assertTrue(all(len(word) <= 40 for word in result['keywords']))

    def test_keywords_string_limits_and_types(self):
        self.assertEqual(parse_result(valid_payload(keywords='历史, #神话、地理'), [])['keywords'],
                         ['历史', '神话', '地理'])
        self.assertEqual(parse_result(valid_payload(keywords=None), [])['keywords'], [])
        many = ['词%d' % index for index in range(15)]
        self.assertEqual(len(parse_result(valid_payload(keywords=many), [])['keywords']), 10)
        with self.assertRaises(AiError):
            parse_result(valid_payload(keywords=5), [])

    def test_extra_keys_are_ignored(self):
        result = parse_result(valid_payload(confidence=0.9, extra={'a': 1}), ['已有主题'])
        self.assertEqual(set(result),
                         {'summary', 'key_points', 'topics', 'series', 'keywords'})


class EnricherCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / 'work'
        self.enricher = Enricher(self.work)
        self.zero_wait = patch.object(enrich, 'RETRY_WAIT', 0)
        self.zero_wait.start()
        self.addCleanup(self.zero_wait.stop)

    def config(self, **changes):
        config = {'ai_base_url': 'https://example.invalid/v1', 'ai_model': 'test-model',
                  'ai_api_key': KEY, 'ai_daily_limit': 200}
        config.update(changes)
        return config

    def ledger(self):
        return json.loads((self.work / LEDGER).read_text(encoding='utf-8'))

    def write_ledger(self, **data):
        (self.work / LEDGER).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


class CredentialsTests(EnricherCase):
    def test_missing_key_is_rejected_without_request(self):
        for key in (None, '', '   '):
            config = self.config()
            if key is None:
                config.pop('ai_api_key')
            else:
                config['ai_api_key'] = key
            with no_network():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(config, {'title': '标题'}, '正文', [])
            self.assertEqual(str(ctx.exception), '尚未设置 AI 密钥，请到设置填写')

    def test_missing_base_url_or_model(self):
        with no_network():
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(ai_base_url=''), {'title': 't'}, '正文', [])
            self.assertEqual(str(ctx.exception), '尚未设置 AI 接口地址，请到设置填写')
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(ai_model='  '), {'title': 't'}, '正文', [])
            self.assertEqual(str(ctx.exception), '尚未设置 AI 模型，请到设置选择')
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(ai_base_url='ftp://x/v1'), {'title': 't'}, '正文', [])
            self.assertEqual(str(ctx.exception), 'AI 接口地址无效，请到设置检查')

    def test_connection_probe_requires_key(self):
        with no_network():
            with self.assertRaises(AiError) as ctx:
                self.enricher.test_connection({'ai_base_url': 'https://x/v1', 'ai_model': 'm'})
        self.assertEqual(str(ctx.exception), '尚未设置 AI 密钥，请到设置填写')


class UsageLedgerTests(EnricherCase):
    def test_usage_defaults_on_empty_work_dir(self):
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 0, 'limit': 200, 'tokens': 0})
        self.assertFalse((self.work / LEDGER).exists())

    def test_daily_limit_blocks_before_request(self):
        self.write_ledger(date=TODAY, count=200, tokens=10, limit=200)
        with no_network():
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(), {'title': 't'}, '正文', [])
        self.assertEqual(str(ctx.exception), '今日 AI 加工已达上限，明天继续')

    def test_daily_limit_comes_from_config(self):
        self.write_ledger(date=TODAY, count=3, tokens=1, limit=200)
        with no_network():
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(ai_daily_limit=3), {'title': 't'}, '正文', [])
        self.assertEqual(str(ctx.exception), '今日 AI 加工已达上限，明天继续')

    def test_cross_day_usage_resets(self):
        self.write_ledger(date=YESTERDAY, count=200, tokens=999, limit=5)
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 0, 'limit': 5, 'tokens': 0})
        with FakeApi(reply()) as api:
            self.enricher.enrich(self.config(ai_base_url=api.url), {'title': 't'}, '正文', [])
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 1, 'limit': 200, 'tokens': 321})

    def test_corrupt_usage_file_is_tolerated(self):
        (self.work / LEDGER).write_text('{not json', encoding='utf-8')
        self.assertEqual((self.enricher.usage()['count'], self.enricher.usage()['tokens'],
                          self.enricher.usage()['limit']), (0, 0, 200))
        with FakeApi(reply()) as api:
            self.enricher.enrich(self.config(ai_base_url=api.url), {'title': 't'}, '正文', [])
        self.assertEqual(self.ledger()['count'], 1)

    def test_usage_write_is_atomic(self):
        real = os.replace
        seen = []

        def spy(source, target):
            seen.append((Path(source), Path(target)))
            return real(source, target)

        with FakeApi(reply()) as api:
            with patch.object(enrich.os, 'replace', side_effect=spy):
                self.enricher.enrich(self.config(ai_base_url=api.url), {'title': 't'}, '正文', [])
        self.assertEqual(len(seen), 1)
        source, target = seen[0]
        self.assertEqual(target, self.work / LEDGER)
        self.assertEqual(source.parent, self.work)
        self.assertTrue(source.name.endswith('.tmp'))
        self.assertTrue(target.is_file())
        self.assertEqual(list(self.work.glob('*.tmp')), [])

    def test_usage_write_failure_keeps_ai_result(self):
        with FakeApi(reply()) as api:
            with patch.object(enrich.os, 'replace', side_effect=OSError('disk full')):
                result = self.enricher.enrich(self.config(ai_base_url=api.url),
                                             {'title': 't'}, '正文', [])
        self.assertEqual(result['summary'], '一条测试素材的摘要。')
        self.assertFalse((self.work / LEDGER).exists())
        self.assertEqual(list(self.work.glob('*.tmp')), [])
        self.assertEqual(self.enricher.usage()['count'], 0)


class RequestTests(EnricherCase):
    def test_success_posts_and_records_usage(self):
        with FakeApi(reply(valid_payload())) as api:
            result = self.enricher.enrich(self.config(ai_base_url=api.url + '/'),
                                          {'title': '标题ABC', 'author': '作者',
                                           'url': 'https://www.douyin.com/video/1'},
                                          '转写正文内容' * 10, ['已有主题'])
        self.assertEqual(set(result),
                         {'summary', 'key_points', 'topics', 'series', 'keywords', 'model'})
        self.assertEqual(result['summary'], '一条测试素材的摘要。')
        self.assertEqual(result['model'], 'test-model')
        self.assertEqual(result['series'], None)
        self.assertEqual(len(api.requests), 1)
        request = api.requests[0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['headers'].get('authorization'), 'Bearer ' + KEY)
        self.assertEqual(request['body']['model'], 'test-model')
        self.assertEqual(request['body']['temperature'], 0.2)
        self.assertEqual(request['body']['max_tokens'], 4096)
        self.assertEqual(request['body']['response_format'], {'type': 'json_object'})
        self.assertEqual([m['role'] for m in request['body']['messages']], ['system', 'user'])
        user = request['body']['messages'][1]['content']
        self.assertIn('标题ABC', user)
        self.assertIn('转写正文内容', user)
        self.assertIn('已有主题', user)
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 1, 'limit': 200, 'tokens': 321})
        self.assertEqual(self.ledger(), {'date': TODAY, 'count': 1, 'tokens': 321, 'limit': 200})
        self.assertNotIn(KEY, (self.work / LEDGER).read_text(encoding='utf-8'))
        self.assertEqual(list(self.work.glob('*.tmp')), [])

    def test_request_timeout_is_90_seconds(self):
        seen = {}
        real = urllib.request.urlopen

        def spy(request, *args, **kwargs):
            seen['timeout'] = kwargs.get('timeout')
            return real(request, *args, **kwargs)

        with FakeApi(reply()) as api:
            with patch.object(urllib.request, 'urlopen', side_effect=spy):
                self.enricher.enrich(self.config(ai_base_url=api.url), {'title': 't'}, '正文', [])
        self.assertEqual(seen['timeout'], 90)

    def test_usage_accumulates_and_survives_new_instance(self):
        with FakeApi(reply(tokens=100), reply(tokens=200)) as api:
            for _ in range(2):
                self.enricher.enrich(self.config(ai_base_url=api.url), {'title': 't'}, '正文', [])
        usage = Enricher(self.work).usage()
        self.assertEqual((usage['count'], usage['tokens']), (2, 300))

    def test_connection_probe_does_not_consume_quota(self):
        with FakeApi(reply()) as api:
            result = self.enricher.test_connection(self.config(ai_base_url=api.url))
        self.assertEqual(result, {'model': 'test-model', 'message': 'AI 接口可用'})
        self.assertEqual(self.enricher.usage()['count'], 0)
        self.assertEqual(len(api.requests), 1)


class RetryTests(EnricherCase):
    def test_server_error_is_retried_once_then_reported(self):
        sleeps = []
        with FakeApi({'status': 500, 'body': {'error': 'boom'}}) as api:
            with patch.object(enrich, 'RETRY_WAIT', 2), \
                    patch.object(enrich.time, 'sleep', sleeps.append):
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        message = str(ctx.exception)
        self.assertIn('HTTP 500', message)
        self.assertIn('已重试 1 次', message)
        self.assertNotIn(KEY, message)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(sleeps, [2])
        self.assertEqual(self.enricher.usage()['count'], 0)
        self.assertFalse((self.work / LEDGER).exists())

    def test_server_error_then_success(self):
        with FakeApi({'status': 502, 'body': {}}, reply(tokens=99)) as api:
            with no_sleep():
                result = self.enricher.enrich(self.config(ai_base_url=api.url),
                                              {'title': 't'}, '正文', [])
        self.assertEqual(result['summary'], '一条测试素材的摘要。')
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 1, 'limit': 200, 'tokens': 99})

    def test_non_json_body_is_retried_then_reported(self):
        with FakeApi({'status': 200, 'body': '这不是 JSON'}) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        self.assertIn('不是有效 JSON', str(ctx.exception))
        self.assertIn('已重试 1 次', str(ctx.exception))
        self.assertEqual(len(api.requests), 2)

    def test_non_json_then_success(self):
        with FakeApi({'status': 200, 'body': 'oops'}, reply()) as api:
            with no_sleep():
                result = self.enricher.enrich(self.config(ai_base_url=api.url),
                                              {'title': 't'}, '正文', [])
        self.assertEqual(result['model'], 'test-model')
        self.assertEqual(len(api.requests), 2)

    def test_missing_choices_is_retried_then_reported(self):
        with FakeApi({'status': 200, 'body': {'id': 'x'}}) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        self.assertIn('缺少 choices', str(ctx.exception))
        self.assertEqual(len(api.requests), 2)

    def test_reasoning_only_response_is_reported_as_thinking(self):
        spent = {'id': 'x', 'choices': [],
                 'usage': {'completion_tokens': 64,
                           'completion_tokens_details': {'reasoning_tokens': 63}}}
        with FakeApi({'status': 200, 'body': spent}) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        self.assertIn('只返回了思考过程', str(ctx.exception))
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(api.requests[1]['body']['max_tokens'], 4096)

    def test_invalid_result_is_retried_then_success(self):
        with FakeApi(reply(valid_payload(summary='')),
                     reply(valid_payload(), tokens=111)) as api:
            with no_sleep():
                result = self.enricher.enrich(self.config(ai_base_url=api.url),
                                              {'title': 't'}, '正文', [])
        self.assertEqual(result['summary'], '一条测试素材的摘要。')
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(self.enricher.usage(),
                         {'date': TODAY, 'count': 1, 'limit': 200, 'tokens': 111})

    def test_invalid_result_twice_fails_without_counting(self):
        with FakeApi(reply(valid_payload(topics=[]))) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        message = str(ctx.exception)
        self.assertIn('AI 未返回可用主题', message)
        self.assertIn('已重试 1 次', message)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(self.enricher.usage()['count'], 0)

    def test_invalid_key_is_not_retried_and_never_echoed(self):
        echo = {'error': 'invalid api key: ' + KEY}
        with FakeApi({'status': 401, 'body': echo}) as api:
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(ai_base_url=api.url),
                                     {'title': 't'}, '正文', [])
        message = str(ctx.exception)
        self.assertIn('密钥无效', message)
        self.assertNotIn(KEY, message)
        self.assertNotIn('Bearer', message)
        self.assertEqual(len(api.requests), 1)

    def test_server_error_then_invalid_key_stops_retrying(self):
        with FakeApi({'status': 500, 'body': {}}, {'status': 403, 'body': {}}) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
        self.assertIn('密钥无效', str(ctx.exception))
        self.assertEqual(len(api.requests), 2)

    def test_network_error_is_retried_once(self):
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError('connection refused')

        with no_sleep(), patch.object(urllib.request, 'urlopen', side_effect=boom):
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(), {'title': 't'}, '正文', [])
        self.assertEqual(len(calls), 2)
        self.assertIn('连接失败', str(ctx.exception))
        self.assertIn('已重试 1 次', str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_timeout_maps_to_chinese_error(self):
        with no_sleep(), patch.object(urllib.request, 'urlopen', side_effect=TimeoutError()):
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(), {'title': 't'}, '正文', [])
        self.assertIn('超时', str(ctx.exception))
        self.assertIn('已重试 1 次', str(ctx.exception))

    def test_errors_never_contain_key(self):
        messages = []
        with no_sleep(), patch.object(urllib.request, 'urlopen',
                                      side_effect=urllib.error.URLError(KEY)):
            with self.assertRaises(AiError) as ctx:
                self.enricher.enrich(self.config(), {'title': 't'}, '正文', [])
            messages.append(str(ctx.exception))
        with FakeApi({'status': 500, 'body': {'error': 'boom ' + KEY}}) as api:
            with no_sleep():
                with self.assertRaises(AiError) as ctx:
                    self.enricher.enrich(self.config(ai_base_url=api.url),
                                         {'title': 't'}, '正文', [])
            messages.append(str(ctx.exception))
        for message in messages:
            self.assertNotIn(KEY, message)
            self.assertNotIn('Bearer', message)


if __name__ == '__main__':
    unittest.main()
