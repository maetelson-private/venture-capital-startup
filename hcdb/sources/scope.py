"""범위 판정(포함/제외/보류): Claude 가 회사 정보(서비스 한 줄·제품·프로그램·분야 태그)로 판정.

출처 'llm-scope', 신뢰도 '중'. 사람이 엑셀에서 고친 범위(manual)는 덮어쓰지 않는다.
기본은 아직 판정이 없는 회사만. --redo 면 llm 판정을 다시 한다.
"""
from concurrent.futures import ThreadPoolExecutor

from .. import db, llm

BATCH = 25


def _info(con, cid: int) -> str:
    r = con.execute("""SELECT c.service_summary, c.bm_summary, c.affiliation,
           (SELECT group_concat(product, '; ') FROM regulatory g WHERE g.company_id=c.id) AS products,
           (SELECT group_concat(value, ', ') FROM company_tags t WHERE t.company_id=c.id) AS tags
         FROM companies c WHERE c.id=?""", (cid,)).fetchone()
    parts = [r["service_summary"], r["bm_summary"], r["products"] and f"제품: {r['products']}",
             r["tags"] and f"태그: {r['tags']}", r["affiliation"] and f"소속: {r['affiliation']}"]
    return " / ".join(p for p in parts if p)[:500]


def run(con, redo=False, workers: int = 4) -> None:
    where = "scope IS NULL" if not redo else \
        "id NOT IN (SELECT company_id FROM field_evidence WHERE field='scope' AND applied=1 AND source='manual')"
    rows = [{"id": r["id"], "name": r["name"], "info": _info(con, r["id"])}
            for r in con.execute(f"SELECT id, name FROM companies WHERE {where} ORDER BY id")]
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    print(f"범위 판정 대상 {len(rows)}곳 ({len(batches)}회 호출)")
    names = {r["id"]: r["name"] for r in rows}
    cnt = {"포함": 0, "제외": 0, "보류": 0}
    with db.run_log(con, "scope-llm", f"n={len(rows)}") as st, ThreadPoolExecutor(workers) as ex:
        for res in ex.map(llm.classify_scope, batches):
            for it in res or []:
                if it.id not in names or it.scope not in cnt:
                    continue
                db.set_fields(con, it.id, {"scope": it.scope, "scope_reason": it.reason}, "llm-scope", None, "중")
                cnt[it.scope] += 1
                st["facts"] += 1
            st["docs"] += 1
            con.commit()
    print(cnt)
