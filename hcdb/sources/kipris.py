"""KIPRIS Plus '특허·실용 공개·등록공보' API — 출원인 이름으로 특허·실용신안 검색.

응답 필드는 KIPRIS Plus 명세(patUtiModInfoSearchSevice) 기준:
applicationNumber, inventionTitle, registerStatus(공개/등록/거절/무효/소멸/취하/포기),
ipcNumber·applicantName(복수값 '|' 구분), applicationDate·registerDate('YYYY/MM/DD hh:mm:ss').
getWordSearch 는 폐기 예정이라 출원인 검색은 getAdvancedSearch(applicant=) 를 쓴다.
※ PCT 여부는 이 API 응답에 없어 is_pct 는 채우지 않는다.
"""
import xml.etree.ElementTree as ET

from .. import config, db
from ..fetch import http_get
from ..normalize import norm_date, norm_name

URL = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"
ROWS = 500  # 명세상 최대
STATUSES = ("등록", "공개", "거절", "무효", "소멸", "취하", "포기")


def _status(s: str | None) -> str | None:
    if not s:
        return None
    return next((k for k in STATUSES if k in s), "출원")


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split("|") if x.strip()]


def parse(xml_bytes: bytes) -> tuple[list[dict], int]:
    """응답 XML → (특허 dict 목록, totalCount). 오류 코드면 예외."""
    root = ET.fromstring(xml_bytes)
    code = root.findtext(".//header/resultCode")
    if code not in (None, "", "00"):
        raise RuntimeError(f"KIPRIS 오류 {code}: {root.findtext('.//header/resultMsg')}")
    out = []
    for it in root.iter("item"):
        out.append({
            "application_no": it.findtext("applicationNumber"),
            "title": " ".join((it.findtext("inventionTitle") or "").split()) or None,
            "status": _status(it.findtext("registerStatus")),
            "ipc": " | ".join(_split(it.findtext("ipcNumber"))) or None,
            "applicants": _split(it.findtext("applicantName")),
            "filed_date": norm_date(it.findtext("applicationDate")),
            "registered_date": norm_date(it.findtext("registerDate")),
        })
    total = int(root.findtext(".//count/totalCount") or 0)
    return out, total


def run(con, max_pages=3) -> None:
    if not config.KIPRIS_KEY:
        raise SystemExit("KIPRIS_KEY 가 .env 에 없습니다 (plus.kipris.or.kr 에서 신청)")
    with db.run_log(con, "kipris") as st:
        for c in con.execute("SELECT id, name FROM companies WHERE is_pre_startup=0").fetchall():
            # 이형 표기까지 모두 비교키로 사용
            keys = {r["alias_norm"] for r in
                    con.execute("SELECT alias_norm FROM company_aliases WHERE company_id=?", (c["id"],))}
            keys.add(norm_name(c["name"]))
            for page in range(1, max_pages + 1):
                r = http_get(URL, check_robots=False, params={
                    "applicant": c["name"], "patent": "true", "utility": "true",
                    "numOfRows": ROWS, "pageNo": page, "ServiceKey": config.KIPRIS_KEY})
                items, total = parse(r.content)
                st["docs"] += 1
                for p in items:
                    # 부분일치 검색이라 공동출원인 중 한 명이라도 정확히 일치할 때만 채택
                    if not p["application_no"] or not any(norm_name(a) in keys for a in p["applicants"]):
                        continue
                    db.upsert_row(con, "patents", {
                        "application_no": p["application_no"], "company_id": c["id"],
                        "title": p["title"], "status": p["status"], "ipc": p["ipc"],
                        "applicants": " | ".join(p["applicants"]),
                        "filed_date": p["filed_date"], "registered_date": p["registered_date"],
                        "source_url": f"kipris:{p['application_no']}",
                    })
                    st["facts"] += 1
                if page * ROWS >= total:
                    break
            con.commit()
    print(f"특허 {st['facts']}건 반영")
