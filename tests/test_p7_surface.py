import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "app"))

import server  # noqa: E402


class P7SurfaceTests(unittest.TestCase):
    def test_preflight_reports_missing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "videos"
            base.mkdir()
            config = {
                "cookies": str(Path(tmp) / "cookies.txt"),
                "vault_path": str(Path(tmp) / "vault"),
                "base_dir": str(base),
                "notify_receive_id": "",
                "ai_api_key": "",
            }
            out = server.preflight(config)
        self.assertEqual(out["missing"], ["cookies", "vault", "notify", "ai"])
        by_key = {item["key"]: item for item in out["items"]}
        self.assertTrue(by_key["base"]["ok"])
        self.assertFalse(by_key["cookies"]["ok"])
        self.assertNotIn("cookies.txt", by_key["ai"]["hint"])

    def test_preflight_does_not_echo_the_ai_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "cookies.txt").write_text("# netscape", encoding="utf-8")
            vault = base / "vault"
            vault.mkdir()
            out = server.preflight({
                "cookies": str(base / "cookies.txt"),
                "vault_path": str(vault),
                "base_dir": str(base),
                "notify_receive_id": "ou_test",
                "ai_api_key": "sk-should-not-leak",
            })
        self.assertEqual(out["missing"], [])
        blobs = str(out)
        self.assertNotIn("sk-should-not-leak", blobs)
        self.assertIn("已保存", blobs)

    def test_branded_404_page_is_self_contained(self):
        page = server.NOT_FOUND_HTML
        self.assertIn("404", page)
        self.assertIn("回到控制台", page)
        self.assertIn('href="/"', page)
        self.assertNotIn("http://", page)
        self.assertNotIn("cdn", page.lower())


if __name__ == "__main__":
    unittest.main()
