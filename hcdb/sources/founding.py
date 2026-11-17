"""설립일 보정 (롱리스트의 업력 확인용). 새 원천을 붙이지 않고, 이미 받아 둔 파일·DB 값만 쓴다.

1. 미래 설립일 정리: 설립일 칸에 예정일이 들어간 예비창업자 → is_pre_startup=1, expected_founding 으로 옮기고 설립일 비움
2. 국민연금 가입사업장(data/opendata/15083277.csv, 이미 받아 둔 파일) 최초 적용일자로 설립일 근사
   - 사명 정규화 일치 → 없으면 표기 변형(variant_key) 일치
   - 사업자번호가 있으면 국민연금 파일의 사업자번호 앞 6자리가 같은 행만
   - 서로 다른 날짜가 둘 이상 남으면 동명 회사일 수 있어 채우지 않음
   - 신뢰도 하 (적용일자는 첫 직원 가입일이라 설립일보다 늦을 수 있음)
3. 병합 때 설립일이 둘로 갈린 곳([설립일 충돌]): 국민연금 적용일자 이전이면서 가장 가까운 후보를 채택.
   국민연금에서 못 찾으면 비워 둔다.
국세청: 상태조회 API 는 영업상태만 주고 설립(개업)일은 주지 않는다. 진위확인 API 는 개업일·대표자명을
넣어 맞는지만 확인하므로, 후보 날짜와 사업자번호·대표자가 모두 있을 때만 쓸 수 있다(여기선 쓰지 않음).
남은 곳은 '업력 미확인'(설립일 없음 + 예비창업 아님) — 롱리스트에 남기고 카드 기본 화면에서 숨긴다.
"""
import csv
import re
from collections import defaultdict
from datetime import date

from .. import config, db
from ..normalize import norm_date, norm_name, variant_key

NPS_FILE = config.DATA_DIR / "opendata" / "15083277.csv"
NPS_SRC = "https://www.data.go.kr/data/15083277/fileData.do"


def _nps_index():
    by_n, by_v = defaultdict(set), defaultdict(set)
    with open(NPS_FILE, encoding="cp949", errors="ignore", newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_nm = next(i for i, c in enumerate(h) if "사업장명" in c)
        i_bz = next(i for i, c in enumerate(h) if "사업자등록번호" in c)
        i_ty = next(i for i, c in enumerate(h) if "형태구분" in c)
        i_dt = next(i for i, c in enumerate(h) if c.startswith("적용일자"))
        for r in rd:
            if r[i_ty] != "1":
                continue
            row = (norm_date(r[i_dt]), re.sub(r"\D", "", r[i_bz])[:6])
            k = norm_name(r[i_nm])
            if k:
                by_n[k].add(row)
            v = variant_key(r[i_nm])
            if v:
                by_v[v].add(row)
    return by_n, by_v


def _nps_date(con, by_n, by_v, cid: int, biz_no: str | None) -> str | None:
    aliases = [r[0] for r in con.execute("SELECT alias FROM company_aliases WHERE company_id=?", (cid,))]
    hits = set().union(*[by_n.get(norm_name(a), set()) for a in aliases]) if aliases else set()
    if not hits:
        hits = set().union(*[by_v.get(variant_key(a), set()) for a in aliases if variant_key(a)]) if aliases else set()
    if biz_no:
        hits = {h for h in hits if h[1] == biz_no[:6]}
    elif min((len(norm_name(a)) for a in aliases), default=0) < 3:
        return None  # 두 글자 사명은 전국 파일에 동명 회사가 흔함 — 사업자번호 없이는 채우지 않음
    dates = {d for d, _ in hits if d}
    return dates.pop() if len(dates) == 1 else None


def fix_future(con, dry_run=False) -> list[dict]:
    today = date.today().isoformat()
    out = []
    for r in con.execute("SELECT id, name, founded_date FROM companies WHERE founded_date > ?", (today,)).fetchall():
        out.append({"id": r["id"], "name": r["name"], "was": r["founded_date"]})
        if dry_run:
            continue
        con.execute("UPDATE companies SET founded_date=NULL WHERE id=?", (r["id"],))
        con.execute("UPDATE field_evidence SET applied=0 WHERE company_id=? AND field='founded_date'", (r["id"],))
        db.set_fields(con, r["id"], {"is_pre_startup": 1, "expected_founding": r["founded_date"][:7]}, "rule", None, "중")
    return out


def run(con, dry_run=False) -> dict:
    res = {"future": fix_future(con, dry_run), "nps": [], "conflict": [], "left": 0}
    by_n, by_v = _nps_index()
    rows = con.execute("""SELECT id, name, biz_no, notes FROM companies
                          WHERE founded_date IS NULL AND coalesce(is_pre_startup,0)=0
                            AND hard_filter='통과' AND scope='포함'""").fetchall()
    with db.run_log(con, "founding", "dry-run" if dry_run else "") as st:
        for r in rows:
            d = _nps_date(con, by_n, by_v, r["id"], r["biz_no"])
            m = re.search(r"\[설립일 충돌\] 병합 전 (\S+) / (\S+)", r["notes"] or "")
            if m:
                cands = sorted({m.group(1), m.group(2)})
                pick = max((c for c in cands if d and c <= d), default=None)
                res["conflict"].append({"id": r["id"], "name": r["name"], "candidates": cands, "nps": d, "pick": pick})
                if pick and not dry_run:
                    ev = con.execute("""SELECT source, source_url FROM field_evidence WHERE company_id=? AND field='founded_date'
                                        AND value=? ORDER BY id LIMIT 1""", (r["id"], pick)).fetchone()
                    con.execute("UPDATE companies SET founded_date=? WHERE id=?", (pick, r["id"]))
                    con.execute("""UPDATE field_evidence SET applied=1 WHERE id=(SELECT id FROM field_evidence
                                   WHERE company_id=? AND field='founded_date' AND value=? ORDER BY id LIMIT 1)""",
                                (r["id"], pick))
                    db.set_field(con, r["id"], "notes",
                                 (r["notes"] or "") + f" / 국민연금 적용일자 {d} 이전 후보 {pick} 채택", "rule", NPS_SRC, "하")
                    st["facts"] += 1
                continue
            if d:
                res["nps"].append({"id": r["id"], "name": r["name"], "date": d})
                if not dry_run:
                    st["facts"] += db.set_field(con, r["id"], "founded_date", d, "od:nps-founding", NPS_SRC, "하")
            else:
                res["left"] += 1
        st["docs"] = len(rows)
        if not dry_run:
            con.commit()
    print(f"미래 설립일 → 예비창업 {len(res['future'])}곳 / 국민연금으로 채움 {len(res['nps'])}곳 / "
          f"충돌 {len(res['conflict'])}곳 / 남음(업력 미확인) {res['left']}곳" + (" (dry-run)" if dry_run else ""))
    return res
