"""헬스케어 투자사 포트폴리오 페이지 → 기업 + '이미 투자받음' 표시.

블루포인트 관점에선 '첫 투자 기회가 지난 기업'을 가르는 필터이면서, 같은 분야의 시드 기업 목록이기도 하다.
투자 라운드는 금액을 모르므로 round='포트폴리오', announced_date=투자연도-01-01 로 넣는다.

지원: DHP(디지털헬스케어파트너스) — 정적 HTML, robots 허용.
"""
import re

from bs4 import BeautifulSoup

from .. import db
from ..fetch import http_get

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
