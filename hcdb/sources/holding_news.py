"""기술지주 관련 기사 → 자회사·피투자 헬스케어 기업.

웹 목록이 없거나 막힌 기술지주(연세·KAIST·부산·성균관·경북·인하 …)도 '○○대기술지주 자회사 설립',
'KAIST 청년창업투자지주 투자' 같은 기사에는 회사명이 나온다. 두 단계로 나눈다:

  1. collect: 기술지주마다 '자회사' / '투자 헬스케어' 로 뉴스 검색 → 제목·요약을 data/holding_news.json 에 모음
     (네이버 키가 있으면 네이버, 없으면 구글뉴스 RSS)
  2. load: 그 기사에서 뽑은 회사 목록(JSON)을 넣는다. 추출은 사람이 하거나 Claude 로 한다
     — 항목: name, holding, relation(자회사/투자/연구소기업), desc, year, url, scope, reason, one, kws

기사에 나온 일부만 잡히므로 전체 포트폴리오는 아니다.
"""
import json

from .. import config, db
from .news import search_google_rss, search_naver

HOLDINGS = ["연세대기술지주", "연세대 바이오헬스기술지주", "KAIST 청년창업투자지주", "카이스트 청년창업투자지주",
            "부산대기술지주", "성균관대기술지주", "경북대기술지주", "인하대기술지주", "울산대기술지주", "경희대기술지주",
            "아주대기술지주", "동국대기술지주", "가천대기술지주", "한림대기술지주", "충북대기술지주", "전북대기술지주",
            "제주대기술지주", "단국대기술지주", "건국대기술지주", "세종대기술지주", "DGIST기술지주", "서울대기술지주",
            "고려대기술지주", "한양대기술지주", "미래과학기술지주", "한국과학기술지주", "의료기술지주", "병원 기술지주회사",
            "연구소기업 헬스케어 설립"]

# 예비·초기 창업팀이 이름으로 나오는 기사 (선정 명단·수상·데모데이·교원/의사 창업). 검색어 그대로 쓴다
EARLY = ["예비창업패키지 헬스케어", "예비창업패키지 의료", "예비창업패키지 디지털헬스", "초기창업패키지 헬스케어",
         "초기창업패키지 의료기기", "창업도약패키지 헬스케어", "학생창업유망팀 헬스케어", "학생창업유망팀 의료",
         "실험실창업탐색팀 바이오", "실험실 창업 헬스케어", "교원창업 헬스케어", "교수 창업 의료기기", "의사 창업 스타트업",
         "의대생 창업", "전공의 창업 헬스케어", "간호사 창업 스타트업", "약사 창업 헬스케어", "병원 교수 창업 디지털헬스",
         "디지털헬스케어 창업경진대회 수상", "헬스케어 창업경진대회 대상", "의료기기 창업경진대회", "바이오헬스 창업경진대회",
         "헬스케어 해커톤 수상", "헬스케어 데모데이", "디지털헬스 데모데이", "헬스케어 IR 피칭 스타트업",
         "헬스케어 스타트업 시드 투자 유치", "디지털치료제 스타트업 설립", "의료 AI 스타트업 설립", "헬스케어 스타트업 창업 1년",
         "창업중심대학 헬스케어", "청년창업사관학교 헬스케어", "K-스타트업 헬스케어 선정", "도전 K-스타트업 헬스케어",
         "보건산업 창업 지원 선정 스타트업", "KHIDI 창업 스타트업 선정", "디지털헬스케어 실증 지원 선정 스타트업",
         "혁신의료기기 스타트업 지정", "의료기기 스타트업 첫 허가", "헬스케어 엑셀러레이팅 선발"]
SETS = {"holding": ("holding_news.json", None), "early": ("early_news.json", EARLY)}

MIN_NEW = 5


def _retry(fn, tries: int = 4):
    """네이버 API 가 가끔 연결을 끊는다 → 잠깐 쉬었다 다시."""
    import time
    for k in range(tries):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))


def collect(holdings: list[str] | None = None, set_name: str = "holding") -> None:
    naver = False
    if config.NAVER_CLIENT_ID:
        try:
            search_naver("기술지주", display=1)
            naver = True
        except Exception as e:  # noqa: BLE001
            print(f"네이버 검색 실패 → 구글뉴스 RSS 사용 ({str(e)[:60]})")
    fname, plain = SETS[set_name]
    path = config.DATA_DIR / fname
    # 이전에 모은 기사는 유지하고, 새로 나온 것만 'new': true 로 표시 (추출은 새 것만 하면 된다)
    out = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    for x in out:
        x["new"] = False
    seen = {x["title"] for x in out}
    for h in holdings or plain or HOLDINGS:
        for q in ([h] if plain else (f"{h} 자회사", f"{h} 투자 헬스케어")):
            # 네이버는 100건씩 start=1,101,…,901 까지. 한 페이지에서 새 기사가 거의 없으면(MIN_NEW 미만) 그 검색어는 끝
            for start in (range(1, 1000, 100) if naver else [1]):
                items = _retry(lambda: search_naver(q, display=100, start=start, sort="sim") if naver
                               else search_google_rss(q))
                new = 0
                for it in items:
                    if it["title"] in seen:
                        continue
                    seen.add(it["title"])
                    new += 1
                    out.append({"holding": h, "title": it["title"], "summary": (it.get("summary") or "")[:300],
                                "date": it["date"], "url": it["url"], "new": True})
                if new < MIN_NEW or len(items) < 100:
                    break
        print(f"  {h}: 누적 {len(out)}건", flush=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")  # 중간 저장
    path.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"기사 {len(out)}건 (이번에 새로 {sum(x['new'] for x in out)}건) → {path}")


def load(con, path: str) -> None:
    items = json.loads(open(path, encoding="utf-8").read())
    new = 0
    with db.run_log(con, "holding-news", path) as st:
        for x in items:
            if x.get("scope") not in ("포함", "보류") or not x.get("name"):
                continue
            existed = db.find_company(con, x["name"]) is not None
            cid = db.get_or_create_company(con, x["name"], source_url=x.get("url"))
            new += not existed
            db.set_fields(con, cid, {"service_summary": x.get("desc") or None}, "holding-news", x.get("url"), "하")
            if not existed:  # 기존 기업의 범위 판정은 그대로 둔다
                vals = {"scope": x["scope"], "scope_reason": x.get("reason")}
                if x.get("one"):
                    vals["one_liner"] = x["one"]
                db.set_fields(con, cid, vals, "holding-news", x.get("url"), "중")
                con.execute("UPDATE companies SET confidence=coalesce(confidence,'하') WHERE id=?", (cid,))
            prog = x.get("program") or f"{x.get('holding') or '기술지주'} {x.get('relation') or '출자'}"
            db.add_tag(con, cid, "program", f"{prog} (기사)", x.get("url"))
            if x.get("pre"):  # 법인 설립 전 팀
                con.execute("UPDATE companies SET is_pre_startup=1 WHERE id=? AND founded_date IS NULL", (cid,))
            if x.get("affiliation"):
                db.set_field(con, cid, "affiliation", x["affiliation"], "holding-news", x.get("url"), "하")
            for k in x.get("kws") or []:
                db.add_tag(con, cid, "kw", k, x.get("url"))
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영 (신규 {new}곳)")
