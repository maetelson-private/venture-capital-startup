"""기관·협회 디렉토리 → 초기 헬스케어 기업 (2026-11-22 점검: 모두 robots 허용·로그인 없음, docs/source_audit_20261122.md).

  campustown   서울 캠퍼스타운 창업기업 — 사업분야 검색(헬스·의료·바이오 등), 상세에 사업자번호·대표·설립일
  kddf         국가신약개발재단 과제 파이프라인 — 수행기관이 기업인 과제 (질환 × 단계 표)
  koreabio     한국바이오협회 회원사 소식 '[신규회원]' 글 — 최근 3년
  kothea       범부처통합헬스케어협회 회원사 목록
  nextunicorn  넥스트유니콘 '디지털 헬스케어' 테마 — 상세에 설립일·대표·홈페이지 (투자 정보는 대부분 비공개라 쓰지 않음)
"""
import re
from datetime import date

from bs4 import BeautifulSoup

from ..fetch import http_get
from ..normalize import norm_biz_no, norm_date
from .events import finish, is_org


def _soup(url, **kw) -> BeautifulSoup:
    r = http_get(url, **kw)
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "html.parser")


def _t(n) -> str:
    return re.sub(r"\s+", " ", n.get_text(" ", strip=True)).strip() if n else ""


# ── 서울 캠퍼스타운 ──────────────────────────────────────────────────────────
CT = "https://campustown.seoul.go.kr"
CT_KW = ["헬스", "의료", "바이오", "메디", "재활", "돌봄", "디지털치료", "병원"]
_CT_NOT = re.compile(r"반려|펫|애견|화장품|뷰티|코스메틱|식품|음료|농업|스마트팜")


def _ct_detail(comp_id: str) -> dict:
    page = f"{CT}/site/main/startup/intro?compId={comp_id}"
    lines = [l.strip() for l in _soup(page).get_text("\n", strip=True).splitlines() if l.strip()]
    kv = {}
    for k in ("사업자등록번호", "대표자", "소속", "업종", "사업내용", "설립일", "임직원", "주요사업", "구분"):
        if k in lines:
            v = lines[lines.index(k) + 1]
            kv[k] = "" if v in ("사업자등록번호", "대표자", "소속", "업종", "사업내용", "설립일", "임직원", "주요사업",
                               "목록으로", "SNS", "홈페이지", "구분") else v
    return {"page": page, "biz_no": norm_biz_no(kv.get("사업자등록번호")), "ceo": kv.get("대표자") or None,
            "founded_date": norm_date(kv.get("설립일")), "affiliation": kv.get("소속") or None,
            "service_summary": (kv.get("주요사업") or kv.get("사업내용") or "")[:300] or None,
            "pre": kv.get("구분") == "예비창업"}


def run_campustown(con, dry_run=False) -> dict:
    rows = {}
    for kw in CT_KW:
        for cp in range(1, 60):
            s = _soup(f"{CT}/site/main/startup/list", params={"cp": cp, "sc": "bsnsRealm", "sv": kw})
            trs = [tr for tr in s.select("div.tb_style_1 tbody tr") if tr.select_one("a[href*=compId]")]
            if not trs:
                break
            for tr in trs:
                a = tr.select_one("a[href*=compId]")
                tds = tr.select("td")
                cid = re.search(r"compId=(\w+)", a["href"]).group(1)
                realm = _t(tds[1]) if len(tds) > 1 else ""
                if not _CT_NOT.search(realm):
                    rows.setdefault(cid, {"name": _t(a), "realm": realm,
                                          "campus": _t(tds[2]) if len(tds) > 2 else ""})
    print(f"캠퍼스타운 사업분야 검색 {len(rows)}곳 (상세 읽는 중)")
    items = []
    for cid, r in rows.items():
        if is_org(r["name"]) or r["name"].startswith("예비창업"):
            continue
        x = {"name": r["name"], **_ct_detail(cid), "kws": [k.strip() for k in r["realm"].split(",") if k.strip()][:4]}
        x["affiliation"] = x.get("affiliation") or r["campus"]
        items.append(x)
    return finish(con, "dir:campustown", f"{CT}/site/main/startup/list", items,
                  lambda x: f"서울 캠퍼스타운 ({x.get('affiliation') or '입주'})", dry_run, conf="상")


# ── KDDF 국가신약개발사업 과제 ───────────────────────────────────────────────
KDDF = "https://www.kddf.org/ko/nationalbiz/pipelinedetail"
KDDF_DISEASES = ["Oncology", "Cardiovascular", "Metabolic", "Neurology", "Immunology", "Ophthalmology",
                 "Respiratory", "Gastrointestinal", "Others"]
KDDF_LEVELS = ["Discovery", "Preclinical", "Phase1", "Phase2"]


def run_kddf(con, dry_run=False, since_year: int | None = None) -> dict:
    since_year = since_year or date.today().year - 3
    items = []
    for dz in KDDF_DISEASES:
        for lv in KDDF_LEVELS:
            s = _soup(KDDF, params={"type": "pl_level", "pl_disease": dz, "pl_level": lv})
            trs = s.select("table.pl-table tbody tr")
            title = None
            for tr in trs:
                tds = tr.select("td")
                if "pl-task-row" in (tr.get("class") or []) and len(tds) == 2:
                    title = _t(tds[1])
                    continue
                if len(tds) < 6:
                    continue
                org, task_no = _t(tds[4]), _t(tds[5])
                y = re.search(r"RS-(\d{4})", task_no)
                if not org or is_org(org) or (y and int(y.group(1)) < since_year):
                    continue
                items.append({"name": org, "service_summary": f"{title} ({_t(tds[1])}, {_t(tds[2])}, {_t(tds[3])})"[:300],
                              "page": f"{KDDF}?type=pl_level&pl_disease={dz}&pl_level={lv}", "kws": ["신약개발"],
                              "_year": y.group(1) if y else ""})
    print(f"KDDF 과제 중 기업 수행 {since_year}년 이후 {len(items)}건")
    return finish(con, "dir:kddf", "https://www.kddf.org/ko/nationalbiz/pipeline", items,
                  lambda x: f"KDDF 국가신약개발사업 과제 {x['_year']}".strip(), dry_run)


# ── 한국바이오협회 신규회원 ──────────────────────────────────────────────────
KBIO = "https://www.koreabio.org/board/board.php"


def run_koreabio(con, dry_run=False, years: int = 3) -> dict:
    cutoff = f"{date.today().year - years}.01.01"
    items, pg = [], 1
    while pg <= 200:
        s = _soup(KBIO, params={"bo_table": "membernews", "pg": pg})
        posts = s.select("div.title_g")
        if not posts:
            break
        oldest = "9999"
        for p in posts:
            title, d = _t(p.select_one(".title")), _t(p.select_one(".date"))
            oldest = min(oldest, d or "9999")
            m = re.match(r"\[\s*신규\s*회원\s*\]\s*(.+)", title)
            if m and d >= cutoff:
                a = p.find_parent("a")
                items.append({"name": m.group(1).strip(), "page": f"{KBIO}{a['href']}" if a else KBIO,
                              "kws": ["바이오협회 신규회원"], "_date": d})
        if oldest < cutoff:
            break
        pg += 1
    print(f"바이오협회 신규회원 글 {len(items)}건 ({cutoff} 이후)")
    return finish(con, "dir:koreabio", f"{KBIO}?bo_table=membernews", items,
                  lambda x: f"한국바이오협회 신규회원 ({x['_date'][:4]})", dry_run)


# ── 범부처통합헬스케어협회(KoTHEA) 회원사 ───────────────────────────────────────
KOTHEA = "http://kothea.or.kr/memberlist"


def run_kothea(con, dry_run=False) -> dict:
    lines = [l.strip() for l in _soup(KOTHEA).get_text("\n", strip=True).splitlines() if l.strip()]
    items, seen = [], set()
    for a, b in zip(lines, lines[1:]):
        # '회사명' 다음 줄이 '홍길동 대표이사' 처럼 직함으로 끝나면 회원사 한 곳 (대표자 이름은 저장하지 않음)
        if re.search(r"(대표|대표이사|원장|센터장|부원장|이사장|회장)$", b) and a not in seen and not is_org(a):
            seen.add(a)
            items.append({"name": a, "kws": ["KoTHEA 회원사"]})
    print(f"KoTHEA 회원사 중 기업 {len(items)}곳")
    return finish(con, "dir:kothea", KOTHEA, items, "범부처통합헬스케어협회 회원사", dry_run)


# ── 넥스트유니콘 디지털 헬스케어 테마 ───────────────────────────────────────────
NU = "https://www.nextunicorn.kr"


def _nu_detail(path: str) -> dict:
    page = NU + path
    s = _soup(page)
    lines = [l.strip() for l in s.get_text("\n", strip=True).splitlines() if l.strip()]

    def after(k):
        return lines[lines.index(k) + 1] if k in lines else None
    name = re.sub(r"\s*기업정보.*$", "", (s.title.string or "") if s.title else "").strip()
    name = re.sub(r"\(.*?\)$", "", name.split("|")[0]).strip()
    home = None
    if "홈페이지" in lines:
        a = s.find(string="홈페이지")
        link = a.find_next("a", href=re.compile(r"^https?://(?!www\.nextunicorn)")) if a else None
        home = link["href"] if link else None
    intro = lines[lines.index(name) + 1] if name in lines else None
    return {"name": name, "page": page, "founded_date": norm_date(after("기업 설립일")),
            "ceo": (after("대표자 명") or "").split("·")[0].strip() or None, "url": home,
            "service_summary": (intro or "")[:300] or None, "kws": ["디지털헬스케어"]}


def run_nextunicorn(con, dry_run=False) -> dict:
    html = http_get(f"{NU}/finder/theme/digital-healthcare").text
    paths = list(dict.fromkeys(re.findall(r'href="(/company/[0-9a-f]+)"', html)))
    items = []
    for p in paths:
        try:
            x = _nu_detail(p)
        except Exception as e:  # noqa: BLE001 — 지워진 회사 페이지는 not-found 로 넘어감
            print(f"  ! {p}: {e}")
            continue
        if x["name"] and not is_org(x["name"]):
            items.append(x)
    print(f"넥스트유니콘 디지털헬스케어 테마 {len(items)}곳")
    return finish(con, "dir:nextunicorn", f"{NU}/finder/theme/digital-healthcare", items,
                  "넥스트유니콘 디지털헬스케어", dry_run)


SOURCES = {"campustown": run_campustown, "kddf": run_kddf, "koreabio": run_koreabio, "kothea": run_kothea,
           "nextunicorn": run_nextunicorn}


def run(con, only=None, dry_run=False) -> list[dict]:
    out = []
    for k, fn in SOURCES.items():
        if only and k not in only:
            continue
        try:
            out.append(fn(con, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001
            print(f"{k}: 실패 {e!r}")
    return out
