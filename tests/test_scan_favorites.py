"""Offline scanner contracts; no browser launch, cookies file or network access."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import scan_favorites as scanner


class Clock:
    def __init__(self):
        self.now = 0
        self.after_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.after_sleep:
            self.after_sleep()


class Page:
    def __init__(self, selected=True, clickable=True, entered=False, has_label=True):
        self.selected = selected
        self.clickable = clickable
        self.entered = entered
        self.has_label = has_label
        self.reason = ""
        self.rows = [{"h": "/video/123", "t": "测试视频", "author": "作者甲"},
                     {"h": "/video/456", "t": "未知作者"}]
        self.height = 1000
        self.collect_count = 0
        self.scroll_count = 0
        self.on_scroll = None
        self.scripts = []
        self.call = Mock()

    def eval(self, js):
        self.scripts.append(js)
        if js == "document.readyState":
            return "complete"
        if js == scanner.MANUAL_JS:
            return self.reason
        if js == scanner.ENTERED_JS:
            return self.entered
        if js.startswith("(" + scanner.HAS_LABEL_JS):
            return self.has_label
        if js.startswith("(" + scanner.SELECTED_JS):
            return self.selected
        if js.startswith("(" + scanner.CLICK_JS):
            return self.clickable
        if 'a[href*="/video/"]' in js:
            self.collect_count += 1
            return self.rows
        if "bestH" in js:
            self.scroll_count += 1
            if self.on_scroll:
                self.on_scroll()
            return self.height
        raise AssertionError("Unexpected JavaScript: " + js)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.page = Page()
        session = Mock()
        session.open_page.return_value = self.page
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        self.session = session
        for target, value in [
            ("EdgeSession", Mock(return_value=session)),
            ("load_cookies", Mock(return_value=[{"name": "session", "value": "fake", "domain": ".douyin.com", "path": "/", "secure": True}])),
            ("time.monotonic", self.clock.monotonic),
            ("time.sleep", self.clock.sleep),
        ]:
            p = patch("scan_favorites." + target, value)
            p.start()
            self.addCleanup(p.stop)

    def scan(self, **kwargs):
        return scanner.scan(log=lambda message: None, **kwargs)

    def test_author_is_collected_unknown_remains_empty_and_deduplicated(self):
        result = self.scan()
        self.assertEqual([r["author"] for r in result], ["作者甲", ""])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["url"], "https://www.douyin.com/video/123")

    def test_label_missing_never_collects(self):
        self.page.selected = False
        self.page.clickable = False
        notify = Mock()
        with self.assertRaises(scanner.ManualRequired):
            self.scan(on_manual=notify)
        self.assertEqual(self.page.collect_count, 0)
        notify.assert_called_once()

    def test_click_success_without_selected_evidence_never_collects(self):
        self.page.selected = False
        with self.assertRaises(scanner.ManualRequired):
            self.scan()
        self.assertEqual(self.page.collect_count, 0)

    def test_folder_entry_detected_by_add_and_back_buttons(self):
        # 2026-11 的真实页面点进收藏夹后没有任何 aria-selected，只有「添加视频」「返回」可作判据；
        # 如果这里仍要求 selected，扫描会在进入成功后误报「无法可靠确认」。
        self.page.selected = False
        self.page.entered = True
        result = self.scan()
        self.assertEqual(len(result), 2)
        self.assertEqual([r["author"] for r in result], ["作者甲", ""])

    def test_folder_list_waits_for_rendering_before_clicking(self):
        # 收藏夹列表异步渲染：名称未出现前不能点，出现后才点并可进入。
        self.page.selected = False
        self.page.has_label = False
        self.page.entered = False
        page = self.page

        def after_sleep():
            if page.has_label is False and page.collect_count == 0 and self.clock.now >= 3:
                page.has_label = True
                page.entered = True
        self.clock.after_sleep = after_sleep
        result = self.scan()
        self.assertEqual(len(result), 2)

    def test_initial_verification_notifies_immediately_and_resumes(self):
        self.page.reason = "验证码"
        times = []
        def notify(message):
            times.append(self.clock.now)
            self.page.reason = ""
        self.scan(on_manual=notify)
        self.assertEqual(times, [0])

    def test_manual_timeout_is_bounded_and_notifies_once(self):
        self.page.reason = "登录失效"
        notify = Mock()
        with self.assertRaises(scanner.ManualRequired):
            self.scan(on_manual=notify)
        self.assertEqual(self.clock.now, 180)
        notify.assert_called_once()
        self.assertEqual(self.page.collect_count, 0)

    def test_headless_manual_raises_without_waiting(self):
        self.page.reason = "验证码"
        notify = Mock()
        with self.assertRaises(scanner.ManualRequired):
            self.scan(headless=True, on_manual=notify)
        self.assertEqual(self.clock.now, 0)
        notify.assert_called_once()

    def test_pre_cancelled_scan_does_not_open_browser(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(scanner.ScanCancelled):
            self.scan(stop_event=stop)
        scanner.EdgeSession.assert_not_called()

    def test_cancel_during_manual_wait(self):
        stop = threading.Event()
        self.page.reason = "验证码"
        self.clock.after_sleep = stop.set
        with self.assertRaises(scanner.ScanCancelled):
            self.scan(stop_event=stop)
        self.assertLessEqual(self.clock.now, 0.25)

    def test_cancel_during_regular_scroll(self):
        stop = threading.Event()
        self.page.on_scroll = stop.set
        with self.assertRaises(scanner.ScanCancelled):
            self.scan(stop_event=stop)

    def test_verification_during_scroll_notifies_and_reconfirms(self):
        def scroll():
            if self.page.scroll_count == 1:
                self.page.reason = "验证码"
                self.page.selected = False
        self.page.on_scroll = scroll
        def notify(message):
            self.page.reason = ""
            self.page.selected = True
        callback = Mock(side_effect=notify)
        result = self.scan(on_manual=callback)
        self.assertEqual(len(result), 2)
        callback.assert_called_once()
        self.assertGreaterEqual(self.page.scroll_count, 6)
        navigations = [c for c in self.page.call.call_args_list if c.args[0] == 'Page.navigate']
        self.assertEqual(len(navigations), 3)

    def test_ready_wait_returns_immediately_for_challenge_even_if_loading(self):
        page = Mock()
        page.eval.side_effect = lambda js: '验证码' if js == scanner.MANUAL_JS else 'loading'
        self.assertTrue(scanner.wait_ready(page, 25))
        self.assertEqual(self.clock.now, 0)

    def test_selected_state_lost_after_scroll_never_returns_partial_success(self):
        self.page.on_scroll = lambda: setattr(self.page, 'selected', False)
        callback = Mock()
        with self.assertRaises(scanner.ManualRequired):
            self.scan(on_manual=callback)
        callback.assert_called_once()

    def test_scroll_limit_is_incomplete_even_when_zero_items(self):
        self.page.rows = []
        with self.assertRaisesRegex(RuntimeError, "扫描不完整"):
            self.scan(max_scroll=2)

    def test_partial_scan_returns_items_and_reason_instead_of_failing(self):
        with self.assertRaisesRegex(RuntimeError, "扫描不完整"):
            self.scan(max_scroll=2)
        result = self.scan(max_scroll=2, allow_partial=True)
        self.assertEqual(len(result), 2)
        self.assertIn("扫描不完整", result.incomplete)

    def test_partial_scan_keeps_items_when_list_height_is_unknown(self):
        self.page.height = 0
        result = self.scan(allow_partial=True)
        self.assertEqual(len(result), 2)
        self.assertIn("高度", result.incomplete)

    def test_full_scan_reports_no_incomplete_reason(self):
        result = self.scan()
        self.assertEqual(len(result), 2)
        self.assertEqual(result.incomplete, '')

    def test_unstable_height_is_incomplete(self):
        self.page.on_scroll = lambda: setattr(self.page, "height", self.page.height + 100)
        with self.assertRaisesRegex(RuntimeError, "扫描不完整"):
            self.scan(max_scroll=8)

    def test_label_argument_uses_json_for_quotes_backslashes_and_newlines(self):
        label = "引号'\"\\\n收藏夹\u2028); throw new Error('injected');//"
        self.scan(favorite=label)
        js = next(js for js in self.page.scripts if js.startswith("(" + scanner.SELECTED_JS))
        argument = js[len("(" + scanner.SELECTED_JS) + 2:-1]
        self.assertEqual(json.loads(argument), label)
        self.assertIn("\\u2028", argument)

    def test_dom_error_propagates_instead_of_empty_success(self):
        self.page.eval = Mock(side_effect=RuntimeError("CDP disconnected"))
        with self.assertRaisesRegex(RuntimeError, "CDP disconnected"):
            self.scan()

    def test_collect_javascript_extracts_author_from_card(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not available for JavaScript DOM fixture")
        self.page.scripts.clear()
        scanner.collect(self.page)
        js = self.page.scripts[-1]
        fixture = """
const img = {getAttribute: () => '封面标题'};
const author = {innerText: '@作者乙'};
const card = {querySelector: () => author};
const a = {closest: () => card, getAttribute: () => '/video/1', innerText: '7938 视频标题',
  getBoundingClientRect: () => ({height: 279}), querySelector: () => img};
const document = {querySelectorAll: () => [a]};
"""
        result = subprocess.run([node, "-e", fixture + "console.log(JSON.stringify(" + js + "))"],
                                capture_output=True, text=True, check=True, encoding="utf-8")
        row = json.loads(result.stdout)[0]
        self.assertEqual(row["author"], "作者乙")
        self.assertEqual(row["t"], "视频标题")
        self.assertEqual(row["h"], "/video/1")

    def test_collect_javascript_falls_back_to_cover_alt_for_author(self):
        """收藏夹卡片没有用户链接，作者只写在封面 alt 的「作者：标题」前缀里。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not available for JavaScript DOM fixture")
        self.page.scripts.clear()
        scanner.collect(self.page)
        js = self.page.scripts[-1]
        fixture = """
const img = {getAttribute: () => '白泽AI：把收藏夹里视频自动搬进你知识库skill #我要上热门'};
const card = {querySelector: () => null};
const a = {closest: () => card, getAttribute: () => '/video/3',
  innerText: '1.2万 把收藏夹里视频自动搬进你知识库skill',
  getBoundingClientRect: () => ({height: 279}), querySelector: () => img};
const document = {querySelectorAll: () => [a]};
"""
        result = subprocess.run([node, "-e", fixture + "console.log(JSON.stringify(" + js + "))"],
                                capture_output=True, text=True, check=True, encoding="utf-8")
        row = json.loads(result.stdout)[0]
        self.assertEqual(row["author"], "白泽AI")
        self.assertEqual(row["t"], "把收藏夹里视频自动搬进你知识库skill")

    def test_collect_javascript_skips_sidebar_recommendations_without_cover(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not available for JavaScript DOM fixture")
        self.page.scripts.clear()
        scanner.collect(self.page)
        js = self.page.scripts[-1]
        fixture = """
const card = {querySelector: () => null};
const a = {closest: () => card, getAttribute: () => '/video/2', innerText: '换工作也能休年假',
  getBoundingClientRect: () => ({height: 17}), querySelector: () => null};
const document = {querySelectorAll: () => [a]};
"""
        result = subprocess.run([node, "-e", fixture + "console.log(JSON.stringify(" + js + "))"],
                                capture_output=True, text=True, check=True, encoding="utf-8")
        self.assertEqual(json.loads(result.stdout), [])

    def test_selected_label_requires_local_evidence_and_rejects_inactive(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not available for JavaScript DOM fixture")
        fixture = """
let classes = ['inactive'];
const e = {children: [], textContent: '有帮助', getClientRects: () => [1],
  getAttribute: () => null, closest: () => null, get classList() {return classes;}};
const document = {querySelectorAll: () => [e]};
const getComputedStyle = () => ({visibility: 'visible'});
"""
        code = fixture + "console.log(JSON.stringify([(" + scanner.SELECTED_JS + ")('有帮助')," + "(classes=['selected'], (" + scanner.SELECTED_JS + ")('有帮助'))]))"
        result = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True, encoding="utf-8")
        self.assertEqual(json.loads(result.stdout), [False, True])

    def test_zero_scroll_height_is_not_success(self):
        self.page.height = 0
        with self.assertRaisesRegex(RuntimeError, "扫描不完整"):
            self.scan()

    def test_login_word_in_video_body_does_not_trigger_manual(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node not available for JavaScript DOM fixture")
        fixture = """
const document = {title: '抖音', body: {innerText: '登录教程 手机号登录'}, querySelectorAll: () => []};
const location = {href: 'https://www.douyin.com/user/self?showTab=favorite_collection'};
"""
        result = subprocess.run([node, "-e", fixture + "console.log(JSON.stringify(" + scanner.MANUAL_JS + "))"],
                                capture_output=True, text=True, check=True, encoding="utf-8")
        self.assertEqual(json.loads(result.stdout), "")


if __name__ == "__main__":
    unittest.main()
