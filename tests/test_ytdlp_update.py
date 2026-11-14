"""yt-dlp 自动更新这层的测试：不联网，wheel 用临时 zip 顶替。"""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import ytdlp_update  # noqa: E402


def make_wheel(folder: Path, marker: str = "x") -> Path:
    """造一个只有 yt_dlp/version.py 的假 wheel。"""
    wheel = folder / "yt_dlp-2099.1.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "")
        archive.writestr("yt_dlp/version.py", f"__version__ = '{marker}'\n")
        archive.writestr("yt_dlp-2099.1.1.dist-info/METADATA", "Name: yt-dlp\n")
    return wheel


class VersionTests(unittest.TestCase):
    def test_version_tuple_parses_dates(self):
        self.assertEqual(ytdlp_update.version_tuple("2026.10.26"), (2026, 10, 26))

    def test_version_tuple_tolerates_junk(self):
        self.assertEqual(ytdlp_update.version_tuple("nightly.2026.10.26.dev0"), (0, 2026, 10, 26, 0))

    def test_empty_version_is_empty_tuple(self):
        self.assertEqual(ytdlp_update.version_tuple(None), ())
        self.assertEqual(ytdlp_update.version_tuple(""), ())

    def test_newer_by_tuple_not_lexical(self):
        # 字典序会把 "2026.9.9" 排到 "2026.10.26" 后面，按数字比才对
        self.assertTrue(ytdlp_update.is_newer("2026.10.26", "2026.9.9"))
        self.assertFalse(ytdlp_update.is_newer("2026.9.9", "2026.10.26"))

    def test_missing_current_means_anything_is_newer(self):
        self.assertTrue(ytdlp_update.is_newer("2026.10.26", None))


class LatestReleaseTests(unittest.TestCase):
    def test_picks_the_universal_wheel(self):
        payload = {
            "info": {"version": "2026.11.1"},
            "releases": {
                "2026.11.1": [
                    {"filename": "yt_dlp-2026.11.1.tar.gz", "url": "https://x/src"},
                    {"filename": "yt_dlp-2026.11.1-py3-none-any.whl", "url": "https://x/wheel"},
                ]
            },
        }
        version, url = ytdlp_update.latest_release(fetch=lambda _u: payload)
        self.assertEqual(version, "2026.11.1")
        self.assertEqual(url, "https://x/wheel")

    def test_raises_when_no_wheel_is_published(self):
        payload = {"info": {"version": "1.0"}, "releases": {"1.0": [{"filename": "a.tar.gz", "url": "u"}]}}
        with self.assertRaises(ValueError):
            ytdlp_update.latest_release(fetch=lambda _u: payload)

    def test_raises_when_version_missing(self):
        with self.assertRaises(ValueError):
            ytdlp_update.latest_release(fetch=lambda _u: {"info": {}, "releases": {}})


class InstallTests(unittest.TestCase):
    def test_wheel_replaces_the_package_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wheel = make_wheel(tmp, marker="new")
            target = tmp / "lib"
            (target / "yt_dlp").mkdir(parents=True)
            (target / "yt_dlp" / "version.py").write_text("__version__ = 'old'\n", encoding="utf-8")

            ytdlp_update.install_wheel("https://x/wheel", target=target,
                                       download=lambda _u, dest: shutil_copy(wheel, dest))
            self.assertIn("new", (target / "yt_dlp" / "version.py").read_text(encoding="utf-8"))
            self.assertFalse((target / ".yt_dlp.old").exists())

    def test_install_creates_target_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            wheel = make_wheel(tmp)
            target = tmp / "fresh" / "lib"
            ytdlp_update.install_wheel("https://x/wheel", target=target,
                                       download=lambda _u, dest: shutil_copy(wheel, dest))
            self.assertTrue((target / "yt_dlp" / "__init__.py").is_file())

    def test_install_without_any_target_is_an_error(self):
        with mock.patch.object(ytdlp_update, "site_dir", return_value=None):
            with self.assertRaises(RuntimeError):
                ytdlp_update.install_wheel("https://x/wheel")


class MaybeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "ytdlp_update.json"
        patcher = mock.patch.object(ytdlp_update, "state_path", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_skips_when_checked_recently(self):
        self.state.write_text(json.dumps({"checked_at": __import__("time").time()}), encoding="utf-8")
        result = ytdlp_update.maybe_update()
        self.assertFalse(result["checked"])
        self.assertEqual(result["skipped"], "recently-checked")

    def test_force_ignores_the_time_window(self):
        self.state.write_text(json.dumps({"checked_at": __import__("time").time()}), encoding="utf-8")
        with mock.patch.object(ytdlp_update, "latest_release", return_value=("2026.10.26", "u")), \
             mock.patch.object(ytdlp_update, "local_version", return_value="2026.10.26"):
            result = ytdlp_update.maybe_update(force=True)
        self.assertTrue(result["checked"])
        self.assertFalse(result["updated"])

    def test_update_path_installs_and_reports(self):
        calls = []
        with mock.patch.object(ytdlp_update, "latest_release", return_value=("2026.11.1", "https://x/w")), \
             mock.patch.object(ytdlp_update, "local_version", return_value="2026.10.26"), \
             mock.patch.object(ytdlp_update, "install_wheel", side_effect=lambda url: calls.append(url)):
            result = ytdlp_update.maybe_update(force=True)
        self.assertEqual(calls, ["https://x/w"])
        self.assertTrue(result["updated"])
        self.assertEqual(result["to"], "2026.11.1")

    def test_network_failure_is_reported_not_raised(self):
        with mock.patch.object(ytdlp_update, "latest_release", side_effect=OSError("断网")):
            result = ytdlp_update.maybe_update(force=True)
        self.assertTrue(result["checked"])
        self.assertFalse(result["updated"])
        self.assertIn("断网", result["error"])
        self.assertIn("失败", ytdlp_update.render(result))

    def test_state_file_records_latest_version(self):
        with mock.patch.object(ytdlp_update, "latest_release", return_value=("2026.11.1", "u")), \
             mock.patch.object(ytdlp_update, "local_version", return_value="2026.11.1"), \
             mock.patch.object(ytdlp_update, "install_wheel"):
            ytdlp_update.maybe_update(force=True)
        saved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(saved["latest"], "2026.11.1")
        self.assertIn("checked_at", saved)

    def test_fresh_enough_borders(self):
        self.state.write_text(json.dumps({}), encoding="utf-8")
        self.assertFalse(ytdlp_update.fresh_enough())
        import time as _time

        self.state.write_text(json.dumps({"checked_at": _time.time() - 25 * 3600}), encoding="utf-8")
        self.assertFalse(ytdlp_update.fresh_enough())
        self.state.write_text(json.dumps({"checked_at": _time.time() - 3600}), encoding="utf-8")
        self.assertTrue(ytdlp_update.fresh_enough())


class RenderTests(unittest.TestCase):
    def test_render_covers_all_three_shapes(self):
        self.assertIn("最新", ytdlp_update.render({"from": "1.0", "to": "1.0"}))
        self.assertIn("已更新", ytdlp_update.render({"updated": True, "from": "1.0", "to": "2.0"}))
        self.assertIn("跳过", ytdlp_update.render({"skipped": "recently-checked"}))


def shutil_copy(src: Path, dest: Path) -> None:
    import shutil

    shutil.copyfile(src, dest)


if __name__ == "__main__":
    unittest.main()
