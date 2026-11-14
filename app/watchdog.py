"""Keep the local console service alive: start it on boot and restart it after a crash.

Design notes:
- The watchdog is deliberately tiny and dependency free. It only speaks HTTP to
  the local service, so a wedged or crashed server is detected by a timeout.
- A user-requested quit writes ``work/watchdog.pause``; the watchdog then stays
  quiet until the next manual start deletes that file. Without this marker the
  "退出服务" button would fight the watchdog forever.
- Single instance is enforced with an exclusive lock on ``work/watchdog.lock``.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SKILL_DIR = APP_DIR.parent
try:  # 数据目录统一由 paths 决定（GUIZANG_HOME 可改），拿不到就退回老位置
    import paths as _paths

    SKILL_DIR = _paths.INSTALL_DIR
    WORK_DIR = _paths.data_dir()
except Exception:  # pragma: no cover - 只在 paths 坏掉时走
    WORK_DIR = SKILL_DIR / 'work'
LOG_PATH = WORK_DIR / 'watchdog.log'
LOCK_PATH = WORK_DIR / 'watchdog.lock'
PAUSE_PATH = WORK_DIR / 'watchdog.pause'
LOG_LIMIT = 200 * 1024

DEFAULT_PORT = 8765
DEFAULT_INTERVAL = 30
FAILURES_BEFORE_RESTART = 2
RESTART_GRACE = 20
# 托盘被杀之后要能自己回来：每 N 轮检查一次（约 10 分钟）
TRAY_CHECK_TICKS = 20
# yt-dlp 更新：ytdlp_update 自己按 24 小时节流，这里只是"多久问一次"
UPDATE_CHECK_TICKS = 60


def log(message):
    """Append one timestamped line, rotating the file once it grows too large."""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_LIMIT:
            LOG_PATH.replace(LOG_PATH.with_suffix('.log.1'))
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with LOG_PATH.open('a', encoding='utf-8') as fh:
            fh.write(f'[{stamp}] {message}\n')
    except OSError:
        pass


def probe(port=DEFAULT_PORT, timeout=6):
    """Return the ping payload, or None when the service is down or unresponsive."""
    url = f'http://127.0.0.1:{port}/api/ping'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode('utf-8'))
            return body if isinstance(body, dict) and body.get('ok') else None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def server_command(port=DEFAULT_PORT):
    """Prefer pythonw so a restart never flashes a console window."""
    try:
        import paths

        exe = paths.pythonw()
    except Exception:  # pragma: no cover
        exe = Path(sys.executable)
    windowless = Path(exe).with_name('pythonw.exe')
    launcher = windowless if windowless.exists() else Path(exe)
    return [str(launcher), str(APP_DIR / 'server.py'), '--port', str(port)]


def ensure_tray_quietly():
    """托盘没在跑就拉起来；失败不影响守护本体。"""
    try:
        from tray import ensure_tray

        ensure_tray()
    except Exception:
        pass


def check_ytdlp_update():
    """检查 yt-dlp 有没有新版（内部 24 小时节流，失败只记一行）。"""
    try:
        import ytdlp_update

        result = ytdlp_update.maybe_update()
        if result.get('updated') or result.get('error'):
            log('yt-dlp：' + ytdlp_update.render(result))
    except Exception as exc:  # pragma: no cover
        log(f'yt-dlp 更新检查异常：{type(exc).__name__}: {exc}')


def start_server(port=DEFAULT_PORT):
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(server_command(port), cwd=str(SKILL_DIR), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    log('服务未响应，已尝试重新启动：' + ' '.join(server_command(port)))


def acquire_lock():
    """Return a handle that keeps this process the only watchdog, or None."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open('a+')
    try:
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        handle.close()
        return None
    return handle


def tick(port=DEFAULT_PORT, failures=0, paused=False):
    """One health check. Returns (failures, restarted, paused)."""
    if PAUSE_PATH.exists():
        if not paused:
            log('检测到退出标记，暂停自动拉起；下次手动启动后恢复')
        return 0, False, True
    if paused:
        log('退出标记已清除，恢复守护')
        paused = False
    if probe(port):
        return 0, False, paused
    failures += 1
    log(f'服务未响应（连续 {failures} 次）')
    if failures < FAILURES_BEFORE_RESTART:
        return failures, False, paused
    start_server(port)
    return 0, True, paused


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = DEFAULT_PORT
    interval = DEFAULT_INTERVAL
    if '--port' in argv:
        port = int(argv[argv.index('--port') + 1])
    if '--interval' in argv:
        interval = max(5, int(argv[argv.index('--interval') + 1]))
    handle = acquire_lock()
    if handle is None:
        log('已有守护进程在运行，退出')
        return 0
    log(f'守护启动：端口 {port}，检查间隔 {interval} 秒')
    ensure_tray_quietly()
    check_ytdlp_update()
    failures, paused = 0, False
    rounds = 0
    try:
        while True:
            failures, restarted, paused = tick(port, failures, paused)
            rounds += 1
            if rounds % TRAY_CHECK_TICKS == 0:
                ensure_tray_quietly()
            if rounds % UPDATE_CHECK_TICKS == 0:
                check_ytdlp_update()
            time.sleep(RESTART_GRACE if restarted else interval)
    except KeyboardInterrupt:
        log('守护收到退出信号')
    finally:
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        handle.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
