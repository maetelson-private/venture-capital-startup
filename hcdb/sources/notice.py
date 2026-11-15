"""공고·보도자료 명단 수집: 게시글 URL(또는 로컬 파일) → 첨부 다운로드 → 텍스트 → Claude 로 명단 추출 → DB.

종류(kind)에 따라 저장 위치가 달라진다.
  program   지원사업·프로그램 선정 (실험실창업탐색팀, 학생창업유망팀, 혁신창업멤버스, 예비창업패키지, 데모데이…)
            → company_tags(program) + grants(program, year, operator)
  sandbox   규제샌드박스 실증특례 승인과제 → regulatory(kind='규제샌드박스')
  innov     혁신의료기기 지정 (보도자료 첨부) → regulatory(kind='혁신의료기기')

헬스케어가 아닌 행(is_health=false)은 기본으로 버린다. --all 로 모두 넣을 수 있다.
"""
import json
import re
from pathlib import Path

from .. import attach, db, llm
from ..fetch import fetch_doc, http_get
from ..normalize import norm_date

KINDS = ("program", "sandbox", "innov")


def _date_in(s: str | None) -> str | None:
    m = re.search(r"\d{4}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}", s or "")
    return norm_date(m.group(0)) if m else None


def _texts_from_url(con, url: str) -> list[tuple[str, str]]:
    """게시글 본문 + 첨부 각각의 (이름, 텍스트)."""
    out = []
    doc = fetch_doc(con, url, "notice", force=True)
    if doc and doc["body"]:
        out.append(("본문", doc["body"]))
    html = http_get(url).text
    for name, link in attach.find_attachments(url, html):
        try:
            path, fname = attach.download(link, referer=url)
            text = attach.extract_text(path)
        except Exception as e:  # noqa: BLE001 — 첨부 하나 실패해도 나머지는 계속
            print(f"  ! 첨부 실패 {name}: {e}")
            continue
        print(f"  첨부 {fname}: {len(text):,}자")
        out.append((fname, text))
    return out


def run(con, src: str, kind: str, program: str | None = None, year: int | None = None,
        operator: str | None = None, keep_all=False, dry_run=False) -> None:
    if kind not in KINDS:
        raise SystemExit(f"kind 는 {KINDS} 중 하나")
    is_url = src.startswith("http")
    texts = _texts_from_url(con, src) if is_url else [(Path(src).name, attach.extract_text(Path(src)))]
    context = f"{program or kind} {year or ''} ({src})"
    entries = []
    for name, text in texts:
        res = llm.extract_roster(text, f"{context} / {name}")
        if res is None:
            raise SystemExit("ANTHROPIC_API_KEY 가 없어 명단 추출을 할 수 없습니다. 텍스트만 확인하려면 attach.extract_text 사용")
        entries += res
    # 본문과 첨부에 같은 팀이 중복으로 나오는 경우 제거
    uniq = {}
    for e in entries:
        uniq.setdefault((e.name.strip(), (e.ceo or "").strip()), e)
    entries = list(uniq.values())
    # 병원·대학·공공기관은 공동 신청자로 명단에 섞여 나오지만 스타트업이 아니므로 제외
    _inst = re.compile(r"병원$|의료원$|대학교$|대학$|산학협력단|재단$|진흥원$|공단$|연구원$|센터$|협회$|^(국립|한국).*(원|공사)$")
    entries = [e for e in entries if not _inst.search(e.name.strip())]
    health = [e for e in entries if keep_all or e.is_health]
    print(f"명단 {len(entries)}건 중 헬스케어 {len([e for e in entries if e.is_health])}건")
    if dry_run:
        for e in health:
            print("  ", json.dumps(e.model_dump(), ensure_ascii=False))
        return

    url = src if is_url else None
    with db.run_log(con, f"notice:{kind}", src) as st:
        for e in health:
            cid = db.get_or_create_company(con, e.name, source_url=url)
            vals = {"affiliation": e.affiliation, "service_summary": e.item,
                    "is_pre_startup": 1 if e.is_pre_startup else None, "sido": e.region}
            if e.ceo and "*" not in e.ceo:  # 가려진 이름은 저장하지 않음
                vals["ceo"] = e.ceo
            db.set_fields(con, cid, vals, f"notice:{kind}", url, "중")
            if kind == "program":
                name = program or "미상 프로그램"
                db.add_tag(con, cid, "program", name, url)
                db.upsert_row(con, "grants", {"company_id": cid, "program": name, "year": year,
                                              "operator": operator, "source_url": url, "confidence": "중"})
            else:
                db.upsert_row(con, "regulatory", {
                    "company_id": cid, "product": e.item,
                    "kind": "규제샌드박스" if kind == "sandbox" else "혁신의료기기",
                    "status": "승인" if kind == "sandbox" else "지정",
                    "event_date": _date_in(e.note) or (f"{year}-01-01" if year else None),
                    "source_url": url})
            st["facts"] += 1
        st["docs"] = len(texts)
        con.commit()
    print(f"{st['facts']}건 반영")
