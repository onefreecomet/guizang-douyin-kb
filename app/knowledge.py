"""File-backed, AI-free Obsidian material publisher and rebuildable SQLite cache.

All returned markdown/snippets are untrusted plain text; rendering/escaping is the
HTTP client's responsibility. Paths in responses are POSIX paths relative to vault.
Only one vault subfolder is managed; topic/series pages are never rewritten.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

try:
    from naming import clean_title, safe_name
except ImportError:
    from scripts.naming import clean_title, safe_name

SUBDIR = '90 - 抖音精选'
_BAD_SUBDIR = set('\\/:*?"<>|')
_BAD_CHARS = set('\r\n\t')


def _subdir_name(value):
    """知识库子目录必须是 vault 下的一层普通目录名，不能是路径。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('知识库子目录不能为空')
    name = value.strip()
    if name != value:
        raise ValueError('知识库子目录不能有首尾空格')
    if len(name) > 60 or name in ('.', '..'):
        raise ValueError('知识库子目录名称无效')
    if any(c in name for c in _BAD_SUBDIR) or _BAD_CHARS & set(name):
        raise ValueError('知识库子目录不能包含路径分隔符或非法字符')
    return name


START = '<!-- AUTO:START -->'
END = '<!-- AUTO:END -->'
TOPIC_DIR = '主题'
SERIES_DIR = '系列'
INDEX_NAME = '00 - 索引.md'
_LOCK_NAME = '.knowledge.lock'
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_KEY = re.compile(r'^([^\s:#][^:\r\n]*):[ \t]*(.*)$')
_OWNED = {'title', 'video_id', 'url', 'author', 'saved_at', 'duration',
          'source', 'favorite', 'keywords', 'processed_at',
          'ai_summary', 'topics', 'series', 'series_index', 'ai_model', 'ai_at'}
_AI_OWNED = ('ai_summary', 'key_points', 'topics', 'series', 'series_index', 'ai_model', 'ai_at')
_PAGE_FIELDS = ('type', 'topic', 'parent', 'series', 'aliases', 'merged_into',
                'kept_page', 'updated_at')
_FROZEN = {'已定稿', '冻结'}
_CONFUSED = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _page_name(value, field='名称'):
    """Topic/series names become file names, so they must be plain and safe."""
    name = _CONFUSED.sub(' ', str(value or '')).strip()
    name = re.sub(r'\s+', ' ', name).strip(' .')
    if not name or len(name) > 60:
        raise ValueError(field + '不合法')
    return name


def _value(text):
    """Decode a frontmatter value written by json.dumps, or hand-written text."""
    raw = str(text if text is not None else '').strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _names(value):
    """Normalise AI/hand-written topic lists into de-duplicated names."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    names = []
    for entry in value:
        name = entry.get('name') if isinstance(entry, dict) else entry
        try:
            name = _page_name(name)
        except ValueError:
            continue
        if name not in names:
            names.append(name)
    return names


def _unquote(value):
    value = value.strip()
    if value.startswith('"'):
        try:
            return json.JSONDecoder().raw_decode(value)[0]
        except (ValueError, TypeError):
            return value
    if value.startswith("'"):
        match = re.match(r"'((?:[^']|'')*)'", value)
        return match.group(1).replace("''", "'") if match else value
    return re.split(r'\s+#', value, maxsplit=1)[0].strip()


def _frontmatter(markdown):
    """Return raw field blocks as well as scalar values, without YAML rewriting."""
    match = re.match(r'\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)', markdown, re.S)
    if not match:
        if re.match(r'\A\ufeff?---(?:\r?\n|$)', markdown):
            raise ValueError('未闭合的 frontmatter，拒绝覆盖')
        return [], {}, markdown
    blocks, values = [], {}
    for line in match.group(1).splitlines(keepends=True):
        field = _KEY.match(line.rstrip('\r\n'))
        if field:
            key, value = field.groups()
            key = str(_unquote(key))
            if key in values:
                raise ValueError('frontmatter 字段重复: ' + key)
            values[key] = _unquote(value)
            blocks.append([key, line])
        elif line.startswith('#'):
            blocks.append([None, line])
        elif blocks:
            blocks[-1][1] += line
        else:
            blocks.append([None, line])
    return blocks, values, markdown[match.end():]


def _is_link(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _no_links(path):
    """Reject every existing symlink/junction component, including dangling links."""
    path = Path(path).absolute()
    for component in [*reversed(path.parents), path]:
        if _is_link(component):
            raise ValueError('不允许符号链接或 junction: ' + str(component))
    return path


class KnowledgeBase:
    def __init__(self, vault_path, subdir=SUBDIR, cache_path=None):
        self.subdir = _subdir_name(subdir)
        self.vault_path = _no_links(Path(vault_path).expanduser())
        if not self.vault_path.is_dir():
            raise ValueError('vault 必须是已存在的目录')
        self.root = self.vault_path / self.subdir
        self._check(self.root)
        self.root.mkdir(exist_ok=True)
        self.cache_path = None
        if cache_path is not None:
            cache = _no_links(Path(cache_path).expanduser())
            if cache.suffix.lower() not in {'.db', '.sqlite', '.sqlite3'}:
                raise ValueError('cache_path 必须是 SQLite 索引文件')
            if not cache.parent.is_dir():
                raise ValueError('索引父目录必须已存在')
            # SQLite can use sidecar files; reject linked sidecars as well.
            for suffix in ('', '-journal', '-wal', '-shm'):
                _no_links(Path(str(cache) + suffix))
            self.cache_path = cache
        key = os.path.normcase(str(self.root))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _check(self, path):
        path = _no_links(path)
        if not path.is_relative_to(self.root):
            raise ValueError('路径越出知识库目录')
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('路径越出知识库目录')
        return path

    def _relative(self, path):
        return path.relative_to(self.vault_path).as_posix()

    def _requested(self, relative_path):
        value = str(relative_path)
        if not value or '\\' in value or '\x00' in value:
            raise ValueError('必须使用知识库内相对路径')
        path = PurePosixPath(value)
        if path.is_absolute() or PureWindowsPath(value).drive or any(
                part in {'', '.', '..'} for part in value.split('/')):
            raise ValueError('路径越出知识库目录')
        if path.parts[0] == self.subdir:
            path = PurePosixPath(*path.parts[1:])
        target = self._check(self.root.joinpath(*path.parts))
        if target.suffix.lower() != '.md':
            raise ValueError('只能读取 Markdown 笔记')
        return target

    @contextmanager
    def _guard(self):
        # One process lock plus an OS byte/file lock coordinates distinct instances
        # and processes even when the SQLite cache is in memory or was deleted.
        with self._lock:
            lock_path = self._check(self.root / '.knowledge.lock')
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(lock_path, flags, 0o600)
            with os.fdopen(descriptor, 'r+b') as handle:
                if os.name == 'nt':
                    import msvcrt
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b'0')
                        handle.flush()
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('知识库正忙')
                            time.sleep(0.05)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _materials(self):
        base = self._check(self.root / '素材')
        if not base.exists():
            return []
        notes = []
        # os.walk with followlinks=False plus component validation also rejects
        # junctions on Windows rather than trusting recursive Path.glob behavior.
        for directory, dirs, files in os.walk(base, followlinks=False):
            self._check(Path(directory))
            dirs[:] = sorted(name for name in dirs
                             if not _is_link(Path(directory) / name))
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() == '.md' and not _is_link(path):
                    notes.append(self._check(path))
        return notes

    def _read(self, path):
        return self._check(path).read_bytes().decode('utf-8-sig')

    def _url(self, path):
        # Obsidian 不把查询串里的 '+' 还原成空格，必须用 %20（quote_via=quote）。
        return 'obsidian://open?' + urlencode({
            'vault': self.vault_path.name, 'file': self._relative(path)}, quote_via=quote)

    def _result(self, path, video_id, title, frozen, created):
        return {'path': self._relative(path), 'title': title, 'video_id': video_id,
                'frozen': frozen, 'created': created, 'obsidian_url': self._url(path)}

    def _atomic_write(self, path, text, previous=None):
        self._check(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._check(path)
        descriptor, temporary = tempfile.mkstemp(prefix='.knowledge-', suffix='.tmp', dir=path.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # Detect an editor/sync write during generation instead of losing it.
            self._check(path)
            if previous is None:
                if path.exists():
                    raise RuntimeError('笔记已被其他程序创建，请重试')
            elif self._read(path) != previous:
                raise RuntimeError('笔记已被其他程序修改，请重试')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def publish(self, item: dict, directory: Path, ai=None) -> dict:
        """Write one material note; ``ai`` carries validated AI fields or None."""
        directory = Path(directory)
        with self._guard():
            info_path = directory / '视频信息.txt'
            info = None
            def field(label):
                nonlocal info
                if info is None:
                    info = info_path.read_text(encoding='utf-8-sig') if info_path.is_file() else ''
                match = re.search(r'^' + re.escape(label) + r'[:：][ \t]*([^\r\n]*)', info, re.M)
                if not match:
                    return ''
                if match.group(1).strip():
                    return match.group(1).strip()
                following = info[match.end():].lstrip('\r\n').splitlines()
                next_line = following[0].strip() if following else ''
                return '' if re.match(r'^(?:视频ID|链接|博主|标题|时长)[:：]', next_line) else next_line
            video_id = str(item.get('video_id') or field('视频ID') or '').strip()
            if not video_id:
                state_path = directory / 'task_state.json'
                if state_path.is_file():
                    video_id = str(json.loads(state_path.read_text(encoding='utf-8-sig')).get('video_id') or '')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', video_id):
                raise ValueError('缺少有效 video_id')
            found = []
            for candidate in self._materials():
                old = self._read(candidate)
                _, values, _ = _frontmatter(old)
                if str(values.get('video_id', '')) == video_id:
                    found.append((candidate, old, values))
            if len(found) > 1:
                raise ValueError('多个素材使用同一 video_id，拒绝覆盖')
            if found and found[0][2].get('status') in {'已定稿', '冻结'}:
                path, _, values = found[0]
                return self._result(path, video_id, values.get('title', path.stem), True, False)

            title = str(item.get('title') or field('标题') or video_id).strip()
            raw_path, copy_path = directory / '语音转文字.txt', directory / '视频文案.txt'
            raw = raw_path.read_text(encoding='utf-8-sig').strip() if raw_path.is_file() else ''
            copy = copy_path.read_text(encoding='utf-8-sig').strip() if copy_path.is_file() else ''
            transcript = raw or copy
            if not transcript:
                raise ValueError('转写或视频文案正文为空')
            if START in transcript or END in transcript:
                raise ValueError('原文包含保留的 AUTO 标记')
            duration = str(item.get('duration') or field('时长') or '')
            if not duration:
                match = re.search(r'^(?:Duration|时长)\s*[:：]\s*(.+)$', raw, re.M | re.I)
                duration = match.group(1).strip() if match else ''
                if not duration and raw:
                    header = re.match(r'^\d{4}-\d{2}-\d{2}[^\r\n|]*\|([^\r\n]+)', raw)
                    duration = header.group(1).strip() if header else ''
            keywords = item.get('keywords')
            if not keywords:
                match = re.search(r'^(?:Keywords?|关键词)\s*[:：]\s*(.+)$', raw, re.M | re.I)
                keywords = re.split(r'\s*[,，、/;；]\s*', match.group(1)) if match else []
            if isinstance(keywords, str):
                keywords = re.split(r'\s*[,，、/;；]\s*', keywords)
            keywords = list(dict.fromkeys(str(k).strip() for k in keywords if str(k).strip()))
            saved_at = str(item.get('saved_at') or item.get('date') or datetime.now().astimezone().date().isoformat())
            try:
                month = datetime.fromisoformat(saved_at.replace('Z', '+00:00')).strftime('%Y-%m')
            except ValueError as error:
                raise ValueError('saved_at 必须为 ISO 日期/时间') from error
            processed = datetime.now().astimezone().isoformat(timespec='seconds')
            metadata = {'title': title, 'video_id': video_id,
                        'url': str(item.get('url') or field('链接') or 'https://www.douyin.com/video/' + video_id),
                        'author': str(item.get('author') or field('博主') or ''),
                        'saved_at': saved_at, 'duration': duration, 'source': '抖音收藏夹',
                        'favorite': str(item.get('favorite') or '有帮助'), 'keywords': keywords,
                        'processed_at': processed}
            previous, old_blocks, old_values = None, [], {}
            if found:
                previous = found[0][1]
                old_blocks, old_values, _ = _frontmatter(previous)
            ai_fields, points = {}, []
            summary = ''
            body_topics, body_series, body_number = [], '', 0
            if ai:
                summary = str(ai.get('summary') or '').strip()
                points = [str(point).strip() for point in (ai.get('key_points') or [])
                          if str(point).strip()]
                topics = _names(ai.get('topics'))[:6]
                series = ''
                raw_series = ai.get('series')
                if isinstance(raw_series, dict):
                    series = _page_name(raw_series.get('name'), '系列名称')
                    number = raw_series.get('index', ai.get('series_index'))
                else:
                    number = ai.get('series_index')
                    if str(raw_series or '').strip():
                        series = _page_name(raw_series, '系列名称')
                ai_fields = {'ai_summary': summary, 'key_points': points, 'topics': topics,
                             'series': series, 'ai_model': str(ai.get('model') or ''),
                             'ai_at': str(ai.get('at') or processed)}
                if series:
                    number = int(number) if isinstance(number, int) or str(number or '').isdigit() else 1
                    ai_fields['series_index'] = number if number > 0 else 1
                body_topics, body_series = topics, series
                body_number = ai_fields.get('series_index', 0)
            elif previous is not None:
                # Republishing without new AI output keeps the earlier AI body.
                summary = str(_value(old_values.get('ai_summary')) or '')
                points = [str(point) for point in (_value(old_values.get('key_points')) or [])
                          if str(point).strip()]
                body_topics = _names(_value(old_values.get('topics')))
                body_series = str(_value(old_values.get('series')) or '')
                number = _value(old_values.get('series_index'))
                body_number = number if isinstance(number, int) and number > 0 else 0
            old_status = str(old_values.get('status') or '')
            if ai and (previous is None or old_status in {'', '待加工'}):
                status = '已加工'
            else:
                status = old_status
            lines = [key + ': ' + json.dumps(value, ensure_ascii=False)
                     for key, value in metadata.items()]
            for key in _AI_OWNED:
                if key in ai_fields:
                    lines.append(key + ': ' + json.dumps(ai_fields[key], ensure_ascii=False))
                elif key in old_values and previous is not None:
                    # An AI-less republish must not silently drop earlier AI fields.
                    kept = [text.rstrip('\r\n') for name, text in old_blocks if name == key]
                    if kept:
                        lines.append(kept[0])
            if status:
                lines.append('status: ' + status)
                old_status = ''  # never duplicate the field from the preserved block
            generated = '\n'.join(lines) + '\n'
            content = ''
            if summary:
                quoted = summary.replace('\r\n', '\n').replace('\n', '\n> ')
                content += '> [!abstract] 摘要\n> ' + quoted + '\n\n'
            if points:
                content += '## 要点\n\n' + '\n'.join(
                    str(number) + '. ' + point.replace('\n', ' ')
                    for number, point in enumerate(points, 1)) + '\n\n'
            content += ('## 原文（带时间戳）\n\n' + transcript + '\n\n## 基础信息\n\n'
                        + '关键词：' + (' / '.join(keywords) or '未提供') + '\n\n时长：'
                        + (duration or '未提供') + '\n')
            notes = []
            if body_topics:
                notes.append('主题：' + ' · '.join(
                    self._wikilink(TOPIC_DIR, name) for name in body_topics))
            if body_series:
                label = self._wikilink(SERIES_DIR, body_series)
                if body_number:
                    label += '（第 ' + str(body_number) + ' 集）'
                notes.append('系列：' + label)
            if notes:
                content += '\n' + '\n'.join(notes) + '\n'
            if copy and copy != transcript:
                if START in copy or END in copy:
                    raise ValueError('文案包含保留的 AUTO 标记')
                content += '\n## 视频文案\n\n' + copy + '\n'
            video_file = next(iter(sorted(directory.glob('*.mp4'))), None)
            if video_file:
                uri = video_file.absolute().as_uri().replace('(', '%28').replace(')', '%29')
                content += '\n[打开本地视频](' + uri + ')\n'
            auto = START + '\n' + content + END
            wrote_status = bool(status)
            if found:
                path = found[0][0]
                body = _frontmatter(previous)[2]
                skip = _OWNED | set(_AI_OWNED) | ({'status'} if wrote_status else set())
                unknown = ''.join(text if text.endswith('\n') else text + '\n'
                                  for key, text in old_blocks if key not in skip)
                if body.count(START) != body.count(END) or body.count(START) > 1:
                    raise ValueError('AUTO 区块损坏，拒绝覆盖')
                if START in body:
                    start, end = body.index(START), body.index(END)
                    if end < start:
                        raise ValueError('AUTO 区块顺序错误')
                    body = body[:start] + auto + body[end + len(END):]
                else:
                    body = body + ('\n\n' if body else '') + auto + '\n'
                markdown = '---\n' + generated + unknown + '---\n' + body
            else:
                name = safe_name(clean_title(title.replace('|', '｜').replace('[', '').replace(']', '').replace('#', '')), 60) or video_id
                path = self._check(self.root / '素材' / month / (name + '.md'))
                if path.exists():
                    path = path.with_name(name + '-' + video_id[-4:] + '.md')
                number = 2
                while path.exists():
                    path = path.with_name(name + '-' + video_id + '-' + str(number) + '.md')
                    number += 1
                markdown = ('---\n' + generated + ('' if wrote_status else 'status: 待加工\n')
                            + '---\n\n' + auto
                            + '\n\n## 我的笔记\n\n<!-- 这里永远由你自己写，程序不会动 -->\n')
            self._atomic_write(path, markdown, previous)
            self._refresh_pages()
            connection, _ = self._reindex()
            connection.close()
            result = self._result(path, video_id, title, False, previous is None)
            result.update({'topics': list(ai_fields.get('topics') or []),
                           'series': str(ai_fields.get('series') or ''), 'status': status,
                           'indexed': True})
            return result

    def _reindex(self):
        if self.cache_path is not None:
            for suffix in ('', '-journal', '-wal', '-shm'):
                _no_links(Path(str(self.cache_path) + suffix))
        connection = sqlite3.connect(str(self.cache_path) if self.cache_path else ':memory:', timeout=30)
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS knowledge_notes (path TEXT PRIMARY KEY, title TEXT NOT NULL, markdown TEXT NOT NULL)')
                connection.execute('DELETE FROM knowledge_notes')
                rows = []
                for path in self._materials():
                    markdown = self._read(path)
                    _, values, _ = _frontmatter(markdown)
                    rows.append((self._relative(path), str(values.get('title') or path.stem), markdown))
                connection.executemany('INSERT INTO knowledge_notes VALUES (?, ?, ?)', rows)
            return connection, len(rows)
        except Exception:
            connection.close()
            raise

    def reindex(self) -> dict:
        with self._guard():
            connection, count = self._reindex()
            connection.close()
            return {'count': count, 'indexed': count}

    def tree(self) -> dict:
        with self._guard():
            items, directories, count = [], {}, 0
            for path in self._materials():
                parts = path.relative_to(self.root).parts
                children = items
                for depth, part in enumerate(parts[:-1], 1):
                    key = parts[:depth]
                    if key not in directories:
                        node = {'path': PurePosixPath(self.subdir, *key).as_posix(),
                                'name': part, 'type': 'directory', 'children': []}
                        directories[key] = node
                        children.append(node)
                    children = directories[key]['children']
                children.append({'path': self._relative(path), 'name': path.name, 'type': 'file'})
                count += 1
            return {'items': items, 'count': count}

    def search(self, query) -> dict:
        query = str(query or '').strip()
        with self._guard():
            connection, _ = self._reindex()
            try:
                if not query:
                    return {'items': [], 'count': 0}
                # instr is literal Unicode substring search, including Chinese and
                # SQL/FTS punctuation. No tokenizer or Dataview dependency.
                rows = connection.execute(
                    'SELECT path, title, markdown FROM knowledge_notes '
                    'WHERE instr(lower(title), lower(?)) > 0 OR instr(lower(markdown), lower(?)) > 0 ORDER BY path',
                    (query, query)).fetchall()
                items = []
                for path, title, markdown in rows:
                    _, _, body = _frontmatter(markdown)
                    haystack = body if query.casefold() in body.casefold() else (
                        title if query.casefold() in title.casefold() else markdown)
                    position = haystack.casefold().find(query.casefold())
                    start = max(0, position - 60)
                    items.append({'path': path, 'title': title,
                                  'snippet': haystack[start:start + max(200, len(query))]})
                return {'items': items, 'count': len(items)}
            finally:
                connection.close()

    def note(self, relative_path) -> dict:
        with self._guard():
            return self._note(self._requested(relative_path))

    def _note(self, path) -> dict:
        markdown = self._read(path)
        _, values, _ = _frontmatter(markdown)
        return {'path': self._relative(path), 'title': str(values.get('title') or path.stem),
                'markdown': markdown, 'obsidian_url': self._url(path)}

    # -- derived pages: topics, series and the vault index -----------------
    def _wikilink(self, directory, name):
        target = PurePosixPath(self.subdir, directory, name).as_posix()
        return '[[' + target + '|' + name + ']]'

    def _link(self, path, title):
        return '[[' + self._relative(path)[:-3] + '|' + title.replace('[[', '').replace(']]', '') + ']]'

    def _pages(self, directory):
        base = self._check(self.root / directory)
        if not base.exists():
            return []
        return [self._check(base / name) for name in sorted(os.listdir(base))
                if name.lower().endswith('.md') and not _is_link(base / name)
                and (base / name).is_file()]

    def _topic_path(self, name):
        return self._check(self.root / TOPIC_DIR / (_page_name(name, '主题名称') + '.md'))

    def _series_path(self, name):
        return self._check(self.root / SERIES_DIR / (_page_name(name, '系列名称') + '.md'))

    def _member(self, path):
        markdown = self._read(path)
        _, values, _ = _frontmatter(markdown)
        number = _value(values.get('series_index'))
        return {'path': path, 'title': str(values.get('title') or path.stem),
                'saved_at': str(values.get('saved_at') or ''),
                'duration': str(values.get('duration') or ''),
                'status': str(values.get('status') or ''),
                'topics': _names(_value(values.get('topics'))),
                'series': str(values.get('series') or ''),
                'series_index': number if isinstance(number, int) and number > 0 else 0}

    def _members(self):
        members = []
        for path in self._materials():
            try:
                members.append(self._member(path))
            except ValueError:
                # A hand-edited broken frontmatter must not break every refresh.
                continue
        return members

    def _properties(self, properties):
        return ''.join(key + ': ' + json.dumps(value, ensure_ascii=False) + '\n'
                       for key, value in properties.items())

    def _page_text(self, properties, auto, previous):
        if previous is None:
            return ('---\n' + self._properties(properties) + '---\n\n' + START + '\n' + auto + END
                    + '\n\n## 我的笔记\n\n<!-- 这里永远由你自己写，程序不会动 -->\n')
        blocks, _, body = _frontmatter(previous)
        if body.count(START) > 1 or body.count(START) != body.count(END):
            raise ValueError('AUTO 区块损坏，拒绝覆盖')
        block = START + '\n' + auto + END
        if START in body:
            start, end = body.index(START), body.index(END)
            if end < start:
                raise ValueError('AUTO 区块顺序错误')
            body = body[:start] + block + body[end + len(END):]
        else:
            body = block + '\n\n' + body.lstrip('\n')
        unknown = ''.join(text if text.endswith('\n') else text + '\n'
                          for key, text in blocks if key not in _PAGE_FIELDS)
        return '---\n' + self._properties(properties) + unknown + '---\n' + body

    def _write_page(self, path, properties, auto, merge=()):
        previous = self._read(path) if path.exists() else None
        if previous is not None:
            _, old, _ = _frontmatter(previous)
            if str(old.get('kept_page', '')).strip().lower() == 'true':
                return False
            for key in merge:
                if key not in properties and key in old:
                    properties[key] = _value(old[key])
        markdown = self._page_text(properties, auto, previous)
        if markdown == previous:
            return False
        self._atomic_write(path, markdown, previous)
        return True

    def _topic_body(self, name, group):
        ordered = sorted(group, key=lambda member: (member['saved_at'], member['title']), reverse=True)
        lines = ['## 简介', '', '本主题收录 ' + str(len(group)) + ' 条收藏。', '',
                 '## 相关视频（' + str(len(group)) + '）', '']
        for member in ordered:
            line = '- ' + self._link(member['path'], member['title'])
            if member['saved_at']:
                line += ' · ' + member['saved_at'][:10]
            if member['duration']:
                line += ' · ' + member['duration']
            lines.append(line)
        return '\n'.join(lines) + '\n'

    def _series_body(self, name, group):
        ordered = sorted(group, key=lambda member: (member['series_index'] or 10 ** 6,
                                                    member['saved_at'], member['title']))
        lines = ['## 系列综述', '', '共 ' + str(len(ordered)) + ' 集。', '', '## 分集', '']
        for position, member in enumerate(ordered, 1):
            line = '- 第 ' + str(member['series_index'] or position) + ' 集 · '
            line += self._link(member['path'], member['title'])
            if member['saved_at']:
                line += ' · ' + member['saved_at'][:10]
            lines.append(line)
        return '\n'.join(lines) + '\n'

    def _index_body(self, members, topics, series):
        recent = sorted(members, key=lambda member: (member['saved_at'], member['title']), reverse=True)
        lines = ['统计：' + str(len(members)) + ' 条素材 · ' + str(len(topics)) + ' 个主题 · '
                 + str(len(series)) + ' 个系列', '']
        if recent:
            lines += ['## 最近入库', '', '| 收藏日期 | 标题 | 主题 | 系列 |',
                      '| --- | --- | --- | --- |']
            for member in recent[:20]:
                cells = [(member['saved_at'][:10] or '—'), self._link(member['path'], member['title']),
                         '、'.join(member['topics']) or '—', member['series'] or '—']
                lines.append('| ' + ' | '.join(cell.replace('|', '\\|') for cell in cells) + ' |')
        if topics:
            lines += ['', '## 按主题浏览', '']
            for name in sorted(topics):
                lines.append('- ' + self._wikilink(TOPIC_DIR, name) + '（' + str(len(topics[name])) + '）')
        if series:
            lines += ['', '## 系列', '']
            for name in sorted(series):
                lines.append('- ' + self._wikilink(SERIES_DIR, name) + '（' + str(len(series[name])) + ' 集）')
        pending = [member for member in recent if member['status'] not in _FROZEN and member['status'] != '已加工']
        if pending:
            lines += ['', '## 待处理', '']
            for member in pending[:20]:
                lines.append('- ' + self._link(member['path'], member['title'])
                             + (' · ' + member['status'] if member['status'] else ''))
        return '\n'.join(lines) + '\n'

    def _safe_page(self, path, properties, auto, merge=()):
        """Write a derived page; a hand-broken page must not block the pipeline."""
        try:
            return bool(self._write_page(path, properties, auto, merge=merge))
        except ValueError:
            return False

    def _refresh_pages(self):
        """Rebuild topic/series/index pages. Caller must hold the guard."""
        members = self._members()
        topics, series = {}, {}
        for member in members:
            for name in member['topics']:
                topics.setdefault(name, []).append(member)
            if member['series']:
                series.setdefault(member['series'], []).append(member)
        written = 0
        for name, group in topics.items():
            properties = {'type': 'topic', 'topic': name, 'aliases': [name]}
            written += self._safe_page(self._topic_path(name), properties,
                                       self._topic_body(name, group), merge=('parent',))
        for name, group in series.items():
            properties = {'type': 'series', 'series': name, 'aliases': [name]}
            written += self._safe_page(self._series_path(name), properties,
                                       self._series_body(name, group))
        written += self._safe_page(self._check(self.root / INDEX_NAME),
                                   {'type': 'index'},
                                   self._index_body(members, topics, series))
        return {'notes': len(members), 'topics': len(topics), 'series': len(series),
                'pages': written}

    def stats(self):
        """Aggregate counts for the console stats panel (read only)."""
        with self._guard():
            members = self._members()
        topics, months, statuses, series = {}, {}, {}, set()
        for member in members:
            for name in member['topics']:
                topics[name] = topics.get(name, 0) + 1
            if member.get('series'):
                series.add(member['series'])
            month = str(member.get('saved_at') or '')[:7]
            if len(month) == 7:
                months[month] = months.get(month, 0) + 1
            status = str(member.get('status') or '未标注')
            statuses[status] = statuses.get(status, 0) + 1
        top = sorted(topics.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
        return {
            'notes': len(members),
            'topics': len(topics),
            'series': len(series),
            'months': [{'month': m, 'count': c} for m, c in sorted(months.items())[-12:]],
            'top_topics': [{'name': n, 'count': c} for n, c in top],
            'statuses': statuses,
        }
    def topic_names(self) -> list:
        with self._guard():
            names = {name for member in self._members() for name in member['topics']}
            names.update(path.stem for path in self._pages(TOPIC_DIR))
            return sorted(names)

    def topics(self) -> dict:
        with self._guard():
            members = self._members()
            counts = {}
            for member in members:
                for name in member['topics']:
                    counts[name] = counts.get(name, 0) + 1
            rows, known, aliases = [], set(), set()
            for path in self._pages(TOPIC_DIR):
                try:
                    _, values, _ = _frontmatter(self._read(path))
                except ValueError:
                    continue
                name = str(values.get('topic') or path.stem)
                if str(values.get('kept_page', '')).strip().lower() == 'true':
                    # Merged alias pages keep frozen notes' links working but are
                    # not offered as separate topics.
                    aliases.add(name)
                    continue
                known.add(name)
                rows.append({'name': name, 'parent': str(values.get('parent') or ''),
                             'count': counts.get(name, 0), 'path': self._relative(path)})
            rows += [{'name': name, 'parent': '', 'count': count, 'path': ''}
                     for name, count in counts.items()
                     if name not in known and name not in aliases]
            rows.sort(key=lambda row: (-row['count'], row['name']))
            return {'items': rows, 'count': len(rows)}

    def series(self) -> dict:
        with self._guard():
            members = self._members()
            counts = {}
            for member in members:
                if member['series']:
                    counts[member['series']] = counts.get(member['series'], 0) + 1
            rows, known = [], set()
            for path in self._pages(SERIES_DIR):
                try:
                    _, values, _ = _frontmatter(self._read(path))
                except ValueError:
                    continue
                name = str(values.get('series') or path.stem)
                known.add(name)
                rows.append({'name': name, 'count': counts.get(name, 0), 'path': self._relative(path)})
            rows += [{'name': name, 'count': count, 'path': ''}
                     for name, count in counts.items() if name not in known]
            rows.sort(key=lambda row: (-row['count'], row['name']))
            return {'items': rows, 'count': len(rows)}

    def refresh(self) -> dict:
        with self._guard():
            result = self._refresh_pages()
            return {'topics': result['topics'], 'series': result['series'],
                    'index': self._relative(self._check(self.root / INDEX_NAME)),
                    'notes': result['notes']}

    def topic_note(self, name) -> dict:
        name = _page_name(name, '主题名称')
        with self._guard():
            path = self._topic_path(name)
            if not path.exists():
                group = [member for member in self._members() if name in member['topics']]
                self._write_page(path, {'type': 'topic', 'topic': name, 'aliases': [name]},
                                 self._topic_body(name, group))
            return self._note(path)

    def series_note(self, name) -> dict:
        name = _page_name(name, '系列名称')
        with self._guard():
            path = self._series_path(name)
            if not path.exists():
                group = [member for member in self._members() if member['series'] == name]
                self._write_page(path, {'type': 'series', 'series': name, 'aliases': [name]},
                                 self._series_body(name, group))
            return self._note(path)

    def merge_topics(self, source, target) -> dict:
        source = _page_name(source, '原主题名称')
        target = _page_name(target, '目标主题名称')
        if source == target:
            raise ValueError('原主题与目标主题不能相同')
        with self._guard():
            moved, frozen = 0, 0
            for path in self._materials():
                markdown = self._read(path)
                blocks, values, body = _frontmatter(markdown)
                names = _names(_value(values.get('topics')))
                if source not in names:
                    continue
                if str(values.get('status') or '') in _FROZEN:
                    frozen += 1
                    continue
                merged = list(dict.fromkeys(
                    target if name == source else name for name in names))
                text = '[' + ', '.join(json.dumps(name, ensure_ascii=False) for name in merged) + ']'
                for block in blocks:
                    if block[0] == 'topics':
                        block[1] = 'topics: ' + text + '\n'
                        break
                else:
                    continue
                lines = [text_ if text_.endswith('\n') else text_ + '\n'
                         for _, text_ in blocks]
                rewritten = []
                for line in body.split('\n'):
                    if line.startswith('主题：'):
                        line = '主题：' + ' · '.join(self._wikilink(TOPIC_DIR, name)
                                                     for name in merged)
                    rewritten.append(line)
                updated = '---\n' + ''.join(lines) + '---\n' + '\n'.join(rewritten)
                self._atomic_write(path, updated, markdown)
                moved += 1
            alias = self._topic_path(source)
            previous = self._read(alias) if alias.exists() else None
            properties = {'type': 'topic', 'topic': source, 'aliases': [source],
                          'merged_into': target, 'kept_page': True}
            body = ('已合并到 ' + self._wikilink(TOPIC_DIR, target)
                    + '，本页保留为别名页。\n')
            self._atomic_write(alias, self._page_text(properties, body, previous), previous)
            self._refresh_pages()
            return {'source': source, 'target': target, 'moved': moved, 'frozen': frozen,
                    'kept_page': True}
