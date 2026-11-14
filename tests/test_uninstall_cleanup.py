"""卸载清理的挑选逻辑：只杀属于本安装目录的进程，别误伤用户别的 python。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import uninstall_cleanup  # noqa: E402


def row(pid, cmd):
    return {"ProcessId": pid, "ParentProcessId": 1, "CommandLine": cmd}


class SelectTests(unittest.TestCase):
    def test_only_processes_under_the_install_dir(self):
        install = Path(r"C:\Users\x\Programs\Guizang")
        rows = [
            row(11, r'"C:\Users\x\Programs\Guizang\runtime\python\pythonw.exe" app\tray.py'),
            row(12, r"C:\Windows\py.exe C:\tools\other\script.py"),
            row(13, r'"C:\Users\x\Programs\Guizang\runtime\python\pythonw.exe" app\watchdog.py --interval 30'),
        ]
        self.assertEqual(uninstall_cleanup.select_targets(rows, install), [11, 13])

    def test_case_insensitive_match(self):
        install = Path(r"C:\Users\X\Programs\Guizang")
        rows = [row(21, r'"c:\users\x\programs\guizang\runtime\python\pythonw.exe" app\tray.py')]
        self.assertEqual(uninstall_cleanup.select_targets(rows, install), [21])

    def test_own_pid_and_callers_are_never_killed(self):
        install = Path(r"C:\App")
        rows = [row(31, r"C:\App\runtime\python\python.exe app\uninstall_cleanup.py"),
                row(32, r"C:\App\runtime\python\python.exe app\server.py")]
        self.assertEqual(
            uninstall_cleanup.select_targets(rows, install, own_pid=31, exclude=[32]), [])

    def test_bad_rows_are_skipped(self):
        install = Path(r"C:\App")
        rows = [
            {"ProcessId": "not-an-int", "CommandLine": r"C:\App\runtime\python\python.exe x"},
            {"ProcessId": 41, "CommandLine": None},
            {"CommandLine": r"C:\App\runtime\python\python.exe app\server.py"},
        ]
        self.assertEqual(uninstall_cleanup.select_targets(rows, install), [])

    def test_empty_input_is_fine(self):
        self.assertEqual(uninstall_cleanup.select_targets([], Path("C:/App")), [])
        self.assertEqual(uninstall_cleanup.select_targets(None, Path("C:/App")), [])


if __name__ == "__main__":
    unittest.main()
