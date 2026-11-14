"""归藏 · 右下角托盘：关掉窗口不等于停掉服务，点托盘图标随时叫回来。

设计要点：
- 纯标准库 ctypes，不引入 pystray / Pillow，运行目录零新增依赖。
- 单实例用命名互斥量；重复启动会立刻退出，不会出现两个图标。
- 服务死活沿用守护进程的判据（127.0.0.1 的 /api/ping），不另立一套。
- 托盘只做四件事：叫回窗口、看作者、退出服务、退出托盘；不改变服务行为。
- 「退出服务」走 /api/quit，与页面左下角按钮完全同一条路径，不会和守护打架。
"""
import ctypes
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from ctypes import wintypes as wt
from pathlib import Path

APP = Path(__file__).resolve().parent
PORT = int(os.environ.get("DY_PORT", "8765"))
TITLE = "归藏 · 抖音收藏知识库"
URL = f"http://127.0.0.1:{PORT}/"
MUTEX_NAME = "Local\\GuizangTrayIcon"

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B             # Win10/11 里托盘右键走这个，不是 WM_RBUTTONUP
WM_TRAYICON = 0x0400 + 20          # WM_APP + 20，托盘回调消息

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01

# 托盘气泡提示：用户明确要求不要系统弹窗，默认全关。
# 想恢复"缩到右下角""已在后台运行"这类提示，把这里改成 True。
BALLOONS = False
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
SW_RESTORE = 9
SM_CXSMICON, SM_CYSMICON = 49, 50
MF_STRING = 0x0000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
ID_OPEN, ID_BROWSER, ID_QUIT_SERVICE, ID_EXIT = 1, 2, 3, 4
ID_ABOUT = 5
TIMER_ID = 1
POLL_MS = 2000


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, wt.WPARAM, wt.LPCWSTR]
user32.TrackPopupMenu.restype = wt.UINT
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID]
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wt.HWND]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]


# ---------------------------------------------------------------- 服务与窗口

def server_alive() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def ensure_server():
    """服务没在跑就按守护进程同样的方式拉起来（pythonw，无黑窗）。"""
    if server_alive():
        return
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else exe
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(runner), str(APP / "server.py"), "--port", str(PORT)],
                     cwd=str(APP.parent), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def find_console():
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip() == TITLE:
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def activate_console() -> bool:
    """把已经开着的控制台窗口叫到前面；没有窗口返回 False。"""
    hwnd = find_console()
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def open_console():
    ensure_server()
    if activate_console():
        return True
    launcher = APP / "launcher.py"
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else exe
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(runner), str(launcher)], cwd=str(APP), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return False


def quit_service():
    """与页面左下角「退出服务」同一条路径：/api/quit 会写暂停标记再关服务。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/quit", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


# ---------------------------------------------------------------- 托盘本体

class Tray:
    def __init__(self):
        self.hwnd = None
        self.hicon = None
        self.was_open = False
        self.hinted = False
        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_TRAYICON
        self._wndproc = WNDPROC(self._on_message)

    # -- 消息处理
    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.was_open = True
                open_console()
            elif event == WM_RBUTTONUP:
                self._menu()
            return 0
        if msg == WM_CONTEXTMENU:
            self._menu()
            return 0
        if msg == WM_COMMAND:
            self._command(wparam & 0xFFFF)
            return 0
        if msg == WM_TIMER:
            self._poll()
            return 0
        if msg == WM_CLOSE:
            self._remove()
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _command(self, cmd):
        if cmd == ID_OPEN:
            self.was_open = True
            open_console()
        elif cmd == ID_BROWSER:
            os.startfile(URL)  # noqa: S606 - 只打开本机控制台
        elif cmd == ID_ABOUT:
            os.startfile(URL + "#help-author")  # noqa: S606 - 只打开本机控制台
        elif cmd == ID_QUIT_SERVICE:
            quit_service()
            self.was_open = False
        elif cmd == ID_EXIT:
            self._remove()
            user32.DestroyWindow(self.hwnd)

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开控制台")
        user32.AppendMenuW(menu, MF_STRING, ID_BROWSER, "在浏览器中打开")
        user32.AppendMenuW(menu, MF_STRING, ID_ABOUT, "关于归藏（by 冰临城下）")
        user32.AppendMenuW(menu, MF_STRING | 0x00000800, 0, None)  # 分隔线
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT_SERVICE, "退出服务（停止后台同步）")
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "只退出托盘")
        point = wt.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # 托盘弹出菜单前必须先把自己切到前台，否则点菜单外部不会收起
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                    point.x, point.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0x0000, 0, 0)
        if cmd:
            self._command(cmd)

    def _poll(self):
        """窗口被关掉时只记状态，不再弹系统气泡（用户要求别老弹窗）。"""
        hwnd = find_console()
        if self.was_open and not hwnd:
            self.was_open = False
            self._balloon("归藏已缩到右下角", "后台服务仍在运行，点托盘图标随时打开。")
        elif hwnd:
            self.was_open = True

    def _balloon(self, title, text):
        if not BALLOONS:      # 见文件顶部 BALLOONS 说明：默认不弹
            return
        self.nid.uFlags = NIF_INFO
        self.nid.szInfo = text[:255]
        self.nid.szInfoTitle = title[:63]
        self.nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

    def _remove(self):
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))

    # -- 启动
    def run(self):
        hinstance = kernel32.GetModuleHandleW(None)
        icon_path = str(APP / "icon.ico")
        if Path(icon_path).exists():
            cx = user32.GetSystemMetrics(SM_CXSMICON)
            cy = user32.GetSystemMetrics(SM_CYSMICON)
            handle = user32.LoadImageW(None, icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
            if not handle:
                handle = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
            self.hicon = handle

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.hIcon = self.hicon
        wc.lpszClassName = "GuizangTrayWnd"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:  # 已注册
                raise ctypes.WinError(ctypes.get_last_error())

        self.hwnd = user32.CreateWindowExW(0, "GuizangTrayWnd", "归藏托盘", 0,
                                           0, 0, 0, 0, None, None, hinstance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.nid.hWnd = self.hwnd
        self.nid.hIcon = self.hicon
        self.nid.szTip = "归藏 · 抖音收藏知识库"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid)):
            raise ctypes.WinError(ctypes.get_last_error())

        user32.SetTimer(self.hwnd, TIMER_ID, POLL_MS, None)
        self.was_open = find_console() is not None
        self._balloon("归藏已在后台运行", "关掉窗口不用怕，点这个图标就能打开。")

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._remove()
        return 0


def acquire_mutex():
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def already_running() -> bool:
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    exists = ctypes.get_last_error() == 183
    kernel32.CloseHandle(handle)
    return exists


def ensure_tray() -> bool:
    """由快捷方式/守护进程调用：托盘没起就悄悄拉一个（pythonw，无黑窗）。"""
    if already_running():
        return False
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.exists() else exe
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(runner), str(Path(__file__).resolve())], cwd=str(APP),
                     creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return True


def main():
    mutex = acquire_mutex()
    if mutex is None:
        return 0
    ensure_server()
    try:
        return Tray().run()
    except Exception as exc:  # 托盘起不来不该影响服务本身
        try:
            try:  # 与其它模块一致：数据目录由 paths 决定
                import paths as _paths

                work = _paths.data_dir()
            except Exception:
                from pathlib import Path as _P

                work = _P(__file__).resolve().parent.parent / "work"
            work.mkdir(parents=True, exist_ok=True)
            with (work / "tray.log").open("a", encoding="utf-8") as fh:
                import time
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 托盘启动失败：{exc!r}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
