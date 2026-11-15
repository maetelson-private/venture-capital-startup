"""공통 수집기: robots.txt 확인, 호스트별 간격, 원문 캐시(raw_documents), JS 페이지는 Playwright."""
import hashlib
import json
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from . import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


class Blocked(Exception):
    pass


def _allowed(url: str) -> bool:
    if not config.RESPECT_ROBOTS:
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = _session.get(base + "/robots.txt", timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots[base] = rp
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(config.USER_AGENT, url)


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    wait = config.MIN_DELAY_SEC - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def http_get(url: str, *, params=None, headers=None, check_robots=True, **kw) -> requests.Response:
    """API 호출 등 캐시 없이 쓰는 저수준 GET."""
    if check_robots and not _allowed(url):
        raise Blocked(f"robots.txt 금지: {url}")
    _throttle(url)
    r = _session.get(url, params=params, headers=headers, timeout=20, **kw)
    r.raise_for_status()
    return r


def http_post_json(url: str, payload, *, params=None) -> dict:
    _throttle(url)
    r = _session.post(url, params=params, data=json.dumps(payload),
                      headers={"Content-Type": "application/json"}, timeout=20)
    r.raise_for_status()
    return r.json()


def render(url: str, wait_selector: str | None = None) -> str:
    """JS 렌더링이 필요한 페이지(연구실 SPA 등)."""
    from playwright.sync_api import sync_playwright
    if not _allowed(url):
        raise Blocked(f"robots.txt 금지: {url}")
    _throttle(url)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=config.USER_AGENT)
        page.goto(url, wait_until="networkidle", timeout=30000)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=10000)
        html = page.content()
        b.close()
    return html


def html_to_text(html: str) -> tuple[str, str | None, str | None]:
    """본문 텍스트, 제목, 발행일 추출 (기사·일반 페이지 공용 휴리스틱)."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav", "aside", "form"]):
        t.decompose()
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.string if soup.title else None)
    pub = None
    for attr in ({"property": "article:published_time"}, {"name": "pubdate"}, {"itemprop": "datePublished"}):
        m = soup.find("meta", attrs=attr)
        if m and m.get("content"):
            pub = m["content"][:10]
            break
    node = soup.find("article") or soup.find(id=re.compile("article|content|news", re.I)) or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))
    return text, (title or "").strip() or None, pub


def fetch_doc(con, url: str, source: str, *, use_render=False, force=False) -> dict | None:
    """URL 을 가져와 raw_documents 에 저장(캐시 적용). 반환: {'id','url','title','body','published_at'}"""
    if not force:
        r = con.execute(
            "SELECT * FROM raw_documents WHERE url=? AND julianday('now') - julianday(fetched_at) < ?",
            (url, config.CACHE_TTL_DAYS)).fetchone()
        if r:
            return dict(r)
    try:
        if use_render:
            html, status = render(url), 200
        else:
            resp = http_get(url)
            resp.encoding = resp.apparent_encoding if resp.encoding in (None, "ISO-8859-1") else resp.encoding
            html, status = resp.text, resp.status_code
    except (requests.RequestException, Blocked) as e:
        print(f"  ! 수집 실패 {url}: {e}")
        return None
    body, title, pub = html_to_text(html)
    h = hashlib.sha1(body.encode("utf-8")).hexdigest()
    con.execute(
        """INSERT INTO raw_documents(url, source, title, published_at, http_status, content_hash, body)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET title=excluded.title, published_at=coalesce(excluded.published_at, published_at),
             http_status=excluded.http_status, content_hash=excluded.content_hash, body=excluded.body,
             fetched_at=datetime('now','localtime'),
             processed=CASE WHEN content_hash=excluded.content_hash THEN processed ELSE 0 END""",
        (url, source, title, pub, status, h, body))
    con.commit()
    return dict(con.execute("SELECT * FROM raw_documents WHERE url=?", (url,)).fetchone())
