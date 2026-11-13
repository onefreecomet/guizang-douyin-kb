"""No external APIs: transcription recovery, content validation and failure reporting."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import uuid
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import batch_transcribe as bt


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        # Ordinary mkdir preserves inherited Windows sandbox permissions.
        self.root = Path(__file__).resolve().parent / ".runs" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.directory = self.root / "sample"
        self.directory.mkdir()
        self.video = self.directory / "video_12345678.mp4"
        self.video.write_bytes(b"mock video")
        self.artifact = self.root / "artifact.txt"
        self.artifact.write_text("Speaker 1 00:00:01.000\n真实正文。", encoding="utf-8")
        self.events = []
        for target, replacement in (
            ("find_video", Mock(side_effect=lambda directory: Path(directory) / self.video.name)),
            ("emit_event", Mock(side_effect=lambda **event: self.events.append(event))),
            ("time.sleep", Mock()),
        ):
            patcher = patch.object(bt, target, replacement) if "." not in target else patch("batch_transcribe." + target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        usage_patcher = patch("usage.add_record", return_value=1)
        self.usage = usage_patcher.start()
        self.addCleanup(usage_patcher.stop)
        # Any unexpected real CLI execution fails the test.
        api_patcher = patch.object(bt, "run_command", side_effect=AssertionError("unexpected external API call"))
        api_patcher.start()
        self.addCleanup(api_patcher.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_import_has_no_dependency_or_directory_side_effects(self):
        with patch.object(bt.Path, "mkdir", side_effect=AssertionError("import mkdir")):
            importlib.reload(bt)

    def test_creation_failure_requires_confirmation_before_file_token_reuse(self):
        with patch.object(bt, "upload_video", return_value="file-1") as upload, \
             patch.object(bt, "create_minute", side_effect=[bt.LarkError("permission denied", code=403), "minute-1"]) as create, \
             patch.object(bt, "get_transcript_from_minutes", return_value=str(self.artifact)):
            with self.assertRaises(bt.LarkError):
                bt.transcribe_video(self.directory)
            state = bt.load_state(self.directory)
            self.assertEqual(state["file_token"], "file-1")
            self.assertEqual(state["transcript_status"], "error")
            self.assertIn("403", state["transcript_error"])
            self.assertTrue(state["minute_creation_pending"])
            self.assertTrue(self.events[-1]["terminal"])
            with patch.dict(bt.os.environ, DY_REBUILD_FAILED_MINUTES="0"):
                with self.assertRaisesRegex(bt.LarkError, "创建结果未知"):
                    bt.transcribe_video(self.directory)
            self.assertEqual(create.call_count, 1)
            with patch.dict(bt.os.environ, DY_REBUILD_FAILED_MINUTES="1"):
                self.assertTrue(bt.transcribe_video(self.directory))
            upload.assert_called_once_with(self.video)
            self.assertEqual(create.call_count, 2)
            self.assertFalse(bt.load_state(self.directory)["minute_creation_pending"])
            self.assertEqual(bt.load_state(self.directory)["minute_token"], "minute-1")

    def test_existing_creation_intent_never_repeats_request(self):
        bt.save_state(self.directory, file_token="file-1", minute_creation_pending=True)
        with patch.dict(bt.os.environ, DY_REBUILD_FAILED_MINUTES="0"), \
             patch.object(bt, "upload_video") as upload, \
             patch.object(bt, "create_minute") as create:
            with self.assertRaisesRegex(bt.LarkError, "飞书核查"):
                bt.transcribe_video(self.directory)
        upload.assert_not_called()
        create.assert_not_called()
        self.assertTrue(bt.load_state(self.directory)["minute_creation_pending"])
        self.assertTrue(self.events[-1]["terminal"])

    def test_create_timeout_keeps_intent_and_event_is_terminal(self):
        bt.save_state(self.directory, file_token="file-1")
        def timed_out(token):
            self.assertTrue(bt.load_state(self.directory)["minute_creation_pending"])
            raise TimeoutError("request outcome unknown")
        with patch.object(bt, "create_minute", side_effect=timed_out):
            with self.assertRaises(TimeoutError):
                bt.transcribe_video(self.directory)
        self.assertTrue(bt.load_state(self.directory)["minute_creation_pending"])
        self.assertTrue(self.events[-1]["terminal"])
        self.assertFalse(self.events[-1]["rate_limited"])

    def test_existing_token_resumes_even_with_stale_creation_intent(self):
        bt.save_state(self.directory, minute_token="known-minute", minute_creation_pending=True)
        with patch.object(bt, "create_minute") as create, \
             patch.object(bt, "get_transcript_from_minutes", return_value=str(self.artifact)) as poll:
            self.assertTrue(bt.transcribe_video(self.directory))
        create.assert_not_called()
        poll.assert_called_once_with("known-minute")
        self.assertFalse(bt.load_state(self.directory)["minute_creation_pending"])
        self.assertFalse(self.events[-1]["terminal"])

    def test_minute_token_reused_after_poll_failure(self):
        with patch.object(bt, "upload_video", return_value="file-1") as upload, \
             patch.object(bt, "create_minute", return_value="minute-1") as create, \
             patch.object(bt, "get_transcript_from_minutes", side_effect=[TimeoutError("poll timeout"), str(self.artifact)]) as poll:
            with self.assertRaises(TimeoutError):
                bt.transcribe_video(self.directory)
            self.assertEqual(bt.load_state(self.directory)["minute_token"], "minute-1")
            self.assertTrue(bt.transcribe_video(self.directory))
            upload.assert_called_once()
            create.assert_called_once()
            self.assertEqual(poll.call_args_list[0].args, ("minute-1",))
            self.assertEqual(poll.call_args_list[1].args, ("minute-1",))
            self.assertEqual(bt.load_state(self.directory)["transcript_status"], "done")
            self.assertTrue(bt.load_state(self.directory)["usage_recorded"])
            bt.transcribe_video(self.directory)
            self.usage.assert_called_once()

    def test_terminal_minute_requires_confirmation_then_reuses_uploaded_file(self):
        bt.save_state(self.directory, file_token="file-1", minute_token="failed-minute", minute_terminal=True)
        with patch.object(bt, "upload_video", side_effect=AssertionError("must reuse upload")), \
             patch.object(bt, "create_minute", return_value="new-minute") as create, \
             patch.object(bt, "get_transcript_from_minutes", return_value=str(self.artifact)):
            with patch.dict(bt.os.environ, DY_REBUILD_FAILED_MINUTES="0"):
                with self.assertRaisesRegex(bt.LarkError, "确认重新创建"):
                    bt.transcribe_video(self.directory)
            create.assert_not_called()
            with patch.dict(bt.os.environ, DY_REBUILD_FAILED_MINUTES="1"):
                self.assertTrue(bt.transcribe_video(self.directory))
            create.assert_called_once_with("file-1")
            self.assertEqual(bt.load_state(self.directory)["previous_minutes"], ["failed-minute"])
            self.assertFalse(bt.load_state(self.directory)["minute_terminal"])

    def test_remote_terminal_failure_is_persisted(self):
        bt.save_state(self.directory, minute_token="failed-minute")
        failure = bt.LarkError("remote failed", error_type="minute_failed")
        failure.terminal = True
        with patch.object(bt, "get_transcript_from_minutes", side_effect=failure):
            with self.assertRaises(bt.LarkError):
                bt.transcribe_video(self.directory)
        self.assertTrue(bt.load_state(self.directory)["minute_terminal"])
        self.assertTrue(self.events[-1]["terminal"])

    def test_existing_plain_raw_only_merges_without_upload(self):
        (self.directory / "视频信息.txt").write_text("标题:\n示例标题", encoding="utf-8")
        (self.directory / "语音转文字.txt").write_text("没有说话人标记的正文。", encoding="utf-8")
        with patch.object(bt, "transcribe_video_lark", side_effect=AssertionError("must reuse raw")):
            self.assertTrue(bt.transcribe_video(self.directory))
        self.assertEqual((self.directory / "视频文案.txt").read_text(encoding="utf-8"), "示例标题\n\n没有说话人标记的正文。\n")
        self.assertEqual([event["status"] for event in self.events], ["running", "done"])
        self.assertTrue(all(event["video_id"] == "12345678" and event["phase"] == "transcribe" and "error" in event for event in self.events))

    def test_empty_raw_and_title_only_copy_are_failure(self):
        (self.directory / "视频信息.txt").write_text("标题:\n只有标题", encoding="utf-8")
        (self.directory / "视频文案.txt").write_text("只有标题\n\n", encoding="utf-8")
        self.assertFalse(bt.transcript_complete(self.directory))
        (self.directory / "语音转文字.txt").write_text("Keywords: test\nSpeaker 1 00:00:01.000\n\n", encoding="utf-8")
        with patch.object(bt, "transcribe_video_lark", side_effect=AssertionError("must not upload empty raw")):
            with self.assertRaisesRegex(ValueError, "正文为空"):
                bt.transcribe_video(self.directory)
        self.assertFalse(bt.transcript_complete(self.directory))
        self.assertEqual(bt.load_state(self.directory)["transcript_status"], "error")
        self.assertEqual(self.events[-1]["status"], "error")

    def test_downloaded_empty_transcript_is_failed_and_tokens_remain(self):
        self.artifact.write_text("", encoding="utf-8")
        bt.save_state(self.directory, file_token="file", minute_token="minute")
        with patch.object(bt, "get_transcript_from_minutes", return_value=str(self.artifact)):
            result = bt.transcribe_directories([self.directory])
        self.assertEqual(result, {"ok": 0, "fail": 1, "failed_dirs": [str(self.directory)]})
        self.assertEqual(bt.load_state(self.directory)["minute_token"], "minute")
        self.usage.assert_not_called()

    def test_rate_limit_accounts_for_remaining_directories(self):
        others = [self.root / "second", self.root / "third"]
        for directory in others:
            directory.mkdir()
        with patch.object(bt, "upload_video", side_effect=bt.RateLimitError("429 rate limit")) as upload:
            result = bt.transcribe_directories([self.directory, *others])
        self.assertEqual(result, {"ok": 0, "fail": 3, "failed_dirs": [str(p) for p in [self.directory, *others]]})
        upload.assert_called_once()
        bt.time.sleep.assert_not_called()
        self.assertEqual(sum(event["status"] == "error" for event in self.events), 3)
        self.assertTrue(all(event["rate_limited"] for event in self.events if event["status"] == "error"))
        for directory in [self.directory, *others]:
            self.assertEqual(bt.load_state(directory)["transcript_status"], "error")

    def test_poll_hard_error_not_swallowed(self):
        with patch.object(bt, "SKILL_DIR", self.root / "work"), \
             patch.object(bt, "_run_lark", side_effect=bt.LarkError("permission denied", error_type="permission", code=403)) as run:
            with self.assertRaisesRegex(bt.LarkError, "permission/403"):
                bt.get_transcript_from_minutes("minute", max_retries=10)
        run.assert_called_once()
        bt.time.sleep.assert_not_called()

    def test_poll_embedded_rate_limit_preserves_code(self):
        response = {"data": {"minutes": [{"error": {
            "type": "rate_limit", "subtype": "minute", "code": 99991400,
            "message": "too many requests"}}]}}
        with patch.object(bt, "SKILL_DIR", self.root / "work"), \
             patch.object(bt, "_run_lark", return_value=response):
            with self.assertRaises(bt.RateLimitError) as caught:
                bt.get_transcript_from_minutes("minute")
        self.assertEqual(caught.exception.code, 99991400)
        bt.time.sleep.assert_not_called()

    def test_item_failure_exit_code_one_keeps_polling_until_transcript_ready(self):
        """刚创建的妙记：CLI 退出码 1 但顶层没有 error，要当作临时状态继续轮询。"""
        failed = subprocess.CompletedProcess(
            [], 1,
            json.dumps({"ok": True, "identity": "user", "data": {"minutes": []},
                        "meta": {"count": 1}}),
            "[minutes +detail] done: 1 total, 0 succeeded, 1 failed",
        )
        ready = subprocess.CompletedProcess(
            [], 0,
            json.dumps({"ok": True, "data": {"minutes": [{
                "minute_token": "minute",
                "artifacts": {"transcript_file": str(self.artifact)},
            }]}}),
            "",
        )
        with patch.object(bt, "SKILL_DIR", self.root / "work"), \
             patch.object(bt, "run_command", side_effect=[failed, ready]) as run:
            transcript = bt.get_transcript_from_minutes("minute", max_retries=3, wait_seconds=1)
        self.assertEqual(transcript, str(self.artifact))
        self.assertEqual(run.call_count, 2)
        bt.time.sleep.assert_called_once_with(1)

    def test_item_failure_is_only_tolerated_when_requested(self):
        payload = {"ok": True, "data": {"minutes": []}, "meta": {"count": 1}}
        response = subprocess.CompletedProcess([], 1, json.dumps(payload), "0 succeeded, 1 failed")
        with patch.object(bt, "run_command", return_value=response):
            with self.assertRaises(bt.LarkError):
                bt._run_lark(["minutes", "+detail"])
            data = bt._run_lark(["minutes", "+detail"], allow_item_failure=True)
        self.assertTrue(data["item_failure"])

    def test_item_failure_never_hides_real_errors(self):
        response = subprocess.CompletedProcess(
            [], 1, json.dumps({"ok": False, "error": {"type": "validation",
                                                     "subtype": "invalid_argument",
                                                     "message": "invalid minute token"}}), "")
        with patch.object(bt, "run_command", return_value=response):
            with self.assertRaises(bt.LarkError) as caught:
                bt._run_lark(["minutes", "+detail"], allow_item_failure=True)
        self.assertEqual(caught.exception.subtype, "invalid_argument")

    def test_non_terminal_minute_error_keeps_polling(self):
        pending = {"data": {"minutes": [{"minute_token": "minute", "error": {
            "type": "service_error", "message": "minute is being generated"}}]}}
        ready = {"data": {"minutes": [{
            "minute_token": "minute",
            "artifacts": {"transcript_file": str(self.artifact)},
        }]}}
        with patch.object(bt, "SKILL_DIR", self.root / "work"), \
             patch.object(bt, "_run_lark", side_effect=[pending, ready]):
            transcript = bt.get_transcript_from_minutes("minute", max_retries=3, wait_seconds=1)
        self.assertEqual(transcript, str(self.artifact))
        bt.time.sleep.assert_called_once_with(1)

    def test_terminal_minute_error_still_raises(self):
        terminal = {"data": {"minutes": [{"minute_token": "minute", "status": "failed",
                                          "error": {"message": "conversion failed"}}]}}
        with patch.object(bt, "SKILL_DIR", self.root / "work"), \
             patch.object(bt, "_run_lark", return_value=terminal) as run:
            with self.assertRaisesRegex(bt.LarkError, "conversion failed"):
                bt.get_transcript_from_minutes("minute", max_retries=3, wait_seconds=1)
        run.assert_called_once()
        bt.time.sleep.assert_not_called()

    def test_upload_timeout_and_structured_error(self):
        response = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "error": {
            "type": "auth", "subtype": "expired", "code": 999, "message": "expired credential"}}), "")
        with patch.object(bt, "run_command", return_value=response) as run:
            with self.assertRaises(bt.LarkError) as caught:
                bt.upload_video(self.video)
        self.assertEqual(caught.exception.error_type, "auth")
        self.assertEqual(caught.exception.subtype, "expired")
        self.assertEqual(caught.exception.code, 999)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], bt.UPLOAD_TIMEOUT_SECONDS)

    def test_cli_nonzero_for_failed_result_and_zero_for_success(self):
        with patch.object(bt, "transcribe_directories", return_value={"ok": 0, "fail": 1, "failed_dirs": [str(self.directory)]}):
            self.assertEqual(bt.main([str(self.root)]), 1)
        with patch.object(bt, "transcribe_directories", return_value={"ok": 1, "fail": 0, "failed_dirs": []}):
            self.assertEqual(bt.main([str(self.root)]), 0)

    def test_completed_batch_does_not_wait(self):
        other = self.root / "other"
        other.mkdir()
        (self.directory / "语音转文字.txt").write_text("first body", encoding="utf-8")
        (other / "语音转文字.txt").write_text("second body", encoding="utf-8")
        result = bt.transcribe_directories([self.directory, other])
        self.assertEqual(result["ok"], 2)
        bt.time.sleep.assert_not_called()

    def test_batch_keeps_45_second_interval_for_remote_tasks(self):
        other = self.root / "other"
        other.mkdir()
        with patch.object(bt, "transcribe_video", return_value=True):
            result = bt.transcribe_directories([self.directory, other])
        self.assertEqual(result["ok"], 2)
        bt.time.sleep.assert_called_once_with(45)

    def test_empty_raw_with_minute_token_refetches_without_upload(self):
        (self.directory / "语音转文字.txt").write_text("", encoding="utf-8")
        bt.save_state(self.directory, minute_token="existing-minute")
        with patch.object(bt, "upload_video", side_effect=AssertionError("no upload")), \
             patch.object(bt, "create_minute", side_effect=AssertionError("no new minute")), \
             patch.object(bt, "get_transcript_from_minutes", return_value=str(self.artifact)) as poll:
            self.assertTrue(bt.transcribe_video(self.directory))
        poll.assert_called_once_with("existing-minute")
        self.assertEqual(bt.load_state(self.directory)["copy_status"], "done")
        self.usage.assert_called_once_with(self.video, self.directory.name,
                                           record_id="existing-minute", video_id="12345678")


if __name__ == "__main__":
    unittest.main()
