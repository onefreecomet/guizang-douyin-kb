"""
零依赖的 Chrome DevTools Protocol 客户端。

不需要 playwright / selenium，只用标准库：
- 自己实现 WebSocket 客户端（RFC 6455）
- 通过 CDP 控制本机 Edge，注入 cookie、导航、执行 JS

用途：抓取抖音收藏夹时，给后端一个能自己操作浏览器的能力。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0")


class WS:
    """极简 WebSocket 客户端，只支持文本帧"""

    def __init__(self, url, timeout=60):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {u.hostname}:{u.port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手期间连接关闭")
            buf += chunk
        if b"101" not in buf.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败: " + buf[:200].decode("latin-1", "replace"))

    def _readn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("连接已关闭")
            buf += chunk
        return buf

    def _read_frame(self):
        b1, b2 = self._readn(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._readn(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._readn(8))[0]
        mask = self._readn(4) if masked else None
        payload = self._readn(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def send(self, text: str):
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        payloads = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:
                raise ConnectionError("收到关闭帧")
            if opcode == 0x9:
                continue
            payloads.append(payload)
            if fin:
                break
        return b"".join(payloads).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    def __init__(self, ws_url, timeout=60):
        self.ws = WS(ws_url, timeout=timeout)
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expression, await_promise=False):
        r = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS 执行出错: {r['exceptionDetails'].get('text')}")
        return (r.get("result") or {}).get("value")

    def close(self):
        self.ws.close()


def find_edge():
    for c in EDGE_CANDIDATES:
        if Path(c).exists():
            return c
    return None


class EdgeSession:
    """启动一个可控的 Edge，用完自动关闭

    profile_dir 传了就复用这个配置目录（能记住登录态和风控验证结果），
    不传就用临时目录，用完删掉。
    """

    def __init__(self, port=9333, headless=True, user_agent=UA, profile_dir=None):
        self.port = port
        self.proc = None
        self.headless = headless
        self.user_agent = user_agent
        self.browser = None
        self.page = None
        if profile_dir:
            self.tmp = Path(profile_dir)
            self.tmp.mkdir(parents=True, exist_ok=True)
            self._persistent = True
        else:
            self.tmp = Path(tempfile.mkdtemp(prefix="dy_edge_"))
            self._persistent = False

    def __enter__(self):
        edge = find_edge()
        if not edge:
            raise RuntimeError("找不到 Edge，无法抓取收藏夹")
        args = [edge,
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.tmp}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--disable-notifications",
                "--window-size=1440,920",
                f"--user-agent={self.user_agent}",
                "about:blank"]
        if self.headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

        ws_url = None
        for _ in range(60):
            time.sleep(0.5)
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/version")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=3) as r:
                    ws_url = json.loads(r.read()).get("webSocketDebuggerUrl")
                if ws_url:
                    break
            except Exception:
                continue
        if not ws_url:
            raise RuntimeError("Edge 调试端口没起来")
        self.browser = CDP(ws_url, timeout=120)
        return self

    def _http(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as r:
            return r.read()

    def open_page(self, url="about:blank"):
        """新开一个页面，返回连到该页面的 CDP 会话（每个页面独立 ws，不用 sessionId）"""
        data = json.loads(self._http(f"/json/new?{urllib.parse.quote(url, safe='')}", "PUT"))
        self.page = CDP(data["webSocketDebuggerUrl"], timeout=120)
        self._tid = data.get("id")
        return self.page

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        if not self._persistent:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)

    def __exit__(self, *a):
        self.close()
