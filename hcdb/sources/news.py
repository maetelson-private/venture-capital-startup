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
def search_naver(query: str, display=50, start=1) -> list[dict]:
    r = http_get("https://openapi.naver.com/v1/search/news.json",
                 params={"query": query, "display": display, "start": start, "sort": "date"},
                 headers={"X-Naver-Client-Id": config.NAVER_CLIENT_ID,
                          "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET},
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


def process_doc(con, doc: dict, require_health=True) -> int:
    """수집된 기사 1건 → DB 반영. 반영한 fact 수 반환."""
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


def run_urls(con, urls: list[str]) -> None:
    with db.run_log(con, "news-url", repr(urls)) as st:
        for u in urls:
            doc = fetch_doc(con, u, "news", force=True)
            if doc:
                st["docs"] += 1
                st["facts"] += process_doc(con, doc, require_health=False)
                print(f"  {doc['title']}: {extract(doc['body'], doc['title'])}")
        con.commit()
