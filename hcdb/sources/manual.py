"""수기 입력 가져오기 (CSV / XLSX). LinkedIn·원티드·IR 자료처럼 자동 수집이 곤란한 곳의 정보는 이 경로로.

열 이름은 엑셀 내보내기의 '기업' 시트 라벨과 같음 (fields.py). 모르는 열은 무시.
특수 열:
  출처 URL           → 그 행 전체의 근거 URL
  신뢰도             → 상/중/하 (없으면 '중'). source='manual' 이라 자동 수집값보다 항상 우선
  고객, 지불 주체, 키워드(과제) …  → 쉼표 구분 다중값
"""
import csv
from pathlib import Path

from openpyxl import load_workbook

from .. import db
from ..fields import LABEL_TO_FIELD, LABEL_TO_TAG


def _rows(path: str, sheet: str | None):
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        wb = load_workbook(p, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(it)]
        for r in it:
            yield dict(zip(header, r))
    else:
        with open(p, encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)


def run(con, path: str, sheet: str | None = None) -> None:
    with db.run_log(con, "manual", path) as st:
        for row in _rows(path, sheet):
            row = {k: (str(v).strip() if v is not None else "") for k, v in row.items() if k}
            name, biz_no = row.get("사명"), row.get("사업자등록번호")
            if not name and not biz_no:
                continue
            cid = None
            if row.get("기업 ID", "").isdigit():
                cid = int(row["기업 ID"])
            cid = cid or db.get_or_create_company(con, name or biz_no, biz_no, row.get("출처 URL"))
            src, conf = row.get("출처 URL") or path, row.get("신뢰도") or "중"
            vals = {LABEL_TO_FIELD[k]: v for k, v in row.items() if k in LABEL_TO_FIELD and v}
            st["facts"] += db.set_fields(con, cid, vals, "manual", src, conf)
            for k, v in row.items():
                if k in LABEL_TO_TAG and v:
                    for t in v.split(","):
                        db.add_tag(con, cid, LABEL_TO_TAG[k], t, src)
            for alias in (row.get("이형 표기") or "").split("|"):
                db.add_alias(con, cid, alias.strip(), src)
            st["docs"] += 1
        con.commit()
    print(f"{st['docs']}행, 칸 {st['facts']}개 반영")
