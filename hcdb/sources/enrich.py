"""설립일 등 빈칸 보강: Claude 웹 검색으로 설립일·대표·홈페이지·시도를 찾는다 (ANTHROPIC_API_KEY).

대상: 설립일이 비어 있는 법인(예비창업팀 제외). 출처는 'web-llm', 신뢰도 '중'.
DART(상)·수기(manual) 값은 덮어쓰지 않는다(db.set_field 우선순위 규칙).
API 호출은 동시에 몇 건씩 보내고, DB 쓰기는 메인 스레드에서 한다.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import db, llm
from ..normalize import norm_date


def _hint(con, cid: int) -> str:
    r = con.execute("""SELECT c.service_summary, c.bm_summary,
                         (SELECT group_concat(product, '; ') FROM regulatory g WHERE g.company_id=c.id) AS products
                       FROM companies c WHERE c.id=?""", (cid,)).fetchone()
    return " / ".join(x for x in (r["service_summary"], r["products"], r["bm_summary"]) if x)[:300]


def run(con, limit: int | None = None, workers: int = 4) -> None:
    targets = con.execute("SELECT id, name FROM companies WHERE founded_date IS NULL AND is_pre_startup=0 "
                          "ORDER BY id").fetchall()[:limit]
    jobs = [(r["id"], r["name"], _hint(con, r["id"])) for r in targets]
    print(f"설립일 빈 기업 {len(jobs)}곳 조회")
    with db.run_log(con, "enrich-web", f"n={len(jobs)}") as st, ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(llm.find_company_facts, name, hint): (cid, name) for cid, name, hint in jobs}
        for f in as_completed(futs):
            cid, name = futs[f]
            st["docs"] += 1
            try:
                facts = f.result()
            except Exception as e:  # noqa: BLE001 — 한 곳 실패해도 계속
                print(f"  ! {name}: {e}")
                continue
            if not facts or not facts.found or not facts.founded_date:
                print(f"  - {name}: 확인 못 함")
                continue
            src = facts.source_url or None
            # 연도만 확인된 경우는 신뢰도를 낮춘다
            conf = "중" if len(facts.founded_date) >= 7 else "하"
            st["facts"] += db.set_fields(con, cid, {
                "founded_date": norm_date(facts.founded_date), "url": facts.url,
                "sido": facts.sido, "ceo": facts.ceo if "," not in facts.ceo else None,
            }, "web-llm", src, conf)
            print(f"  + {name}: {facts.founded_date} ({src})")
            con.commit()
    print(f"{st['docs']}곳 조회, 칸 {st['facts']}개 반영")
