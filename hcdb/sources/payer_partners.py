"""보험사·기업 협업 프로그램 명단 → DB 에 이미 있는 기업에만 지불자 근거를 붙인다 (새 기업은 만들지 않음).

- 삼성금융 C-Lab Outside 2026 본선 기사 (smarttoday): 삼성생명·삼성화재가 고른 기업 → 실손·민간보험, basis 추정, 신뢰도 하.
  같은 기사에 "본선 진출 17개사는 각각 3천만원의 솔루션 개발비를 지원받고 … 5개월간 협업" 이 있지만, 제품 구매가 아니라
  협업 개발비 지원이라 지불자 근거로 보지 않는다 (2026-11-17 사용자 결정). 삼성카드·삼성증권 선정사는 지불자 목록에 맞는 칸이 없어 붙이지 않는다.
- KB스타터스 포트폴리오 (KB Innovation HUB 공개 JSON): 지불 문장이 없으므로 basis 추정, 신뢰도 하.
  의료·건강 관련 소개인 곳만, "KB금융 보험 계열사와 협업 대상" 이라는 이유로 실손·민간보험을 추정한다.
- 이미 붙은 지불자 값은 지우지 않고 더한다. 상태가 '근거 없음'뿐이면 그 표시를 이 값으로 바꾼다. manual 은 건드리지 않는다.
- 수집은 fetch 규칙 그대로 (robots·간격·캐시).
"""
import re

from .. import db, fetch
from .payer_task import UNKNOWN

CLAB_URL = "https://www.smarttoday.co.kr/ko-kr/articles/107753"
CLAB_FEE = "본선에 진출한 17개 스타트업은 각각 3천만원의 솔루션 개발비를 지원받고 삼성금융사 현업부서와 5개월간 협업 아이디어를 구체화하게 된다."
KB_API = "https://kbfg.com/kbinnovationhub/starters/portfolio/getyear?region_type=0"
KB_PAGE = "https://kbfg.com/kbinnovationhub/starters/portfolio/list"
HEALTH = re.compile(r"의료|헬스|건강|병원|환자|진료|질환|치료|케어|돌봄|시니어|간병|정신|심리|수면|운동|영양|보험")
NOT_HUMAN = re.compile(r"반려|동물|가축|축산|펫")
INSURERS = ("삼성생명", "삼성화재")


def clab(con) -> list[dict]:
    doc = fetch.fetch_doc(con, CLAB_URL, "clab")
    if not doc:
        return []
    out = []
    for line in doc["body"].splitlines():
        aff = next((a for a in INSURERS if line.startswith(a)), None)
        if not aff:
            continue
        for m in re.finditer(r"▲([^▲(]+)\(", line):
            out.append({"name": m.group(1).strip(), "payer": "실손·민간보험", "basis": "추정", "confidence": "하",
                        "quote": f"{aff}은 ▲{m.group(1).strip()}"[:120] if f"{aff}은 ▲{m.group(1).strip()}" in line
                        else line[max(0, m.start() - 40): m.end() + 40][:120],
                        "url": CLAB_URL, "note": "program:clab",
                        "reason": f"{aff} 선정 · 같은 기사: {CLAB_FEE[:60]}…"})
    return out


def kb(con) -> list[dict]:
    r = fetch.http_post_form(KB_API, {})
    out = []
    for x in r.json().get("selectList", []):
        intro = x.get("intro_cn") or ""
        if not HEALTH.search(intro) or NOT_HUMAN.search(intro):
            continue
        out.append({"name": x["bzenty_nm"].strip(), "payer": "실손·민간보험", "basis": "추정", "confidence": "하",
                    "quote": None, "url": KB_PAGE, "note": "program:kb-starters",
                    "reason": f"KB스타터스 {x.get('port_yr')} 선정('{intro[:40]}') — KB금융 보험 계열사와 협업 대상이라 보험사 고객으로 추정"})
    return out


def _attach(con, cid: int, e: dict) -> str:
    if con.execute("SELECT 1 FROM tag_evidence WHERE company_id=? AND kind='payer' AND applied=1 AND source='manual'",
                   (cid,)).fetchone():
        return "manual 이라 건너뜀"
    if con.execute("SELECT 1 FROM tag_evidence WHERE company_id=? AND kind='payer' AND applied=1 AND value=? AND note=?",
                   (cid, e["payer"], e["note"])).fetchone():
        return "이미 있음"
    # '근거 없음' 표시만 있던 곳은 그 표시를 내림
    con.execute("UPDATE tag_evidence SET applied=0 WHERE company_id=? AND kind='payer' AND applied=1 AND value IN (?, '미상')",
                (cid, UNKNOWN))
    con.execute("DELETE FROM company_tags WHERE company_id=? AND kind='payer' AND value IN (?, '미상')", (cid, UNKNOWN))
    con.execute("""INSERT INTO tag_evidence(company_id, kind, value, quote, source, source_url, confidence, note, basis, reason, applied)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                (cid, "payer", e["payer"], e["quote"], "partner-list", e["url"], e["confidence"], e["note"],
                 e["basis"], e["reason"]))
    db.add_tag(con, cid, "payer", e["payer"], e["url"])
    return "붙임"


def run(con, dry_run=False) -> list[dict]:
    rows = clab(con) + kb(con)
    log = []
    for e in rows:
        cid = db.find_company(con, e["name"])  # 새 기업은 만들지 않음
        if cid is None:
            log.append({**e, "id": None, "result": "DB 에 없음 (추가하지 않음)"})
            continue
        ll = con.execute("SELECT hard_filter='통과' AND scope='포함' FROM companies WHERE id=?", (cid,)).fetchone()[0]
        res = "dry-run" if dry_run else _attach(con, cid, e)
        log.append({**e, "id": cid, "longlist": bool(ll), "result": res})
    if not dry_run:
        con.commit()
    n = sum(1 for x in log if x["result"] == "붙임")
    print(f"C-Lab·KB 명단 {len(rows)}곳 중 DB 기업 {sum(1 for x in log if x['id'])}곳, 지불자 근거 붙임 {n}곳"
          + (" (dry-run)" if dry_run else ""))
    return log
