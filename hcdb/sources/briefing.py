"""정책브리핑(korea.kr) 보도자료 → 첨부(hwpx/hwp/pdf)의 선정 명단 표 → 기업 + 프로그램 태그.

모든 부처 보도자료가 한 곳에 모여 있어서, '아기유니콘 50개사 선정' 같은 명단을 부처 사이트를 따로 돌지 않고 얻는다.
명단은 LLM 없이 표 규칙으로 뽑는다 (첨부 텍스트의 표 행은 ' | ' 로 이어져 있음):
  1. '기업명/업체명/회사명/팀명' 칸이 있는 머리행을 찾고, 같은 머리가 좌우로 반복되면(2단 표) 묶음으로 나눈다
  2. 병합 셀 때문에 칸이 빠진 행은 앞 행의 분야를 이어받는다
  3. 헬스케어(분야 칸이나 행 전체에 의료·바이오 키워드)인 행만 새 기업으로 만들고,
     나머지는 이미 DB 에 있는 기업일 때만 프로그램 태그를 붙인다 ('아기유니콘 선정'은 그 자체로 신호라서)
"""
import re

from bs4 import BeautifulSoup

from .. import attach, db
from ..fetch import http_get
from .univ import is_health

LIST_URL = "https://www.korea.kr/briefing/pressReleaseList.do"
BASE = "https://www.korea.kr"

QUERIES = ["아기유니콘", "예비유니콘", "초격차 스타트업", "디지털헬스케어 선정", "바이오헬스 선정", "의료기기 선정 기업",
           "헬스케어 스타트업 선정", "K-바이오 랩허브", "보건산업 창업", "창업도약패키지 선정", "혁신기업 국가대표",
           "딥테크 챌린지", "규제샌드박스 실증특례 의료"]
# 제목이 '선정 결과'인 것만 (모집 공고·계획 발표는 명단이 없음)
_RESULT = re.compile(r"선정|최종|지정|승인|확정|명단|선발|수상|시상")
_SKIP = re.compile(r"모집|공고|신청|접수|설명회|계획\b|추진계획|협찬|홍보 협력|국방|간담회")
_PROGRAMS = [  # (제목 패턴, 프로그램 이름)
    (r"아기\s*(유니콘|거대신생기업)", "아기유니콘"), (r"예비\s*(유니콘|거대신생기업)", "예비유니콘"),
    (r"초격차", "초격차 스타트업 1000+"), (r"혁신기업\s*국가대표", "혁신기업 국가대표 1000"),
    (r"딥테크\s*챌린지", "딥테크 챌린지"), (r"랩\s*허브", "K-바이오 랩허브"), (r"창업도약", "창업도약패키지"),
    (r"규제\s*샌드박스|실증특례", "규제샌드박스"), (r"혁신의료기기", "혁신의료기기"),
]
_NAME_HEAD = re.compile(r"^(기업|업체|회사|팀|기관|신청기업|참여기업|선정기업)\s*명?$|^기업명|^업체명|^회사명|^팀명")
_CEO_HEAD = re.compile(r"대표")
_CAT_HEAD = re.compile(r"분야|업종|기술|부문|산업|품목|주요\s*제품|사업\s*내용|아이템")
_NUM = re.compile(r"^\d{1,4}$")
_INST = re.compile(r"병원$|의료원$|대학교$|대학$|산학협력단|재단$|진흥원$|공단$|연구원$|센터$|협회$|^(국립|한국).*(원|공사)$")


def search(query: str, start: str = "2019-01-01", max_pages: int = 5) -> list[dict]:
    from datetime import date
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = http_get(LIST_URL, params={"srchWord": query, "startDate": start, "endDate": f"{date.today()}",
                                       "pageIndex": page})
        r.encoding = "utf-8"
        new = 0
        for li in BeautifulSoup(r.text, "html.parser").select(".list_type li"):
            a = li.select_one("a[href*='pressReleaseView']")
            if not a:
                continue
            nid = re.search(r"newsId=(\d+)", a["href"]).group(1)
            if nid in seen:
                continue
            seen.add(nid)
            new += 1
            title = (li.select_one("strong") or a).get_text(" ", strip=True)
            src = li.select_one(".source")
            spans = [s.get_text(strip=True) for s in src.select("span")] if src else []
            out.append({"title": re.sub(r"\s+", " ", title), "url": f"{BASE}/briefing/pressReleaseView.do?newsId={nid}",
                        "date": spans[0] if spans else None, "dept": spans[1] if len(spans) > 1 else None})
        if not new:
            break
    return out


def program_of(title: str) -> str:
    for pat, name in _PROGRAMS:
        if re.search(pat, title):
            return name
    title = re.sub(r"^\s*[\[(][^\])]*[\])]\s*", "", title)  # [보도자료] 같은 머리말
    return re.sub(r"[「」『』｢｣\"'’‘“”]", "", title).strip()[:40]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split(" | ")]


def parse_roster(text: str) -> list[dict]:
    """첨부 텍스트에서 명단 표 행 → [{name, ceo, category, row}]."""
    rows, lines = [], text.split("\n")
    i = 0
    while i < len(lines):
        head = _cells(lines[i]) if " | " in lines[i] else []
        name_idx = [k for k, c in enumerate(head) if len(c) <= 15 and _NAME_HEAD.search(c)]
        if not name_idx or len(head) > 30:
            i += 1
            continue
        # 2단 표: 머리가 n 번 반복 → 묶음 크기 = 첫 반복 시작 간격
        size = (name_idx[1] - name_idx[0]) if len(name_idx) > 1 else len(head)
        if name_idx[0] >= size:
            size = len(head)
        first = head[:size]
        ni = name_idx[0]
        ci = next((k for k, c in enumerate(first) if _CEO_HEAD.search(c)), None)
        ki = next((k for k, c in enumerate(first) if _CAT_HEAD.search(c)), None)
        numbered = bool(re.search(r"연번|번호|순번|^no", first[0], re.I))
        carry: dict[int, str] = {}
        i += 1
        while i < len(lines) and " | " in lines[i]:
            cells = _cells(lines[i])
            if numbered:  # 연번 숫자가 나오는 자리마다 새 묶음
                starts = [k for k, c in enumerate(cells) if _NUM.match(c) and (k == 0 or k >= ni + 1)]
                groups = [cells[s:e] for s, e in zip(starts, starts[1:] + [len(cells)])]
            else:
                groups = [cells[s:s + size] for s in range(0, len(cells), size)]
            for g_no, g in enumerate(groups):
                if len(g) <= ni:
                    continue
                name = g[ni]
                cat = g[ki] if ki is not None and ki < len(g) and g[ki] else carry.get(g_no, "")
                if ki is not None and ki < len(g) and g[ki]:
                    carry[g_no] = g[ki]
                name, eng = split_name(name)
                if not _valid_name(name):
                    continue
                ceo = g[ci] if ci is not None and ci < len(g) else None
                if ceo and (len(ceo) > 12 or _NUM.match(ceo)):
                    ceo = None
                rows.append({"name": name, "eng": eng, "ceo": ceo, "category": cat, "row": " | ".join(g)})
            i += 1
    uniq = {}
    for r in rows:
        uniq.setdefault(r["name"], r)
    return list(uniq.values())


_LABEL = re.compile(r"^[가-힣A-Za-z·\s]+\(\d+\)$|^(바이오\s*·?\s*헬스|소재\s*·\s*부품|디지털\s*헬스(케어)?|Digital Health|헬스케어|"
                    r"의료기기|바이오|기타|합계|소계)$", re.I)


def split_name(s: str) -> tuple[str, str | None]:
    """'㈜메디웨일 (Mediwhale)' → ('㈜메디웨일', 'Mediwhale')"""
    m = re.match(r"^(.+?)\s*\(([A-Za-z][A-Za-z0-9&.,\s'-]*)\)\s*$", s.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # '주식회사 라피끄 (이범주, 78년)' 처럼 대표·부가정보가 괄호로 붙은 경우는 떼어낸다
    return re.sub(r"\s+\([^)]*\)\s*$", "", s.strip()), None


def _valid_name(s: str) -> bool:
    s = s.strip()
    return (not _LABEL.search(s) and s.count(" ") < 3 and 2 <= len(s) <= 30 and not _NUM.match(s) and not _NAME_HEAD.search(s) and "합계" not in s
            and not re.search(r"\s등$|^\(|^-$|^※|외\s*\d+|분야|(제조|개발|판매|공급)$|[‘’']\d\d\.", s) and not _INST.search(s))


def _attachment_texts(url: str) -> list[tuple[str, str]]:
    r = http_get(url)
    r.encoding = "utf-8"
    atts = attach.find_attachments(url, r.text)
    # 한글 원본을 먼저, PDF 는 한글 첨부가 없을 때만 (같은 내용이라 둘 다 읽으면 두 번 센다)
    hwp = [a for a in atts if re.search(r"\.hwpx?$", a[0], re.I)]
    out = []
    for name, link in hwp or [a for a in atts if a[0].lower().endswith(".pdf")]:
        try:
            path, fname = attach.download(link, referer=url)
            out.append((fname, attach.extract_text(path)))
        except Exception as e:  # noqa: BLE001
            print(f"  ! 첨부 실패 {name}: {e}")
    return out


def run(con, queries: list[str] | None = None, start: str = "2019-01-01", dry_run=False) -> None:
    done = {r[0] for r in con.execute("SELECT url FROM raw_documents WHERE source='briefing'")}
    posts, seen = [], set()
    for q in queries or QUERIES:
        for p in search(q, start):
            if p["url"] in seen or p["url"] in done or not _RESULT.search(p["title"]) or _SKIP.search(p["title"]):
                continue
            seen.add(p["url"])
            posts.append(p)
    print(f"선정 결과 보도자료 {len(posts)}건 (이미 처리한 {len(done)}건 제외)")
    with db.run_log(con, "briefing", repr(queries or "default")) as st:
        for p in posts:
            prog = program_of(p["title"])
            year = int(p["date"][:4]) if p.get("date") else None
            rows = []
            for fname, text in _attachment_texts(p["url"]):
                rows += parse_roster(text)
            health = [r for r in rows if is_health(r["category"], r["row"])]
            print(f"- {p['date']} {p['title'][:50]} → 명단 {len(rows)} / 헬스케어 {len(health)} [{prog}]")
            if dry_run:
                for r in health:
                    print("     ", r["name"], "|", r["category"][:30])
                continue
            tagged = 0
            for r in rows:
                h = r in health
                cid = db.get_or_create_company(con, r["name"], source_url=p["url"], create=h)
                if cid is None:
                    continue
                db.add_alias(con, cid, r.get("eng"), p["url"])
                if h and r["ceo"] and "*" not in r["ceo"]:
                    db.set_field(con, cid, "ceo", r["ceo"], "briefing", p["url"], "중")
                if h and r["category"]:
                    db.add_tag(con, cid, "kw_means", r["category"][:30], p["url"])
                db.add_tag(con, cid, "program", f"{prog} {year or ''}".strip(), p["url"])
                if prog in ("규제샌드박스", "혁신의료기기"):
                    db.upsert_row(con, "regulatory", {
                        "company_id": cid, "product": r["category"] or None, "kind": prog,
                        "status": "승인" if prog == "규제샌드박스" else "지정",
                        "event_date": p.get("date"), "source_url": p["url"]})
                else:
                    db.upsert_row(con, "grants", {"company_id": cid, "program": prog, "year": year,
                                                  "operator": p.get("dept"), "source_url": p["url"], "confidence": "중"})
                tagged += 1
            # 처리 표시 (다음 실행 때 건너뜀)
            con.execute("INSERT OR IGNORE INTO raw_documents(url, source, title, fetched_at) VALUES (?,?,?,datetime('now'))",
                        (p["url"], "briefing", p["title"]))
            st["docs"] += 1
            st["facts"] += tagged
            con.commit()
    print(f"보도자료 {st['docs']}건, 기업 연결 {st['facts']}건")
