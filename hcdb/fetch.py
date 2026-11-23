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
            if r.status_code >= 500:
                # RFC 9309: 서버 오류면 '전부 금지'로 본다
                lines = ["User-agent: *", "Disallow: /"]
            elif r.status_code == 200:
                text = r.content.decode("utf-8", "ignore").lstrip("﻿")  # BOM 이 있으면 첫 줄을 못 읽음
                # 'User-agent:' 바로 뒤 빈 줄 때문에 그룹이 끊기는 파일이 있어 빈 줄은 버린다
                lines = [l for l in text.splitlines() if l.strip()]
            else:  # 404 등 → 제한 없음
                lines = []
            rp.parse(lines)
            _robots[base] = rp
        except requests.RequestException:
            # 연결 실패도 확인할 수 없으니 금지로 본다 (예전엔 허용으로 취급했음)
            rp.parse(["User-agent: *", "Disallow: /"])
            _robots[base] = rp
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


def http_post_form(url: str, data: dict, *, check_robots=True) -> requests.Response:
    """사이트 화면이 쓰는 AJAX(폼 POST) 목록 호출용."""
    if check_robots and not _allowed(url):
        raise Blocked(f"robots.txt 금지: {url}")
    _throttle(url)
    r = _session.post(url, data=data, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20)
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
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:  # noqa: BLE001  광고·채팅 위젯이 계속 통신하면 networkidle 이 안 온다 → 그려진 만큼 쓴다
            if "Timeout" not in type(e).__name__:
                b.close()
                raise
            page.wait_for_timeout(2000)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=10000)
        html = page.content()
        for f in page.frames[1:]:  # iframe 안 내용(옛 홈페이지는 본문이 프레임에 있다)
            try:
                html += f.content()
            except Exception:  # noqa: BLE001
                pass
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


def page_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """같은 호스트로 가는 링크 (주소, 글자) 목록. 하위 페이지(회사소개·서비스) 찾기용."""
    from urllib.parse import urljoin
    host = urlparse(base_url).netloc.removeprefix("www.")
    out, seen = [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        u = urljoin(base_url, a["href"]).split("#")[0]
        if urlparse(u).scheme in ("http", "https") and urlparse(u).netloc.removeprefix("www.") == host \
                and u not in seen:
            seen.add(u)
            out.append((u, a.get_text(" ", strip=True)[:40]))
    return out


def fetch_doc(con, url: str, source: str, *, use_render=False, force=False,
              links: list | None = None) -> dict | None:
    """URL 을 가져와 raw_documents 에 저장(캐시 적용). 반환: {'id','url','title','body','published_at'}
    links 에 리스트를 넘기면 새로 받아온 경우에 한해 같은 호스트 링크를 채운다(캐시 적중 시엔 비어 있음)."""
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
    if links is not None:
        links.extend(page_links(html, url))
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
