"""Knowledge-base integration tests using real, isolated filesystem fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'app'))
from knowledge import KnowledgeBase, SUBDIR, START, END


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        # Inherit workspace ACLs, matching the project's existing tests; the
        # secure Windows tempfile ACL excludes the sandbox AppContainer.
        self.fixture = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        self.fixture.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.vault = self.fixture / 'vault'
        self.vault.mkdir()
        self.cache = self.fixture / 'knowledge.sqlite'
        self.kb = KnowledgeBase(self.vault, cache_path=self.cache)
        self.source = self.fixture / 'downloads' / '2026-11-12' / '01_video'
        self.source.mkdir(parents=True)
        (self.source / '视频信息.txt').write_text(
            '视频ID: 12345678\n链接: https://www.douyin.com/video/12345678\n博主: 测试博主\n标题:\n原始标题\n', encoding='utf-8')
        self.raw = 'Duration: 10min 14s\nKeywords: 以诺书 / 古代文明\nSpeaker 1 00:00:01.200\n带时间戳的第一段。\nSpeaker 1 00:03:21.000\n第二段探讨文明。'
        (self.source / '语音转文字.txt').write_text(self.raw, encoding='utf-8')
        (self.source / '视频文案.txt').write_text('原始标题\n\n带时间戳的第一段。\n第二段探讨文明。', encoding='utf-8')
        self.item = {'video_id': '12345678', 'title': '原始标题', 'saved_at': '2026-11-12'}

    def cleanup(self):
        expected = (ROOT / 'tests' / '.runs').resolve()
        assert self.fixture.resolve().parent == expected and len(self.fixture.name) == 32
        shutil.rmtree(self.fixture)

    def publish(self, **changes):
        return self.kb.publish(dict(self.item, **changes), self.source)

    def path(self, result):
        return self.vault / result['path']

    def junction(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                self.skipTest('symlink unavailable')
            command = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if command.returncode:
                self.skipTest('symlink/junction unavailable')

    def test_publish_actual_formats_and_reader(self):
        result = self.kb.publish({}, self.source)
        # Missing date uses current month; explicit date controls the layout.
        result = self.publish()
        text = self.path(result).read_text(encoding='utf-8')
        self.assertIn(self.raw, text)
        self.assertIn('关键词：以诺书 / 古代文明', text)
        self.assertIn('时长：10min 14s', text)
        self.assertIn('## 我的笔记', text)
        self.assertIn('author: "测试博主"', text)
        self.assertEqual(result['video_id'], '12345678')
        self.assertFalse(result['frozen'])
        note = self.kb.note(result['path'])
        self.assertEqual(note['markdown'], text)
        self.assertEqual(note['title'], '原始标题')
        self.assertTrue(note['obsidian_url'].startswith('obsidian://open?vault=vault&file='))
        # Obsidian 不把 '+' 还原成空格，链接里必须是 %20
        self.assertIn('%20', note['obsidian_url'])
        self.assertNotIn('+', note['obsidian_url'])
        self.assertEqual(self.kb.note(result['path'].removeprefix(SUBDIR + '/')), note)

    def test_blank_author_never_becomes_next_label_and_cst_duration_parsed(self):
        (self.source / '视频信息.txt').write_text('视频ID: 12345678\n博主:\n\n标题:\n原始标题\n', encoding='utf-8')
        (self.source / '语音转文字.txt').write_text('2026-11-12 21:45:23 CST|10min 14s\n\nSpeaker 1 00:00:00.200\n正文。', encoding='utf-8')
        result = self.kb.publish({}, self.source)
        text = self.path(result).read_text(encoding='utf-8')
        self.assertIn('author: ""', text)
        self.assertIn('duration: "10min 14s"', text)

    def test_month_and_filename_rules(self):
        result = self.publish(title='题|目[#测试]/非法:字符?')
        self.assertTrue(result['path'].startswith(SUBDIR + '/素材/2026-11/'))
        name = self.path(result).name
        self.assertIn('｜', name)
        for char in '[]#/:?':
            self.assertNotIn(char, name)
        self.assertEqual(len(list((self.vault / SUBDIR / '素材').rglob('*.md'))), 1)

    def test_repeat_id_preserves_handwriting_custom_yaml_and_location(self):
        result = self.publish()
        path = self.path(result)
        text = path.read_text(encoding='utf-8')
        custom = 'rating: 5\ntags:\n  - 手工\ncustom: |\n  多行内容\n我的分类: 学习\n'
        text = text.replace('status: 待加工', custom + 'status: 待加工')
        text = text.replace(START, '手写前言\n\n' + START)
        text += '\n手写尾注 [[我的链接]]\n'
        path.write_text(text, encoding='utf-8')
        updated = self.publish(title='改名后标题', saved_at='2027-01-01')
        self.assertEqual(updated['path'], result['path'])
        self.assertFalse(updated['created'])
        actual = path.read_text(encoding='utf-8')
        self.assertIn(custom, actual)
        self.assertIn('手写前言\n\n' + START, actual)
        self.assertTrue(actual.endswith('\n手写尾注 [[我的链接]]\n'))
        self.assertIn('title: "改名后标题"', actual)
        self.assertEqual(self.kb.tree()['count'], 1)

    def test_complete_freeze_and_existing_topic_series_entries(self):
        for status in ('已定稿', '冻结'):
            with self.subTest(status=status):
                result = self.publish()
                path = self.path(result)
                path.write_text(path.read_text(encoding='utf-8').replace('status: 待加工',
                                'status: "' + status + '" # 用户已确认'), encoding='utf-8')
                topics = self.vault / SUBDIR / '主题'
                series = self.vault / SUBDIR / '系列'
                topics.mkdir(exist_ok=True)
                series.mkdir(exist_ok=True)
                topic = topics / '主题.md'
                serial = series / '系列.md'
                topic.write_text('- [[原始标题]] — 冻结列表项', encoding='utf-8')
                serial.write_text('1. [[原始标题]] — 冻结系列项', encoding='utf-8')
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (path, topic, serial)}
                # Frozen publish works even with no remaining download directory.
                changed = self.kb.publish(dict(self.item, title='不应改名', saved_at='2028-02-01'), self.fixture / 'missing')
                self.assertTrue(changed['frozen'])
                self.assertEqual(changed['path'], result['path'])
                info = self.source / '视频信息.txt'
                original_info = info.read_bytes()
                info.write_bytes(b'\xff')
                try:
                    self.assertTrue(self.publish()['frozen'])
                finally:
                    info.write_bytes(original_info)
                self.kb.reindex()
                for p, (data, mtime) in before.items():
                    self.assertEqual(p.read_bytes(), data)
                    self.assertEqual(p.stat().st_mtime_ns, mtime)
                # Unfreeze solely to exercise the second value in this fixture.
                path.write_text(path.read_text(encoding='utf-8').replace('status: "' + status + '" # 用户已确认',
                                                                       'status: 待加工'), encoding='utf-8')

    def test_same_title_and_same_suffix_do_not_overwrite(self):
        first = self.publish(video_id='10001111', title='同名')
        second = self.publish(video_id='20001111', title='同名')
        third = self.publish(video_id='30001111', title='同名')
        self.assertEqual(len({first['path'], second['path'], third['path']}), 3)
        self.assertTrue(second['path'].endswith('同名-1111.md'))
        for result in (first, second, third):
            self.assertIn('video_id: "' + result['video_id'] + '"', self.path(result).read_text(encoding='utf-8'))

    def test_rebuild_search_chinese_literals_and_external_edits(self):
        result = self.publish()
        database = sqlite3.connect(self.cache)
        try:
            self.assertEqual(database.execute('SELECT count(*) FROM knowledge_notes').fetchone()[0], 1)
        finally:
            database.close()
        self.assertEqual(self.kb.search('文明')['count'], 1)
        self.assertEqual(self.kb.search("' OR 1=1 --")['count'], 0)
        self.assertEqual(self.kb.search('')['count'], 0)
        self.assertEqual(self.kb.search('not-here')['count'], 0)
        self.assertEqual(self.cache.resolve(), (self.fixture / 'knowledge.sqlite').resolve())
        self.cache.unlink()
        restored = KnowledgeBase(self.vault, cache_path=self.cache)
        self.assertEqual(restored.reindex(), {'count': 1, 'indexed': 1})
        self.assertEqual(restored.search('文明')['items'][0]['path'], result['path'])
        path = self.path(result)
        path.write_text(path.read_text(encoding='utf-8') + '\nObsidian 手动追加冰山词\n', encoding='utf-8')
        self.assertEqual(restored.search('冰山词')['count'], 1)

    def test_xss_is_only_returned_as_original_text(self):
        payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
        (self.source / '语音转文字.txt').write_text('00:00:01 ' + payload, encoding='utf-8')
        result = self.publish(title=payload)
        note = self.kb.note(result['path'])
        self.assertIn(payload, note['markdown'])
        self.assertEqual(note['title'], payload)
        self.assertNotIn('html', note)
        self.assertIn(payload, self.kb.search('<script>')['items'][0]['snippet'])
        self.assertNotIn('<mark>', self.kb.search('<script>')['items'][0]['snippet'])

    def test_custom_subdir_is_honoured(self):
        """知识库子目录可自定义：写入与读取都落在新目录里。"""
        self.assertEqual(self.kb.subdir, SUBDIR)
        custom = KnowledgeBase(self.vault, subdir='我的视频库', cache_path=self.cache)
        self.assertEqual(custom.subdir, '我的视频库')
        result = custom.publish(self.item, self.source)
        self.assertTrue(result['path'].startswith('我的视频库/素材/'), result['path'])
        self.assertTrue((self.vault / '我的视频库' / '素材').is_dir())
        self.assertFalse((self.vault / SUBDIR / '素材').exists())
        note = custom.note(result['path'])
        self.assertEqual(note['title'], '原始标题')
        self.assertEqual(custom.tree()['items'][0]['path'].split('/')[0], '我的视频库')

    def test_controlled_subdir_and_traversal_are_rejected(self):
        outside = self.vault / '私人笔记.md'
        outside.write_text('不能读取', encoding='utf-8')
        for subdir in ('', '  ', '..', '.', SUBDIR + '/../其他目录', '其他/目录',
                       'a:b', 'a\\b', str(self.fixture)):
            with self.subTest(subdir=subdir), self.assertRaises(ValueError):
                KnowledgeBase(self.vault, subdir=subdir)
        for requested in ('../私人笔记.md', SUBDIR + '/../私人笔记.md', '/私人笔记.md',
                          str(outside), 'C:/Windows/test.md', '素材\\..\\私人笔记.md',
                          '素材//a.md', './素材/a.md', '素材/a.txt', '素材/a.md/../b.md'):
            with self.subTest(path=requested), self.assertRaises(ValueError):
                self.kb.note(requested)
        self.assertEqual(outside.read_text(encoding='utf-8'), '不能读取')

    def test_linked_root_and_linked_material_directory_are_rejected(self):
        other = self.fixture / 'outside'
        other.mkdir()
        (other / 'secret.md').write_text('秘密', encoding='utf-8')
        new_vault = self.fixture / 'linked-vault'
        new_vault.mkdir()
        self.junction(new_vault / SUBDIR, other)
        with self.assertRaises(ValueError):
            KnowledgeBase(new_vault)
        self.junction(self.vault / SUBDIR / '素材', other)
        with self.assertRaises(ValueError):
            self.kb.reindex()
        with self.assertRaises(ValueError):
            self.publish()
        with self.assertRaises(ValueError):
            self.kb.note('素材/secret.md')
        self.assertEqual((other / 'secret.md').read_text(encoding='utf-8'), '秘密')

    def test_nested_link_not_traversed_or_returned(self):
        self.publish()
        other = self.fixture / 'outside'
        other.mkdir()
        (other / 'secret.md').write_text('隐私冰山词', encoding='utf-8')
        self.junction(self.vault / SUBDIR / '素材' / 'external', other)
        self.assertEqual(self.kb.reindex()['count'], 1)
        self.assertEqual(self.kb.search('隐私冰山词')['count'], 0)
        with self.assertRaises(ValueError):
            self.kb.note('素材/external/secret.md')

    def test_explicit_cache_parent_link_and_wrong_file_are_rejected(self):
        with self.assertRaises(ValueError):
            KnowledgeBase(self.vault, cache_path=self.fixture / 'settings.json')
        with self.assertRaises(ValueError):
            KnowledgeBase(self.vault, cache_path=self.fixture / 'missing' / 'index.db')
        other = self.fixture / 'cache-other'
        other.mkdir()
        self.junction(self.fixture / 'cache-link', other)
        with self.assertRaises(ValueError):
            KnowledgeBase(self.vault, cache_path=self.fixture / 'cache-link' / 'index.db')

    def test_default_cache_is_memory_and_tree_count_is_note_count(self):
        memory = KnowledgeBase(self.vault)
        self.assertEqual(memory.tree(), {'items': [], 'count': 0})
        result = memory.publish(self.item, self.source)
        tree = memory.tree()
        self.assertEqual(tree['count'], 1)
        material = tree['items'][0]
        self.assertEqual((material['name'], material['type']), ('素材', 'directory'))
        self.assertEqual(material['children'][0]['children'][0]['path'], result['path'])
        self.assertEqual(memory.search('文明')['count'], 1)
        self.assertFalse(self.cache.exists())

    def test_copy_fallback_and_invalid_sources_fail_without_notes(self):
        (self.source / '语音转文字.txt').unlink()
        result = self.publish()
        self.assertIn('带时间戳的第一段。', self.kb.note(result['path'])['markdown'])
        (self.source / '视频文案.txt').unlink()
        with self.assertRaises(ValueError):
            self.publish(video_id='other')
        with self.assertRaises(ValueError):
            self.publish(video_id='../bad')
        self.assertEqual(self.kb.tree()['count'], 1)

    def test_malformed_auto_or_frontmatter_is_not_overwritten(self):
        result = self.publish()
        path = self.path(result)
        original = path.read_text(encoding='utf-8')
        for malformed in (original.replace(END, ''), original + START + END,
                          original.replace('video_id: ', 'video_id: "duplicate"\nvideo_id: '),
                          '---\ntitle: unfinished'):
            path.write_text(malformed, encoding='utf-8')
            with self.assertRaises(ValueError, msg=repr(malformed)):
                self.publish()
            self.assertEqual(path.read_text(encoding='utf-8'), malformed)

    def test_note_without_auto_gets_generated_block_and_preserves_all_user_body(self):
        result = self.publish()
        path = self.path(result)
        body = '\r\n# 手写正文\r\n\r\n用户原来的全部内容\r\n'
        path.write_bytes(('---\r\nvideo_id: "12345678"\r\nstatus: 草稿\r\nrating: 5\r\n---\r\n' + body).encode('utf-8'))
        self.publish()
        data = path.read_bytes().decode('utf-8')
        self.assertIn(body, data)
        self.assertEqual(data.count(START), 1)
        self.assertIn('rating: 5', data)
        self.assertIn('status: 草稿', data)

    def test_threads_with_distinct_instances_deduplicate_and_avoid_name_collisions(self):
        def run(index):
            kb = KnowledgeBase(self.vault, cache_path=self.cache)
            return kb.publish(dict(self.item, video_id=str(index % 4), title='并发同名'), self.source)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(16)))
        self.assertEqual(self.kb.tree()['count'], 4)
        self.assertEqual(len({result['path'] for result in results}), 4)
        self.assertFalse(list((self.vault / SUBDIR).rglob('*.tmp')))

    def test_separate_processes_deduplicate_using_os_lock(self):
        script = ('import sys,json; from pathlib import Path; '
                  'sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); '
                  'from knowledge import KnowledgeBase; '
                  'KnowledgeBase(sys.argv[3]).publish(json.loads(sys.argv[5]),Path(sys.argv[4]))')
        command = [sys.executable, '-c', script, str(ROOT / 'app'), str(ROOT / 'scripts'),
                   str(self.vault), str(self.source), json.dumps(self.item, ensure_ascii=False)]
        processes = [subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(4)]
        try:
            self.assertEqual([process.wait(timeout=30) for process in processes], [0] * 4)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(self.kb.tree()['count'], 1)
        self.assertFalse(list((self.vault / SUBDIR).rglob('*.tmp')))


class DerivedPageTests(unittest.TestCase):
    """Topic, series and index pages derived from accepted notes (P2)."""

    def setUp(self):
        self.fixture = ROOT / 'tests' / '.runs' / uuid.uuid4().hex
        self.fixture.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.vault = self.fixture / 'vault'
        self.vault.mkdir()
        self.cache = self.fixture / 'knowledge.sqlite'
        self.kb = KnowledgeBase(self.vault, cache_path=self.cache)
        self.source = self.fixture / 'downloads' / '2026-11-12' / '01_video'
        self.source.mkdir(parents=True)
        (self.source / '视频信息.txt').write_text(
            '视频ID: 12345678\n链接: https://www.douyin.com/video/12345678\n博主: 测试博主\n标题:\n原始标题\n', encoding='utf-8')
        (self.source / '语音转文字.txt').write_text(
            'Speaker 1 00:00:01.200\n带时间戳的第一段。', encoding='utf-8')
        (self.source / '视频文案.txt').write_text('原始标题\n\n带时间戳的第一段。', encoding='utf-8')
        self.item = {'video_id': '12345678', 'title': '原始标题', 'saved_at': '2026-11-12'}
        self.ai = {'summary': '一条摘要。', 'key_points': ['要点一', '要点二'],
                   'topics': ['神话', '古代文明'], 'series': '圣经系列', 'series_index': 2,
                   'model': 'deepseek-v4.1-flash', 'at': '2026-11-13T15:00:00+08:00'}

    def cleanup(self):
        expected = (ROOT / 'tests' / '.runs').resolve()
        assert self.fixture.resolve().parent == expected and len(self.fixture.name) == 32
        shutil.rmtree(self.fixture)

    def publish(self, ai=None, **changes):
        return self.kb.publish(dict(self.item, **changes), self.source, ai)

    def path(self, result):
        return self.vault / result['path']

    def page(self, kind, name):
        return self.vault / SUBDIR / kind / (name + '.md')

    def test_ai_publish_writes_note_fields_and_derived_pages(self):
        result = self.publish(ai=self.ai)
        text = self.path(result).read_text(encoding='utf-8')
        self.assertIn('ai_summary: "一条摘要。"', text)
        self.assertIn('key_points: ["要点一", "要点二"]', text)
        self.assertIn('topics: ["神话", "古代文明"]', text)
        self.assertIn('series: "圣经系列"', text)
        self.assertIn('series_index: 2', text)
        self.assertIn('status: 已加工', text)
        self.assertIn('> [!abstract] 摘要', text)
        self.assertIn('## 要点', text)
        self.assertIn('1. 要点一', text)
        self.assertIn('主题：[[90 - 抖音精选/主题/神话|神话]]', text)
        self.assertIn('系列：[[90 - 抖音精选/系列/圣经系列|圣经系列]]（第 2 集）', text)
        self.assertEqual(result['topics'], ['神话', '古代文明'])
        self.assertEqual(result['series'], '圣经系列')
        self.assertEqual(result['status'], '已加工')
        self.assertTrue(result['indexed'])
        topic = self.page('主题', '神话').read_text(encoding='utf-8')
        self.assertIn('type: "topic"', topic)
        self.assertIn('## 相关视频（1）', topic)
        self.assertIn('原始标题', topic)
        self.assertIn('## 我的笔记', topic)
        series = self.page('系列', '圣经系列').read_text(encoding='utf-8')
        self.assertIn('## 分集', series)
        self.assertIn('第 2 集', series)
        index = (self.vault / SUBDIR / '00 - 索引.md').read_text(encoding='utf-8')
        self.assertIn('统计：1 条素材 · 2 个主题 · 1 个系列', index)
        self.assertIn('## 最近入库', index)
        self.assertIn('\\|', index)
        self.assertNotIn('｜', index)
        self.assertIn('## 按主题浏览', index)
        self.assertIn('## 系列', index)
        listing = self.kb.topics()
        self.assertEqual(listing['count'], 2)
        self.assertEqual({row['name'] for row in listing['items']}, {'神话', '古代文明'})
        self.assertEqual({row['count'] for row in listing['items']}, {1})
        self.assertTrue(all(row['path'].startswith(SUBDIR + '/主题/') for row in listing['items']))
        self.assertEqual(self.kb.series(), {'items': [{'name': '圣经系列', 'count': 1,
                                                       'path': SUBDIR + '/系列/圣经系列.md'}],
                                            'count': 1})
        self.assertEqual(self.kb.tree()['count'], 1)

    def test_republish_without_ai_keeps_ai_fields(self):
        first = self.publish(ai=self.ai)
        second = self.publish(title='改名后的标题')
        self.assertEqual(second['path'], first['path'])
        text = self.path(first).read_text(encoding='utf-8')
        self.assertIn('ai_summary: "一条摘要。"', text)
        self.assertIn('series: "圣经系列"', text)
        self.assertIn('status: 已加工', text)
        self.assertIn('title: "改名后的标题"', text)
        self.assertIn('## 要点', text)

    def test_merge_topics_moves_unfrozen_keeps_frozen_and_alias_page(self):
        first = self.publish(ai={'summary': '甲', 'key_points': ['一'],
                                 'topics': ['旧主题', '共享主题'], 'series': None})
        second = self.publish(video_id='87654321', ai={'summary': '乙', 'key_points': ['二'],
                                                       'topics': ['旧主题'], 'series': None})
        frozen_path = self.path(second)
        frozen_path.write_text(frozen_path.read_text(encoding='utf-8').replace(
            'status: 已加工', 'status: 已定稿'), encoding='utf-8')
        before = frozen_path.read_bytes()
        result = self.kb.merge_topics('旧主题', '新主题')
        self.assertEqual(result, {'source': '旧主题', 'target': '新主题',
                                  'moved': 1, 'frozen': 1, 'kept_page': True})
        moved = self.path(first).read_text(encoding='utf-8')
        self.assertIn('topics: ["新主题", "共享主题"]', moved)
        self.assertNotIn('旧主题', moved)
        self.assertEqual(frozen_path.read_bytes(), before)
        alias = self.page('主题', '旧主题').read_text(encoding='utf-8')
        self.assertIn('merged_into: "新主题"', alias)
        self.assertIn('kept_page: true', alias)
        names = {row['name'] for row in self.kb.topics()['items']}
        self.assertNotIn('旧主题', names)
        self.assertIn('新主题', names)
        self.assertIn('共享主题', names)

    def test_topic_page_keeps_handwriting_and_unknown_fields(self):
        self.publish(ai=self.ai)
        page = self.page('主题', '神话')
        text = page.read_text(encoding='utf-8')
        text = text.replace('type: "topic"', 'type: "topic"\nrating: 5', 1)
        text += '\n手写补充 [[我的链接]]\n'
        page.write_text(text, encoding='utf-8')
        self.publish(video_id='87654321', ai=self.ai)
        updated = page.read_text(encoding='utf-8')
        self.assertIn('## 相关视频（2）', updated)
        self.assertIn('rating: 5', updated)
        self.assertTrue(updated.endswith('\n手写补充 [[我的链接]]\n'))
        self.assertEqual(updated.count(START), 1)

    def test_ai_series_dict_from_enrich_is_normalised(self):
        """enrich 返回 {'name','index'}；publish 必须存成干净的系列名与集数。"""
        result = self.publish(ai={'summary': '摘要', 'key_points': ['要点'],
                                  'topics': [], 'series': {'name': '阿努纳奇起源故事', 'index': 3},
                                  'model': 'deepseek-v4.1-flash'})
        text = self.path(result).read_text(encoding='utf-8')
        self.assertIn('series: "阿努纳奇起源故事"', text)
        self.assertIn('series_index: 3', text)
        self.assertNotIn("{'name'", text)
        self.assertIn('系列：[[90 - 抖音精选/系列/阿努纳奇起源故事|阿努纳奇起源故事]]（第 3 集）', text)
        self.assertEqual(result['series'], '阿努纳奇起源故事')

    def test_refresh_is_idempotent_and_preserves_broken_page(self):
        self.publish(ai=self.ai)
        broken = self.page('主题', '神话')
        broken.write_text('---\ntitle: 未闭合\n', encoding='utf-8')
        before = broken.read_bytes()
        first = self.kb.refresh()
        self.assertEqual(first['notes'], 1)
        self.assertIn('index', first)
        self.assertEqual(broken.read_bytes(), before)
        snapshot = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (self.vault / SUBDIR).rglob('*.md')}
        second = self.kb.refresh()
        self.assertEqual(second['topics'], first['topics'])
        for path, (data, mtime) in snapshot.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.stat().st_mtime_ns, mtime)


if __name__ == '__main__':
    unittest.main()
