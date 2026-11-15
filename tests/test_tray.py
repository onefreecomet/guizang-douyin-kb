"""托盘进程级回归：11-14 那次 `TIMER_ID` NameError 不该再回来。

这些用例只碰纯函数与 mock，不会真的建窗口、不会真的起进程。
"""
import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

APP = ROOT / "app"


class TraySourceTests(unittest.TestCase):
    """不导入托盘也能查的静态检查：模块顶层的名字必须真的定义了。"""

    def _bound_names(self, target) -> set:
        """`A, B = 1, 2` 这种元组解包也要算作定义——11-14 那次 NameError 正是漏看这里。"""
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            names = set()
            for item in target.elts:
                names |= self._bound_names(item)
            return names
        if isinstance(target, ast.Starred):
            return self._bound_names(target.value)
        return set()

    def _module_globals(self) -> set:
        tree = ast.parse((APP / "tray.py").read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    names |= self._bound_names(target)
            elif isinstance(node, ast.AnnAssign):
                names |= self._bound_names(node.target)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        return names

    def test_settimer_references_a_defined_timer_id(self):
        globals_ = self._module_globals()
        self.assertIn("TIMER_ID", globals_, "TIMER_ID 必须定义在模块顶层")
        source = (APP / "tray.py").read_text(encoding="utf-8")
        self.assertIn("user32.SetTimer(self.hwnd, TIMER_ID, POLL_MS, None)", source)
        self.assertIn("POLL_MS", globals_)

    def test_constants_used_by_the_menu_and_balloon_exist(self):
        globals_ = self._module_globals()
        for name in ("WM_TRAYICON", "WM_TIMER", "ID_OPEN", "ID_BROWSER", "ID_ABOUT",
                     "ID_QUIT_SERVICE", "ID_EXIT", "NIF_INFO", "MUTEX_NAME", "BALLOONS"):
            self.assertIn(name, globals_, name)

    def test_every_module_compiles(self):
        for path in sorted(APP.glob("*.py")):
            with self.subTest(module=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@unittest.skipUnless(os.name == "nt", "托盘只跑在 Windows 上")
class TrayBehaviourTests(unittest.TestCase):
    def setUp(self):
        import tray

        self.tray = tray

    def test_single_instance_short_circuits_before_touching_the_service(self):
        with mock.patch.object(self.tray, "acquire_mutex", return_value=None), \
                mock.patch.object(self.tray, "ensure_server") as ensure:
            self.assertEqual(self.tray.main(), 0)
        ensure.assert_not_called()

    def test_ensure_tray_skips_when_a_tray_is_already_running(self):
        with mock.patch.object(self.tray, "already_running", return_value=True), \
                mock.patch.object(self.tray.subprocess, "Popen") as popen:
            self.assertFalse(self.tray.ensure_tray())
        popen.assert_not_called()

    def test_ensure_tray_spawns_pythonw_without_a_console(self):
        with mock.patch.object(self.tray, "already_running", return_value=False), \
                mock.patch.object(self.tray.subprocess, "Popen") as popen:
            self.assertTrue(self.tray.ensure_tray())
        popen.assert_called_once()
        cmd = popen.call_args[0][0]
        self.assertTrue(str(cmd[0]).lower().endswith(("pythonw.exe", "python.exe")))
        self.assertTrue(str(cmd[1]).endswith("tray.py"))
        self.assertTrue(popen.call_args[1]["creationflags"])

    def test_quit_service_uses_the_same_api_as_the_console_button(self):
        with mock.patch.object(self.tray.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__ = mock.Mock()
            opened.return_value.__enter__.return_value.status = 200
            opens = self.tray.quit_service()
        self.assertTrue(opens)
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, f"http://127.0.0.1:{self.tray.PORT}/api/quit")
        self.assertEqual(request.method, "POST")

    def test_watchdog_spawned_tray_never_undoes_a_quit(self):
        """守护补拉托盘时若用户刚点过「退出服务」，托盘必须既不清标记也不拉服务。"""
        import paths

        marker = self._marker()
        marker.write_text("用户退出服务", encoding="utf-8")
        try:
            with mock.patch.dict(os.environ, {self.tray.SPAWN_ENV: "1"}), \
                    mock.patch.object(self.tray, "acquire_mutex", return_value=object()), \
                    mock.patch.object(self.tray, "ensure_server") as ensure, \
                    mock.patch.object(paths, "pause_marker", return_value=marker):
                self.assertEqual(self.tray.main(), 0)
            ensure.assert_not_called()
            self.assertTrue(marker.exists(), "用户主动退出不能被托盘悄悄撤销")
        finally:
            marker.unlink(missing_ok=True)

    def test_user_started_tray_clears_the_marker_before_serving(self):
        """用户亲手拉起托盘 = 希望服务回来：先清标记，再确保服务。"""
        import paths

        marker = self._marker()
        marker.write_text("用户退出服务", encoding="utf-8")
        try:
            env = {k: v for k, v in os.environ.items() if k != self.tray.SPAWN_ENV}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(self.tray, "acquire_mutex", return_value=object()), \
                    mock.patch.object(self.tray, "ensure_server") as ensure, \
                    mock.patch.object(paths, "pause_marker", return_value=marker):
                self.tray.main()
        finally:
            marker.unlink(missing_ok=True)
        ensure.assert_called_once()

    def _marker(self):
        runs = ROOT / "tests" / ".runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"pause-{os.getpid()}-{id(self)}.marker"
        path.unlink(missing_ok=True)
        return path


if __name__ == "__main__":
    unittest.main()
