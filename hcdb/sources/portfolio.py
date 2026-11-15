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
