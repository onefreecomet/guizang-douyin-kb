"""Watchdog rules: liveness probe, pause marker, restart after repeated failures."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import watchdog


class Reply:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TickTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pause = self.root / 'watchdog.pause'
        for target, value in [('WORK_DIR', self.root), ('PAUSE_PATH', self.pause),
                              ('LOG_PATH', self.root / 'watchdog.log')]:
            p = patch.object(watchdog, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.start_server = patch.object(watchdog, 'start_server', Mock()).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.temp.cleanup()

    def test_healthy_service_never_restarts_and_clears_failures(self):
        with patch.object(watchdog, 'probe', Mock(return_value={'ok': True, 'pid': 1})):
            self.assertEqual(watchdog.tick(port=1, failures=1, paused=False), (0, False, False))
        self.start_server.assert_not_called()

    def test_restart_only_after_two_consecutive_failures(self):
        with patch.object(watchdog, 'probe', Mock(return_value=None)):
            self.assertEqual(watchdog.tick(port=1, failures=0, paused=False), (1, False, False))
            self.start_server.assert_not_called()
            self.assertEqual(watchdog.tick(port=1, failures=1, paused=False), (0, True, False))
        self.start_server.assert_called_once_with(1)

    def test_pause_marker_blocks_restart_until_cleared(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'probe', Mock(return_value=None)):
            self.assertEqual(watchdog.tick(port=1, failures=3, paused=False), (0, False, True))
        self.start_server.assert_not_called()
        self.pause.unlink()
        with patch.object(watchdog, 'probe', Mock(return_value={'ok': True})):
            self.assertEqual(watchdog.tick(port=1, failures=0, paused=True), (0, False, False))

    def test_probe_requires_a_truthy_ok_field(self):
        with patch('watchdog.urllib.request.urlopen', Mock(return_value=Reply(b'{"ok": false}'))):
            self.assertIsNone(watchdog.probe(port=1))
        with patch('watchdog.urllib.request.urlopen', Mock(return_value=Reply(b'not json'))):
            self.assertIsNone(watchdog.probe(port=1))
        with patch('watchdog.urllib.request.urlopen', Mock(return_value=Reply(b'{"ok": true, "pid": 7}'))):
            self.assertEqual(watchdog.probe(port=1)['pid'], 7)

    def test_probe_rejects_non_200(self):
        reply = Reply(b'{"ok": true}')
        reply.status = 503
        with patch('watchdog.urllib.request.urlopen', Mock(return_value=reply)):
            self.assertIsNone(watchdog.probe(port=1))

    def test_log_rotates_and_never_raises(self):
        log = self.root / 'watchdog.log'
        log.write_text('x' * (watchdog.LOG_LIMIT + 1), encoding='utf-8')
        watchdog.log('测试')
        self.assertFalse(log.read_text(encoding='utf-8').startswith('x'))
        self.assertTrue((self.root / 'watchdog.log.1').exists())

    def test_log_swallows_unwritable_directory(self):
        with patch.object(watchdog, 'WORK_DIR', self.root / 'nope' / 'deeper'), \
                patch.object(watchdog, 'LOG_PATH', self.root / 'nope' / 'deeper' / 'watchdog.log'):
            (self.root / 'nope').write_text('file, not a directory', encoding='utf-8')
            watchdog.log('不应抛错')


class BootTests(unittest.TestCase):
    """开机自启：上一次开机留下的退出标记要清掉，本次开机后写的不许动。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pause = self.root / 'watchdog.pause'
        for target, value in [('WORK_DIR', self.root), ('PAUSE_PATH', self.pause),
                              ('LOG_PATH', self.root / 'watchdog.log')]:
            p = patch.object(watchdog, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.start_server = patch.object(watchdog, 'start_server', Mock()).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.temp.cleanup()

    def test_uptime_is_a_positive_number(self):
        seconds = watchdog.uptime_seconds()
        self.assertIsNotNone(seconds)
        self.assertGreater(seconds, 0)

    def test_marker_from_a_previous_boot_is_cleared(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'uptime_seconds', Mock(return_value=60.0)):
            stamp = self.pause.stat().st_mtime
            os.utime(self.pause, (stamp - 700.0, stamp - 700.0))  # 开机前 11 分钟写的
            self.assertTrue(watchdog.clear_stale_pause())
        self.assertFalse(self.pause.exists())

    def test_marker_written_after_boot_is_respected(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'uptime_seconds', Mock(return_value=7200.0)):
            self.assertFalse(watchdog.clear_stale_pause())
        self.assertTrue(self.pause.exists(), '用户刚自己退的服务，不能被守护进程擅自撤销')

    def test_unknown_uptime_leaves_the_marker_alone(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'uptime_seconds', Mock(return_value=None)):
            self.assertFalse(watchdog.clear_stale_pause())
        self.assertTrue(self.pause.exists())

    def test_bootstrap_starts_the_service_immediately(self):
        with patch.object(watchdog, 'probe', Mock(return_value=None)):
            self.assertTrue(watchdog.bootstrap(port=1))
        self.start_server.assert_called_once_with(1)

    def test_bootstrap_does_nothing_when_the_service_is_up(self):
        with patch.object(watchdog, 'probe', Mock(return_value={'ok': True, 'pid': 1})):
            self.assertFalse(watchdog.bootstrap(port=1))
        self.start_server.assert_not_called()

    def test_bootstrap_respects_a_marker_from_this_boot(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'uptime_seconds', Mock(return_value=7200.0)), \
                patch.object(watchdog, 'probe', Mock(return_value=None)):
            self.assertFalse(watchdog.bootstrap(port=1))
        self.start_server.assert_not_called()

    def test_bootstrap_clears_a_stale_marker_then_starts_the_service(self):
        self.pause.write_text('用户退出服务', encoding='utf-8')
        with patch.object(watchdog, 'uptime_seconds', Mock(return_value=60.0)):
            stamp = self.pause.stat().st_mtime
            os.utime(self.pause, (stamp - 86400.0, stamp - 86400.0))
            with patch.object(watchdog, 'probe', Mock(return_value=None)):
                self.assertTrue(watchdog.bootstrap(port=1))
        self.start_server.assert_called_once_with(1)
        self.assertFalse(self.pause.exists())
        self.assertIn('上一次开机', (self.root / 'watchdog.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
