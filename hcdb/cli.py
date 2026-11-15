"""python -m hcdb <명령>"""
import argparse

from . import db, export, filters
from .sources import dart, kipris, lab, manual, mfds, news, notice, nps, nts


def main(argv=None):
    p = argparse.ArgumentParser(prog="hcdb", description="디지털 헬스케어 스타트업 DB")
    p.add_argument("--db", help="DB 파일 경로 (기본 data/startups.db)")
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("init", help="DB 생성/스키마 갱신")

    s = sp.add_parser("add", help="기업 한 곳 수기 추가")
    s.add_argument("name")
    s.add_argument("--biz-no")
    s.add_argument("--url")
    s.add_argument("--pre", action="store_true", help="예비창업팀")

    s = sp.add_parser("news", help="뉴스 검색 → 기사 수집 → 추출")
    s.add_argument("-q", "--query", action="append", help="검색어 (여러 번 지정 가능)")
    s.add_argument("--pages", type=int, default=1)
    s.add_argument("--limit", type=int)

    s = sp.add_parser("feeds", help="업계 언론 RSS (벤처스퀘어·와우테일·히트뉴스 등) → 헬스케어 투자·선정 기사 추출")
    s.add_argument("names", nargs="*", help="특정 매체만 (생략 시 전체)")

    s = sp.add_parser("article", help="기사 URL 직접 넣어 추출")
    s.add_argument("urls", nargs="+")

    sp.add_parser("nts", help="국세청 영업상태 갱신 (DATA_GO_KR_KEY)")
    s = sp.add_parser("dart", help="DART 기업개황·결산 (DART_KEY)")
    s.add_argument("--years", type=int, default=3)
    sp.add_parser("kipris", help="KIPRIS 특허 (KIPRIS_KEY)")

    sp.add_parser("mfds", help="식약처 혁신의료기기 지정 현황 (DATA_GO_KR_KEY + 활용신청)")

    s = sp.add_parser("notice", help="공고·보도자료 URL/파일 → 첨부 명단 추출 (ANTHROPIC_API_KEY)")
    s.add_argument("src", help="게시글 URL 또는 로컬 HWP/HWPX/PDF/XLSX 파일")
    s.add_argument("--kind", choices=notice.KINDS, default="program")
    s.add_argument("--program", help="프로그램명 (예: 실험실창업탐색팀)")
    s.add_argument("--year", type=int)
    s.add_argument("--operator", help="주관·운영 기관")
    s.add_argument("--all", action="store_true", help="헬스케어 아닌 팀도 저장")
    s.add_argument("--dry-run", action="store_true")

    s = sp.add_parser("nps", help="국민연금 가입사업장 CSV 가져오기")
    s.add_argument("csv")

    s = sp.add_parser("labs", help="연구실·프로그램 페이지 수집")
    s.add_argument("--config", default="config/labs.json")
    s.add_argument("--dry-run", action="store_true", help="DB 에 넣지 않고 추출 결과만 출력")

    s = sp.add_parser("import", help="CSV/XLSX 수기 데이터 가져오기")
    s.add_argument("file")
    s.add_argument("--sheet")

    sp.add_parser("filter", help="하드 필터 재판정")

    s = sp.add_parser("export", help="엑셀로 내보내기")
    s.add_argument("-o", "--out")
    s.add_argument("--template", action="store_true", help="빈 입력 양식만")

    s = sp.add_parser("merge", help="중복 기업 병합")
    s.add_argument("keep_id", type=int)
    s.add_argument("drop_id", type=int)

    sp.add_parser("stats", help="현황 요약")

    a = p.parse_args(argv)
    con = db.connect(a.db)
    db.init(con)  # 매번 호출해도 안전 (IF NOT EXISTS)

    match a.cmd:
        case "init":
            print("DB 준비 완료")
        case "add":
            cid = db.get_or_create_company(con, a.name, a.biz_no)
            db.set_fields(con, cid, {"url": a.url, "is_pre_startup": 1 if a.pre else None}, "manual")
            con.commit()
            print(f"기업 ID {cid}")
        case "news":
            news.run(con, a.query, a.pages, a.limit)
        case "feeds":
            news.run_feeds(con, a.names or None)
        case "article":
            news.run_urls(con, a.urls)
        case "nts":
            nts.run(con)
        case "dart":
            dart.run(con, a.years)
        case "kipris":
            kipris.run(con)
        case "nps":
            nps.run(con, a.csv)
        case "mfds":
            mfds.run(con)
        case "notice":
            notice.run(con, a.src, a.kind, a.program, a.year, a.operator, a.all, a.dry_run)
        case "labs":
            lab.run(con, a.config, a.dry_run)
        case "import":
            manual.run(con, a.file, a.sheet)
        case "filter":
            filters.run(con)
        case "export":
            export.run(con, a.out, a.template)
        case "merge":
            db.merge_companies(con, a.keep_id, a.drop_id)
            con.commit()
            print("병합 완료")
        case "stats":
            q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
            print(f"기업 {q('SELECT count(*) FROM companies')} (예비 {q('SELECT count(*) FROM companies WHERE is_pre_startup=1')})")
            for r in con.execute("SELECT coalesce(hard_filter,'미판정') f, count(*) n FROM companies GROUP BY 1"):
                print(f"  하드필터 {r['f']}: {r['n']}")
            print(f"라운드 {q('SELECT count(*) FROM funding_rounds')} / 투자사 {q('SELECT count(*) FROM investors')}"
                  f" / 특허 {q('SELECT count(*) FROM patents')} / 원문 {q('SELECT count(*) FROM raw_documents')}")
            for r in con.execute("SELECT source, max(started_at) t, sum(n_facts) n FROM crawl_runs GROUP BY source"):
                print(f"  {r['source']}: 마지막 {r['t']}, 누적 fact {r['n']}")


if __name__ == "__main__":
    main()
