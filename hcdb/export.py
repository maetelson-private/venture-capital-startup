"""엑셀 내보내기. '기업' 시트는 FIRST 기업별 시트에 붙여 넣기 좋은 한 줄 = 한 기업 형태.
내보낸 '기업' 시트를 고쳐서 `hcdb import` 로 다시 넣으면 수정값이 manual 근거로 반영된다."""
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from . import config
from .fields import COMPANY_FIELDS, TAG_FIELDS, VIEW_FIELDS

SHEETS = {
    "투자라운드": """SELECT c.name 사명, r.round 라운드, r.announced_date 일자, r.amount_krw 금액_원,
                    (SELECT group_concat(i.name) FROM round_investors ri JOIN investors i ON i.id=ri.investor_id
                     WHERE ri.round_id=r.id) 투자사, r.confidence 신뢰도, r.source_url 출처
                FROM funding_rounds r JOIN companies c ON c.id=r.company_id ORDER BY c.name, r.announced_date""",
    "투자사": "SELECT name 투자사, type 유형, grp 그룹, notes 메모 FROM investors ORDER BY name",
    "지원금": """SELECT c.name 사명, g.program 사업명, g.year 연도, g.amount_krw 금액_원, g.operator 운영사,
                g.source_url 출처 FROM grants g JOIN companies c ON c.id=g.company_id""",
    "결산": """SELECT c.name 사명, f.fiscal_year 연도, f.revenue_krw 매출, f.op_income_krw 영업이익,
              f.total_equity_krw 자본총계, f.source 원천 FROM financials f JOIN companies c ON c.id=f.company_id""",
    "인원추이": "SELECT name 사명, month 월, headcount 인원, delta 증감 FROM v_headcount_trend ORDER BY name, month",
    "PoC·파트너": """SELECT c.name 사명, p.partner_name 상대, p.relation 관계, p.partner_type 상대유형,
                    p.started_date 시작, p.source_url 출처 FROM partners p JOIN companies c ON c.id=p.company_id""",
    "인허가": """SELECT c.name 사명, g.product 제품, g.kind 구분, g.status 상태, g.device_class 등급,
               g.event_date 일자, g.source_url 출처 FROM regulatory g JOIN companies c ON c.id=g.company_id""",
    "특허": """SELECT c.name 사명, p.application_no 출원번호, p.title 명칭, p.status 상태, p.is_pct PCT,
              p.filed_date 출원일, p.registered_date 등록일 FROM patents p JOIN companies c ON c.id=p.company_id""",
    "수상": """SELECT c.name 사명, w.name 명칭, w.kind 구분, w.event_date 일자, w.source_url 출처
              FROM awards w JOIN companies c ON c.id=w.company_id""",
    "근거": """SELECT c.name 사명, e.field 칸, e.value 값, e.source 원천, e.confidence 신뢰도,
              e.applied 채택, e.collected_at 수집, e.source_url 출처
              FROM field_evidence e JOIN companies c ON c.id=e.company_id ORDER BY c.name, e.field, e.id""",
    "원문": "SELECT id, source 원천, title 제목, published_at 발행, url FROM raw_documents ORDER BY id DESC",
}

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def _write(ws, header, rows):
    ws.append(header)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), HEADER_FILL
    for r in rows:
        ws.append(list(r))
    ws.freeze_panes = "B2"
    for i, h in enumerate(header, 1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(len(str(h)) * 2 + 2, 10), 40)
    ws.auto_filter.ref = ws.dimensions


def run(con, out: str | None = None, template=False) -> Path:
    path = Path(out or config.DATA_DIR / f"startups_{datetime.now():%Y%m%d_%H%M}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "기업"
    # 기업 시트: 기업 ID → 단일값 칸 → 태그 칸 → 계산 칸
    # 고객·지불주체·키워드는 아래 태그 칸에서 kind 별로 따로 보여주므로 VIEW 의 합친 칸은 뺌
    view_cols = [c for c in VIEW_FIELDS if c not in ("id", "customers", "payers", "keywords")]
    cols = ["id"] + list(COMPANY_FIELDS) + view_cols
    header = ["기업 ID"] + list(COMPANY_FIELDS.values()) + [VIEW_FIELDS[c] for c in view_cols]
    # 가져오기용 태그 칸 (키워드는 kind 별로 분리)
    tag_header = [TAG_FIELDS[k] for k in TAG_FIELDS]
    header += tag_header + ["출처 URL"]
    rows = []
    if not template:
        for r in con.execute(f"SELECT {','.join(cols)} FROM v_companies ORDER BY id"):
            tags = {k: [] for k in TAG_FIELDS}
            for t in con.execute("SELECT kind, value FROM company_tags WHERE company_id=?", (r["id"],)):
                tags.setdefault(t["kind"], []).append(t["value"])
            rows.append(list(r) + [", ".join(tags[k]) for k in TAG_FIELDS] + [""])
    _write(ws, header, rows)
    if not template:
        for name, sql in SHEETS.items():
            cur = con.execute(sql)
            _write(wb.create_sheet(name), [d[0] for d in cur.description], cur.fetchall())
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    print(f"저장: {path}")
    return path
