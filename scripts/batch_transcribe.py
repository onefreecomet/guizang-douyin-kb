"""飞书妙记批量转写，持久化任务 token，支持恢复和可靠失败报告。"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from .task_state import load_state, save_state, find_video, emit_event, read_video_id, atomic_text
except ImportError:
    from task_state import load_state, save_state, find_video, emit_event, read_video_id, atomic_text

try:
    from .command_runner import run_command
except ImportError:
    from command_runner import run_command

LARK_BIN = os.environ.get("LARK_CLI") or shutil.which("lark-cli") or "lark-cli"
SKILL_DIR = Path(os.environ.get("LARK_WORK_DIR") or Path(__file__).resolve().parent.parent / "work")
INTERVAL_BETWEEN_VIDEOS = 45
RATE_LIMIT_WAIT_MINUTES = 10
LARK_TIMEOUT_SECONDS = 300
UPLOAD_TIMEOUT_SECONDS = 600
SPEAKER_LINE_RE = re.compile(r'^\S.*\s+\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*$')
LEGACY_SPEAKER_RE = re.compile(r'^Speaker\s+\d+\s*$', re.IGNORECASE)


class LarkError(RuntimeError):
    """保留飞书类型、子类型、错误码和进程退出码。"""
    def __init__(self, message, error_type=None, subtype=None, code=None, returncode=None):
        self.error_type = error_type
        self.subtype = subtype
        self.code = code
        self.returncode = returncode
        details = [str(v) for v in (error_type, subtype, code) if v is not None]
        if returncode:
            details.append(f"exit={returncode}")
        super().__init__(f"{'/'.join(details)}: {message}" if details else message)


class RateLimitError(LarkError):
    """飞书限流；停止本次批处理，保留全部待恢复目录。"""


def is_rate_limit_error(error):
    if isinstance(error, RateLimitError):
        return True
    text = str(error).lower()
    return any(part in text for part in (
        "rate limit", "rate_limit", "ratelimit", "too many requests", "429", "99991400", "限流"
    ))


def _run_lark(args, cwd=None, timeout=LARK_TIMEOUT_SECONDS, allow_item_failure=False):
    """调用飞书 CLI；空输出、坏 JSON 和硬错误均立即报错。

    allow_item_failure=True 用于批量读取接口（minutes +detail）：刚创建的妙记还没处理完时，
    CLI 会以退出码 1 结束并打印「N total, 0 succeeded, 1 failed」，但 stdout 仍是合法 JSON、
    顶层没有 error。这种属于单条目临时失败，交给调用方按条目轮询，不能当硬错误中断重试。
    """
    result = run_command(
        [LARK_BIN, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )
    raw = (result.stdout or "").strip()
    if not raw:
        raise LarkError(f"lark-cli 无输出: {(result.stderr or '')[:500]}",
                        error_type="empty_response", returncode=result.returncode)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LarkError(f"lark-cli JSON 无效: {raw[:300]} | {(result.stderr or '')[:300]}",
                        error_type="invalid_json", returncode=result.returncode) from exc
    if not isinstance(data, dict):
        raise LarkError("lark-cli 返回值不是 JSON 对象", error_type="invalid_response")
    code = data.get("code")
    if result.returncode or data.get("ok") is False or code not in (None, 0, "0"):
        err = data.get("error") or {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        error = LarkError(
            err.get("message") or data.get("msg") or (result.stderr or "飞书调用失败")[:500],
            error_type=err.get("type"), subtype=err.get("subtype"),
            code=err.get("code", code), returncode=result.returncode,
        )
        if is_rate_limit_error(error):
            raise RateLimitError(str(error), error_type=error.error_type,
                                 subtype=error.subtype, code=error.code,
                                 returncode=error.returncode)
        if allow_item_failure and data.get("ok") is not False and not err:
            data["item_failure"] = True
            return data
        raise error
    return data


def upload_video(video_path: Path) -> str:
    data = _run_lark(
        ["drive", "+upload", "--file", video_path.name, "--format", "json"],
        cwd=video_path.parent, timeout=UPLOAD_TIMEOUT_SECONDS,
    )
    token = (data.get("data") or {}).get("file_token")
    if not token:
        raise LarkError(f"未拿到 file_token: {data}", error_type="missing_token")
    return token


def create_minute(file_token: str) -> str:
    data = _run_lark(["minutes", "+upload", "--file-token", file_token, "--format", "json"])
    payload = data.get("data") or data
    token = payload.get("minute_token")
    if not token:
        url = payload.get("minute_url", "")
        token = url.rstrip("/").split("/")[-1] if url else None
    if not token:
        raise LarkError(f"未拿到 minute_token: {data}", error_type="missing_token")
    return token


def get_transcript_from_minutes(minute_token: str, max_retries: int = 10,
                                wait_seconds: int = 30):
    """仅对成功但尚无产物的响应继续轮询，API 硬错误立即传播。

    刚创建的妙记在服务端还没就绪时，+detail 会把它算作单条失败（CLI 退出码 1、顶层没有 error）；
    这种临时状态必须继续轮询，否则上传成功、钱已花掉却把视频标成失败。
    """
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(max_retries):
        print(f"  等待妙记处理... ({i + 1}/{max_retries})")
        data = _run_lark([
            "minutes", "+detail", "--minute-tokens", minute_token,
            "--transcript", "--output-dir", "transcript", "--overwrite", "--format", "json",
        ], cwd=SKILL_DIR, allow_item_failure=True)
        minutes = (data.get("data") or {}).get("minutes") or []
        if minutes:
            minute = minutes[0]
            if minute.get("error") or minute.get("status") in ("failed", "error"):
                error = minute.get("error") or {}
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                failure = LarkError(
                    error.get("message") or f"妙记处理失败: {minute}",
                    error_type=error.get("type") or "minute_failed",
                    subtype=error.get("subtype"), code=error.get("code", minute.get("code")),
                )
                failure.terminal = minute.get("status") in ("failed", "error")
                if is_rate_limit_error(failure):
                    raise RateLimitError(str(failure), error_type=failure.error_type,
                                         subtype=failure.subtype, code=failure.code)
                if failure.terminal:
                    raise failure
                print(f"  妙记暂时不可用（{failure}），继续等待")
            else:
                transcript = (minute.get("artifacts") or {}).get("transcript_file")
                if transcript:
                    path = Path(transcript)
                    if not path.is_absolute():
                        path = SKILL_DIR / path
                    if path.is_file():
                        return str(path)
        elif data.get("item_failure"):
            print("  妙记仍在处理（本条暂不可读），继续等待")
        if i < max_retries - 1:
            time.sleep(wait_seconds)
    raise TimeoutError("妙记处理超时")


def transcribe_video_lark(video_path: Path, video_dir=None) -> str:
    """每取得一个 token 立即落盘；重试跳过已成功的远程步骤。"""
    video_path = Path(video_path)
    video_dir = Path(video_dir) if video_dir is not None else video_path.parent
    state = load_state(video_dir)
    minute_token = state.get("minute_token")
    creation_unknown = bool(state.get("minute_creation_pending") and not minute_token)
    if state.get("minute_terminal") or creation_unknown:
        if os.environ.get("DY_REBUILD_FAILED_MINUTES") != "1":
            reason = ("上次妙记创建结果未知，请先到飞书核查；确认重新创建可能重复付费"
                      if creation_unknown else "飞书已确认此转写任务失败，请确认重新创建任务后重试")
            raise LarkError(reason, error_type="minute_creation_unknown" if creation_unknown else "minute_terminal")
        history = list(state.get("previous_minutes") or [])
        if minute_token:
            history.append(minute_token)
        save_state(video_dir, minute_token=None, minute_terminal=False, minute_creation_pending=False,
                   previous_minutes=history, usage_recorded=False)
        minute_token = None
    if not minute_token:
        file_token = state.get("file_token")
        if not file_token:
            file_token = upload_video(video_path)
            save_state(video_dir, file_token=file_token)
        # 写前日志必须先于可能计费的请求；超时、崩溃或杀进程均不能证明远端未创建。
        save_state(video_dir, minute_creation_pending=True)
        minute_token = create_minute(file_token)
        # token 和清除 intent 在同一次原子替换中提交。
        save_state(video_dir, minute_token=minute_token, minute_creation_pending=False)
    elif state.get("minute_creation_pending"):
        # 已有 token 就能恢复轮询，不需要重新创建，也不应阻止恢复。
        save_state(video_dir, minute_creation_pending=False)
    try:
        transcript_file = get_transcript_from_minutes(minute_token)
    except LarkError as exc:
        if getattr(exc, "terminal", False):
            save_state(video_dir, minute_terminal=True)
        raise
    if not transcript_file:
        raise TimeoutError("妙记处理超时")
    src_file = Path(transcript_file)
    if not src_file.is_absolute():
        for base in (SKILL_DIR, video_dir, Path.cwd()):
            candidate = base / src_file
            if candidate.is_file():
                src_file = candidate
                break
    return src_file.read_text(encoding="utf-8-sig")


def transcript_body(text: str) -> str:
    """去掉说话人及其前置元信息；无标记时保留普通正文。"""
    lines = text.splitlines()
    markers = [i for i, line in enumerate(lines)
               if SPEAKER_LINE_RE.match(line.strip()) or LEGACY_SPEAKER_RE.match(line.strip())]
    if markers:
        lines = lines[markers[0]:]
    return "\n".join(line for line in lines
                     if not SPEAKER_LINE_RE.match(line.strip())
                     and not LEGACY_SPEAKER_RE.match(line.strip())).strip()


def _title(video_dir):
    path = video_dir / "视频信息.txt"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8-sig")
    match = re.search(r'标题[:：][ \t]*\n([^\n]+)', text)
    return match.group(1).strip() if match else ""


def merge_video_copy(video_dir: Path):
    video_dir = Path(video_dir)
    text = (video_dir / "语音转文字.txt").read_text(encoding="utf-8-sig")
    body = transcript_body(text)
    if not body:
        raise ValueError("转写正文为空，无法生成视频文案")
    title = _title(video_dir)
    atomic_text(video_dir / "视频文案.txt", f"{title}\n\n{body}\n" if title else f"{body}\n")
    return body


def transcript_complete(video_dir):
    """旧任务也校验正文，标题或空文件不能充当已完成产物。"""
    video_dir = Path(video_dir)
    copy_file = video_dir / "视频文案.txt"
    if not copy_file.is_file():
        return False
    raw_file = video_dir / "语音转文字.txt"
    if raw_file.is_file():
        body = transcript_body(raw_file.read_text(encoding="utf-8-sig"))
        return bool(body) and body in copy_file.read_text(encoding="utf-8-sig")
    body = copy_file.read_text(encoding="utf-8-sig").strip()
    title = _title(video_dir)
    if title and body.startswith(title):
        body = body[len(title):].strip()
    return bool(transcript_body(body))


def _event(video_dir, status, error="", rate_limited=False):
    state = load_state(video_dir)
    emit_event(video_id=read_video_id(video_dir), phase="transcribe", status=status, error=error,
               terminal=bool(state.get("minute_terminal") or
                             (state.get("minute_creation_pending") and not state.get("minute_token"))),
               rate_limited=bool(rate_limited))


def transcribe_video(video_dir: Path):
    """成功返回 True；失败保存原因、发事件并抛出原始异常。"""
    video_dir = Path(video_dir)
    try:
        save_state(video_dir, transcript_status="running", transcript_error="")
        _event(video_dir, "running")
        raw_file = video_dir / "语音转文字.txt"
        video_file = find_video(video_dir)
        if raw_file.is_file():
            body = transcript_body(raw_file.read_text(encoding="utf-8-sig"))
            if not body and load_state(video_dir).get("minute_token"):
                # 空产物可用已有妙记重取，但不重复上传或创建任务。
                text = transcribe_video_lark(video_file or video_dir / "video.mp4", video_dir)
                atomic_text(raw_file, text)
            merge_video_copy(video_dir)
        elif not transcript_complete(video_dir):
            if video_file is None and not load_state(video_dir).get("minute_token"):
                raise FileNotFoundError(f"没有有效视频: {video_dir}")
            text = transcribe_video_lark(video_file or video_dir / "video.mp4", video_dir)
            atomic_text(raw_file, text)
            merge_video_copy(video_dir)
        if not transcript_complete(video_dir):
            raise ValueError("视频文案没有有效转写正文")
        state = load_state(video_dir)
        if not state.get("usage_recorded") and state.get("minute_token") and video_file:
            try:
                if __package__:
                    from . import usage
                else:
                    import usage
                usage.add_record(video_file, video_dir.name,
                                 record_id=state["minute_token"], video_id=read_video_id(video_dir))
                save_state(video_dir, usage_recorded=True)
            except Exception as exc:
                print(f"  ⚠️ 用量记录失败: {exc}")
        save_state(video_dir, transcript_status="done", transcript_error="", copy_status="done")
        _event(video_dir, "done")
        return True
    except Exception as exc:
        save_state(video_dir, transcript_status="error", transcript_error=f"{type(exc).__name__}: {exc}",
                   copy_status="error")
        _event(video_dir, "error", f"{type(exc).__name__}: {exc}", rate_limited=is_rate_limit_error(exc))
        if is_rate_limit_error(exc) and not isinstance(exc, RateLimitError):
            raise RateLimitError(str(exc)) from exc
        raise


def _needs_remote(video_dir):
    raw_file = video_dir / "语音转文字.txt"
    if raw_file.is_file():
        return (not transcript_body(raw_file.read_text(encoding="utf-8-sig"))
                and bool(load_state(video_dir).get("minute_token")))
    return not transcript_complete(video_dir)


def transcribe_directories(dirs) -> dict:
    """逐项转写；限流立即停止，未运行项也加入失败明细。"""
    dirs = [Path(directory) for directory in dirs]
    result = {"ok": 0, "fail": 0, "failed_dirs": []}
    remote_started = False
    for index, directory in enumerate(dirs):
        try:
            if _needs_remote(directory):
                if remote_started:
                    time.sleep(INTERVAL_BETWEEN_VIDEOS)
                remote_started = True
            if transcribe_video(directory) is not True:
                raise RuntimeError("转写未返回成功结果")
            result["ok"] += 1
        except Exception as exc:
            result["failed_dirs"].append(str(directory))
            print(f"  ❌ {directory.name}: {type(exc).__name__}: {exc}")
            if is_rate_limit_error(exc):
                reason = f"RateLimitError: 因前序任务限流未执行: {exc}"
                for remaining in dirs[index + 1:]:
                    save_state(remaining, transcript_status="error", transcript_error=reason, copy_status="error")
                    _event(remaining, "error", reason, rate_limited=True)
                    result["failed_dirs"].append(str(remaining))
                print(f"飞书限流，请等待 {RATE_LIMIT_WAIT_MINUTES} 分钟后重试")
                break
    result["fail"] = len(result["failed_dirs"])
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    base_dir = Path(argv[0]) if argv else Path.home() / "抖音视频下载"
    try:
        dirs = sorted((directory for directory in base_dir.iterdir()
                       if directory.is_dir() and (find_video(directory) is not None
                           or (directory / "语音转文字.txt").is_file())), key=lambda path: path.name)
        result = transcribe_directories(dirs)
    except Exception as exc:
        print(f"批量转写失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
