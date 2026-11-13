"""一次性连通性探测：GET /models 与 POST /chat/completions。密钥只从环境变量读取。"""
import json
import os
import urllib.error
import urllib.request

base = os.environ["AMR_BASE"].rstrip("/")
key = os.environ["AMR_KEY"]
model = os.environ["AMR_MODEL"]


def call(path, payload=None, method="GET", timeout=90):
    url = base + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:800]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


status, body = call("/models")
print("MODELS_STATUS", status)
print("MODELS_BODY", body[:2000])
print("-" * 40)

status, body = call(
    "/chat/completions",
    {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "max_tokens": 32,
    },
    method="POST",
)
print("CHAT_STATUS", status)
print("CHAT_BODY", body[:1500])
