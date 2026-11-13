"""chat.py 单元测试：检索选稿、提问校验、提示词内容与空库短路。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'app') not in sys.path:
    sys.path.insert(0, str(ROOT / 'app'))

import chat  # noqa: E402


class FakeKB:
    def __init__(self, notes=None, hits=None, total=None):
        self.notes = notes or {}
        self.hits = hits or []
        self.total = len(self.notes) if total is None else total
        self.search_calls = []

    def tree(self):
        return {'items': [], 'count': self.total}

    def search(self, query):
        self.search_calls.append(query)
        return {'items': [{'path': path, 'title': title, 'snippet': ''} for path, title in self.hits],
                'count': len(self.hits)}

    def note(self, path):
        markdown, title = self.notes[path]
        return {'path': path, 'title': title, 'markdown': markdown}

    def topics(self):
        return {'items': [{'name': '阿努纳奇与苏美尔神话'}], 'count': 1}

    def series(self):
        return {'items': [{'name': '阿努纳奇起源故事'}], 'count': 1}


class FakeEnricher:
    def __init__(self, text='这是答案。'):
        self.text = text
        self.calls = []

    def chat(self, config, messages):
        self.calls.append(messages)
        return {'text': self.text, 'model': 'test-model', 'tokens': 12}


NOTE = ('''---
title: "测试笔记"
status: 已加工
---

<!-- AUTO:START -->
> [!abstract] 摘要
阿努纳奇来自苏美尔神话。

## 要点
1. 第一条要点
<!-- AUTO:END -->

## 我的笔记
''', '测试笔记')


class AnswerTests(unittest.TestCase):
    def test_rejects_empty_and_overlong_question(self):
        kb = FakeKB({'a.md': NOTE})
        with self.assertRaises(ValueError):
            chat.answer(FakeEnricher(), {}, kb, '  ')
        with self.assertRaises(ValueError):
            chat.answer(FakeEnricher(), {}, kb, 'x' * (chat.QUESTION_MAX + 1))

    def test_empty_knowledge_base_skips_ai_call(self):
        enricher = FakeEnricher()
        result = chat.answer(enricher, {}, FakeKB({}, total=0), '讲讲阿努纳奇')
        self.assertEqual(result['sources'], [])
        self.assertIn('知识库里还没有笔记', result['answer'])
        self.assertEqual(enricher.calls, [])

    def test_answer_uses_matched_notes_and_returns_sources(self):
        notes = {'素材/2026-11/一.md': NOTE,
                 '素材/2026-11/二.md': ('---\ntitle: "二"\n---\n\n第二篇内容。', '二')}
        kb = FakeKB(notes, hits=[('素材/2026-11/二.md', '二'), ('素材/2026-11/一.md', '测试笔记')])
        enricher = FakeEnricher('阿努纳奇是苏美尔神话中的神。')
        result = chat.answer(enricher, {}, kb, '阿努纳奇是谁')
        self.assertEqual(result['answer'], '阿努纳奇是苏美尔神话中的神。')
        self.assertEqual([item['path'] for item in result['sources']],
                         ['素材/2026-11/二.md', '素材/2026-11/一.md'])
        self.assertEqual(result['model'], 'test-model')
        self.assertEqual(kb.search_calls, ['阿努纳奇是谁'])
        user_message = enricher.calls[0][1]['content']
        self.assertIn('用户问题：阿努纳奇是谁', user_message)
        self.assertIn('[笔记 1] 二', user_message)
        self.assertIn('第二篇内容。', user_message)
        # frontmatter 与 HTML 注释不进提示词
        self.assertNotIn('<!-- AUTO:START -->', user_message)
        self.assertNotIn('status: 已加工', user_message)
        system_message = enricher.calls[0][0]['content']
        self.assertIn('归藏', system_message)
        self.assertIn('知识库里还没有相关内容', system_message)

    def test_long_note_is_truncated(self):
        long_note = ('---\ntitle: "长"\n---\n' + '内容' * 4000, '长')
        kb = FakeKB({'素材/长.md': long_note}, hits=[('素材/长.md', '长')])
        enricher = FakeEnricher()
        chat.answer(enricher, {}, kb, '讲了什么')
        user_message = enricher.calls[0][1]['content']
        self.assertIn('…（后略）', user_message)
        self.assertLess(len(user_message), chat.NOTE_CHARS + 1200)

    def test_note_without_hits_falls_back_to_tree(self):
        class TreeKB(FakeKB):
            def tree(self):
                return {'count': 1, 'items': [{'name': '素材', 'type': 'directory', 'children': [
                    {'name': '2026-11', 'type': 'directory', 'children': [
                        {'name': '一.md', 'type': 'file', 'path': '素材/2026-11/一.md'}]}]}]}

        kb = TreeKB({'素材/2026-11/一.md': NOTE})
        enricher = FakeEnricher()
        result = chat.answer(enricher, {}, kb, '随便问问')
        self.assertEqual([item['path'] for item in result['sources']], ['素材/2026-11/一.md'])
        self.assertIn('测试笔记', enricher.calls[0][1]['content'])


if __name__ == '__main__':
    unittest.main()
