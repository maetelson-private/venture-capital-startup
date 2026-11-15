"""TIPS 창업기업 (jointips.or.kr 공개 API) — 헬스케어·바이오의료 분야 선정기업.

팁스 선정 = 운영사(AC/VC)가 이미 투자했다는 뜻이므로 '첫 투자 기회가 지난 기업' 표시이자,
같은 분야 시드 기업 목록이기도 하다. 설립일·대표·홈페이지·소개·운영사가 함께 온다.
"""
import json

from .. import db
from ..fetch import http_get

API = "https://jointips.or.kr/api/cms/public/startup/list"
SRC = "https://jointips.or.kr/network/startups"
QUERIES = [{"industryStd": "BIO_MED"}, {"industry12": "HEALTH"}]
REGION = {"11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시", "29": "광주광역시",
          "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시", "41": "경기도", "42": "강원특별자치도",
          "51": "강원특별자치도", "43": "충청북도", "44": "충청남도", "45": "전북특별자치도", "52": "전북특별자치도",
          "46": "전라남도", "47": "경상북도", "48": "경상남도", "50": "제주특별자치도"}


def fetch_all() -> list[dict]:
    seen, out = set(), []
    for q in QUERIES:
        page = 1
        while True:
            js = http_get(API, check_robots=False, params={"page": page, "size": 100, **q}).json()
            items = js.get("data") or []
            for it in items:
                if it["id"] not in seen:
                    seen.add(it["id"])
                    out.append(it)
            if len(items) < 100:
                break
            page += 1
    return out


def run(con) -> None:
    items = fetch_all()
    print(f"TIPS 헬스케어·바이오의료 선정기업 {len(items)}곳")
    new = 0
    with db.run_log(con, "tips") as st:
        for it in items:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            existed = db.find_company(con, name) is not None
            cid = db.get_or_create_company(con, name, source_url=SRC)
            new += not existed
            codes = json.loads(it.get("industry12Codes") or "[]") + json.loads(it.get("industryStdCodes") or "[]")
            db.set_fields(con, cid, {
                "ceo": it.get("ceo"), "founded_date": it.get("estDt"), "url": it.get("homepageUrl"),
                "sido": REGION.get(str(it.get("regionCd") or "")), "sigungu": it.get("sigunguNm"),
                "service_summary": (it.get("intro") or "")[:300] or None,
            }, "tips", SRC, "상")
            for c in codes:
                db.add_tag(con, cid, "kw_means", {"HEALTH": "헬스케어", "BIO": "바이오", "BIO_MED": "바이오·의료"}.get(c, c), SRC)
            year = int(it["selYear"]) if str(it.get("selYear") or "").isdigit() else None
            op = it.get("operatorName")
            db.upsert_row(con, "grants", {"company_id": cid, "program": "TIPS", "year": year, "operator": op,
                                          "source_url": SRC, "confidence": "상"})
            db.add_tag(con, cid, "program", f"TIPS {year or ''}".strip(), SRC)
            if op:
                db.add_round(con, cid, "TIPS 운영사 투자", f"{year}-01-01" if year else None, None, [op], SRC, "상")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영 (신규 {new}곳)")
