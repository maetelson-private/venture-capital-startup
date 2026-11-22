"""전시회·행사 참가기업 → 초기 헬스케어 기업 (2026-11-22 점검: 모두 robots 허용·로그인 없음, docs/source_audit_20261122.md).

  kimes       KIMES 참가업체 API — '스타트업' 배지(2) 또는 의료IT 분류(9)만. 사업자번호 포함
  kimesbusan  KIMES BUSAN 참가업체 API — 같은 형식, 스타트업 배지 또는 의료IT(1409)
  ghcw        헬스케어위크(벡스코) 참가기업 — Flourish 표의 데이터 (복지·재활, 항노화관)
  reha        REHA 홈케어·재활·복지전시회 참가업체 — 케어테크·로봇·스마트 분류만
  khf         K-HOSPITAL FAIR 특별관(겨자씨키움센터·이노헬스랩·NIPA 디지털헬스 등) '참가기업' 목록
  biokorea    BIO KOREA 참가업체 JSON — 한국 기업 중 Pharma·Medical Device·Digital Health 등

공통: --dry-run 이면 DB 에 쓰지 않고 data/sources_dryrun_<이름>.md 에 '새 회사/기존 회사' 표를 쓴다.
"""
import json
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from .. import config, db
from ..fetch import _throttle, http_get

# 대학·연구소·재단·협회·병원 등 회사가 아닌 참가자
ORG_RE = re.compile(r"대학교|대학$|산학협력단|연구소$|연구원|연구센터|재단|진흥원|협회|학회|병원$|의료원|센터$|공단|"
                    r"\(재\)|\(사\)|재\)|사단법인|재단법인|시청|도청|군청|구청|부산시|사업단|조합$|위원회|평가원|공사$|보험공단|^지원분야$|과학기술원|의학원|기술원$|국립|KIST|원자력")
_CORP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(재\)|\(사\)")


def clean(name: str) -> str:
    return re.sub(r"\s+", " ", _CORP.sub("", name or "")).strip(" ,·-")


def is_org(name: str) -> bool:
    return bool(ORG_RE.search(name or ""))


_GENERIC_HOST = re.compile(r"naver\.com|naver\.me|instagram|facebook|youtube|linktr|notion|wixsite|blogspot|kakao|"
                           r"google\.|tistory|imweb\.me|modoo\.at|smartstore|ncloudstorage|github\.io")


def host(url: str | None) -> str | None:
    if not url:
        return None
    h = re.sub(r"^[a-z]+://", "", url.strip().lower()).split("/")[0].split(":")[0].removeprefix("www.")
    return h if "." in h and not _GENERIC_HOST.search(h) else None


def _host_index(con) -> dict[str, int]:
    idx: dict[str, set] = {}
    for r in con.execute("SELECT id, url FROM companies WHERE url IS NOT NULL AND url != ''"):
        h = host(r["url"])
        if h:
            idx.setdefault(h, set()).add(r["id"])
    return {h: next(iter(ids)) for h, ids in idx.items() if len(ids) == 1}


def match(con, x: dict, hosts: dict[str, int]) -> int | None:
    """사업자번호·사명(이형 포함) → 홈페이지 도메인 순으로 기존 회사를 찾는다. 영문명만 있는 원천(BIO KOREA)용."""
    cid = db.find_company(con, x["name"], x.get("biz_no"))
    if cid is None and host(x.get("url")) in hosts:
        cid = hosts[host(x.get("url"))]
    return cid


def save(con, label: str, src: str, x: dict, program: str, conf="중") -> int:
    """x: name, biz_no?, page?, url?, ceo?, founded_date?, service_summary?, sido?, kws?, pre?, affiliation?"""
    if x.get("_cid"):
        cid = x["_cid"]
        db.add_alias(con, cid, x["name"], x.get("page") or src)
    else:
        cid = db.get_or_create_company(con, x["name"], x.get("biz_no"), x.get("page") or src)
    db.set_fields(con, cid, {k: x.get(k) for k in ("ceo", "url", "founded_date", "service_summary", "sido",
                                                    "affiliation") if x.get(k)},
                  label, x.get("page") or src, conf)
    if x.get("pre"):
        db.set_field(con, cid, "is_pre_startup", 1, label, x.get("page") or src, conf)
    db.add_tag(con, cid, "program", program, src)
    for k in x.get("kws") or []:
        db.add_tag(con, cid, "kw", k, src)
    return cid


def finish(con, label: str, src: str, items: list[dict], program, dry_run: bool, conf="중") -> dict:
    """중복 이름을 합치고, dry-run 이면 표만 쓰고, 아니면 저장. program 은 문자열 또는 item→문자열 함수."""
    seen, uniq = set(), []
    for x in items:
        x["name"] = clean(x["name"])
        key = (x["name"], x.get("biz_no"))
        if x["name"] and key not in seen:
            seen.add(key)
            uniq.append(x)
    hosts = _host_index(con)
    for x in uniq:
        x["_cid"] = match(con, x, hosts)
    new = [x for x in uniq if x["_cid"] is None]
    stats = {"label": label, "total": len(uniq), "new": len(new)}
    if dry_run:
        lines = [f"# {label} dry-run ({date.today()})", "", f"- 대상 {len(uniq)}곳 · DB 에 없는 새 회사 {len(new)}곳", "",
                 "| 새 회사 | 사업자번호 | 설명 | 홈페이지 |", "|---|---|---|---|"]
        lines += [f"| {x['name']} | {x.get('biz_no') or ''} | {(x.get('service_summary') or '')[:60]} | "
                  f"{x.get('url') or ''} |" for x in new]
        (config.DATA_DIR / f"sources_dryrun_{label.replace(':', '_')}.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"[dry-run] {label}: {len(uniq)}곳 중 새 회사 {len(new)}곳")
        return stats
    with db.run_log(con, label, f"n={len(uniq)}") as st:
        for x in uniq:
            save(con, label, src, x, program(x) if callable(program) else program, conf)
            st["facts"] += 1
        con.commit()
    print(f"{label}: {len(uniq)}곳 반영 (새 회사 {len(new)}곳)")
    return stats


# ── KIMES / KIMES BUSAN ───────────────────────────────────────────────────
def _kimes(base: str, year: int) -> list[dict]:
    rows, page = [], 1
    while True:
        _throttle(base)
        r = requests.post(f"{base}/api/web/audience/exhibitor", timeout=30,
                          headers={"User-Agent": config.USER_AGENT, "Content-Type": "application/json"},
                          json={"lang": "kor", "curPage": page, "pageList": 100, "exhiYear": year, "keyword": "",
                                "productMainCategory": None, "productSubCategory": None, "exhiHall": None,
                                "country": None})
        r.raise_for_status()
        js = r.json()
        rows += js.get("list") or []
        if page >= (js.get("paging") or {}).get("totalPage", 1):
            return rows
        page += 1


_SIDO = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)")


def _kimes_items(rows, it_codes: set[str]) -> list[dict]:
    out = []
    for r in rows:
        info, online = r.get("introduceInfo") or {}, r.get("onlineInfo") or {}
        badges, cats = set(r.get("badgeSet") or []), set(r.get("productMainCategorySet") or [])
        startup, it = 2 in badges, bool(cats & it_codes)
        name = info.get("companyNameKor") or info.get("companyName") or ""
        if not (startup or it) or is_org(name):
            continue
        sido = _SIDO.match(online.get("addrDefaultKor") or "")
        out.append({"name": name, "biz_no": (r.get("sn") or "")[:10], "url": online.get("homepage"),
                    "service_summary": (info.get("descKor") or info.get("desc") or "")[:300] or None,
                    "sido": sido.group(1) if sido else None,
                    "kws": ["스타트업관"] if startup else []})
    return out


def run_kimes(con, dry_run=False, year=None) -> dict:
    year = year or date.today().year
    rows = _kimes("https://api2nd.kimes.kr", year)
    items = _kimes_items(rows, {"9"})
    print(f"KIMES {year} 참가업체 {len(rows)}곳 중 스타트업·의료IT {len(items)}곳")
    return finish(con, "ev:kimes", "https://www.kimes.kr/", items, f"KIMES {year} 참가", dry_run)


def run_kimesbusan(con, dry_run=False, year=None) -> dict:
    year = year or date.today().year
    rows = _kimes("https://api.kimesbusan.com", year)
    items = _kimes_items(rows, {"1409"})
    print(f"KIMES BUSAN {year} 참가업체 {len(rows)}곳 중 스타트업·의료IT {len(items)}곳")
    return finish(con, "ev:kimesbusan", "https://kimesbusan.com/", items, f"KIMES BUSAN {year} 참가", dry_run)


# ── 헬스케어위크 (벡스코) — 참가기업 표가 Flourish 임베드 ─────────────────────
GHCW = {2026: "https://flo.uri.sh/visualisation/30035692/embed", 2025: "https://flo.uri.sh/visualisation/20752527/embed"}
GHCW_KEEP = {"복지·재활", "디지털헬스", "의료기기", "헬스케어"}
_GHCW_NOT = re.compile(r"화장품|식품|버섯|골프|건강보조|음료|차\(茶\)|의류|신발")


def run_ghcw(con, dry_run=False) -> dict:
    items = []
    for year, url in GHCW.items():
        html = http_get(url).text
        m = re.search(r"_Flourish_data\s*=\s*(\{.*?\})\s*,\s*\n", html, re.S)
        if not m:
            print(f"  헬스케어위크 {year}: 데이터 못 찾음")
            continue
        for row in json.loads(m.group(1)).get("rows", []):
            c = (row.get("columns") or []) + [""] * 5
            _booth, hall, name, product, home = c[:5]
            if hall not in GHCW_KEEP or not name or is_org(name) or _GHCW_NOT.search(product or ""):
                continue
            items.append({"name": name, "service_summary": product or None, "url": home or None,
                          "kws": [hall], "_year": year})
    print(f"헬스케어위크 참가기업 중 복지·재활 {len(items)}건")
    return finish(con, "ev:ghcw", "https://ghcw.co.kr/sub01/sub03.php", items,
                  lambda x: f"헬스케어위크 {x['_year']} 참가", dry_run)


# ── REHA 홈케어·재활·복지전시회 ─────────────────────────────────────────────
REHA = "https://www.rehahomecare.com/ko/visitor/exhi_list.asp"
REHA_KEEP = re.compile(r"케어테크|로봇|스마트|IoT|AI|디지털", re.I)


def run_reha(con, dry_run=False) -> dict:
    items, page = [], 1
    while page <= 40:
        r = http_get(REHA, params={"page": page})
        r.encoding = r.apparent_encoding
        trs = [t for t in re.split(r"<tr[^>]*>", r.text) if "exhi_listview.asp?idx=" in t]
        if not trs:
            break
        for tr in trs:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            name = BeautifulSoup(tds[1], "html.parser").get_text(" ", strip=True) if len(tds) > 1 else ""
            home = re.search(r'href="(https?://[^"]+)"', tds[3]) if len(tds) > 3 else None
            cat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(re.findall(r"<!--(.*?)-->", tr, re.S)))).strip()
            idx = re.search(r"idx=(\d+)", tr).group(1)
            if REHA_KEEP.search(cat) and name and not is_org(name):
                items.append({"name": name, "url": home.group(1) if home else None, "service_summary": cat or None,
                              "page": f"https://www.rehahomecare.com/ko/visitor/exhi_listview.asp?idx={idx}",
                              "kws": ["케어테크"] if "케어테크" in cat else []})
        page += 1
    print(f"REHA 참가업체 {page - 1}쪽 중 케어테크·로봇·스마트 {len(items)}곳")
    return finish(con, "ev:reha", REHA, items, "REHA 홈케어·재활·복지전시회 참가", dry_run)


# ── K-HOSPITAL FAIR 특별관 ─────────────────────────────────────────────────
KHF_SITEMAP = "https://khospital.org/page-sitemap.xml"
# 병원 안에서 만든 팀(아직 법인 전)이 대부분인 관
KHF_PRE = re.compile(r"granum-sinapis|겨자씨")


def _khf_page(url: str) -> tuple[str, list[tuple[str, str]]]:
    """참가기업 카드: 로고 이미지가 있는 칸 안의 '<strong>회사명</strong>' 한 줄 + 다음 글 상자(설명).
    카드 구조가 아닌 페이지(표·세미나 위주)는 이름·설명이 섞이므로 읽지 않는다."""
    s = BeautifulSoup(http_get(url).content.decode("utf-8", "ignore"), "html.parser")
    title = (s.title.string if s.title else url).split("|")[0].split(" - ")[0].strip()
    pairs = []
    for d in s.select("div.fusion-text"):
        st, t = d.select("strong"), d.get_text(" ", strip=True)
        if len(st) != 1 or st[0].get_text(" ", strip=True) != t or not (1 < len(t) <= 30) or not re.search(r"[가-힣]", t):
            continue
        col = d.find_parent(class_=re.compile("fusion-column-wrapper"))
        if not (col and col.find("img")):
            continue
        desc = ""
        for nxt in d.find_next_siblings("div", class_="fusion-text"):
            desc = nxt.get_text(" ", strip=True)
            if not re.match(r"^(Booth|부스)\s", desc, re.I):
                break
            desc = ""
        pairs.append((t, desc))
    return title, pairs


def run_khf(con, dry_run=False) -> dict:
    locs = re.findall(r"<loc>([^<]+)</loc>", http_get(KHF_SITEMAP).text)
    pages = [u.replace("http://", "https://") for u in locs if "/special-exhibition/" in u
             and u.rstrip("/").split("/")[-1] != "special-exhibition"]
    items = []
    for u in pages:
        title, pairs = _khf_page(u)
        pre = bool(KHF_PRE.search(u) or KHF_PRE.search(title))
        for name, desc in pairs:
            if is_org(name):
                continue
            items.append({"name": name, "service_summary": desc[:300] or None, "page": u, "_prog": f"KHF {title}",
                          "pre": pre, "affiliation": "서울성모병원 겨자씨키움센터" if pre else None})
    print(f"KHF 특별관 {len(pages)}쪽에서 참가기업 {len(items)}건")
    return finish(con, "ev:khf", "https://khospital.org/special-exhibition/", items, lambda x: x["_prog"], dry_run)


# ── BIO KOREA ──────────────────────────────────────────────────────────────
BIOKOREA = "https://www.biokorea.org/resource/www/data/exhbList.json"
BIOKOREA_KEEP = {"Pharma", "Medical Device", "Digital Health", "Bio Venture", "Biotech", "Regenerative Medicine",
                 "Healthcare", "Medical Device & Digital Health"}


def run_biokorea(con, dry_run=False) -> dict:
    rows = http_get(BIOKOREA).json()
    items = []
    for r in rows:
        name, cat = (r.get("entGrpNmEn") or "").strip(), r.get("lclsfNm") or ""
        if r.get("ntnNmEn") != "Republic of Korea" or not name or not any(k.lower() in cat.lower() for k in BIOKOREA_KEEP):
            continue
        if re.search(r"universit|institute|foundation|hospital|association|center|council", name, re.I):
            continue
        items.append({"name": name, "url": r.get("homepageUrl") or None, "kws": [cat]})
    print(f"BIO KOREA 참가업체 {len(rows)}곳 중 한국 헬스케어 기업 {len(items)}곳")
    return finish(con, "ev:biokorea", "https://www.biokorea.org/program/exhibition.do", items,
                  f"BIO KOREA {date.today().year} 참가", dry_run)


SOURCES = {"kimes": run_kimes, "kimesbusan": run_kimesbusan, "ghcw": run_ghcw, "reha": run_reha, "khf": run_khf,
           "biokorea": run_biokorea}


def run(con, only=None, dry_run=False) -> list[dict]:
    out = []
    for k, fn in SOURCES.items():
        if only and k not in only:
            continue
        try:
            out.append(fn(con, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001 — 한 곳이 바뀌어도 나머지는 계속
            print(f"{k}: 실패 {e!r}")
    return out
