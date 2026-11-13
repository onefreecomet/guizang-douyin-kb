"""
从 WorkBuddy 内置浏览器导出抖音 cookies（Netscape 格式），供 yt-dlp / 控制台使用。

原理：
1. 浏览器运行时 Cookies 数据库被独占锁，普通复制会失败
2. 用卷影复制（VSS）拿一份一致快照
3. 本机内置浏览器的 agent 分区把 cookie 明文存在 value 列，直接读取即可
4. 若遇到 v10/v11 加密值，用 DPAPI + AES-GCM 解密

用法：
    python export_cookies.py [输出路径]
默认输出 <用户主目录>\抖音视频下载\cookies.txt
"""
import base64
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

SESSION = Path(os.path.expanduser("~")) / ".workbuddy" / "app" / "session"
DEFAULT_OUT = Path.home() / "抖音视频下载" / "cookies.txt"

# 优先这几个分区（agent 浏览器曾在这里存抖音登录态），其余作为兜底
PREFERRED = [
    SESSION / "Partitions" / "agent-browser-preview-webview" / "Network" / "Cookies",
    SESSION / "Network" / "Cookies",
]


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(blob: bytes) -> bytes:
    buf_in = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob),
                                              ctypes.POINTER(ctypes.c_char)))
    buf_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buf_in), None, None, None, None, 0, ctypes.byref(buf_out))
    if not ok:
        raise RuntimeError("DPAPI 解密失败")
    data = ctypes.string_at(buf_out.pbData, buf_out.cbData)
    ctypes.windll.kernel32.LocalFree(buf_out.pbData)
    return data


def load_aes_key(session_dir: Path):
    """从 Local State 取 AES key（v10/v11 加密时用）"""
    ls = session_dir / "Local State"
    if not ls.exists():
        return None
    cfg = json.loads(ls.read_text(encoding="utf-8"))
    enc = cfg.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        return None
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None
    return dpapi_decrypt(raw[5:])


def decrypt_value(blob: bytes, key: bytes):
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v11"):
        if not key:
            return ""
        from Crypto.Cipher import AES
        nonce, payload = blob[3:15], blob[15:]
        ct, tag = payload[:-16], payload[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode("utf-8", "replace")
    if blob[:3] == b"v20":
        raise RuntimeError("cookie 使用 App-Bound 加密(v20)，当前脚本暂不支持，"
                           "请在 Edge 里改用插件导出")
    return blob.decode("utf-8", "replace")


def snapshot_via_vss(paths, out_dir: Path):
    """用卷影复制把被锁的数据库复制出来"""
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = ['$ErrorActionPreference = "Stop"',
             '$res = ([wmiclass]"Win32_ShadowCopy").Create("C:\\", "ClientAccessible")',
             'if ($res.ReturnValue -ne 0) { Write-Output "VSS_FAIL"; exit 1 }',
             '$id = $res.ShadowID',
             '$dev = (Get-WmiObject Win32_ShadowCopy | Where-Object { $_.ID -eq $id }).DeviceObject']
    for i, p in enumerate(paths):
        rel = str(p)[len("C:\\"):] if str(p).upper().startswith("C:\\") else str(p)
        target = out_dir / f"c{i}.db"
        lines.append(f'$s = Join-Path $dev "{rel}"')
        lines.append('if ([System.IO.File]::Exists($s)) { '
                     f'[System.IO.File]::Copy($s, "{target}", $true) }}')
    lines.append('(Get-WmiObject Win32_ShadowCopy -Filter "ID=\'$id\'").Delete() | Out-Null')
    lines.append('Write-Output "DONE"')

    ps = out_dir / "snap.ps1"
    ps.write_text("\n".join(lines), encoding="utf-8")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if "DONE" not in (r.stdout or ""):
        print("PowerShell 输出:", (r.stdout or "")[:600])
        print("PowerShell 错误:", (r.stderr or "")[:600])
    return "DONE" in (r.stdout or "")


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    candidates = [p for p in PREFERRED if p.exists()]
    # 兜底：把 session 下所有分区都列上
    if SESSION.exists():
        for p in SESSION.rglob("Cookies"):
            if p.is_file() and p not in candidates:
                candidates.append(p)

    print(f"发现 {len(candidates)} 个 cookies 数据库，准备快照...")
    tmp = Path(tempfile.mkdtemp(prefix="dycookie_"))
    if not snapshot_via_vss(candidates, tmp):
        print("卷影复制失败，请确认以管理员身份运行")
        return 1

    key = load_aes_key(SESSION)
    best_rows, best_src = [], None
    for i, src in enumerate(candidates):
        db = tmp / f"c{i}.db"
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(db)
            rows = con.execute(
                "select host_key, name, value, encrypted_value, path, expires_utc, is_secure "
                "from cookies where host_key like '%douyin%'"
            ).fetchall()
        except Exception:
            continue
        if len(rows) > len(best_rows):
            best_rows, best_src = rows, src

    if not best_rows:
        print("没有找到抖音 cookies，请确认已在内置浏览器登录抖音")
        return 1

    lines = ["# Netscape HTTP Cookie File",
             "# exported from WorkBuddy browser session", ""]
    kept = 0
    for host, name, value, enc, path, exp, secure in best_rows:
        val = value or decrypt_value(enc, key)
        if not val:
            continue
        expiry = int(exp / 1000000 - 11644473600) if exp else 0
        if expiry < 0:
            expiry = 0
        lines.append("\t".join([
            host, "TRUE" if host.startswith(".") else "FALSE", path or "/",
            "TRUE" if secure else "FALSE", str(expiry), name, val,
        ]))
        kept += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    names = {r[1] for r in best_rows}
    critical = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "ttwid"} & names
    print(f"来源: {best_src}")
    print(f"已导出 {kept} 条 -> {out_path} ({out_path.stat().st_size} 字节)")
    print(f"关键登录 cookie: {sorted(critical)}")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
