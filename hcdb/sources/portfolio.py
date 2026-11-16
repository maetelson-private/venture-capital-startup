"""헬스케어 투자사 포트폴리오 페이지 → 기업 + '이미 투자받음' 표시.

블루포인트 관점에선 '첫 투자 기회가 지난 기업'을 가르는 필터이면서, 같은 분야의 시드 기업 목록이기도 하다.
투자 라운드는 금액을 모르므로 round='포트폴리오', announced_date=투자연도-01-01 로 넣는다.

지원: DHP(디지털헬스케어파트너스) — 정적 HTML, robots 허용.
"""
import re

from bs4 import BeautifulSoup

from .. import db
from ..fetch import http_get, http_post_form

DHP_URL = "https://dhpartners.io/portfolio"


def dhp() -> list[dict]:
    r = http_get(DHP_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [l for l in (soup.select_one("main") or soup.body).get_text("\n", strip=True).split("\n") if l.strip()]
    i = next(k for k, l in enumerate(lines) if l == "개 회사") + 1
    out = []
    # 반복 단위: 회사명 / 한 줄 소개 / 분야 / 투자연도 / 국가
    while i + 4 < len(lines):
        name, desc, cat, year, country = lines[i:i + 5]
        if not re.fullmatch(r"20\d\d", year):
            i += 1
            continue
        out.append({"name": name, "desc": desc, "category": cat, "year": int(year), "country": country})
        i += 5
    return out


DCAMP_URL = "https://dcamp.kr/startups"


def dcamp() -> list[dict]:
    """D.CAMP 스타트업: 페이지 안 `const startups=[...]` JSON. 사업자번호·설립일·업종·투자 여부 포함."""
    import json
    r = http_get(DCAMP_URL)
    r.encoding = "utf-8"
    m = re.search(r"const\s+startups\s*=\s*(\[.*?\]);", r.text, re.S)
    return json.loads(m.group(1)) if m else []


def run_dcamp(con) -> None:
    items = [x for x in dcamp() if "헬스케어" in (x.get("industry_main") or "") or "바이오" in (x.get("industry_main") or "")]
    print(f"D.CAMP 바이오/헬스케어 {len(items)}곳")
    with db.run_log(con, "portfolio:dcamp") as st:
        for x in items:
            name = (x.get("name_ko") or x.get("name_en") or "").strip()
            if not name:
                continue
            cid = db.get_or_create_company(con, name, x.get("business_registration_number"), DCAMP_URL)
            db.add_alias(con, cid, x.get("name_en"), DCAMP_URL)
            db.set_fields(con, cid, {
                "biz_no": x.get("business_registration_number"), "founded_date": x.get("founding_date"),
                "ceo": x.get("representative_name"), "url": x.get("homepage_url"),
                "service_summary": " — ".join(filter(None, [x.get("service_name"), x.get("service_description")]))[:300] or None,
            }, "portfolio:dcamp", DCAMP_URL, "상")
            if x.get("industry_sub"):
                db.add_tag(con, cid, "kw_means", x["industry_sub"], DCAMP_URL)
            db.add_tag(con, cid, "program", "D.CAMP " + (x.get("family_group_label") or "입주"), DCAMP_URL)
            if x.get("has_investment"):
                db.add_round(con, cid, "D.CAMP 투자", None, None, ["디캠프"], DCAMP_URL, "상")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


def run(con, korea_only=True) -> None:
    items = dhp()
    if korea_only:
        items = [x for x in items if x["country"] == "한국"]
    print(f"DHP 포트폴리오 {len(items)}곳")
    with db.run_log(con, "portfolio:dhp") as st:
        for x in items:
            name = re.sub(r"\s*\(.*\)$", "", x["name"]).strip()
            cid = db.get_or_create_company(con, name, source_url=DHP_URL)
            db.add_alias(con, cid, x["name"], DHP_URL)
            db.set_fields(con, cid, {"service_summary": x["desc"]}, "portfolio:dhp", DHP_URL, "중")
            db.add_tag(con, cid, "kw_means", x["category"], DHP_URL)
            db.add_round(con, cid, "포트폴리오", f"{x['year']}-01-01", None, ["디지털헬스케어파트너스"], DHP_URL, "상")
            con.execute("UPDATE investors SET type=coalesce(type,'AC'), grp=coalesce(grp,'G2') WHERE name='디지털헬스케어파트너스'")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


D2SF_API = "https://d2sf.naver.com/api/portfolios"
D2SF_URL = "https://d2sf.naver.com/ko/portfolio"


def d2sf(sector: str = "Healthcare") -> list[dict]:
    """NAVER D2SF 포트폴리오 공개 API (페이지 화면이 쓰는 것). 한 번에 10건, searchAfter 로 다음 페이지."""
    out, after = [], None
    while True:
        params = {"langCode": "ko", "sector": sector, "display": 10}
        if after:
            params.update(searchAfterPublishAt=after[0], searchAfterId=after[1])
        js = http_get(D2SF_API, params=params).json()
        out += js.get("list") or []
        after = js.get("searchAfter")
        if not js.get("list") or not after or len(out) >= (js.get("total") or 0):
            return out


def _stage_year(s: str | None) -> tuple[str | None, int | None]:
    m = re.match(r"\s*([^,]+?)\s*,\s*(20\d\d)", s or "")
    return (m.group(1).strip(), int(m.group(2))) if m else (None, None)


def run_d2sf(con, korea_only=True) -> None:
    items = d2sf()
    rows = []
    for x in items:
        tags = {t["tag"]["subCategory"]: [] for t in x.get("portfolioTags", [])}
        for t in x.get("portfolioTags", []):
            tags[t["tag"]["subCategory"]].append(t["tag"]["name"])
        if korea_only and "South Korea" not in tags.get("region", []):
            continue
        rows.append((x, tags))
    print(f"D2SF 헬스케어 {len(items)}곳 중 한국 {len(rows)}곳")
    with db.run_log(con, "portfolio:d2sf") as st:
        for x, tags in rows:
            name = x["companyName"].strip()
            cid = db.get_or_create_company(con, name, source_url=D2SF_URL)
            home = next((l["url"].strip() for l in x.get("portfolioLinks") or [] if l.get("type") == "home"), None)
            vals = {"service_summary": (x.get("description") or "").strip() or None, "ceo": x.get("founder"),
                    "url": home}
            db.set_fields(con, cid, vals, "portfolio:d2sf", D2SF_URL, "상")
            year = (x.get("foundedAt") or "")[:4]
            if year.isdigit():  # 연도만 공개 → 7월 1일로 두고 신뢰도 '하' (설립일 보강 규칙과 같음)
                db.set_field(con, cid, "founded_date", f"{year}-07-01", "portfolio:d2sf", D2SF_URL, "하")
            for sec in tags.get("sector", []):
                if sec != "Healthcare":
                    db.add_tag(con, cid, "kw_means", sec, D2SF_URL)
            db.add_tag(con, cid, "program", "NAVER D2SF", D2SF_URL)
            stage, y = _stage_year(x.get("fundedByNaver"))
            db.add_round(con, cid, stage or "D2SF 투자", f"{y}-01-01" if y else None, None, ["네이버 D2SF"],
                         D2SF_URL, "상")
            later, ly = _stage_year(x.get("latestFunded"))
            if later and (later, ly) != (stage, y):
                db.add_round(con, cid, later, f"{ly}-01-01" if ly else None, None, [], D2SF_URL, "중")
            st["facts"] += 1
        con.execute("UPDATE investors SET type=coalesce(type,'CVC') WHERE name='네이버 D2SF'")
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 액셀러레이터·VC 헬스케어 포트폴리오 (시드이자 '이미 투자받음' 필터) ─────────────────
def _flight(html: str) -> str:
    """Next.js App Router 페이지의 self.__next_f.push([1,"…"]) 조각을 이어 붙인 텍스트."""
    import json
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    return "".join(json.loads('"' + c + '"') for c in chunks)


def _objs(text: str, start_pat: str, need: str | None = None) -> list[dict]:
    import json
    dec, out = json.JSONDecoder(), []
    for m in re.finditer(start_pat, text):
        try:
            o, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if need is None or need in o:
            out.append(o)
    return out


def futureplay() -> list[dict]:
    url = "https://futureplay.co/our-companies"
    text = _flight(http_get(url).text)
    out = []
    for o in _objs(text, r'\{"id":\d+,"name":\{"en"'):
        if any("HEALTH" in ((c.get("content") or {}).get("en") or "") for c in o.get("categories") or []):
            desc = (o.get("description") or {}).get("ko") or ""
            out.append({"name": (o["name"].get("ko") or o["name"].get("en") or "").strip(), "alias": o["name"].get("en"),
                        "ceo": (o.get("representative") or {}).get("ko"), "url": o.get("website"),
                        "desc": None if desc.startswith("$") else desc[:300]})
    return out


def lotte() -> list[dict]:
    url = "https://www.lotteventures.com/portfolio"
    text = _flight(http_get(url).text)
    out = []
    for o in _objs(text, r'\{"id":\d+,"name":"', "sectors"):
        if "areaBio" in o.get("sectors", []):
            prog = ", ".join(f"{m.get('kind')} {m.get('batchNo') or ''}".strip() for m in o.get("memberships") or [])
            out.append({"name": o["name"].strip(), "alias": o.get("nameAlt"), "desc": o.get("shortDescription"),
                        "program": prog})
    return out


def springcamp() -> list[dict]:
    r = http_get("https://springcamp.co/portfolio")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in s.select("a.company-card"):
        cat = a.select_one(".card-category")
        if cat and "health" in cat.get_text().lower():
            out.append({"name": a.select_one(".card-name").get_text(strip=True),
                        "url": a.get("href") if a.get("href", "").startswith("http") else None})
    return out


def mashup() -> list[dict]:
    r = http_get("https://mashupventures.co/portfolio/")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in s.select("a.catalog-card"):
        tags = (a.select_one("p.card-tags").get_text(" ", strip=True) if a.select_one("p.card-tags") else "")
        if "health" in tags.lower() or "헬스" in tags:
            d = a.select_one("p.card-description")
            out.append({"name": a.select_one("h3").get_text(strip=True), "desc": d.get_text(" ", strip=True) if d else None})
    return out


def kakao() -> list[dict]:
    out, seen = [], set()
    for page in range(1, 15):
        r = http_get("https://www.kakao.vc/family", params={"bd68ea8e_page": page} if page > 1 else None)
        r.encoding = "utf-8"
        items = [it for it in BeautifulSoup(r.text, "html.parser").select(".w-dyn-item")
                 if it.select_one("[fs-cmsfilter-field=company]")]
        new = 0
        for it in items:
            g = lambda sel: (it.select_one(sel).get_text(" ", strip=True) if it.select_one(sel) else "")  # noqa: E731
            name = g("[fs-cmsfilter-field=company]")
            if not name or name in seen:
                continue
            seen.add(name)
            new += 1
            sectors = " ".join(x.get_text(" ", strip=True) for x in it.select("[fs-cmsfilter-field=sector]"))
            if "헬스" in sectors:
                out.append({"name": name, "desc": g(".family-item-preview") or None})
        if not new:
            break
    return out


VCS = {  # key: (수집 함수, 투자사 이름, 투자사 유형)
    "futureplay": (futureplay, "퓨처플레이", "AC"),
    "lotte": (lotte, "롯데벤처스", "CVC"),
    "springcamp": (springcamp, "스프링캠프", "AC"),
    "mashup": (mashup, "매쉬업벤처스", "AC"),
    "kakao": (kakao, "카카오벤처스", "VC"),
}


def run_vc(con, key: str) -> None:
    fn, investor, typ = VCS[key]
    try:
        items = [x for x in fn() if x.get("name")]
    except Exception as e:  # noqa: BLE001
        print(f"{investor}: 실패 {e!r}")
        return
    print(f"{investor} 헬스케어 포트폴리오 {len(items)}곳")
    with db.run_log(con, f"portfolio:{key}") as st:
        for x in items:
            name = re.sub(r"\(주\)|㈜|주식회사", "", x["name"]).strip()
            cid = db.get_or_create_company(con, name, source_url=key)
            db.add_alias(con, cid, x.get("alias"), key)
            db.set_fields(con, cid, {"service_summary": x.get("desc"), "ceo": x.get("ceo"), "url": x.get("url")},
                          f"portfolio:{key}", None, "중")
            db.add_tag(con, cid, "program", f"{investor} 포트폴리오" + (f" ({x['program']})" if x.get("program") else ""), None)
            db.add_round(con, cid, "포트폴리오", None, None, [investor], None, "상")
            st["facts"] += 1
        con.execute("UPDATE investors SET type=coalesce(type,?) WHERE name=?", (typ, investor))
        con.commit()


# ── 2차: 액셀러레이터·CVC·연구소 기술지주 (2026-11 조사) ─────────────────────────────
def _health(*t) -> bool:
    from .univ import is_health
    return is_health(*t)


def iaccel() -> list[dict]:
    """인포뱅크 아이엑셀: 바이오/헬스케어 분류(category_seq2=17) JSON."""
    r = http_post_form("http://www.iaccel.net/portfolio/getPortfolioList.json", {
        "startPage": 1, "numPerPage": 500, "category_seq1": 2, "category_seq2_list[]": 17})
    rows = ((r.json().get("data") or {}).get("result") or [{}])[0].get("connectPortfolioList") or []
    return [{"name": x.get("title", ""), "desc": x.get("content"), "ceo": x.get("representative"),
             "url": x.get("site_url")} for x in rows]


def clab() -> list[dict]:
    """삼성 C-Lab Outside 육성기업 + 스핀오프 (설명 키워드로 헬스케어만)."""
    import json as _j
    out = []
    for ep in ("portpolio", "spinoff"):
        r = http_post_form(f"https://www.samsungclab.com/api/{ep}/list", {})
        try:
            data = r.json()
        except _j.JSONDecodeError:
            continue
        for grp in data if isinstance(data, list) else data.get("data", []):
            for x in grp.get("list", []) if isinstance(grp, dict) else []:
                desc = x.get("companyDesc") or x.get("companyDescShort")
                if _health(desc, x.get("companyDescShort")):
                    out.append({"name": x.get("companyName", ""), "alias": x.get("companyNameEn"), "desc": desc,
                                "url": x.get("companyLink"), "program": f"{x.get('year', '')} {ep}"})
    return out


def kiac() -> list[dict]:
    """한국투자액셀러레이터: 카드의 분야 라벨로 바이오/헬스케어."""
    r = http_get("https://ac.koreainvestment.com/portfolio")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    out = []
    for it in s.select("div.portfolio-item"):
        g = lambda sel: (it.select_one(sel).get_text(" ", strip=True) if it.select_one(sel) else "")  # noqa: E731
        typ, name, say = g(".portfolio-item-type"), g(".portfolio-item-title"), g(".portfolio-item-say")
        if name and (re.search(r"바이오|헬스|Health|Bio|의료|Medi", typ, re.I) or _health(say)):
            out.append({"name": name, "desc": say})
    return out


def etri() -> list[dict]:
    """ETRI홀딩스(연구소 기술지주): '바이오·메디컬' + 설명에 의료 단서가 있는 ICT."""
    r = http_get("https://www.etriholdings.com/portfolio")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in s.select("li > a"):
        divi, comp, part = (a.select_one(f".text p.{c}") for c in ("divi", "company", "part"))
        if not comp:
            continue
        d, desc = (divi.get_text(strip=True) if divi else ""), (part.get_text(" ", strip=True) if part else "")
        if "바이오" in d or "메디" in d or _health(desc):
            out.append({"name": comp.get_text(strip=True), "desc": desc,
                        "url": a.get("href") if a.get("href", "").startswith("http") else None})
    return out


def theventures() -> list[dict]:
    r = http_get("https://theventures.vc/ko/companies")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in s.select("a[href]"):
        nm = a.select_one("div.font-medium")
        muted = [d.get_text(strip=True) for d in a.select("div.text-muted-foreground")]
        if nm and muted and "Healthcare" in muted[0] and (len(muted) < 2 or "한국" in muted[1] or "Korea" in muted[1]):
            out.append({"name": nm.get_text(strip=True), "url": a["href"] if a["href"].startswith("http") else None})
    return out


def ccvc() -> list[dict]:
    """쿨리지코너인베스트먼트: 공개 API, BIO 분류(category1=6)."""
    js = http_get("https://ccvc.co.kr/api/user/0/mapping/product", params={
        "fields": "id,name,name_en,simple_desc,tags,categories", "ordering": "id", "page_length": 500,
        "category1": 6}).json()
    return [{"name": x.get("name", ""), "alias": x.get("name_en"), "desc": x.get("simple_desc")}
            for x in js.get("data", [])]


def signite() -> list[dict]:
    """시그나이트파트너스(신세계 CVC): 공개 JSON, 'Healthcare' 태그 또는 설명 키워드."""
    out, url, params = [], "https://invest-api.lukuku.co/portfolios", {"ordering": "-my_order", "limit": 100}
    while url:
        js = http_get(url, params=params, check_robots=False).json()
        for x in js.get("results", []):
            tags = " ".join(x.get("tagsNameList") or [])
            if "Health" in tags or _health(x.get("content")):
                out.append({"name": x.get("companyName1", ""), "desc": x.get("content"), "url": x.get("link")})
        url, params = js.get("next"), None
    return out


def kingo() -> list[dict]:
    """킹고스프링(성균관대 기반 AC): 바이오 메뉴(/21), 이름만."""
    r = http_get("https://www.kingospring.com/21")
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    w = s.select("div._gallery_wrap")
    items = (w[0] if w else s).select("div._item")
    out = []
    for it in items:
        h = it.select_one("h4")
        if h and h.get_text(strip=True):
            a = it.select_one("a.item_container")
            out.append({"name": h.get_text(strip=True),
                        "url": a["href"] if a and a.get("href", "").startswith("http") else None})
    return out


VCS.update({
    "iaccel": (iaccel, "인포뱅크 아이엑셀", "AC"),
    "clab": (clab, "삼성 C-Lab Outside", "CVC"),
    "kiac": (kiac, "한국투자액셀러레이터", "AC"),
    "etri": (etri, "ETRI홀딩스", "기술지주"),
    "theventures": (theventures, "더벤처스", "AC"),
    "ccvc": (ccvc, "쿨리지코너인베스트먼트", "VC"),
    "signite": (signite, "시그나이트파트너스", "CVC"),
    "kingo": (kingo, "킹고스프링", "AC"),
})
