"""KIPRIS Plus 특허·실용신안 출원인 검색.
※ 엔드포인트·필드명은 KIPRIS Plus 에서 신청한 상품 문서로 한 번 확인할 것 (상품마다 조금씩 다름)."""
import xml.etree.ElementTree as ET

from .. import config, db
from ..fetch import http_get
from ..normalize import norm_date

URL = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"


def _status(s: str | None) -> str | None:
    if not s:
        return None
    for k in ("등록", "공개", "거절", "소멸", "취하", "포기"):
        if k in s:
            return k
    return "출원"


def run(con, max_pages=3) -> None:
    if not config.KIPRIS_KEY:
        raise SystemExit("KIPRIS_KEY 가 .env 에 없습니다 (plus.kipris.or.kr 에서 신청)")
    with db.run_log(con, "kipris") as st:
        for c in con.execute("SELECT id, name FROM companies WHERE is_pre_startup=0").fetchall():
            for page in range(1, max_pages + 1):
                r = http_get(URL, check_robots=False, params={
                    "applicant": c["name"], "patent": "true", "utility": "true",
                    "numOfRows": 100, "pageNo": page, "ServiceKey": config.KIPRIS_KEY})
                items = list(ET.fromstring(r.content).iter("item"))
                for it in items:
                    app_no = it.findtext("applicationNumber")
                    if not app_no:
                        continue
                    # 출원인명이 정확히 일치하는지 한 번 더 확인 (부분일치 검색이라)
                    applicant = it.findtext("applicantName") or ""
                    if c["name"].replace(" ", "") not in applicant.replace(" ", ""):
                        continue
                    db.upsert_row(con, "patents", {
                        "application_no": app_no, "company_id": c["id"],
                        "title": it.findtext("inventionTitle"),
                        "status": _status(it.findtext("registerStatus")),
                        "is_pct": 1 if (it.findtext("internationalApplicationNumber") or "").strip() else 0,
                        "filed_date": norm_date(it.findtext("applicationDate")),
                        "registered_date": norm_date(it.findtext("registerDate")),
                        "source_url": f"kipris:{app_no}",
                    })
                    st["facts"] += 1
                st["docs"] += 1
                if len(items) < 100:
                    break
            con.commit()
    print(f"특허 {st['facts']}건 반영")
