"""국민연금 가입 사업장 내역 (공공데이터포털 파일데이터, 월별 CSV) → 월별 인원 스냅샷.

data.go.kr 에서 '국민연금공단_국민연금 가입 사업장 내역' CSV 를 매달 내려받아 넣으면 추이가 쌓인다.
CSV 의 사업자번호는 앞 6자리만 공개 → 앞 6자리 + 정규화 사명 둘 다 맞아야 매칭.
"""
import csv
import re

from .. import db
from ..normalize import norm_name


def _col(header: list[str], *cands: str) -> int:
    for i, h in enumerate(header):
        if any(c in h for c in cands):
            return i
    raise KeyError(f"열을 찾을 수 없음: {cands} / {header}")


def run(con, path: str) -> None:
    targets: dict[tuple[str, str], int] = {}
    for r in con.execute("SELECT c.id, c.biz_no, a.alias_norm FROM companies c "
                         "JOIN company_aliases a ON a.company_id=c.id WHERE c.biz_no IS NOT NULL"):
        targets[(r["biz_no"][:6], r["alias_norm"])] = r["id"]
    # 사업자번호 없는 기업은 사명만으로 (동명 위험 → 신뢰도 하, 근거에만 남김)
    name_only = {r["alias_norm"]: r["company_id"] for r in con.execute(
        "SELECT a.alias_norm, a.company_id FROM company_aliases a JOIN companies c ON c.id=a.company_id "
        "WHERE c.biz_no IS NULL")}

    enc = "cp949"
    try:
        open(path, encoding=enc).readline()
    except UnicodeDecodeError:
        enc = "utf-8-sig"
    with open(path, encoding=enc, newline="") as f, db.run_log(con, "nps", path) as st:
        rd = csv.reader(f)
        h = next(rd)
        i_ym, i_nm = _col(h, "자료생성년월"), _col(h, "사업장명")
        i_bn, i_cnt = _col(h, "사업자등록번호"), _col(h, "가입자수")
        try:
            i_new, i_out = _col(h, "신규취득자수"), _col(h, "상실가입자수")
        except KeyError:
            i_new = i_out = None
        for row in rd:
            st["docs"] += 1
            key = (re.sub(r"\D", "", row[i_bn])[:6], norm_name(row[i_nm]))
            cid, src = targets.get(key), "nps"
            if cid is None and key[1] in name_only:
                cid, src = name_only[key[1]], "nps-name"
            if cid is None:
                continue
            ym = re.sub(r"\D", "", row[i_ym])
            db.upsert_row(con, "headcount_snapshots", {
                "company_id": cid, "month": f"{ym[:4]}-{ym[4:6]}", "headcount": int(row[i_cnt] or 0),
                "new_hires": int(row[i_new] or 0) if i_new is not None else None,
                "leavers": int(row[i_out] or 0) if i_out is not None else None, "source": src,
            })
            if src == "nps":
                db.set_field(con, cid, "headcount", int(row[i_cnt] or 0), "nps", "data.go.kr 국민연금 가입사업장", "상")
            st["facts"] += 1
        con.commit()
    print(f"CSV {st['docs']}행 스캔, 인원 스냅샷 {st['facts']}건")
