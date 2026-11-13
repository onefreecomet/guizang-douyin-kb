"""通过 Edge/CDP 读取明确选中的抖音收藏夹；验证和登录交由用户处理。"""
import json
import sys
import time
from pathlib import Path

from cdp_browser import EdgeSession

FAV_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"
DEFAULT_COOKIES = Path.home() / "抖音视频下载" / "cookies.txt"
PROFILE = Path(__file__).resolve().parent.parent / "work" / "edge-profile"
VERIFY_WAIT = 180
FOLDER_WAIT = 10  # 点开「收藏夹」标签后等列表渲染
ENTER_WAIT = 8    # 点收藏夹名称后等页面切到收藏夹内容


class ManualRequired(RuntimeError):
    """需要人工处理；调用方应保持 needs_manual，下一次重新扫描。"""


class ScanCancelled(RuntimeError):
    """用户停止扫描。"""


# 标签始终作为 JSON 参数传入，不能拼进 JS 字符串字面量。
CLICK_JS = r"""(label) => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const els = [...document.querySelectorAll('button,[role="tab"],a,div,p,span,li')]
    .filter(e => visible(e) && e.children.length === 0 && (e.textContent || '').trim() === label);
  const targets = [...new Set(els.map(e => e.closest('button,[role="tab"],a,[role="button"]') || e))];
  if (targets.length !== 1) return false;
  targets[0].scrollIntoView({block: 'center'});
  targets[0].click();
  return true;
}"""

SELECTED_JS = r"""(label) => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const labels = [...document.querySelectorAll('button,[role="tab"],a,div,p,span,li')]
    .filter(e => visible(e) && e.children.length === 0 && (e.textContent || '').trim() === label);
  const selected = e => e.getAttribute('aria-selected') === 'true' ||
    ['page', 'true'].includes(e.getAttribute('aria-current')) ||
    e.getAttribute('data-state') === 'active' ||
    [...e.classList].some(c => /^(active|selected|current|.*[_-](active|selected|current))$/i.test(c));
  return labels.some(e => {
    // 仅检查标签自身/所属交互控件，不接受外层整个收藏区的 active。
    const control = e.closest('button,[role="tab"],a,[role="button"]');
    if (selected(e) || (control && selected(control))) return true;
    // 收藏夹卡片进入详情页时可能没有 selected，需明确的收藏夹 ID 路由及同名标题。
    const link = e.closest('a[href]');
    if (!link) return false;
    const u = new URL(link.href, location.href), here = new URL(location.href);
    const id = u.searchParams.get('collection_id') || u.searchParams.get('mix_id');
    return !!id && (here.searchParams.get('collection_id') === id || here.searchParams.get('mix_id') === id) &&
      [...document.querySelectorAll('h1,h2,[role="heading"]')].some(h => visible(h) && h.textContent.trim() === label);
  });
}"""

# 进入某个收藏夹后，文件夹列表会被视频列表替换，顶部出现「添加视频」「返回」两个操作按钮；
# 列表态只有「新建收藏夹」。二者不会同时出现，因此比 aria-selected 更适合作为进入证据：
# 2026-11 的页面点进收藏夹后，收藏夹名称节点没有任何 selected/active 标记，URL 也不带 collection_id。
ENTERED_JS = r"""(() => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const leaf = text => [...document.querySelectorAll('button,[role="button"],span,div,a')]
    .filter(e => visible(e) && e.children.length === 0 && (e.textContent || '').trim() === text);
  return leaf('返回').length > 0 && leaf('添加视频').length > 0;
})()"""

# 只判断收藏夹名称是否已渲染（点击收藏夹标签后列表是异步出现的），不做任何点击。
HAS_LABEL_JS = r"""(label) => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  return [...document.querySelectorAll('button,[role="tab"],a,div,p,span,li')]
    .some(e => visible(e) && e.children.length === 0 && (e.textContent || '').trim() === label);
}"""

MANUAL_JS = r"""(() => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const title = (document.title || '').toLowerCase();
  if (/验证码|安全验证|人机验证|captcha/.test(title)) return '验证码';
  if ([...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="verify"],[id*="captcha"],[class*="captcha"],#verify-bar')].some(visible)) return '验证码';
  // 正文或视频标题中的“登录”不能证明会话失效。
  if (/^https?:\/\/[^/]+\/(login|passport)(\/|\?|$)/i.test(location.href)) return '登录失效';
  const dialogs = [...document.querySelectorAll('[role="dialog"],[data-e2e="login-dialog"],[class*="login-modal"],[class*="login-panel"]')].filter(visible);
  if (dialogs.some(d => /扫码登录|登录后|手机号登录|验证码登录/.test(d.innerText || '') &&
      d.querySelector('input,canvas,img,[data-e2e*="login"]'))) return '登录失效';
  return '';
})()"""


def load_cookies(path: Path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 7:
            continue
        domain, _flag, cpath, secure, expires, name, value = f[:7]
        if "douyin" not in domain:
            continue
        try:
            exp = float(expires)
        except ValueError:
            exp = -1
        out.append({"name": name, "value": value, "domain": domain,
                    "path": cpath or "/", "secure": secure.upper() == "TRUE",
                    "expires": exp if exp > 0 else None})
    return out


def _check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise ScanCancelled("收藏夹扫描已停止")


def _pause(seconds, stop_event=None):
    end = time.monotonic() + seconds
    while True:
        _check_cancel(stop_event)
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def wait_ready(page, seconds=20, stop_event=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _check_cancel(stop_event)
        # 验证中间页可能一直不触发 complete，先让调用方即时通知并等待人工。
        if _manual_reason(page) or page.eval("document.readyState") == "complete":
            return True
        _pause(0.5, stop_event)
    return False


def _manual_reason(page):
    return page.eval(MANUAL_JS) or ""


def is_verify(page):
    return _manual_reason(page) == "验证码"


def _handle_manual(page, log, on_manual, stop_event, headless):
    _check_cancel(stop_event)
    reason = _manual_reason(page)
    if not reason:
        return False
    message = f"抖音{reason}，请在浏览器窗口内手工处理；最多等待 {VERIFY_WAIT} 秒，超时后请在控制台继续重新扫描"
    log(message)
    if on_manual is not None:
        on_manual(message)  # 每次拦截首先通知，不等超时。
    if headless:
        raise ManualRequired(message + "（当前为无头模式，请切换可见浏览器）")
    end = time.monotonic() + VERIFY_WAIT
    while True:
        _check_cancel(stop_event)
        if not _manual_reason(page):
            log("人工验证/登录已完成，重新确认收藏夹后继续")
            return True
        if time.monotonic() >= end:
            raise ManualRequired(message)
        _pause(min(1, end - time.monotonic()), stop_event)


def _label_eval(page, script, label):
    return page.eval(f"({script})({json.dumps(label, ensure_ascii=True)})")


def _entered(page, favorite):
    # 两个判据任一成立即可：新判据看「添加视频/返回」，旧判据看 aria-selected 与 collection_id。
    return bool(page.eval(ENTERED_JS)) or bool(_label_eval(page, SELECTED_JS, favorite))


def _wait(page, predicate, seconds, stop_event):
    end = time.monotonic() + seconds
    while True:
        _check_cancel(stop_event)
        if predicate():
            return True
        if time.monotonic() >= end:
            return False
        _pause(0.5, stop_event)


def _ensure_favorite(page, favorite, log, on_manual, stop_event, headless):
    _handle_manual(page, log, on_manual, stop_event, headless)
    if _entered(page, favorite):
        return
    # 导航及尝试次数均有界，找不到/仅点击成功绝不开始 collect。
    _label_eval(page, CLICK_JS, "收藏夹")
    # 收藏夹列表异步渲染：先等名称出现，避免在空列表上点空。
    _wait(page, lambda: _label_eval(page, HAS_LABEL_JS, favorite), FOLDER_WAIT, stop_event)
    for _ in range(3):
        _handle_manual(page, log, on_manual, stop_event, headless)
        if _entered(page, favorite):
            return
        if _label_eval(page, HAS_LABEL_JS, favorite):
            _label_eval(page, CLICK_JS, favorite)
            if _wait(page, lambda: _entered(page, favorite), ENTER_WAIT, stop_event):
                log(f"已确认进入收藏夹「{favorite}」")
                return
            _handle_manual(page, log, on_manual, stop_event, headless)
            if _entered(page, favorite):
                log(f"已确认进入收藏夹「{favorite}」")
                return
            # 点进去又退回列表（例如中途弹过登录层）：重新列一遍收藏夹再试。
            _label_eval(page, CLICK_JS, "收藏夹")
            _wait(page, lambda: _label_eval(page, HAS_LABEL_JS, favorite), FOLDER_WAIT, stop_event)
    message = f"无法可靠确认已进入收藏夹「{favorite}」，未采集任何其他页面内容，请人工检查收藏夹/登录后重试"
    if on_manual is not None:
        on_manual(message)
    raise ManualRequired(message)


def _restart_favorite(page, favorite, log, on_manual, stop_event, headless):
    # 验证可能使虚拟列表停留在底部；重新导航才能保证从头扫描完整列表。
    page.call("Page.navigate", {"url": FAV_URL})
    wait_ready(page, 25, stop_event)
    _ensure_favorite(page, favorite, log, on_manual, stop_event, headless)


def scroll_bottom(page):
    return page.eval(r"""(() => {
      let best = null, bestH = 0;
      for (const el of document.querySelectorAll('*')) {
        if (el.scrollHeight > el.clientHeight + 300 && el.scrollHeight > bestH) {
          bestH = el.scrollHeight; best = el;
        }
      }
      if (best) { best.scrollTop = best.scrollHeight; return bestH; }
      window.scrollTo(0, document.body.scrollHeight);
      return document.body.scrollHeight;
    })()""")


def collect(page):
    # 只收收藏夹里带封面的视频卡：页面侧栏的推荐位是纯文字链接（无缩略图、高度很小），
    # 若不过滤会把别人的推荐视频当成本次新增。
    # 作者优先取卡片里的用户链接；收藏夹卡片常常没有该链接，此时退回封面 alt 的「作者：标题」前缀。
    return page.eval(r"""(() => [...document.querySelectorAll('a[href*="/video/"]')].map(a => {
      const box = a.getBoundingClientRect();
      const img = a.querySelector('img');
      if (!img || !box || box.height < 60) return null;
      const card = a.closest('li,article,[data-e2e*="video"],[data-e2e*="aweme"]') || a;
      const link = card.querySelector('[data-e2e*="author"],a[href*="/user/"]');
      const raw = (a.innerText || '').trim().replace(/\s+/g, ' ');
      // 卡片文本以点赞数开头（例如「7938 标题」），去掉后才是标题。
      const title = raw.replace(/^\d+(?:\.\d+)?\s*万?\s+/, '');
      const alt = img.getAttribute('alt') || '';
      let author = link ? (link.innerText || link.getAttribute('title') || '').trim().replace(/^@/, '') : '';
      if (!author) {
        const cut = alt.indexOf('：');
        const head = cut > 0 ? alt.slice(0, cut).trim() : '';
        if (head && head.length <= 30) author = head;
      }
      return {h: a.getAttribute('href') || '',
        t: (title || alt).slice(0, 200),
        author: author};
    }).filter(row => row))()""") or []


class ScanCancelled(RuntimeError):
    """The caller asked to stop; no partial list is returned."""


class ScanResult(list):
    """Items found by a scan, plus why it stopped early when a full pass was not possible."""

    incomplete = ''


def scan(cookies_file=DEFAULT_COOKIES, favorite="有帮助", headless=False,
         max_scroll=25, log=print, profile_dir=None, on_manual=None, stop_event=None,
         allow_partial=False):
    """完整扫描后返回列表；取消、人工超时或不完整扫描均抛异常，禁止误判无新增。

    on_manual(message) 由 autopilot 接管 needs_manual 和通知；本模块不发送消息。
    time.monotonic/time.sleep 可替换，测试无需真实等待或访问网站。
    allow_partial=True 时，滚动上限用尽或列表高度异常不再抛错，而是返回已收集条目，
    并在返回值的 incomplete 属性里写明原因，避免一次扫描失败让整轮同步作废。
    """
    _check_cancel(stop_event)
    if not isinstance(favorite, str) or not favorite.strip():
        raise ValueError("收藏夹名称不能为空")
    if max_scroll < 1:
        raise ValueError("max_scroll 必须大于零")
    cookies = load_cookies(Path(cookies_file))
    if not cookies:
        raise RuntimeError(f"cookies 文件里没有 douyin 记录: {cookies_file}")
    log(f"载入 {len(cookies)} 条 cookie")
    items = {}
    incomplete = ''
    with EdgeSession(headless=headless, profile_dir=profile_dir or PROFILE) as sess:
        page = sess.open_page("about:blank")
        page.call("Network.enable")
        page.call("Page.enable")
        page.call("Page.navigate", {"url": "https://www.douyin.com"})
        wait_ready(page, 20, stop_event)
        for c in cookies:
            _check_cancel(stop_event)
            params = {"name": c["name"], "value": c["value"], "domain": c["domain"],
                      "path": c["path"], "secure": c["secure"], "httpOnly": False, "sameSite": "Lax"}
            if c.get("expires"):
                params["expires"] = c["expires"]
            page.call("Network.setCookie", params)
        page.call("Page.navigate", {"url": FAV_URL})
        wait_ready(page, 25, stop_event)
        _handle_manual(page, log, on_manual, stop_event, headless)
        _pause(2, stop_event)
        _ensure_favorite(page, favorite, log, on_manual, stop_event, headless)
        idle, last_h = 0, None
        for i in range(max_scroll):
            _check_cancel(stop_event)
            if _handle_manual(page, log, on_manual, stop_event, headless):
                # 人工可能改变当前页面，丢弃本轮累计，从头重新定位。
                items.clear()
                idle, last_h = 0, None
                _restart_favorite(page, favorite, log, on_manual, stop_event, headless)
            if not _entered(page, favorite):
                message = f"已不在收藏夹「{favorite}」内，扫描中止，请重新扫描"
                if on_manual is not None:
                    on_manual(message)
                raise ManualRequired(message)
            before = len(items)
            for row in collect(page):
                href = row.get("h") or ""
                if "/video/" not in href:
                    continue
                vid = href.split("/video/", 1)[1].split("?", 1)[0].strip("/")
                if not vid.isdigit():
                    continue
                author = (row.get("author") or "").strip()
                if vid not in items:
                    items[vid] = {"video_id": vid, "url": f"https://www.douyin.com/video/{vid}",
                                  "title": (row.get("t") or "")[:120], "author": author}
                elif author and not items[vid]["author"]:
                    items[vid]["author"] = author
            h = scroll_bottom(page)
            if not isinstance(h, (int, float)) or h <= 0:
                message = "无法确认滚动列表高度，扫描不完整"
                if not allow_partial:
                    raise RuntimeError(message)
                incomplete = message
                log(message + '，先处理已收集的 ' + str(len(items)) + ' 条')
                break
            idle = idle + 1 if len(items) == before and h == last_h else 0
            last_h = h
            # 必须在最终一次滚动后也检查验证，不能把验证码页面的静止当到底。
            _pause(1.4, stop_event)
            if _manual_reason(page):
                _handle_manual(page, log, on_manual, stop_event, headless)
                items.clear()
                idle, last_h = 0, None
                _restart_favorite(page, favorite, log, on_manual, stop_event, headless)
                continue
            if not _entered(page, favorite):
                message = f"已不在收藏夹「{favorite}」内，扫描中止，请重新扫描"
                if on_manual is not None:
                    on_manual(message)
                raise ManualRequired(message)
            if idle >= 4:
                break
            if i and i % 5 == 0:
                log(f"滚动中… 已收集 {len(items)} 条")
        else:
            message = f"收藏夹扫描不完整：已达 max_scroll={max_scroll}，列表尚未稳定，请增大滚动上限后重试"
            if not allow_partial:
                raise RuntimeError(message)
            incomplete = message
            log(message + '，先处理已收集的 ' + str(len(items)) + ' 条')
    try:
        from naming import clean_title
        for it in items.values():
            it["title"] = clean_title(it["title"])[:120]
    except ImportError:
        pass
    result = ScanResult(items.values())
    result.incomplete = incomplete
    return result


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    data = scan(Path(args[0]) if args else DEFAULT_COOKIES, headless="--headless" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"共 {len(data)} 条")
        for i, it in enumerate(data, 1):
            print(f"{i:3d}. {it['video_id']}  {it['title'][:50]}")


if __name__ == "__main__":
    main()
