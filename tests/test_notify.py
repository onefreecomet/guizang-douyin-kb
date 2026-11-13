"""Unit tests for the Feishu notification command construction.

No network, no credentials: run_command is replaced with a fake that records
the argument list and returns canned lark-cli JSON.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import server


class Completed:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ''


def config(**overrides):
    base = dict(notify_receive_id='ou_fixture000000000000000000000000', notify_receive_id_type='open_id')
    base.update(overrides)
    return base


class NotificationTests(unittest.TestCase):
    def run_send(self, cfg, responses):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return responses[len(calls) - 1]

        with patch.object(server, 'run_command', side_effect=fake_run):
            server.send_notification(cfg, '内容')
        return calls

    def test_empty_recipient_is_rejected_before_any_command(self):
        calls = []
        with patch.object(server, 'run_command', side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(ValueError):
                server.send_notification(config(notify_receive_id='  '), '内容')
        self.assertEqual(calls, [])

    def test_recipient_kind_prefix_is_validated(self):
        for cfg in (config(notify_receive_id='oc_fixture'), config(notify_receive_id_type='chat_id')):
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError):
                    server.send_notification(cfg, '内容')

    def test_bot_identity_is_used_when_user_scope_is_missing(self):
        missing = Completed(json.dumps({'ok': False, 'identity': 'user',
                                        'error': {'type': 'authorization', 'subtype': 'missing_scope'}}), 1)
        sent = Completed(json.dumps({'ok': True, 'identity': 'bot', 'data': {'message_id': 'om_fixture'}}))
        calls = self.run_send(config(), [missing, sent])
        self.assertEqual([call[call.index('--as') + 1] for call in calls], ['bot', 'user'])
        self.assertIn('ou_fixture000000000000000000000000', calls[0])
        self.assertIn('--user-id', calls[0])
        self.assertIn('内容', calls[0])

    def test_chat_id_uses_chat_flag_and_first_success_wins(self):
        sent = Completed(json.dumps({'ok': True, 'identity': 'bot'}))
        calls = self.run_send(config(notify_receive_id='oc_fixture', notify_receive_id_type='chat_id'), [sent])
        self.assertEqual(len(calls), 1)
        self.assertIn('--chat-id', calls[0])
        self.assertNotIn('--user-id', calls[0])

    def test_missing_scope_on_both_identities_reports_permission_problem(self):
        missing = Completed(json.dumps({'ok': False, 'error': {'type': 'authorization', 'subtype': 'missing_scope'}}), 1)
        with self.assertRaises(RuntimeError) as caught:
            self.run_send(config(), [missing, missing])
        self.assertIn('缺少发消息权限', str(caught.exception))

    def test_generic_failure_is_not_reported_as_success(self):
        failed = Completed(json.dumps({'ok': False, 'error': {'type': 'server_error'}}), 1)
        with self.assertRaises(RuntimeError) as caught:
            self.run_send(config(), [failed, failed])
        self.assertNotIn('缺少发消息权限', str(caught.exception))

    def test_unparsable_output_is_never_treated_as_delivered(self):
        broken = Completed('lark-cli crashed', 1)
        with self.assertRaises(RuntimeError):
            self.run_send(config(), [broken, broken])

    def test_zero_exit_but_explicit_failure_payload_is_rejected(self):
        body = Completed(json.dumps({'ok': False, 'error': {'type': 'invalid_param'}}), 0)
        with self.assertRaises(RuntimeError):
            self.run_send(config(), [body, body])


if __name__ == '__main__':
    unittest.main()
