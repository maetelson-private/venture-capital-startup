"""뉴스: 검색(네이버 API → 없으면 구글뉴스 RSS)으로 기사 발견 → 본문 수집 → 규칙 기반 추출.

규칙 추출은 신뢰도 '하'로 넣는다. 사람이 엑셀에서 확인 후 manual 로 다시 넣으면 그 값이 우선.
"""
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from .. import config, db
from ..fetch import fetch_doc, http_get
from ..normalize import norm_name, parse_krw

DEFAULT_QUERIES = [
    "디지털헬스케어 스타트업 투자 유치",
    "헬스케어 스타트업 시드 투자",
    "의료 AI 스타트업 프리A",
    "디지털치료제 스타트업 투자",
    "헬스케어 스타트업 팁스 선정",
]


# ───────────── 발견 ─────────────
def search_naver(query: str, display=50, start=1, sort="date") -> list[dict]:
    # 2026 검색 API 는 NAVER API HUB(네이버 클라우드)로 이관: 주소·헤더가 바뀜. 값은 API HUB 의 Client ID/Secret
    r = http_get("https://naverapihub.apigw.ntruss.com/search/v1/news",
                 params={"query": query, "display": display, "start": start, "sort": sort},
                 headers={"X-NCP-APIGW-API-KEY-ID": config.NAVER_CLIENT_ID,
                          "X-NCP-APIGW-API-KEY": config.NAVER_CLIENT_SECRET},
                 check_robots=False)
    out = []
    for it in r.json().get("items", []):
        strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or ""))  # noqa: E731
        out.append({"url": it.get("originallink") or it["link"], "title": strip(it["title"]),
                    "summary": strip(it.get("description")),
                    "date": parsedate_to_datetime(it["pubDate"]).date().isoformat()})
    return out


def search_google_rss(query: str) -> list[dict]:
    """키 없이 쓰는 대안. 링크가 구글 리다이렉트라 본문 수집은 실패할 수 있어 제목·요약 위주."""
    r = http_get("https://news.google.com/rss/search",
                 params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}, check_robots=False)
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        desc = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        out.append({"url": it.findtext("link"), "title": it.findtext("title"), "summary": desc,
                    "date": parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()})
    return out


def discover(con, queries=None, pages=1) -> list[dict]:
    hits = []
    for q in queries or DEFAULT_QUERIES:
        if config.NAVER_CLIENT_ID:
            for p in range(pages):
                hits += search_naver(q, start=1 + p * 50)
        else:
            hits += search_google_rss(q)
    seen, uniq = set(), []
    for h in hits:
        if h["url"] not in seen:
            seen.add(h["url"])
            uniq.append(h)
    return uniq


# ───────────── 추출 ─────────────
_ROUND = re.compile(
    r"(프리\s*시드|시드|프리\s*시리즈\s*[A-D]|프리\s*[A-D]|시리즈\s*[A-D]|브릿지|Pre-?\s*Series\s*[A-D]|Series\s*[A-D]|"
    r"Pre-?\s*A|Seed)", re.I)
# "스타트업 OOO(대표 홍길동)" 패턴 — 국내 스타트업 기사에서 가장 흔한 사명 표기
_COMPANY_CEO = re.compile(r"([가-힣A-Za-z0-9&·]{2,20})\s*\(\s*대표(?:이사)?\s*([가-힣]{2,4})(?:\s*[,·][^)]*)?\)")
_INVESTOR = re.compile(
    r"([가-힣A-Za-z0-9&]{1,20}(?:벤처스|벤처캐피탈|인베스트먼트|파트너스|캐피탈|액셀러레이터|기술지주|"
    r"투자파트너스|자산운용|창업투자|에쿼티|Ventures|Partners|Capital|Investment))")
_SENT = re.compile(r"(?<=[.!?다])\s+")
_HEALTH = re.compile(r"헬스케어|의료|병원|환자|진단|치료|디지털치료|바이오|임상|웰니스|건강|의료기기|원격", re.I)

_ROUND_NORM = [
    (r"프리\s*시드", "프리시드"), (r"pre-?\s*(series\s*)?a|프리\s*(시리즈\s*)?a", "프리A"),
    (r"시리즈\s*([a-d])|series\s*([a-d])", None), (r"시드|seed", "시드"), (r"브릿지", "브릿지"),
]


def norm_round(s: str) -> str:
    s0 = s.lower()
    for pat, label in _ROUND_NORM:
        m = re.search(pat, s0, re.I)
        if m:
            return label or (m.group(1) or m.group(2)).upper()
    return s


# 제목의 "케어웨이브, 30억 프리A 투자 유치" 형태
_TITLE_COMPANY = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?([가-힣A-Za-z0-9&·]{2,20})\s*[,，]")


def extract(text: str, title: str | None = None) -> dict:
    """기사 본문에서 사명·대표·라운드·금액·투자사 후보 추출."""
    text = f"{title or ''}\n{text}"
    out = {"companies": [], "round": None, "amount": None, "investors": [], "health": bool(_HEALTH.search(text))}
    for name, ceo in _COMPANY_CEO.findall(text):
        name = re.sub(r"^(스타트업|기업|업체|회사|플랫폼)", "", name)
        if name and (name, ceo) not in out["companies"]:
            out["companies"].append((name, ceo))
    if not out["companies"] and title and (m := _TITLE_COMPANY.match(title)):
        out["companies"].append((m.group(1), None))
    for sent in _SENT.split(text):
        if "투자" not in sent:
            continue
        if not out["round"] and (m := _ROUND.search(sent)):
            out["round"] = norm_round(m.group(1))
        if not out["amount"] and (m := re.search(r"\d[\d,.]*\s*(?:조\s*\d[\d,.]*\s*)?억\s*원?", sent)):
            out["amount"] = parse_krw(m.group(0))
        for inv in _INVESTOR.findall(sent):
            inv = re.sub(r"^(그리고|및|와|과|등)", "", inv)
            if inv not in out["investors"]:
                out["investors"].append(inv)
    own = {norm_name(c) for c, _ in out["companies"]}
    out["investors"] = [i for i in out["investors"] if norm_name(i) not in own]
    return out


def apply_article(con, doc: dict, a, require_health=True) -> int:
    """Claude 추출 결과(llm.Article) → DB. 신뢰도 '중'."""
    if not a.is_startup_news or not a.company or (require_health and not a.is_health):
        return 0
    url, n = doc["url"], 0
    cid = db.get_or_create_company(con, a.company, source_url=url)
    db.add_alias(con, cid, a.company_en, url)
    con.execute("INSERT OR IGNORE INTO doc_company_links VALUES (?,?)", (doc["id"], cid))
    n += db.set_fields(con, cid, {
        "ceo": a.ceo, "name_en": a.company_en, "bm_summary": a.bm_summary, "service_summary": a.service_summary,
        "headcount": a.headcount or None, "founded_date": f"{a.founded_year}-01-01" if a.founded_year else None,
    }, "news-llm", url, "중")
    for t in a.customers:
        db.add_tag(con, cid, "customer", t, url)
    for t in a.payers:
        db.add_tag(con, cid, "payer", t, url)
    if a.round or a.amount_krw:
        rid = db.add_round(con, cid, a.round or None, doc.get("published_at"), a.amount_krw or None,
                           a.investors, url, "중")
        if a.lead_investor:
            iid = db.get_or_create_investor(con, a.lead_investor)
            con.execute("INSERT OR REPLACE INTO round_investors VALUES (?,?,1)", (rid, iid))
        n += 1
    for p in a.partners:
        db.upsert_row(con, "partners", {"company_id": cid, "partner_name": p, "relation": "PoC·파트너",
                                        "source_url": url, "confidence": "중"})
    for prog in a.programs:
        db.add_tag(con, cid, "program", prog, url)
        is_tips = "팁스" in prog or "TIPS" in prog.upper()
        db.upsert_row(con, "grants", {"company_id": cid, "program": "TIPS" if is_tips else prog,
                                      "year": int((doc.get("published_at") or "0000")[:4]) or None,
                                      "operator": a.tips_operator if is_tips else None,
                                      "source_url": url, "confidence": "중"})
    if a.regulatory:
        db.set_field(con, cid, "regulatory_status", a.regulatory, "news-llm", url, "하")
    con.execute("UPDATE companies SET confidence=coalesce(confidence,'중') WHERE id=?", (cid,))
    return n + 1


def process_doc(con, doc: dict, require_health=True) -> int:
    """수집된 기사 1건 → DB 반영. 반영한 fact 수 반환.
    ANTHROPIC_API_KEY 가 있으면 Claude 추출(본문이 충분할 때), 없으면 규칙 기반."""
    if config.ANTHROPIC_API_KEY and len(doc.get("body") or "") > 200:
        from .. import llm
        con.commit()  # Claude 호출(수 초) 동안 쓰기 잠금을 잡고 있지 않도록
        a = llm.extract_article(doc.get("title") or "", doc["body"])
        con.execute("UPDATE raw_documents SET processed=1 WHERE id=?", (doc["id"],))
        return apply_article(con, doc, a, require_health) if a else 0
    ex = extract(doc["body"] or "", doc.get("title"))
    if require_health and not ex["health"]:
        con.execute("UPDATE raw_documents SET processed=1 WHERE id=?", (doc["id"],))
        return 0
    n = 0
    # 기사 대부분은 주인공 기업이 첫 번째 "(대표 ...)" 표기
    for name, ceo in ex["companies"][:1]:
        # 제목에서만 잡힌 이름(대표 표기 없음)은 투자 신호가 있을 때만 기업으로 등록 (기관·센터 오탐 방지)
        if ceo is None and not (ex["round"] or ex["amount"]):
            break
        if re.search(r"센터|협회|재단|진흥원|대학교|병원$|부$|청$", name):
            break
        cid = db.get_or_create_company(con, name, source_url=doc["url"])
        con.execute("INSERT OR IGNORE INTO doc_company_links VALUES (?,?)", (doc["id"], cid))
        n += db.set_field(con, cid, "ceo", ceo, "news", doc["url"], "하")
        if ex["round"] or ex["amount"]:
            db.add_round(con, cid, ex["round"], doc.get("published_at"), ex["amount"],
                         ex["investors"], doc["url"], "하")
            n += 1
        con.execute("UPDATE companies SET confidence=coalesce(confidence,'하') WHERE id=?", (cid,))
    con.execute("UPDATE raw_documents SET processed=1 WHERE id=?", (doc["id"],))
    return n


def store_snippet(con, h: dict) -> dict:
    con.execute(
        "INSERT OR IGNORE INTO raw_documents(url, source, title, published_at, body) VALUES (?,?,?,?,?)",
        (h["url"], "news-snippet", h["title"], h["date"], h.get("summary") or ""))
    return dict(con.execute("SELECT * FROM raw_documents WHERE url=?", (h["url"],)).fetchone())


def run(con, queries=None, pages=1, limit=None) -> None:
    with db.run_log(con, "news", repr(queries)) as st:
        hits = discover(con, queries, pages)
        print(f"검색 결과 {len(hits)}건")
        for h in hits[:limit]:
            # 구글뉴스 링크는 robots 로 막혀 있어 본문 대신 제목+요약만 저장
            doc = None if "news.google.com" in h["url"] else fetch_doc(con, h["url"], "news")
            if not doc:
                doc = store_snippet(con, h)
            if not doc.get("published_at"):
                con.execute("UPDATE raw_documents SET published_at=? WHERE id=?", (h["date"], doc["id"]))
                doc["published_at"] = h["date"]
            st["docs"] += 1
            if not doc["processed"]:
                st["facts"] += process_doc(con, doc)
            con.commit()
        print(f"기사 {st['docs']}건 처리, fact {st['facts']}건 반영")


# ───────────── 업계 언론 RSS (6단계: 노출) ─────────────
# 피드 본문(content:encoded)이 있으면 그걸 쓰고, 없으면 robots 가 허용할 때만 기사 페이지를 가져온다.
FEEDS = {
    "벤처스퀘어": "https://www.venturesquare.net/feed",
    "스타트업레시피": "https://startuprecipe.co.kr/feed",
    "와우테일": "https://wowtale.net/feed/",
    "플래텀": "https://platum.kr/feed",
    "히트뉴스": "https://www.hitnews.co.kr/rss/allArticle.xml",
    "모비인사이드": "https://www.mobiinside.co.kr/feed/",
}
# 과거 기사까지 훑을 때 쓰는 헬스케어 태그 피드 (WordPress ?paged=N 으로 과거 페이지)
ARCHIVE_FEEDS = {
    "벤처스퀘어#헬스케어": "https://www.venturesquare.net/tag/%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4/feed/",
    "벤처스퀘어#디지털헬스케어": "https://www.venturesquare.net/tag/%EB%94%94%EC%A7%80%ED%84%B8%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4/feed/",
    "플래텀#헬스케어": "https://platum.kr/archives/tag/%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4/feed",
}
_SIGNAL = re.compile(r"투자|유치|선정|팁스|TIPS|시드|시리즈|프리A|데모데이|액셀러레이팅|혁신의료기기|실증특례|PoC|MOU", re.I)
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _feed_items(url: str, page: int = 1) -> list[dict]:
    r = http_get(url, check_robots=False, params={"paged": page} if page > 1 else None)
    if r.status_code != 200 or not r.content.strip():
        return []
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        raw = it.findtext(_CONTENT) or it.findtext("description") or ""
        body = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        pub = it.findtext("pubDate")
        try:
            date = parsedate_to_datetime(pub).date().isoformat() if pub else None
        except (TypeError, ValueError):
            date = (pub or "")[:10] or None
        out.append({"url": it.findtext("link"), "title": (it.findtext("title") or "").strip(),
                    "body": re.sub(r"\s+", " ", body).strip(), "date": date})
    return out


def _collect(url: str, pages: int, since: str | None) -> list[dict]:
    """피드 1~pages 페이지를 모은다. since(YYYY-MM-DD)보다 오래된 기사가 나오면 멈춤."""
    items = []
    for p in range(1, pages + 1):
        try:
            batch = _feed_items(url, p)
        except Exception:  # noqa: BLE001 — 마지막 페이지 넘어가면 404/파싱 오류
            break
        if not batch:
            break
        items += batch
        if since and all((b["date"] or "9999") < since for b in batch):
            break
    return items


def run_feeds(con, names: list[str] | None = None, pages: int = 1, since: str | None = None,
              archive: bool = False) -> None:
    feeds = {**FEEDS, **(ARCHIVE_FEEDS if archive else {})}
    with db.run_log(con, "news-rss", f"{names} pages={pages} since={since}") as st:
        for name, url in feeds.items():
            if names and not any(n in name for n in names):
                continue
            items = [h for h in _collect(url, pages, since) if not since or (h["date"] or "9999") >= since]
            hits = [h for h in items if _SIGNAL.search(h["title"] + h["body"][:500])
                    and _HEALTH.search(h["title"] + h["body"][:1500])]
            print(f"[{name}] {len(items)}건 중 헬스케어·투자 신호 {len(hits)}건")
            for h in hits:
                if con.execute("SELECT 1 FROM raw_documents WHERE url=? AND processed=1", (h["url"],)).fetchone():
                    continue
                if len(h["body"]) < 300:  # 피드에 요약만 있으면 페이지를 가져옴 (robots 허용 시)
                    doc = fetch_doc(con, h["url"], "news-rss")
                    if not doc:
                        continue
                else:
                    con.execute("INSERT OR IGNORE INTO raw_documents(url, source, title, published_at, body) "
                                "VALUES (?,?,?,?,?)", (h["url"], f"rss:{name}", h["title"], h["date"], h["body"]))
                    doc = dict(con.execute("SELECT * FROM raw_documents WHERE url=?", (h["url"],)).fetchone())
                st["docs"] += 1
                st["facts"] += process_doc(con, doc)
                con.commit()
        print(f"기사 {st['docs']}건 처리, fact {st['facts']}건 반영")


def run_urls(con, urls: list[str]) -> None:
    with db.run_log(con, "news-url", repr(urls)) as st:
        for u in urls:
            doc = fetch_doc(con, u, "news", force=True)
            if doc:
                st["docs"] += 1
                st["facts"] += process_doc(con, doc, require_health=False)
                print(f"  {doc['title']}: {extract(doc['body'], doc['title'])}")
        con.commit()
