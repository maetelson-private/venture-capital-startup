"""롱리스트 연락 창구 수집: 홈페이지(첫 화면 + 문의·회사소개 하위 페이지)에서 공개된 회사 연락처를 찾는다.

찾는 것: 이메일(mailto·본문), 대표 전화(tel·본문, 팩스 제외), 문의 폼 페이지, 공식 SNS·카카오 채널.
robots.txt 와 호스트별 간격(fetch.http_get)을 따른다. 키 불필요.

  --dry-run : data/contacts.json·data/contacts_dryrun.md 만 쓰고 DB 는 건드리지 않음
  (기본)    : companies.contact_channel 에 요약 한 줄 반영 (출처 'web-contact', 신뢰도 '중')
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .. import config, db
from ..fetch import Blocked, http_get, render

SOURCE = "web-contact"
MAX_SUBPAGES = 4

_SUB_RE = re.compile(r"contact|문의|about|company|회사\s*소개|오시는|찾아오|location|map|partner|제휴|협력|"
                     r"support|고객\s*센터|inquiry|cs\b|footer|introduce|intro", re.I)
_FORM_RE = re.compile(r"contact|문의|inquiry|제휴|partnership|상담", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])")
_EMAIL_FULL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:[a-z]{2,6})")
_TLD_OK = re.compile(r"\.(com|net|org|io|ai|co|kr|me|biz|info|health|care|app|bio|tech|dev|us|jp|cn|edu|so|xyz|team|im|global|life|med|clinic)$")
_GENERIC = re.compile(r"^(contact|info|cs|bd|biz|business|sales|hello|hi|support|help|admin|partner|partnership|global|"
                      r"headoffice|office|marketing|pr|ir|inquiry|service|team|mail|official|help|hr|recruit)", re.I)
_OBF_RE = re.compile(r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}| at |＠)\s*([A-Za-z0-9-]+(?:\s*(?:\.|\[dot\]|\(dot\))\s*[A-Za-z0-9-]+)+)", re.I)
# 02-123-4567 / 031-1234-5678 / 010-1234-5678 / 1588-1234 / +82-2-...
_PHONE_RE = re.compile(r"(?<![\d-])(?:\+82[-.\s]?\(?0?\)?\s?)?(0\d{1,2}[-.)\s]\s?\d{3,4}[-.\s]\d{4}|1[5-9]\d{2}[-.\s]\d{4})(?![\d-])")
_FAX_RE = re.compile(r"fax|팩스|F\s*[.:]", re.I)
_BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|example\.|sentry|wixpress|domain\.com|email\.com|"
                        r"yourmail|@2x|godaddy|cafe24\.com$|sample|test@|user@|name@", re.I)
_SOCIAL = {
    "linkedin": re.compile(r"linkedin\.com/(company|school|showcase)/[^/?#\s\"']+", re.I),
    "kakao": re.compile(r"pf\.kakao\.com/[^/?#\s\"']+", re.I),
    "instagram": re.compile(r"instagram\.com/[A-Za-z0-9_.]+", re.I),
    "facebook": re.compile(r"facebook\.com/[A-Za-z0-9_.-]+", re.I),
    "youtube": re.compile(r"youtube\.com/(@|channel/|c/|user/)[^/?#\s\"']+", re.I),
    "blog": re.compile(r"blog\.naver\.com/[A-Za-z0-9_-]+", re.I),
    "x": re.compile(r"(?:twitter|x)\.com/[A-Za-z0-9_]+", re.I),
}
_SOCIAL_SKIP = re.compile(r"/(sharer|share|intent|plugins|dialog|tr|login|p|reel|explore|watch|embed)\b", re.I)


def _norm_url(u: str) -> str | None:
    u = (u or "").strip()
    if not u or "." not in u:
        return None
    u = re.split(r"[\s,]+", u)[0]  # 주소가 여러 개 적힌 경우 첫 번째
    if not re.match(r"https?://", u, re.I):
        u = "http://" + u
    return u


def _get(url: str, why: list | None = None) -> tuple[str, str] | None:
    """(최종 주소, HTML). 실패하면 None 이고 why 에 사유를 붙인다."""
    try:
        r = http_get(url)
    except Blocked:
        why is not None and why.append("robots 금지")
        return None
    except requests.HTTPError as e:
        why is not None and why.append(f"HTTP {e.response.status_code}")
        return None
    except requests.RequestException:
        why is not None and why.append("접속 불가")
        return None
    if "html" not in r.headers.get("Content-Type", "html"):
        return None
    r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
    return r.url, r.text


def _why(start: str, why: list[str]) -> str:
    """robots 확인 단계 실패는 fetch 가 '금지'로 묶으므로, 사이트 자체가 열리는지 한 번 더 본다."""
    if "robots 금지" not in why:
        return why[-1] if why else "알 수 없음"
    try:
        requests.head(start, timeout=10, allow_redirects=True, headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as e:
        return "접속 불가(" + type(e).__name__.replace("Error", "") + ")"
    return "robots 금지"


def _phone_ok(num: str, context: str) -> bool:
    digits = re.sub(r"\D", "", num)
    return 8 <= len(digits) <= 11 and not _FAX_RE.search(context)


def extract(html: str, base: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = {"emails": set(), "phones": set(), "forms": set(), "social": {}, "subpages": []}
    host = urlparse(base).netloc.removeprefix("www.")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        text = a.get_text(" ", strip=True)
        if low.startswith("mailto:"):
            out["emails"].add(href[7:].split("?")[0].strip())
            continue
        if low.startswith("tel:"):
            num = href[4:].strip()
            if _phone_ok(num, text):
                out["phones"].add(num)
            continue
        u = urljoin(base, href).split("#")[0]
        p = urlparse(u)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc.removeprefix("www.") == host:
            if _SUB_RE.search(text) or _SUB_RE.search(p.path):
                out["subpages"].append(u)
            if _FORM_RE.search(text) or _FORM_RE.search(p.path):
                out["forms"].add(u)
            continue
        for kind, rx in _SOCIAL.items():
            m = rx.search(u)
            if m and not _SOCIAL_SKIP.search(urlparse(u).path) and kind not in out["social"]:
                out["social"][kind] = "https://" + m.group(0)
    # 문의 폼: textarea 가 있는 form
    for f in soup.find_all("form"):
        if f.find("textarea"):
            out["forms"].add(base)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    for m in _EMAIL_RE.finditer(text):
        out["emails"].add(m.group(0))
    for m in _OBF_RE.finditer(text):
        dom = re.sub(r"\s*(?:\[dot\]|\(dot\))\s*", ".", m.group(2)).replace(" ", "")
        out["emails"].add(f"{m.group(1)}@{dom}")
    for line in text.splitlines():
        for m in _PHONE_RE.finditer(line):
            # 같은 줄에서 번호 바로 앞 20자에 '팩스/Fax' 가 있으면 제외
            ctx = line[max(0, m.start() - 20):m.start()]
            if _phone_ok(m.group(1), ctx):
                out["phones"].add(m.group(1))
    out["emails"] = {e for e in (x.strip().strip(".").lower() for x in out["emails"])
                     if _EMAIL_FULL.fullmatch(e) and _TLD_OK.search(e) and not _BAD_EMAIL.search(e) and len(e) < 80}
    return out


def _norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p)
    if d.startswith("82"):
        d = "0" + d[2:].lstrip("0")
    if d.startswith("02"):
        return f"02-{d[2:-4]}-{d[-4:]}"
    if len(d) == 8:
        return f"{d[:4]}-{d[4:]}"
    return f"{d[:3]}-{d[3:-4]}-{d[-4:]}"


def crawl(cid: int, name: str, url: str) -> dict:
    res = {"id": cid, "name": name, "url": url, "status": "ok", "pages": [],
           "emails": [], "phones": [], "forms": [], "social": {}}
    start = _norm_url(url)
    why: list[str] = []
    got = None
    if start:
        p = urlparse(start)
        host = p.netloc.removeprefix("www.")
        tries = [start, f"{p.scheme}://{p.netloc}/"]
        for sch in ("https", "http"):
            for h in (p.netloc, host if p.netloc.startswith("www.") else "www." + host):
                tries.append(f"{sch}://{h}/")
        for u in dict.fromkeys(tries):
            got = _get(u, why)
            if got:
                break
    if not got:
        res["status"] = ("실패: " + _why(start, why)) if start else "URL 형식 오류"
        return res
    final, html = got
    res["pages"].append(final)
    agg = extract(html, final)
    emails, phones, forms, social = set(agg["emails"]), set(agg["phones"]), set(agg["forms"]), dict(agg["social"])
    seen = {final, start}
    subs = [u for u in dict.fromkeys(agg["subpages"]) if u not in seen][:MAX_SUBPAGES]
    for u in subs:
        g = _get(u)
        if not g:
            continue
        res["pages"].append(g[0])
        e = extract(g[1], g[0])
        emails |= e["emails"]
        phones |= e["phones"]
        forms |= {f for f in e["forms"] if f == g[0]}  # 하위 페이지는 실제 폼이 있는 것만
        for k, v in e["social"].items():
            social.setdefault(k, v)
    # 첫 화면의 '문의' 링크는 실제 폼이 있는지 확인된 것을 우선, 없으면 링크만
    res["emails"] = sorted(emails, key=lambda e: (not _GENERIC.match(e), e))  # 대표 메일 먼저
    res["phones"] = sorted({_norm_phone(p) for p in phones})
    res["forms"] = sorted(forms)[:3]
    res["social"] = social
    if not (emails or phones or forms or social):
        # JS 로 그리는 화면일 수 있어 브라우저로 한 번 더 (robots 는 render 가 확인)
        try:
            rh = render(final)
            e = extract(rh, final)
            res["emails"] = sorted(e["emails"], key=lambda x: (not _GENERIC.match(x), x))
            res["phones"] = sorted({_norm_phone(p) for p in e["phones"]})
            res["forms"] = sorted(e["forms"])[:3]
            res["social"] = e["social"]
            res["pages"].append(final + " (렌더링)")
        except Exception:  # noqa: BLE001
            pass
        if not (res["emails"] or res["phones"] or res["forms"] or res["social"]):
            res["status"] = "연락처 못 찾음"
    return res


def summary(r: dict) -> str:
    parts = []
    if r["emails"]:
        parts.append("메일 " + ", ".join(r["emails"][:2]))
    if r["phones"]:
        parts.append("전화 " + ", ".join(r["phones"][:2]))
    if r["forms"]:
        parts.append("문의 " + r["forms"][0])
    for k in ("linkedin", "kakao"):
        if k in r["social"]:
            parts.append(f"{k} {r['social'][k]}")
    if not parts and r["social"]:  # SNS 만 찾은 곳은 첫 번째 SNS 라도
        k, v = next(iter(r["social"].items()))
        parts.append(f"{k} {v}")
    return " · ".join(parts)


def _targets(con, ids, limit):
    q = """SELECT id, name, url FROM v_companies WHERE hard_filter='통과' AND scope='포함'
           AND url IS NOT NULL AND url!=''"""
    rows = con.execute(q + " ORDER BY id").fetchall()
    if ids:
        rows = [r for r in rows if r["id"] in set(ids)]
    return rows[:limit]


def _report(results: list[dict], total_longlist: int, path) -> None:
    n = len(results)
    cnt = lambda f: sum(1 for r in results if f(r))  # noqa: E731
    lines = [
        "# 연락 창구 크롤링 결과 (dry-run)", "",
        f"- 롱리스트 {total_longlist}곳 중 홈페이지 URL 있는 곳 {n}곳 크롤링 (URL 없는 곳은 대상 아님)",
        f"- 이메일 {cnt(lambda r: r['emails'])} · 전화 {cnt(lambda r: r['phones'])} · 문의 폼/페이지 {cnt(lambda r: r['forms'])}"
        f" · LinkedIn {cnt(lambda r: 'linkedin' in r['social'])} · 카카오채널 {cnt(lambda r: 'kakao' in r['social'])}",
        f"- 하나라도 찾음 {cnt(lambda r: summary(r) or r['social'])} · 못 찾음 {cnt(lambda r: r['status'].startswith('연락처'))}"
        f" · 접속 실패 {cnt(lambda r: r['status'].startswith(('실패', 'URL')))}", "",
        "| id | 기업 | 상태 | 이메일 | 전화 | 문의 | SNS |", "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["id"], r["name"], r["status"], "<br>".join(r["emails"][:3]), "<br>".join(r["phones"][:2]),
            "<br>".join(r["forms"][:1]), " ".join(r["social"].keys())))
    path.write_text("\n".join(lines), encoding="utf-8")


def run(con, dry_run=False, ids=None, limit=None, workers=6, load=False) -> None:
    if load:  # 직전 dry-run 결과(data/contacts.json)를 다시 크롤링하지 않고 그대로 반영
        results = json.loads((config.DATA_DIR / "contacts.json").read_text(encoding="utf-8"))
        if ids:
            results = [r for r in results if r["id"] in set(ids)]
        print(f"data/contacts.json {len(results)}곳 불러옴")
        return _apply(con, results)
    targets = _targets(con, ids, limit)
    total = con.execute("SELECT count(*) FROM v_companies WHERE hard_filter='통과' AND scope='포함'").fetchone()[0]
    print(f"홈페이지 있는 롱리스트 {len(targets)}곳 크롤링 (워커 {workers})")
    results = []
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(crawl, r["id"], r["name"], r["url"]): r for r in targets}
        for f in as_completed(futs):
            r = f.result()
            results.append(r)
            print(f"  [{len(results)}/{len(targets)}] {r['name']}: {summary(r) or r['status']}")
    results.sort(key=lambda r: r["id"])
    (config.DATA_DIR / "contacts.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    _report(results, total, config.DATA_DIR / "contacts_dryrun.md")
    print(f"→ {config.DATA_DIR / 'contacts_dryrun.md'}")
    if dry_run:
        return
    _apply(con, results)


def _apply(con, results: list[dict]) -> None:
    n = 0
    with db.run_log(con, "contacts", f"n={len(results)}") as st:
        for r in results:
            s = summary(r)
            if s and db.set_field(con, r["id"], "contact_channel", s, SOURCE, r["pages"][0], "중"):
                n += 1
            st["docs"] += 1
        con.commit()
    print(f"contact_channel 반영 {n}곳")
