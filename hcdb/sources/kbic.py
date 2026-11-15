"""한국보건산업진흥원 보건산업혁신창업센터(KBIC) 창업기업소개 — 약 300개사, 실명, 분야 태그, 상세에 설립일·대표·인원·매출.

목록: /kbic/company/list?pageNum=N (8개씩) → (기업명, 분야, intrdComId)
상세: /kbic/company/view?intrdComId=N → 대표자·주소·설립일자·임직원 수·매출·정부지원사업·회사소개
분야가 '의약품'인 곳은 기본으로 건너뛴다(디지털헬스케어·의료기기만). --all 로 전부.
"""
import re

from bs4 import BeautifulSoup

from .. import db
from ..fetch import http_get
from ..normalize import norm_date, parse_krw

BASE = "https://www.khidi.or.kr/kbic/company"
Q = {"menuKey": "yPgNmjntsr", "siteId": "SITE00002"}
KEEP = ("디지털헬스케어", "의료기기")


def list_companies(max_pages: int = 60) -> list[dict]:
    out, seen = [], set()
    for p in range(1, max_pages + 1):
        r = http_get(f"{BASE}/list", params={**Q, "pageNum": p, "rowCnt": 8})
        r.encoding = "utf-8"
        rows = 0
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            m = re.search(r"intrdComId=(\d+)", a["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            spans = [s.get_text(strip=True) for s in a.find_all(["span", "p", "em", "strong", "div"]) if s.get_text(strip=True)]
            text = a.get_text("|", strip=True).split("|")
            name, field = (text[0], text[-1]) if len(text) >= 2 else (a.get_text(strip=True), "")
            out.append({"id": m.group(1), "name": name, "field": field or (spans[-1] if spans else "")})
            rows += 1
        if rows == 0:
            break
    return out


def _after(lines: list[str], label: str) -> str | None:
    for i, l in enumerate(lines):
        if l == label and i + 1 < len(lines):
            return lines[i + 1]
    return None


def _between(lines: list[str], start: str, stops: tuple[str, ...]) -> list[str]:
    out, on = [], False
    for l in lines:
        if l == start:
            on = True
            continue
        if on and (l in stops):
            break
        if on:
            out.append(l)
    return out


def detail(cid: str) -> dict:
    url = f"{BASE}/view?intrdComId={cid}&menuKey={Q['menuKey']}&siteId={Q['siteId']}"
    r = http_get(url)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    lines = [l for l in (soup.select_one("main") or soup.body).get_text("\n", strip=True).split("\n") if l.strip()]
    addr = _after(lines, "주소") or ""
    parts = addr.split()
    revenue = {}
    # "주요 현황(백만원)" 아래: 자본금, 2022매출, 2023매출, 2024예상매출 … 다음에 같은 개수의 숫자
    if "주요 현황(백만원)" in lines:
        i = lines.index("주요 현황(백만원)") + 1
        heads = []
        while i < len(lines) and not re.fullmatch(r"[\d,.\-]+", lines[i]):
            heads.append(lines[i])
            i += 1
        vals = lines[i:i + len(heads)]
        for h, v in zip(heads, vals):
            y = re.match(r"(20\d\d)매출", h)
            if y and re.fullmatch(r"[\d,]+", v):
                revenue[int(y.group(1))] = int(v.replace(",", "")) * 1_000_000
    hc = _after(lines, "임직원 수") or ""
    intro = " ".join(_between(lines, "회사 소개 및 향후 비전", ("특허 등록 및 출원", "정부지원사업 참여이력", "서비스/제품 소개")))
    return {
        "url": url,
        "ceo": _after(lines, "대표자"),
        "founded_date": norm_date((_after(lines, "설립일자") or "").rstrip(".")),
        "sido": parts[0] if parts else None,
        "sigungu": parts[1] if len(parts) > 1 else None,
        "headcount": int(re.sub(r"\D", "", hc)) if re.search(r"\d", hc) else None,
        "revenue": revenue,
        "grants": _between(lines, "정부지원사업 참여이력", ("서비스/제품 소개", "타 기술과의 차별점", "특허 등록 및 출원")),
        "patents": _between(lines, "특허 등록 및 출원", ("정부지원사업 참여이력", "서비스/제품 소개")),
        "founders": " / ".join(_between(lines, "임원 소개", ("회사 소개", "회사 소개 및 향후 비전")))[:400],
        "summary": intro[:300] or None,
    }


def run(con, keep_all=False) -> None:
    comps = list_companies()
    targets = [c for c in comps if keep_all or any(k in c["field"] for k in KEEP)]
    print(f"KBIC 창업기업 {len(comps)}곳 중 대상 {len(targets)}곳 (분야: {', '.join(KEEP) if not keep_all else '전체'})")
    with db.run_log(con, "kbic") as st:
        for c in targets:
            try:
                d = detail(c["id"])
            except Exception as e:  # noqa: BLE001
                print(f"  ! {c['name']}: {e}")
                continue
            url = d["url"]
            cid = db.get_or_create_company(con, c["name"], source_url=url)
            st["facts"] += db.set_fields(con, cid, {
                "ceo": d["ceo"], "founded_date": d["founded_date"], "sido": d["sido"], "sigungu": d["sigungu"],
                "headcount": d["headcount"], "service_summary": d["summary"],
            }, "kbic", url, "상")
            db.add_tag(con, cid, "program", "KBIC 보건산업혁신창업센터", url)
            if c["field"]:
                db.add_tag(con, cid, "kw_means", c["field"], url)
            for y, rev in d["revenue"].items():
                db.upsert_row(con, "financials", {"company_id": cid, "fiscal_year": y, "revenue_krw": rev,
                                                  "source": "kbic", "source_url": url})
            for g in d["grants"]:
                prog = g.split(" - ")[0].strip()
                if prog:
                    db.upsert_row(con, "grants", {"company_id": cid, "program": "TIPS" if "팁스" in prog or "TIPS" in prog.upper() else prog,
                                                  "source_url": url, "confidence": "상"})
            if d["founders"]:
                con.execute("UPDATE companies SET notes=coalesce(notes||' / ','')||? WHERE id=? AND (notes IS NULL OR notes NOT LIKE '%[임원]%')",
                            ("[임원] " + d["founders"], cid))
            con.execute("UPDATE companies SET confidence='상' WHERE id=?", (cid,))
            st["docs"] += 1
            con.commit()
    print(f"{st['docs']}곳 반영")
