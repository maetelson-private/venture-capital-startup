"""정부·기관 게시판 자동 순회 → 선정·지정·승인 공고만 골라 notice 로 넘긴다.

게시판마다 검색 파라미터가 제각각이라 검색 대신 '최근 N페이지 목록 + 제목 규칙' 방식으로 찾는다.
이미 처리한 게시글(raw_documents 에 source='notice' 로 남은 URL)은 건너뛴다.

새 게시판 추가: BOARDS 에 한 줄. link 는 게시글 href 에 들어 있는 고유 패턴(정규식).
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import db
from ..fetch import Blocked, http_get
from . import notice

BOARDS = [
    {"name": "식약처 보도자료", "url": "https://www.mfds.go.kr/brd/m_99/list.do", "page": "page",
     "link": r"view\.do\?seq=\d+",
     "search": {"param": "srchWord", "extra": {"srchTp": "0"}, "keywords": ["혁신의료기기"]}},
    {"name": "식약처 공지", "url": "https://www.mfds.go.kr/brd/m_74/list.do", "page": "page",
     "link": r"view\.do\?seq=\d+"},
    {"name": "과기정통부 보도자료", "url": "https://www.msit.go.kr/bbs/list.do", "page": "pageIndex",
     "params": {"sCode": "user", "mId": "307", "mPid": "208", "bbsSeqNo": "94"},
     "link": r"/bbs/view\.do\?.*nttSeqNo=\d+"},
    {"name": "산업부 보도자료", "url": "https://www.motie.go.kr/kor/article/ATCL3f49a5a8c", "page": "pageIndex",
     "link": r"/kor/article/ATCL3f49a5a8c/\d+/view",
     "search": {"param": "searchKeyword", "extra": {"searchCondition": "1"}, "keywords": ["규제특례", "샌드박스"]}},
]

# (제목 정규식, kind, program) — 위에서부터 처음 맞는 규칙 적용
RULES = [
    (r"실험실\s*창업|창업\s*탐색", "program", "실험실창업탐색팀"),
    (r"학생\s*창업\s*유망", "program", "학생창업유망팀"),
    (r"혁신\s*창업\s*멤버스|보건산업\s*혁신\s*창업", "program", "보건산업혁신창업센터 혁신창업멤버스"),
    (r"혁신\s*의료\s*기기.*지정|지정.*혁신\s*의료\s*기기", "innov", None),
    (r"(규제\s*특례|실증\s*특례|샌드박스).*(승인|심의)|(승인|심의).*(규제\s*특례|샌드박스)", "sandbox", None),
    (r"예비\s*창업\s*패키지", "program", "예비창업패키지"),
    (r"초기\s*창업\s*패키지", "program", "초기창업패키지"),
    (r"청년\s*창업\s*사관학교", "program", "청년창업사관학교"),
    (r"팁스|TIPS", "program", "TIPS"),
    (r"데모\s*데이|오픈\s*이노베이션", "program", "데모데이·오픈이노베이션"),
]
# 선정 결과가 아닌 '모집' 공고는 명단이 없으므로 제외
_RESULT = re.compile(r"선정|지정|승인|결과|발대식|출범|합격|최종")
_SKIP = re.compile(r"모집\s*(공고|안내)|신청\s*안내|설명회|채용|입찰|제도\s*개선|사례\s*공유|한자리|책임진다|신속처리|투자유치\s*나선다")


def classify(title: str) -> tuple[str, str | None] | None:
    if _SKIP.search(title) or not _RESULT.search(title):
        return None
    for pat, kind, program in RULES:
        if re.search(pat, title, re.I):
            return kind, program
    return None


def list_posts(board: dict, pages: int, keyword: str | None = None) -> list[tuple[str, str]]:
    out = []
    for p in range(1, pages + 1):
        params = {**board.get("params", {}), board["page"]: p}
        if keyword:
            sc = board["search"]
            params.update({**sc.get("extra", {}), sc["param"]: keyword})
        try:
            r = http_get(board["url"], params=params)
        except Blocked as e:
            print(f"  ! {e}")
            break
        r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if re.search(board["link"], a["href"]):
                title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                out.append((title, urljoin(r.url, a["href"])))
    return out


def run(con, pages: int = 3, only: list[str] | None = None, dry_run=False) -> None:
    found = []
    for b in BOARDS:
        if only and not any(o in b["name"] for o in only):
            continue
        posts = list_posts(b, pages)
        # 검색을 지원하는 게시판은 키워드 검색 결과도 훑어 과거 공고까지 잡는다
        for kw in b.get("search", {}).get("keywords", []):
            posts += list_posts(b, pages, kw)
        posts = list(dict(((u, t) for t, u in posts)).items())
        posts = [(t, u) for u, t in posts]
        hits = [(t, u, *c) for t, u in posts if (c := classify(t))]
        print(f"[{b['name']}] 게시글 {len(posts)}건 중 선정·지정·승인 {len(hits)}건")
        for t, u, kind, program in hits:
            done = con.execute("SELECT 1 FROM raw_documents WHERE url=? AND source='notice'", (u,)).fetchone()
            print(f"   {'(처리됨) ' if done else ''}{kind:7} {program or '':14} {t[:60]}")
            if not done:
                found.append((t, u, kind, program))
    if dry_run or not found:
        return
    with db.run_log(con, "boards", f"pages={pages}") as st:
        for t, u, kind, program in found:
            year = int(m.group(0)) if (m := re.search(r"20\d\d", t)) else None
            print(f"\n▶ {t}")
            try:
                notice.run(con, u, kind, program, year)
            except SystemExit as e:  # notice 가 키 없음 등으로 멈추면 전체를 멈춤
                raise e
            except Exception as e:  # noqa: BLE001 — 한 건 실패해도 다음 공고 계속
                print(f"  ! 처리 실패: {e}")
                continue
            st["docs"] += 1
