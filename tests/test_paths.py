"""A 阶段（自助安装）的回归测试：路径解析、快捷方式生成、体检报告。

这些测试不碰真实环境：全部用临时目录 + mock，跑在任何机器上结果都一样。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import create_shortcut  # noqa: E402
import doctor  # noqa: E402
import paths  # noqa: E402


class DataDirTests(unittest.TestCase):
    def test_default_data_dir_sits_next_to_the_code(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.ENV_HOME, None)
            self.assertEqual(paths.data_dir(), paths.INSTALL_DIR / "work")

    def test_home_override_moves_data_dir(self):
        with mock.patch.dict(os.environ, {paths.ENV_HOME: r"D:\guizang-data"}):
            self.assertEqual(paths.data_dir(), Path(r"D:\guizang-data"))

    def test_app_dir_is_the_folder_holding_this_module(self):
        self.assertEqual(paths.APP_DIR, ROOT / "app")
        self.assertEqual(paths.INSTALL_DIR, ROOT)


class NodeResolutionTests(unittest.TestCase):
    def test_bundled_node_beats_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bundled = bin_dir / "node" / "node.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"MZ")
            with mock.patch.object(paths, "BIN_DIR", bin_dir), \
                 mock.patch.object(paths, "_newest_node_version_dir", return_value=None), \
                 mock.patch.object(paths.shutil, "which", return_value=r"C:\somewhere\node.exe"):
                self.assertEqual(paths.find_node(), bundled)

    def test_env_var_points_at_a_specific_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "node.exe"
            exe.write_bytes(b"MZ")
            with mock.patch.dict(os.environ, {paths.ENV_NODE: str(exe)}):
                self.assertEqual(paths.find_node(), exe)

    def test_falls_back_to_path_when_bundled_copy_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "BIN_DIR", Path(tmp) / "bin"), \
                 mock.patch.object(paths, "_newest_node_version_dir", return_value=None), \
                 mock.patch.object(paths.shutil, "which", return_value=r"C:\node\node.exe"):
                os.environ.pop(paths.ENV_NODE, None)
                self.assertEqual(paths.find_node(), Path(r"C:\node\node.exe"))

    def test_nothing_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "BIN_DIR", Path(tmp) / "bin"), \
                 mock.patch.object(paths, "_newest_node_version_dir", return_value=None), \
                 mock.patch.object(paths.shutil, "which", return_value=None):
                os.environ.pop(paths.ENV_NODE, None)
                self.assertIsNone(paths.find_node())

    def test_legacy_version_dirs_sort_numerically_not_lexically(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("9.0.0", "20.11.0", "22.22.2-6"):
                (base / name).mkdir()
                (base / name / "node.exe").write_bytes(b"MZ")
            newest = paths._newest_node_version_dir(base)
            self.assertEqual(newest.name, "22.22.2-6")

    def test_empty_version_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(paths._newest_node_version_dir(Path(tmp) / "nope"))


class LarkResolutionTests(unittest.TestCase):
    def test_lark_next_to_legacy_node_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            node_dir = Path(tmp) / "node-ver"
            node_dir.mkdir()
            lark = node_dir / "lark-cli.cmd"
            lark.write_text("@echo off\n", encoding="utf-8")
            with mock.patch.object(paths, "BIN_DIR", Path(tmp) / "bin"), \
                 mock.patch.object(paths, "find_node", return_value=node_dir / "node.exe"), \
                 mock.patch.object(paths.shutil, "which", return_value=None):
                os.environ.pop(paths.ENV_LARK, None)
                self.assertEqual(paths.find_lark(), str(lark))

    def test_lark_in_legacy_workbuddy_dir_is_found_when_node_is_bundled(self):
        """我们自己带了 bin/node 之后，node 旁边没有 lark-cli；旧环境里那份仍要被找到。"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            ver = home / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2-6"
            ver.mkdir(parents=True)
            (ver / "node.exe").write_bytes(b"\0")
            lark = ver / "lark-cli.cmd"
            lark.write_text("@echo off\n", encoding="utf-8")
            bundled = Path(tmp) / "bin" / "node"
            bundled.mkdir(parents=True)
            (bundled / "node.exe").write_bytes(b"\0")
            with mock.patch.object(paths, "BIN_DIR", Path(tmp) / "bin"), \
                 mock.patch.object(paths.Path, "home", return_value=home), \
                 mock.patch.object(paths.shutil, "which", return_value=None):
                os.environ.pop(paths.ENV_LARK, None)
                self.assertEqual(paths.find_node(), bundled / "node.exe")
                self.assertEqual(paths.find_lark(), str(lark))

    def test_missing_lark_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "BIN_DIR", Path(tmp) / "bin"), \
                 mock.patch.object(paths.Path, "home", return_value=Path(tmp) / "nohome"), \
                 mock.patch.object(paths, "find_node", return_value=None), \
                 mock.patch.object(paths.shutil, "which", return_value=None):
                os.environ.pop(paths.ENV_LARK, None)
                self.assertIsNone(paths.find_lark())
                ok, detail = paths.probe_lark()
                self.assertFalse(ok)
                self.assertEqual(detail, "未找到")


class ShortcutTests(unittest.TestCase):
    def test_desktop_shortcut_targets_pythonw_and_launcher(self):
        spec = create_shortcut.shortcut_spec("desktop")
        self.assertTrue(spec["path"].name.endswith(".lnk"))
        self.assertTrue(spec["target"].lower().endswith(("pythonw.exe", "python.exe")))
        self.assertIn("launcher.py", spec["arguments"])
        self.assertEqual(spec["workdir"], str(paths.APP_DIR))
        self.assertEqual(spec["window_style"], 7)

    def test_startup_shortcut_runs_the_watchdog(self):
        spec = create_shortcut.shortcut_spec("startup")
        self.assertIn("watchdog.py", spec["arguments"])
        self.assertIn("Startup", str(spec["path"]))
        self.assertIn("开机自启", spec["description"])

    def test_ps_script_escapes_single_quotes(self):
        spec = {
            "path": Path(r"C:\Users\O'Brien\Desktop\x.lnk"),
            "target": r"C:\py\pythonw.exe",
            "arguments": r'"C:\it'"'"'s\launcher.py"',
            "workdir": r"C:\app",
            "icon": "",
            "description": "归藏",
            "window_style": 7,
        }
        script = create_shortcut.ps_script(spec)
        self.assertIn("O''Brien", script)
        self.assertIn("$sc.Save()", script)
        # 每个字符串值都要被单引号包住，否则路径里的空格会把参数拆开
        for line in script.splitlines():
            if "$sc." in line and "= '" in line:
                self.assertTrue(line.rstrip().rstrip(";").endswith("'"), line)

    def test_ps_script_skips_empty_icon(self):
        spec = create_shortcut.shortcut_spec("desktop")
        spec["icon"] = ""
        self.assertNotIn("IconLocation", create_shortcut.ps_script(spec))


class DoctorTests(unittest.TestCase):
    def _stub_probes(self):
        patcher = mock.patch.multiple(
            paths,
            probe_node=mock.Mock(return_value=(True, "v22.22.2")),
            probe_ytdlp=mock.Mock(return_value=(True, "2026.10.26")),
            probe_lark=mock.Mock(return_value=(True, "1.2.3")),
            find_node=mock.Mock(return_value=Path(r"C:\app\bin\node\node.exe")),
            find_edge=mock.Mock(return_value=Path(r"C:\edge\msedge.exe")),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_green_when_everything_is_present(self):
        self._stub_probes()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cookies = base / "cookies.txt"
            cookies.write_text("# netscape", encoding="utf-8")
            vault = base / "vault"
            vault.mkdir()
            cfg = {
                "cookies": str(cookies),
                "vault_path": str(vault),
                "base_dir": str(base),
                "ai_api_key": "sk-test-1234567890",
                "ai_model": "deepseek-v4.1-flash",
                "notify_receive_id": "ou_x",
                "notify_enabled": True,
            }
            with mock.patch.object(paths, "data_dir", return_value=base):
                rows = doctor.checks(cfg)
                text = doctor.render(rows)
                # main() 会自己读 config.json，写一份进去验证退出码
                (base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    code = doctor.main(["--json"])
        states = {key: state for key, _, state, _ in rows}
        self.assertEqual(states["cookies"], "ok")
        self.assertEqual(states["vault"], "ok")
        self.assertEqual(states["node"], "ok")
        self.assertIn("关键项全部通过", text)
        self.assertEqual(code, 0)

    def test_missing_cookies_and_vault_are_critical(self):
        self._stub_probes()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = {
                "cookies": str(base / "nope.txt"),
                "vault_path": str(base / "no-vault"),
                "base_dir": str(base),
            }
            with mock.patch.object(paths, "data_dir", return_value=base):
                rows = doctor.checks(cfg)
                text = doctor.render(rows)
        states = {key: state for key, _, state, _ in rows}
        self.assertEqual(states["cookies"], "bad")
        self.assertEqual(states["vault"], "bad")
        self.assertIn("还有 2 个关键项没过", text)

    def test_optional_gaps_never_fail_the_run(self):
        self._stub_probes()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cookies = base / "cookies.txt"
            cookies.write_text("# netscape", encoding="utf-8")
            vault = base / "vault"
            vault.mkdir()
            cfg = {"cookies": str(cookies), "vault_path": str(vault), "base_dir": str(base)}
            with mock.patch.object(paths, "data_dir", return_value=base):
                rows = doctor.checks(cfg)
                text = doctor.render(rows)
        states = {key: state for key, _, state, _ in rows}
        self.assertEqual(states["ai_key"], "skip")
        self.assertEqual(states["notify"], "skip")
        self.assertIn("可选功能未启用", text)

    def test_ai_key_is_masked_in_output(self):
        cfg = {"ai_api_key": "sk-amr-supersecretvalue", "ai_model": "m"}
        rows = doctor.checks(cfg)
        detail = next(d for k, _, _, d in rows if k == "ai_key")
        self.assertNotIn("supersecretvalue", detail)
        self.assertIn("sk-a", detail)

    def test_render_uses_three_states(self):
        rows = [
            ("python", "Python", "ok", "3.13"),
            ("cookies", "抖音 cookies", "bad", "没有"),
            ("lark", "飞书妙记", "skip", "没装"),
        ]
        text = doctor.render(rows)
        for mark in (doctor.OK, doctor.BAD, doctor.SKIP):
            self.assertIn(mark, text)


if __name__ == "__main__":
    unittest.main()
