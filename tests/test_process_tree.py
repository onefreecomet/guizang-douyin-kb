"""Real offline Windows descendant cleanup; no browser or cloud calls."""
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from process_tree import ProcessTree


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object regression')
class ProcessTreeTests(unittest.TestCase):
    def run_tree(self, close_only=False):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        marker = ROOT / 'tests' / '.runs' / (uuid.uuid4().hex + '.pid')
        marker.parent.mkdir(exist_ok=True)
        tree = ProcessTree([sys.executable, str(ROOT / 'tests' / 'process_fixture.py'), str(marker)],
                           env=os.environ, cwd=ROOT)
        handle = None
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                if tree.proc.poll() is not None:
                    self.fail('Worker exited before launching child: ' + tree.proc.stdout.read())
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'worker never created child marker')
            child_pid = int(marker.read_text(encoding='ascii'))
            handle = kernel.OpenProcess(0x100000, False, child_pid)  # SYNCHRONIZE
            self.assertTrue(handle, 'child should be running')
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
            if close_only:
                tree.close()
            else:
                tree.terminate()
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0, 'descendant survived cleanup')
            tree.proc.wait(timeout=5)
        finally:
            tree.close()
            if tree.proc.poll() is None:
                tree.proc.kill(); tree.proc.wait()
            tree.proc.stdout.close()
            if handle:
                kernel.CloseHandle(handle)
            assert marker.resolve().parent == (ROOT / 'tests' / '.runs').resolve()
            marker.unlink(missing_ok=True)

    def test_worker_has_no_console_window(self):
        code = ('import sys, ctypes; '
                'assert sys.stdin.readline().strip() == "GO"; '
                'print(ctypes.windll.kernel32.GetConsoleWindow())')
        tree = ProcessTree([sys.executable, '-c', code], env=os.environ, cwd=ROOT)
        try:
            stdout, _ = tree.proc.communicate(timeout=10)
            self.assertEqual(tree.proc.returncode, 0, stdout)
            self.assertEqual(stdout.strip(), '0', 'worker created a console window')
        finally:
            if tree.proc.poll() is None:
                tree.terminate()
            tree.close()

    def test_cmd_command_has_no_console_and_captures_output(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        from command_runner import run_command
        batch = ROOT / 'tests' / '.runs' / (uuid.uuid4().hex + '.cmd')
        batch.parent.mkdir(exist_ok=True)
        batch.write_text('@echo off\n"%~1" -c "import ctypes,sys; print(ctypes.windll.kernel32.GetConsoleWindow()); print(\'stderr-ok\',file=sys.stderr)"\n', encoding='ascii')
        try:
            result = run_command([str(batch), sys.executable], timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '0', 'cmd target created a console window')
            self.assertEqual(result.stderr.strip(), 'stderr-ok')
        finally:
            assert batch.resolve().parent == (ROOT / 'tests' / '.runs').resolve()
            batch.unlink(missing_ok=True)

    def test_stop_terminates_worker_and_child(self):
        self.run_tree()

    def test_owner_handle_close_kills_worker_and_child(self):
        self.run_tree(close_only=True)

    def test_command_timeout_kills_cmd_and_grandchild(self):
        import ctypes
        import subprocess
        from ctypes import wintypes
        sys.path.insert(0, str(ROOT / 'scripts'))
        from command_runner import run_command
        marker = ROOT / 'tests' / '.runs' / (uuid.uuid4().hex + '.pid')
        batch = marker.with_suffix('.cmd')
        batch.write_text('@echo off\n"%~1" "%~dp0..\\process_fixture.py" "%~2"\n', encoding='ascii')
        started = time.monotonic()
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([str(batch), sys.executable, str(marker)], timeout=2)
            self.assertLess(time.monotonic() - started, 8)
            self.assertTrue(marker.exists(), 'cmd must start descendant before timeout')
            pid = int(marker.read_text(encoding='ascii'))
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
                finally:
                    kernel.CloseHandle(handle)
        finally:
            assert marker.resolve().parent == (ROOT/'tests'/'.runs').resolve()
            marker.unlink(missing_ok=True)
            batch.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
