"""文件名清理规则，被下载脚本和整理脚本共用"""
import re

# Windows 文件名里不允许的字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {f"COM{i}" for i in range(1, 10)}
             | {f"LPT{i}" for i in range(1, 10)})


def safe_name(text: str, max_chars: int = 50) -> str:
    """把标题清理成合法的 Windows 文件名，并截断到指定字数"""
    if not text:
        return ""
    t = _ILLEGAL.sub(" ", str(text))
    t = re.sub(r"\s+", " ", t).strip()
    # 去掉结尾的话题标签和 @ 提及，标题里这些是噪声
    t = re.sub(r"(\s*#[^\s#]+)+\s*$", "", t)
    t = re.sub(r"(\s*@[^\s@]+)+\s*$", "", t)
    t = t.lstrip("@# ")
    t = t.strip(". ，。、,.;；")
    if not t:
        return ""
    if t.upper() in _RESERVED:
        t = "_" + t
    t = t[:max_chars].strip(" .，。、,.;；！？!?")
    return t


# 卡片文本里常见的前缀标记
_MARKER = re.compile(r"^(共创|置顶|广告|热榜|直播中|预约)\s+")
_DURATION = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s+")
# 播放量：7867 / 1.2万 / 10.6万 / 1.2亿，后面必须跟空格才认为它是独立字段
_PLAYS = re.compile(r"^\d+(?:\.\d+)?\s*(?:万|亿)?\s+")
# 结尾的发布时间
_PUBTIME = re.compile(r"\s*[·•]\s*(?:\d+\s*(?:秒|分钟|小时|天)前|昨天|前天|\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日)\s*$")
# 结尾的作者
_AUTHOR = re.compile(r"\s*@\s*[^\s@#·]+\s*$")


def clean_title(raw: str) -> str:
    """从抖音卡片的整段文本里剥出标题。

    纯规则匹配，没有语义理解：
    先剥尾部的作者和发布时间，再剥头部的标记、时长、播放量，
    最后处理抖音常见的「标题重复两遍」情况。
    """
    if not raw:
        return ""
    t = re.sub(r"\s+", " ", str(raw)).strip()

    # 尾部：发布时间 → 作者 → 话题标签（顺序不能反）
    for _ in range(3):
        before = t
        t = _PUBTIME.sub("", t).strip()
        t = _AUTHOR.sub("", t).strip()
        t = re.sub(r"(\s*#[^\s#]+)+\s*$", "", t).strip()
        if t == before:
            break

    # 头部：标记 → 时长 → 播放量（时长和播放量可能交替出现）
    for _ in range(3):
        before = t
        t = _MARKER.sub("", t)
        t = _DURATION.sub("", t)
        t = _PLAYS.sub("", t)
        t = t.strip()
        if t == before:
            break

    # 抖音部分卡片会把标题重复一遍，去掉重复段
    if len(t) > 20:
        half = len(t) // 2
        a, b = t[:half].strip(" 　"), t[half:].strip(" 　")
        if len(a) > 8 and b.startswith(a):
            t = a

    return t.strip()


def title_to_display(raw: str, max_chars: int = 50) -> str:
    """直接拿到可用作文件名的标题"""
    return safe_name(clean_title(raw), max_chars)

