"""Regression checks with synthetic media and mocked network/CLI calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import unittest
import uuid
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'app'))
import task_state as state
import download_with_cookies as download
import usage
import server


def box(kind, payload):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


def media():
    header = b'\0' * 12 + struct.pack('>II', 1000, 2000)
    handler = b'\0' * 8 + b'vide'
    return (box(b'ftyp', b'isom\0\0\0\0') +
            box(b'moov', box(b'mvhd', header) + box(b'trak', box(b'mdia', box(b'hdlr', handler)))) +
            box(b'mdat', b'example video payload'))


class CoreTests(unittest.TestCase):
    def setUp(self):
        # Normal inherited permissions; Python 3.13 secure tempfile ACLs exclude AppContainers.
        self.root = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.log = contextlib.redirect_stdout(io.StringIO())
        self.log.__enter__()
        self.addCleanup(self.log.__exit__, None, None, None)

    def cleanup(self):
        # Verify the exact test root before deleting only our fixture.
        expected_parent = (ROOT / 'tests' / '.runs').resolve()
        assert self.root.resolve().parent == expected_parent and len(self.root.name) == 32
        import shutil
        shutil.rmtree(self.root)

    def video_dir(self, vid='12345678', title='已重命名标题', date='2026-11-12'):
        directory = self.root / date / ('01_' + title)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (title + '.mp4')).write_bytes(media())
        (directory / '视频信息.txt').write_text(f'视频ID: {vid}\n标题:\n{title}', encoding='utf-8')
        return directory

    def batch(self, videos):
        directory = self.root / '2026-11-13'
        directory.mkdir(exist_ok=True)
        path = directory / 'video_list.json'
        path.write_text(json.dumps(videos), encoding='utf-8')
        return path

    def test_media_rejects_large_http_body_and_truncated_mp4(self):
        path = self.root / 'file.mp4'
        path.write_bytes(media())
        self.assertTrue(state.valid_video(path))
        path.write_bytes(media()[:-3])
        self.assertFalse(state.valid_video(path))
        path.write_bytes(b'<html>' + b'x' * 20000)
        self.assertFalse(state.valid_video(path))

    def test_renamed_legacy_video_deduplicates_across_dates(self):
        self.video_dir()
        self.assertEqual(state.downloaded_ids(self.root), {'12345678'})
        path = self.batch([{'video_id':'12345678','url':'https://www.douyin.com/video/12345678','title':'新标题'}])
        with patch.dict(os.environ, SKILL_SKIP_TRANSCRIBE='1'), patch.object(download, 'download_video') as call:
            result = download.download_favorites(path, None, self.root)
        call.assert_not_called()
        self.assertEqual(result, {'ok':1,'fail':0,'failed_ids':[]})

    def test_curl_nonzero_never_promotes_even_structurally_valid_file(self):
        parsed = '@@'.join([]) + '__GV_JSON_BEGIN__' + json.dumps({'success':True,'data':{'videoItemVoList':[{'fileType':'video','baseUrl':'https://example.test/media'}]}}) + '__GV_JSON_END__'
        def run(cmd, **kwargs):
            if cmd[0] == 'curl.exe':
                Path(cmd[cmd.index('-o')+1]).write_bytes(media())
                return subprocess.CompletedProcess(cmd, 28, '', 'timeout')
            return subprocess.CompletedProcess(cmd, 0, parsed, '')
        with patch.object(download, 'get_node_path', return_value='node'), patch.object(download, 'run_command', side_effect=run):
            success, _, _ = download.download_video_greenvideo('url', self.root, 'test', max_retries=1)
        self.assertFalse(success)
        self.assertFalse((self.root / 'test.mp4').exists())

    def test_valid_curl_uses_partial_then_promotes(self):
        parsed = '__GV_JSON_BEGIN__' + json.dumps({'success':True,'data':{'videoItemVoList':[{'fileType':'video','baseUrl':'https://example.test/media'}]}}) + '__GV_JSON_END__'
        def run(cmd, **kwargs):
            if cmd[0] == 'curl.exe':
                target = Path(cmd[cmd.index('-o')+1])
                self.assertEqual(target.suffix, '.part')
                self.assertIn('--fail', cmd)
                target.write_bytes(media())
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return subprocess.CompletedProcess(cmd, 0, parsed, '')
        with patch.object(download, 'get_node_path', return_value='node'), patch.object(download, 'run_command', side_effect=run):
            success, path, _ = download.download_video_greenvideo('url', self.root, 'test', max_retries=1)
        self.assertTrue(success)
        self.assertTrue(state.valid_video(path))
        self.assertFalse((self.root / 'test.mp4.part').exists())

    def test_batch_mixed_failure_preserves_success_and_stable_failed_directory(self):
        self.video_dir()
        videos = [{'video_id':v,'url':'https://www.douyin.com/video/'+v,'title':'same'} for v in ('12345678','98765432')]
        path = self.batch(videos)
        with patch.dict(os.environ, SKILL_SKIP_TRANSCRIBE='1'), patch.object(download, 'download_video', return_value=(False,None,'both_failed','HTTP 403')) as call:
            result = download.download_favorites(path,None,self.root)
            first_directory = call.call_args.args[1]
            retry = download.download_favorites(path,None,self.root)
        self.assertEqual(result, {'ok':1,'fail':1,'failed_ids':['98765432']})
        self.assertEqual(retry, result)
        self.assertEqual(call.call_args.args[1], first_directory)
        self.assertEqual(state.load_state(first_directory)['download_status'], 'error')

    def test_new_batches_do_not_reuse_numbered_directory(self):
        existing = self.video_dir()
        path = self.batch([{'video_id':'98765432','url':'https://www.douyin.com/video/98765432','title':'已重命名标题'}])
        def succeed(url, directory, name, cookies):
            target = directory / (name + '.mp4'); target.write_bytes(media())
            return True,target,'mock',None
        with patch.dict(os.environ, SKILL_SKIP_TRANSCRIBE='1'), patch.object(download,'download_video',side_effect=succeed):
            download.download_favorites(path,None,self.root)
        self.assertTrue(existing.exists())
        self.assertEqual(len(state.downloaded_ids(self.root)),2)

    def test_usage_same_minute_is_recorded_once(self):
        directory = self.video_dir()
        with patch.object(usage,'FILE',self.root/'usage.json'), patch.object(usage,'WORK',self.root):
            usage.add_record(directory/'已重命名标题.mp4',record_id='minute-1',video_id='12345678')
            usage.add_record(directory/'已重命名标题.mp4',record_id='minute-1',video_id='12345678')
            records = usage.load()['records']
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['video_id'],'12345678')

    def test_backfill_uses_metadata_and_does_not_double_count_renamed_video(self):
        directory = self.video_dir()
        (directory/'语音转文字.txt').write_text('body',encoding='utf-8')
        with patch.object(usage,'FILE',self.root/'usage.json'), patch.object(usage,'WORK',self.root):
            usage.save({'quota_minutes':300,'records':[{'video_id':'12345678','month':'2026-11','seconds':2}]})
            usage.backfill(self.root)
            self.assertEqual(len(usage.load()['records']),1)

    def test_corrupt_checkpoint_is_not_silently_overwritten(self):
        (self.root/'task_state.json').write_text('broken',encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            state.save_state(self.root, minute_token='replacement')
        self.assertEqual((self.root/'task_state.json').read_text(), 'broken')

    def test_events_count_full_pipeline_not_download_only(self):
        rows = [dict(video_id='1',download='pending',transcribe='pending',status='pending',error='')]
        with patch.dict(server.JOB, items=rows, transcribe=True), patch.object(server,'LAST_JOB',self.root/'last.json'):
            server.process_event(state.EVENT_PREFIX+json.dumps(dict(video_id='1',phase='download',status='done')))
            self.assertEqual(server.JOB['ok'],0)
            server.process_event(state.EVENT_PREFIX+json.dumps(dict(video_id='1',phase='transcribe',status='error',error='quota')))
            self.assertEqual(server.JOB['fail'],1)
            self.assertEqual(rows[0]['error'],'quota')

    def test_restarted_running_job_becomes_retryable_stopped(self):
        checkpoint = self.root/'last.json'
        checkpoint.write_text(json.dumps({'state':'running','items':[{'video_id':'1','status':'pending'}]}),encoding='utf-8')
        with patch.dict(server.JOB), patch.object(server,'LAST_JOB',checkpoint):
            server.restore_job()
            self.assertEqual(server.JOB['state'],'stopped')

    def test_immediate_stop_before_spawn_is_honored(self):
        with patch.dict(server.JOB, state='running', items=[], ok=0, fail=0), patch.object(server,'LAST_JOB',self.root/'last.json'), patch.object(server,'ProcessTree') as tree:
            server.STOP_EVENT.set()
            try:
                server.worker([],self.root,self.root/'cookies',False)
            finally:
                server.STOP_EVENT.clear()
            tree.assert_not_called()
            self.assertEqual(server.JOB['state'],'stopped')


class SharePageTests(unittest.TestCase):
    """分享页直链是 yt-dlp 签名失效后的主力通道，解析规则要单独守住。"""
    def setUp(self):
        self.root = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        expected_parent = (ROOT / 'tests' / '.runs').resolve()
        assert self.root.resolve().parent == expected_parent and len(self.root.name) == 32
        import shutil
        shutil.rmtree(self.root)

    def share_html(self, urls, desc='标题', author='作者'):
        payload = {'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': [{
            'desc': desc,
            'author': {'nickname': author},
            'video': {'duration': 12345, 'play_addr': {'url_list': urls}},
        }]}}}}
        return ('<html><script>window._ROUTER_DATA = ' + json.dumps(payload, ensure_ascii=False)
                + '</script></html>')

    def test_share_video_id_accepts_douyin_url_forms(self):
        for url in ('https://www.douyin.com/video/7704266308103228726',
                    'https://www.iesdouyin.com/share/video/7704266308103228726/?region=CN',
                    'https://v.douyin.com/video/7704266308103228726'):
            self.assertEqual(download.share_video_id(url), '7704266308103228726')
        self.assertIsNone(download.share_video_id('https://www.douyin.com/user/self'))
        self.assertEqual(download.share_video_id('7704266308103228726'), '7704266308103228726')

    def test_share_play_info_prefers_unwatermarked_url_and_parses_nested_json(self):
        html = self.share_html(['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc'])
        info = download.share_play_info(html)
        self.assertEqual(info['desc'], '标题')
        self.assertEqual(info['author'], '作者')
        self.assertEqual(info['duration_ms'], 12345)
        self.assertEqual(info['urls'], ['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc'])
        # 骨架页（未登录）没有 videoInfoRes：必须明确判成拿不到直链
        self.assertIsNone(download.share_play_info(
            '<html><script>window._ROUTER_DATA = {"loaderData":{"video_layout":null}}'
            '</script></html>'))

    def test_share_play_info_survives_braces_inside_strings(self):
        html = self.share_html(['https://aweme.snssdk.com/aweme/v1/play/?video_id=x{1}'],
                              desc='带 { 和 } 的标题')
        info = download.share_play_info(html)
        self.assertEqual(info['desc'], '带 { 和 } 的标题')
        self.assertTrue(info['urls'][0].endswith('video_id=x{1}'))

    def test_download_video_share_promotes_partial_only_on_valid_media(self):
        cookies = self.root / 'cookies.txt'
        cookies.write_text('# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/'
                           '\tTRUE\t1799670250\tsessionid\tabc\n', encoding='utf-8')
        media_bytes = media()

        def run(cmd, **kwargs):
            if cmd[0] == 'curl.exe':
                target = Path(cmd[cmd.index('-o') + 1])
                self.assertEqual(target.suffix, '.part')
                self.assertIn('https://aweme.snssdk.com/aweme/v1/play/', cmd[-1])
                target.write_bytes(media_bytes)
                return subprocess.CompletedProcess(cmd, 0, '', '')
            raise AssertionError('unexpected command ' + str(cmd))

        with (patch.object(download, 'fetch_share_html',
                           return_value=self.share_html(['https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc'])),
              patch.object(download, 'run_command', side_effect=run)):
            success, path, error = download.download_video_share(
                'https://www.douyin.com/video/7704266308103228726', self.root, 'video_x', str(cookies))

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertTrue(state.valid_video(path))
        self.assertFalse((self.root / 'video_x.mp4.part').exists())

    def test_download_video_share_refuses_without_cookies(self):
        success, path, error = download.download_video_share(
            'https://www.douyin.com/video/7704266308103228726', self.root, 'video_x', None)
        self.assertFalse(success)
        self.assertIn('cookies', error)

    def test_failed_share_then_greenvideo_keeps_both_reasons(self):
        def share(url, out, name, cookies_file=None, max_retries=3):
            return False, None, '分享页没有返回视频信息（登录态可能已失效）'

        def green(url, out, name, max_retries=6):
            return False, None, 'greenvideo 解析未获取到视频直链：请求次数较多，请完成验证码后继续（code 4291）'

        with (patch.object(download, 'download_video_ytdlp',
                           return_value=(False, None, 'Fresh cookies are needed')),
              patch.object(download, 'download_video_share', side_effect=share),
              patch.object(download, 'download_video_greenvideo', side_effect=green)):
            success, path, method, error = download.download_video(
                'https://www.douyin.com/video/7704266308103228726', self.root, 'video_x', None)

        self.assertFalse(success)
        self.assertEqual(method, 'both_failed')
        self.assertIn('分享页没有返回视频信息', error)
        self.assertIn('请求次数较多', error)


if __name__ == '__main__':
    unittest.main()
