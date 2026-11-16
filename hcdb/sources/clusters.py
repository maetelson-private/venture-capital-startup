"""대학 창업지원단·지역 바이오/의료 클러스터·테크노파크 입주기업 (2026-11 조사, robots 허용 페이지만).

  hyu       한양대 서울 스타트업 배출현황 (JSON API, Healthcare 분류)
  gbia      김해의생명산업진흥원 입주기업
  kustartup 고려대 KU Startup 포트폴리오 (바이오·의료 분류, 창업동아리 포함)
  kmedi_web K-MEDI hub 연구실 임대·분양·토지분양 입주기업
  khu       경희대 창업보육센터 (서울/홍릉 바이오·국제·판교)
  gjtp      광주테크노파크 입주기업 (JSON)
  ewha      이화여대 창업지원단 입주기업 (JSON)
  cbf       춘천바이오산업진흥원 입주기업
  jbf       전남바이오진흥원 입주기업 (JSON, 예비창업자 포함)
  knu       경북대 창업지원단 보육기업
  yubi      영남대 창업보육센터 (바이오)
  gwtp      강원테크노파크 입주기업
  kmu       계명대 창업보육센터 (예비창업 표시)
  erica     한양대 ERICA 창업보육센터 (입주·졸업)
헬스케어 판단은 분야 필드가 있으면 그것, 없으면 아이템 설명 키워드(univ.is_health). 이후 scope 판정이 한 번 더 거른다.
"""
import re

from bs4 import BeautifulSoup

from .. import db
from ..fetch import Blocked, http_get, http_post_form, http_post_json
from .univ import is_health

_CORP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(재\)|\(사\)")
_NOT = re.compile(r"화장품|코스메틱|뷰티|반려|애견|펫|식품|음료|농업|축산|수산|사료")


def _n(s) -> str:
    return re.sub(r"\s+", " ", _CORP.sub("", s or "")).strip()


def _t(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def _soup(url, method="get", data=None, **kw) -> BeautifulSoup:
    r = http_post_form(url, data or {}) if method == "post" else http_get(url, **kw)
    r.encoding = r.encoding if r.encoding and r.encoding.lower() not in ("iso-8859-1",) else "utf-8"
    if "charset=euc-kr" in r.text[:2000].lower():
        r.encoding = "euc-kr"
    return BeautifulSoup(r.text, "html.parser")


def _table_rows(s: BeautifulSoup, sel="table tbody tr") -> list[list[str]]:
    return [[_t(td) for td in tr.find_all("td")] for tr in s.select(sel) if tr.find_all("td")]


# ── 각 사이트 ────────────────────────────────────────────────────────────
def hyu() -> list[dict]:
    out, page = [], 1
    while page < 30:
        js = http_post_json("https://startup.hanyang.ac.kr/api/startup_present/list", {
            "searchValue": "", "searchField": "company_name", "businessIdList": [12], "techIdList": [], "pageNo": page})
        lst = (js.get("data") or {}).get("list") or []
        for x in lst:
            out.append({"name": x.get("companyName"), "desc": x.get("item"), "url": x.get("homepage"),
                        "affiliation": "한양대학교"})
        total = int(((js.get("data") or {}).get("page") or {}).get("totalCount") or 0)
        if not lst or page * 10 >= total:
            break
        page += 1
    return out


def gbia() -> list[dict]:
    s = _soup("https://www.gbia.or.kr/sub/info/sub_2.php")
    out = []
    for tb in s.select("table")[:3]:  # 4번째 표는 대부분 기관
        for r in _table_rows(tb, "tbody tr"):
            if len(r) >= 3 and r[1]:
                out.append({"name": r[1], "desc": r[2]})
    return [x for x in out if is_health(x["desc"], x["name"]) or True]


def kustartup() -> list[dict]:
    out, seen = [], set()
    for page in range(1, 20):
        s = _soup("https://kustartup.korea.ac.kr/portfolio/portfolio.do", "post", {
            "seq": 0, "currentPage": page, "orderCol": 1, "orderByNum": 1, "searchType": "", "bizAreas": "바이오·의료"})
        items = s.select("span.hbText")
        new = 0
        for sp in items:
            nm = _t(sp)
            if nm and nm not in seen:
                seen.add(nm)
                new += 1
                a = sp.find_parent(attrs={"onclick": True})
                m = re.search(r"goView\((\d+)\)", a.get("onclick", "") if a else "")
                out.append({"name": nm, "seq": m.group(1) if m else None, "affiliation": "고려대학교"})
        if not new:
            break
    for x in out:  # 상세: 아이템·창업구분(동아리면 예비)
        if not x["seq"]:
            continue
        d = _soup("https://kustartup.korea.ac.kr/portfolio/portfolio_view.do", "post", {"seq": x["seq"]})
        txt = d.get_text("\n", strip=True)
        m = re.search(r"아이템\s*\n?(.+)", txt)
        x["desc"] = m.group(1)[:200] if m else None
        x["pre"] = "동아리" in txt
    return out


def kmedi_web() -> list[dict]:
    out = []
    for mid in ("00400391", "00400390", "00401311"):
        s = _soup("https://www.kmedihub.re.kr/index.do", params={"menu_id": mid})
        for li in s.select("ul.lst_infra > li"):
            nm = _t(li.select_one("p.title"))
            kv = {_t(th): _t(th.find_next_sibling("td")) for th in li.select("table.conLst2 th")}
            if nm and not re.search(r"재단|센터|연구원|대학", nm):
                out.append({"name": nm, "desc": kv.get("연구분야")})
    return out


def khu() -> list[dict]:
    out = []
    for mno in ("15200018", "15200049", "15200118"):
        s = _soup("https://startup.khu.ac.kr/startup_kor/user/contents/view.do", params={"menuNo": mno})
        for r in _table_rows(s, "table.tbl_standard_type1 tbody tr"):
            if len(r) >= 3 and r[1]:
                out.append({"name": r[1], "desc": r[2], "affiliation": "경희대학교"})
    return out


def gjtp() -> list[dict]:
    r = http_post_form("https://www.gjtp.or.kr/home/room/info.cs?act=searchCompany",
                       {"dongCd": "", "searchCondition": "companyNm", "searchKeyword": ""})
    js = r.json()
    rows = js if isinstance(js, list) else js.get("list") or js.get("data") or []
    return [{"name": x.get("companyNm"), "desc": x.get("mainProduct")} for x in rows]


def ewha() -> list[dict]:
    out, page = [], 1
    while page < 15:
        js = http_get("https://startup.ewha.ac.kr/api/board/content", params={"page": page, "boardEnName": "startup"}).json()
        lst = js.get("list") or (js.get("data") or {}).get("list") or js.get("content") or []
        if not lst:
            break
        for x in lst:
            out.append({"name": x.get("title"), "desc": x.get("sub01"), "url": x.get("sub03"), "affiliation": "이화여자대학교"})
        page += 1
    return out


def cbf() -> list[dict]:
    out = []
    for pg in range(1, 8):
        s = _soup("https://www.cbf.or.kr/twb_cbf/user_company_list.php",
                  params={"pagecode": "01_02_03_00_00", "s_dong": 6, "pg": pg})
        rows = [r for r in _table_rows(s) if len(r) >= 3]
        if not rows:
            break
        for r in rows:
            out.append({"name": r[0] if not r[0].isdigit() else r[1], "desc": " ".join(r[-2:])})
    return out


def jbf() -> list[dict]:
    js = http_get("https://jbf.kr/web/get_resident_center_companies_info.do").json()
    rows = []

    def walk(o):
        if isinstance(o, dict):
            if "dm_company_name" in o:
                rows.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(js)
    return [{"name": x.get("dm_company_name"), "desc": x.get("dm_biz_item"),
             "pre": "예비" in (x.get("dm_company_name") or "")} for x in rows]


def knu() -> list[dict]:
    out = []
    for p in ("changbo_dg2", "changbo_sj2", "company2", "techno2", "it2", "herb2"):
        s = _soup(f"https://startup.knu.ac.kr/web/page/center/{p}.php")
        for tr in s.select("table.table_st5 tr"):
            td = [_t(x) for x in tr.find_all("td")]
            if len(td) >= 3 and td[0] and not td[0].startswith("기업명"):
                out.append({"name": td[0], "desc": td[-1], "affiliation": "경북대학교"})
    return out


def yubi() -> list[dict]:
    s = _soup("https://yubi.or.kr/customer/lists", "post", {"businessType[]": "바이오"})
    out = []
    for tr in s.select("tr"):
        c, it = tr.select_one("td.company"), tr.select_one("td.item")
        if c:
            out.append({"name": _t(c), "desc": _t(it), "affiliation": "영남대학교"})
    return out


def gwtp() -> list[dict]:
    s = _soup("https://www.gwtp.or.kr/gwtp/pageview.php", params={"url": "sub04b", "keyvalue": "sub04"})
    # 열: 번호 | 지역 | 업체명 | 입주유형 | 대표자 | 사업분야 | 연락처 (아이템 설명이 없어 사명·분야로만 판단)
    return [{"name": r[2], "desc": r[5]} for r in _table_rows(s) if len(r) >= 6 and r[2]]


def kmu() -> list[dict]:
    s = _soup("https://cms.kmu.ac.kr/kmubi/35166/subview.do")
    out = []
    for a in s.select("article.company-card"):
        out.append({"name": _t(a.select_one("h4")), "desc": _t(a.select_one("p.item")),
                    "pre": "PRE" in _t(a.select_one("p.type")).upper(), "affiliation": "계명대학교"})
    return out


def erica() -> list[dict]:
    out = []
    for path, key in (("info_enterance.php", "info_entrance"), ("info_graduation.php", "info_graduation")):
        for page in range(1, 15):
            s = _soup(f"https://hbi.hanyang.ac.kr/info/{path}", params={"page": page, "code": key})
            # 열: No | 기업명 | 대표 | 분야 | 아이템 | …
            rows = [{"name": r[1], "desc": f"{r[3]} {r[4]}", "affiliation": "한양대학교 ERICA"}
                    for r in _table_rows(s, "table.bbs_con tbody tr") if len(r) >= 5 and r[1]]
            if not rows:
                break
            out += rows
    return out


SITES = {  # key: (함수, 라벨, 분야 필드로 이미 헬스케어만 오는지)
    "hyu": (hyu, "한양대 스타트업", True), "gbia": (gbia, "김해의생명산업진흥원 입주", False),
    "kustartup": (kustartup, "고려대 KU Startup", True), "kmedi_web": (kmedi_web, "K-MEDI hub 입주", False),
    "khu": (khu, "경희대 창업보육센터", False), "gjtp": (gjtp, "광주TP 입주", False),
    "ewha": (ewha, "이화여대 창업지원단 입주", False), "cbf": (cbf, "춘천바이오산업진흥원 입주", False),
    "jbf": (jbf, "전남바이오진흥원 입주", False), "knu": (knu, "경북대 창업지원단", False),
    "yubi": (yubi, "영남대 창업보육센터", True), "gwtp": (gwtp, "강원TP 입주", False),
    "kmu": (kmu, "계명대 창업보육센터", False), "erica": (erica, "한양대 ERICA 창업보육센터", False),
}


def run(con, only=None) -> None:
    for key, (fn, label, pre_filtered) in SITES.items():
        if only and key not in only:
            continue
        try:
            items = [x for x in fn() if x.get("name")]
        except Blocked as e:
            print(f"{label}: robots 금지 → 건너뜀 ({e})")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"{label}: 실패 {e!r}")
            continue
        keep = [x for x in items if (pre_filtered or is_health(x.get("desc"), x["name"]))
                and not _NOT.search(f"{x['name']} {x.get('desc') or ''}")]
        with db.run_log(con, f"cl:{key}") as st:
            for x in keep:
                name = _n(x["name"])
                if not name or re.search(r"^예비창업", name):
                    continue
                cid = db.get_or_create_company(con, name, source_url=label)
                db.set_fields(con, cid, {"service_summary": x.get("desc") or None, "url": x.get("url") or None,
                                         "affiliation": x.get("affiliation")}, f"cl:{key}", None, "중")
                db.add_tag(con, cid, "program", label, None)
                if x.get("pre"):
                    con.execute("UPDATE companies SET is_pre_startup=1 WHERE id=? AND founded_date IS NULL", (cid,))
                st["facts"] += 1
            con.commit()
        print(f"{label}: {len(items)}곳 중 헬스케어 {st['facts']}곳 반영")
