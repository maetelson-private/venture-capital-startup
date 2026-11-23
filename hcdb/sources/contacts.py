"""롱리스트 연락 창구 수집: 홈페이지(첫 화면 + 문의·회사소개 하위 페이지)에서 공개된 회사 연락처를 찾는다.

찾는 것: 이메일(mailto·본문), 대표 전화(tel·본문, 팩스 제외), 문의 폼 페이지, 공식 SNS·카카오 채널.
robots.txt 와 호스트별 간격(fetch.http_get)을 따른다. 키 불필요.

  --dry-run : data/contacts.json·data/contacts_dryrun.md 만 쓰고 DB 는 건드리지 않음
  (기본)    : companies.contact_channel 에 요약 한 줄 반영 (출처 'web-contact', 신뢰도 '중')
"""
import html
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


# ── 홈페이지가 없는 롱리스트 기업: 네이버 웹문서 검색 → 후보 도메인 열어서 회사명 확인 ─────────────────
# NAVER API HUB 애플리케이션에서 '검색 > 웹문서'가 켜져 있어야 한다 (뉴스만 켜져 있으면 401)
NAVER_WEB = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
_SKIP_HOST = re.compile(
    r"naver\.|daum\.|kakao|google\.|youtube|instagram|facebook|linkedin|twitter|x\.com|tistory|blog|brunch|velog|"
    r"notion\.|wikipedia|namu\.wiki|go\.kr$|ac\.kr$|re\.kr$|or\.kr$|thevc|innoforest|rocketpunch|wanted|saramin|"
    r"jobkorea|jobplanet|catch\.co|nextunicorn|bizinfo|k-startup|smes|kipris|dbpia|scienceon|riss|"
    r"news|ilbo|times|press|today|economy|biz\.|joongang|chosun|donga|hankyung|mk\.co|etnews|zdnet|"
    r"edaily|hani|khan|newsis|yna\.co|yonhap|mt\.co|sedaily|asiae|heraldcorp|fnnews|dt\.co|inews|"
    r"medical|doctorsnews|dailymedi|docdocdoc|medigate|hitnews|kmedinfo|pharm|bosa|venturesquare|platum|"
    r"wowtale|startupn|besuccess|apps\.apple|play\.google|smartstore|coupang|11st|gmarket|ohou|"
    r"pdf|scribd|slideshare|github\.io|wixsite|imweb\.me|modoo\.at|creatorlink|cafe24\.com$|"
    r"holdings|career|recruit|greetinghr|flexg|shop|mall|daily|journal|magazine|tv\.|portal|hub\.|kotra|kised")
# 홈페이지가 헬스케어 회사의 것인지 (같은 이름의 다른 업종 회사를 거르기 위해)
_HEALTH_PAGE = re.compile(r"의료|헬스|건강|병원|진단|치료|재활|돌봄|요양|바이오|메디|환자|의사|간호|수면|치매|인지|"
                          r"심리|정신|웰니스|시니어|헬스케어|임상|약국|치과|health|medical|clinic|care|bio|patient", re.I)
_NOT_COMPANY_TITLE = re.compile(r"의원|치과|한의원|병원|클리닉|안과|센터|학교|학원|웨딩|동물병원|부동산|"
                                r"인테리어|간판|자동문|악기|서버|살롱|트레이닝")
_CORP_WORDS = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s+")


def _naver_web(query: str, display: int = 10) -> list[dict]:
    r = http_get(NAVER_WEB, params={"query": query, "display": display}, check_robots=False,
                 headers={"X-NCP-APIGW-API-KEY-ID": config.NAVER_CLIENT_ID,
                          "X-NCP-APIGW-API-KEY": config.NAVER_CLIENT_SECRET})
    return r.json().get("items", [])


def _cands(items) -> list[str]:
    out = []
    for it in items:
        h = urlparse(it.get("link") or "").netloc.lower().removeprefix("www.")
        if h and "." in h and not _SKIP_HOST.search(h) and h not in out:
            out.append(h)
    return out[:4]


def _names(name: str) -> list[str]:
    base = _CORP_WORDS.sub("", name or "")
    out = {base}
    m = re.match(r"(.+?)\((.+)\)$", base)  # '뉴지엄(NUSEUM)' → 뉴지엄, NUSEUM
    if m:
        out |= {m.group(1), m.group(2)}
    return [n for n in out if len(n) >= 2]


def _verify(host: str, names: list[str]) -> str | None:
    """후보 도메인 첫 화면에 회사명(띄어쓰기·법인 표기 제거)이 있으면 최종 주소를 돌려준다."""
    for u in (f"https://{host}/", f"https://www.{host}/", f"http://{host}/"):
        got = _get(u)
        if not got:
            continue
        final, html = got
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        raw_title = soup.title.get_text() if soup.title else ""
        if _NOT_COMPANY_TITLE.search(raw_title):  # 같은 이름의 병의원·학원·센터·쇼핑몰 (사람이 검토할 때 가장 많이 걸러낸 유형)
            return None
        title = _CORP_WORDS.sub("", raw_title).lower()
        text = _CORP_WORDS.sub("", soup.get_text(" ")).lower()
        for n in (n.lower() for n in names):
            # 제목 안의 회사명은 앞뒤가 한글로 이어지면 안 된다 ('엠에스메디' ≠ '엠에스메디캠프', '네스트' ≠ '노르딕네스트')
            if n in title and not re.search(rf"(^|[^가-힣]){re.escape(n)}($|[^가-힣])", title):
                title = title.replace(n, "")
            # 제목에 회사명이 있으면 확정. 본문에만 있으면 4글자 이상 이름이 두 번 이상 나올 때만 (짧은 이름은 남의 사이트에도 흔함)
            if (n in title or (len(n) >= 4 and text.count(n) >= 2)) and _HEALTH_PAGE.search(text + " " + title):
                return final
        return None  # 열리긴 했는데 회사명이 없다 → 다른 회사
    return None


# ── 네이버 지역(업체) 검색: 사업장 등록명이 회사명과 똑같고 업종이 맞는 곳의 등록 링크 ─────────────────
NAVER_LOCAL = "https://naverapihub.apigw.ntruss.com/search/v1/local"
# 회사가 아닌 같은 이름 가게를 거르는 업종 (지역 검색은 상호만 같은 음식점·미용실·병의원이 많다)
_LOCAL_BAD_CAT = re.compile(r"음식|술집|카페|미용|네일|쇼핑|패션|의류|건설|인쇄|부동산|숙박|여행|자동차|학원|교육|"
                            r"병원|의원|치과|약국|스포츠|헬스장|요양원|사진|웨딩|반려|애견|꽃|인테리어|유학|종교|"
                            r"주점|베이커리|식품|농업|축산|수산|운송|이사|세탁|청소|공연|숙소|펜션|노래")
_SOCIAL_HOST = {"instagram.com": "instagram", "youtube.com": "youtube", "blog.naver.com": "blog",
                "facebook.com": "facebook", "pf.kakao.com": "kakao", "linkedin.com": "linkedin"}
_SIDO = {"서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주": "광주", "대전": "대전", "울산": "울산",
         "세종": "세종", "경기": "경기", "강원": "강원", "충북": "충청북", "충청북": "충청북", "충남": "충청남",
         "충청남": "충청남", "전북": "전", "전라북": "전", "전남": "전라남", "전라남": "전라남", "경북": "경상북",
         "경상북": "경상북", "경남": "경상남", "경상남": "경상남", "제주": "제주"}


def _norm(s: str) -> str:
    return _CORP_WORDS.sub("", re.sub(r"<.*?>", "", html.unescape(s or ""))).lower()


def _sido_ok(sido: str | None, addr: str) -> bool | None:
    """DB 소재지와 업체 주소가 같은 시도인지. DB 소재지가 없으면 None."""
    if not sido:
        return None
    key = next((v for k, v in _SIDO.items() if sido.startswith(k)), sido[:2])
    return addr.startswith(key) or addr.startswith(sido[:2])


def find_local(cid: int, name: str, sido: str | None) -> dict:
    res = {"id": cid, "name": name, "url": "", "social": "", "query": "local", "category": "", "addr": ""}
    names = {_norm(n) for n in _names(name)}
    try:
        r = http_get(NAVER_LOCAL, params={"query": _CORP_WORDS.sub(" ", name).strip(), "display": 5},
                     check_robots=False, headers={"X-NCP-APIGW-API-KEY-ID": config.NAVER_CLIENT_ID,
                                                  "X-NCP-APIGW-API-KEY": config.NAVER_CLIENT_SECRET})
        items = r.json().get("items", [])
    except (requests.RequestException, ValueError, Blocked):
        return res
    hits = []
    for it in items:
        if _norm(it.get("title")) not in names or _LOCAL_BAD_CAT.search(it.get("category") or ""):
            continue
        addr = it.get("roadAddress") or it.get("address") or ""
        ok = _sido_ok(sido, addr)
        if ok is False:
            continue
        # 이름이 짧으면(한글 3자 미만) 소재지까지 맞아야
        if ok is None and min(len(n) for n in names) < 3:
            continue
        hits.append(it)
    links = {it.get("link") for it in hits if it.get("link")}
    if not hits or len(links) > 1:  # 없거나, 같은 이름 업체가 서로 다른 링크를 갖고 있어 누군지 모름
        return res
    it = hits[0] if not links else next(h for h in hits if h.get("link"))
    res.update(category=it.get("category", ""), addr=(it.get("roadAddress") or "")[:40])
    link = (it.get("link") or "").strip()
    if not link:
        return res
    host = urlparse(link if "://" in link else "http://" + link).netloc.lower().removeprefix("www.")
    soc = next((v for k, v in _SOCIAL_HOST.items() if host.endswith(k)), None)
    if soc:
        res["social"] = f"{soc} {link}"
    elif not _SKIP_HOST.search(host):
        got = _get(link if "://" in link else "http://" + link)
        if got:  # 열리는 곳만 (네이버 업체 등록 링크는 사업자가 직접 넣은 주소)
            res["url"] = got[0]
    return res


def find_homepage(cid: int, name: str, summary: str) -> dict:
    names = _names(name)
    res = {"id": cid, "name": name, "url": "", "query": ""}
    if not names or all(len(n) < 3 for n in names if re.search(r"[가-힣]", n)) and not any(
            re.search(r"[A-Za-z]{4,}", n) for n in names):
        res["query"] = "이름이 너무 짧아 건너뜀"
        return res
    kw = re.split(r"[,\s/·]+", summary or "")
    queries = [f'"{names[0]}"'] + ([f'{names[0]} {kw[0]}'] if kw and kw[0] else [])
    for q in queries:
        try:
            items = _naver_web(q)
        except requests.RequestException as e:
            res["query"] = f"검색 실패 {e.__class__.__name__}"
            return res
        for h in _cands(items):
            url = _verify(h, names)
            if url:
                res.update(url=url, query=q)
                return res
    res["query"] = " / ".join(queries)
    return res


def run_find(con, ids=None, dry_run=False, workers=4) -> list[dict]:
    """롱리스트(하드필터 통과·범위 포함, 예비창업팀 제외) 중 홈페이지가 없는 곳."""
    rows = con.execute("""SELECT id, name, service_summary, sido FROM v_companies
                          WHERE hard_filter='통과' AND scope='포함' AND is_pre_startup=0
                          AND (url IS NULL OR url='') ORDER BY id""").fetchall()
    if ids:
        rows = [r for r in rows if r["id"] in set(ids)]
    print(f"홈페이지 없는 롱리스트 {len(rows)}곳 네이버 지역(업체) 검색 → 못 찾으면 웹문서 검색")

    def one(r):
        loc = find_local(r["id"], r["name"], r["sido"])
        if loc["url"]:
            return loc
        web = find_homepage(r["id"], r["name"], r["service_summary"] or "")
        web.update(social=loc["social"], category=loc["category"], addr=loc["addr"])
        return web

    out = []
    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(one, rows):
            out.append(r)
            print(f"  [{len(out)}/{len(rows)}] {r['name']}: {r['url'] or r.get('social') or '-'} ({r['query'][:20]})")
    (config.DATA_DIR / "homepages_found.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    found = [r for r in out if r["url"]]
    social = [r for r in out if not r["url"] and r.get("social")]
    print(f"홈페이지 확인 {len(found)}곳, 홈페이지는 없고 SNS만 {len(social)}곳 → data/homepages_found.json")
    if not dry_run:
        for r in found:
            db.set_field(con, r["id"], "url", r["url"], "naver-local" if r["query"] == "local" else "naver-web",
                         r["url"], "중")
        for r in social:  # 연락처가 비어 있을 때만 SNS 를 연락 수단으로
            cur = con.execute("SELECT contact_channel FROM companies WHERE id=?", (r["id"],)).fetchone()[0]
            if not cur:
                db.set_field(con, r["id"], "contact_channel", r["social"], "naver-local", r["social"].split()[1], "중")
        con.commit()
    return found
