"""Exercise HTTP start -> partial failure -> retry -> restart record -> quit offline."""
import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import urllib.request
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import server
from test_core import media


class ApiTests(unittest.TestCase):
    def test_start_partial_failure_then_retry_only_failed_stage(self):
        root = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        root.mkdir(parents=True)
        original_job = copy.deepcopy(server.JOB)
        original_worker = server.WORKER_THREAD
        original_quitting = server.QUITTING
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        serving = threading.Thread(target=http.serve_forever, daemon=True)
        serving.start()
        prefix = f'http://127.0.0.1:{http.server_address[1]}'
        def request(route, data=None):
            if data is None:
                req = urllib.request.Request(prefix + route)
            else:
                req = urllib.request.Request(prefix + route, json.dumps(data).encode(), {'Content-Type':'application/json'})
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        try:
            with patch.object(server, 'LAST_JOB', root/'last_job.json'):
                server.JOB.update(state='idle', items=[], request_items=[])
                server.WORKER_THREAD = None
                server.QUITTING = False
                items = []
                directories = []
                for index, vid in enumerate(('12345678','98765432')):
                    directory = root/'2026-11-12'/f'{index+1:02d}_旧视频'
                    directory.mkdir(parents=True)
                    (directory/'标题.mp4').write_bytes(media())
                    (directory/'视频信息.txt').write_text(f'视频ID: {vid}\n标题:\n标题',encoding='utf-8')
                    (directory/'语音转文字.txt').write_text('有效正文' if index == 0 else '',encoding='utf-8')
                    items.append(dict(video_id=vid,url='https://www.douyin.com/video/'+vid,title='标题'))
                    directories.append(directory)
                result = request('/api/start', dict(items=items, base_dir=str(root), cookies=str(root/'missing-cookies'),transcribe=True))
                self.assertTrue(result['ok'])
                server.WORKER_THREAD.join(timeout=15)
                self.assertFalse(server.WORKER_THREAD.is_alive())
                job = request('/api/job')
                self.assertEqual(job['state'], 'partial', job)
                self.assertEqual((job['ok'],job['fail']), (1,1))
                self.assertEqual(job['items'][0]['status'], 'done')
                self.assertEqual(job['items'][1]['status'], 'error')
                (directories[1]/'语音转文字.txt').write_text('修复后的正文',encoding='utf-8')
                self.assertTrue(request('/api/retry',{})['ok'])
                server.WORKER_THREAD.join(timeout=15)
                self.assertFalse(server.WORKER_THREAD.is_alive())
                job = request('/api/job')
                self.assertEqual(job['state'],'done',job)
                self.assertEqual((job['ok'],job['fail'],job['total']),(1,0,1))
                self.assertEqual(job['items'][0]['video_id'],'98765432')
                self.assertFalse((directories[1]/'_download').exists(), 'retry should never invoke a downloader')
                saved = json.loads((root/'last_job.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['state'],'done')
                files = request('/api/files?job=1')
                self.assertEqual(len(files['items']), 1)
                self.assertEqual(files['items'][0]['path'], str(directories[1]))
                self.assertTrue(files['items'][0]['copy'])
                self.assertTrue(request('/api/quit',{})['ok'])
                serving.join(timeout=5)
                self.assertFalse(serving.is_alive())
        finally:
            if server.WORKER_THREAD and server.WORKER_THREAD.is_alive():
                server.stop_job()
                server.WORKER_THREAD.join(timeout=20)
            if serving.is_alive():
                http.shutdown(); serving.join(timeout=5)
            http.server_close()
            server.JOB.clear(); server.JOB.update(original_job)
            server.WORKER_THREAD = original_worker
            server.QUITTING = original_quitting
            server.STOP_EVENT.clear()
            # Delete only the verified fixture, never a computed arbitrary root.
            assert root.resolve().parent == (ROOT/'tests'/'.runs').resolve() and len(root.name) == 32
            import shutil
            shutil.rmtree(root)


if __name__ == '__main__':
    unittest.main()
