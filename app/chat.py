"""Knowledge-base Q&A: answer questions from the user's own Douyin notes.

The model gets a description of this project plus the most relevant notes (and the
topic/series overview), so it can answer both "这条视频讲了什么" and "这个程序是怎么
工作的". Notes are untrusted text and are only used as context.
"""
import re

NOTE_LIMIT = 3
NOTE_CHARS = 3000
QUESTION_MAX = 500
OVERVIEW_TOPICS = 30
KEYWORD_LIMIT = 6

_PROGRAM_DEFAULTS = {'favorite': '有帮助', 'schedule_time': '04:00', 'kb_subdir': '90 - 抖音精选'}


def system_prompt(config=None):
    """程序说明跟着当前配置走：改了收藏夹、运行时间或子目录后，助手不会再说错。"""
    cfg = dict(_PROGRAM_DEFAULTS)
    if isinstance(config, dict):
        for key in cfg:
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                cfg[key] = value.strip()
    favorite = cfg['favorite']
    schedule = cfg['schedule_time']
    subdir = cfg['kb_subdir']
    return (
        '你是「归藏」的知识库助手，服务于用户自己的 Obsidian 知识库 '
        '「' + subdir + '」。\n'
        '\n'
        '# 这个程序是怎么工作的（程序说明，可以直接根据它回答）\n'
        '- 用户在手机抖音把视频收藏到收藏夹「' + favorite + '」，这台常开的电脑每天 ' + schedule +
        '（北京时间）自动扫描该收藏夹；其它时间不会自动扫描，需要时可在控制台点「立即同步」。\n'
        '- 处理分五步：01 扫描收藏 → 02 下载校验（保存到本机「抖音视频下载」下的日期目录，'
        '登录 cookies 失效或出现验证码时会暂停并提示人工处理）→ 03 语音转写（用飞书妙记，'
        '失败可在控制台「任务队列」里重试，已创建的妙记会复用，不会重复计费）→ '
        '04 写入笔记（Markdown 写进 Obsidian）→ 05 AI 加工（生成摘要、要点、主题、系列）。\n'
        '- 每篇素材笔记的 frontmatter 记录标题、视频 ID、链接、作者、收藏时间、时长、关键词、主题、'
        '系列、AI 摘要、处理状态（待加工/已加工/已定稿/冻结）；正文里 `<!-- AUTO:START -->` 与 '
        '`<!-- AUTO:END -->` 之间由程序维护，区块外是「我的笔记」，永远由用户自己写、程序不动；'
        '把状态改成已定稿或冻结后，程序不会再改这篇笔记的任何内容。\n'
        '- 知识库结构：' + subdir + '/下有 素材/YYYY-MM/、主题/、系列/ 和 00 - 索引.md；'
        '主题页按 AI 归类聚合，系列页按集数排序；索引页每天刷新（最近入库、按主题浏览、待处理）。\n'
        '- AI 默认每天最多加工 200 条，用量在控制台「自动同步」页可见；密钥、通知接收人存在本机配置里，'
        '不会写进笔记。\n'
        '\n'
        '# 回答规则\n'
        '1. 问视频内容（讲了什么、观点、结论）时，只依据下面给出的参考笔记回答，'
        '引用时写出笔记标题；笔记里没有就直说「知识库里还没有相关内容」，不要编造视频里没出现的信息。\n'
        '2. 问这个程序怎么用、怎么配置、某个功能是什么时，依据上面的「程序说明」回答；'
        '这类回答不用勉强关联笔记。\n'
        '3. 两者都能答时先给结论再给依据；用简体中文，简洁具体，可用 Markdown 列表和加粗。\n'
    )


SYSTEM_PROMPT = system_prompt()
_TAG = re.compile(r'<!--.*?-->', re.S)
_FRONTMATTER = re.compile(r'\A---\n.*?\n---\n', re.S)
_SPLIT = re.compile(r'[\s，。！？、；：,.!?;:()（）\[\]【】"\'“”‘’]+')


def _plain(markdown):
    """去掉 frontmatter 与注释，留下可读正文。"""
    text = _FRONTMATTER.sub('', markdown or '')
    return _TAG.sub('', text).strip()


def keywords(question, limit=KEYWORD_LIMIT):
    """把问题拆成可用于检索的词，按长度优先，避免整句永远搜不到。"""
    parts = [part.strip() for part in _SPLIT.split(question or '')]
    words = []
    for part in parts:
        if len(part) >= 2 and part not in words:
            words.append(part)
    words.sort(key=len, reverse=True)
    return words[:limit]


def select_notes(kb, question, limit=NOTE_LIMIT):
    """按问题挑相关笔记：先整句，再逐个关键词，最后退回最近入库。"""
    picked = []
    seen = set()

    def add(items):
        for item in items or []:
            path = item.get('path')
            if not path or path in seen:
                continue
            seen.add(path)
            picked.append({'path': path, 'title': item.get('title') or path})
            if len(picked) >= limit:
                return True
        return False

    try:
        if add((kb.search(question) or {}).get('items')):
            return picked
    except Exception:
        pass
    for word in keywords(question):
        if word == question:  # 整句已经搜过一次
            continue
        try:
            if add((kb.search(word) or {}).get('items')):
                return picked
        except Exception:
            continue
    if picked:
        return picked
    try:
        for entry in kb.tree().get('items') or []:
            for month in entry.get('children') or []:
                for note in reversed(month.get('children') or []):
                    path = note.get('path')
                    if path and path not in seen:
                        seen.add(path)
                        picked.append({'path': path, 'title': note.get('name') or path})
                        if len(picked) >= limit:
                            return picked
    except Exception:
        pass
    return picked


def _overview(kb):
    try:
        topics = [item.get('name') for item in (kb.topics().get('items') or [])][:OVERVIEW_TOPICS]
    except Exception:
        topics = []
    try:
        series = [item.get('name') for item in (kb.series().get('items') or [])]
    except Exception:
        series = []
    return topics, series


def build_messages(question, notes, topics, series, total, config=None):
    lines = ['知识库概览：共 ' + str(total) + ' 篇素材笔记。']
    if topics:
        lines.append('已有主题：' + '、'.join(topics))
    if series:
        lines.append('已有系列：' + '、'.join(series))
    if notes:
        lines.append('')
        lines.append('参考笔记：')
    for index, note in enumerate(notes, start=1):
        body = _plain(note.get('markdown') or '')
        if len(body) > NOTE_CHARS:
            body = body[:NOTE_CHARS] + '…（后略）'
        lines.append('')
        lines.append('[笔记 ' + str(index) + '] ' + str(note.get('title')))
        lines.append(body)
    lines.append('')
    lines.append('用户问题：' + question)
    return [{'role': 'system', 'content': system_prompt(config)},
            {'role': 'user', 'content': '\n'.join(lines)}]


def answer(enricher, config, kb, question):
    """回答用户提问；返回 {'answer', 'sources', 'model'}。"""
    text = str(question or '').strip()
    if not text:
        raise ValueError('请输入要问的问题')
    if len(text) > QUESTION_MAX:
        raise ValueError('问题太长了，请控制在 ' + str(QUESTION_MAX) + ' 字以内')
    total = int(kb.tree().get('count') or 0)
    if total == 0:
        return {'answer': '知识库里还没有笔记。等收藏处理完成后再来问，或者先去控制台点「立即同步」。',
                'sources': [], 'model': ''}
    picked = select_notes(kb, text)
    notes = []
    for item in picked:
        try:
            note = kb.note(item['path'])
        except (ValueError, OSError):
            continue
        notes.append({'path': note.get('path'), 'title': note.get('title'), 'markdown': note.get('markdown')})
    topics, series = _overview(kb)
    messages = build_messages(text, notes, topics, series, total, config)
    result = enricher.chat(config, messages)
    sources = [{'path': note['path'], 'title': note['title']} for note in notes]
    return {'answer': result.get('text') or '', 'sources': sources, 'model': result.get('model') or ''}
