"""보육·병원 창업·협회 디렉토리 → 초기 헬스케어 기업 (2026-11 조사, 모두 robots 허용·로그인 없음).

  dhnet      한국디지털헬스산업협회 디지털헬스넷 기업 디렉토리 (~316, 전부 디지털헬스, 상세에 설립일·대표·인원)
  trials     KHIDI 의료기기 임상시험계획 승인 목록 중 '소프트웨어' 품목 (DTx·의료 AI 의뢰사) — 키 없이
  snubh      분당서울대병원 창업보육센터 입주기업
  rdh        KHIDI 연구중심병원 창업기업 (20개 연구중심병원 교원·연구자 창업, 상세에 설립일)
  yonsei     연세대 창업지원단 입주기업 중 [바이오/헬스케어]
"""
import re

from bs4 import BeautifulSoup

from .. import db
from ..fetch import http_get, http_post_form
from ..normalize import norm_date
from .univ import is_health

UA_HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def _soup(url, **kw):
    r = http_get(url, **kw)
    r.encoding = "utf-8"
    return BeautifulSoup(r.text, "html.parser")


def _t(n) -> str:
    return re.sub(r"\s+", " ", n.get_text(" ", strip=True)).strip() if n else ""


def _save(con, label: str, src: str, x: dict, conf="중", program: str | None = None) -> int:
    cid = db.get_or_create_company(con, x["name"], x.get("biz_no"), x.get("page") or src)
    db.set_fields(con, cid, {k: x.get(k) for k in ("ceo", "url", "founded_date", "service_summary", "headcount")
                             if x.get(k)}, label, x.get("page") or src, conf)
    db.add_tag(con, cid, "program", program or label, src)
    for k in x.get("kws") or []:
        db.add_tag(con, cid, "kw", k, src)
    return cid


# ── 디지털헬스넷 (KODHIA) ───────────────────────────────────────────────────
DHNET = "https://dhnet.kodhia.or.kr"


def dhnet() -> list[dict]:
    out, page = [], 1
    while True:
        r = http_post_form(f"{DHNET}/network/compAct.html", {
            "page": page, "sflag": "N", "pIndustry": "", "pHealthcatea": "", "pHealthcateb": "", "pHealthcatec": "",
            "viewrow": 12, "myId": "", "wrd": "", "ord": "p_bno desc", "pSupply": "Y", "pDemand": "Y"})
        js = r.json()
        out += js.get("list") or []
        if page >= int(js.get("LastPage") or 1):
            return out
        page += 1


def dhnet_detail(no) -> dict:
    page = f"{DHNET}/core/?cid=99&bPno={no}"
    s = _soup(page)
    kv = {}
    for dl in s.select("dl.info_company, li.item_info dl.inr"):
        for dt in dl.select("dt"):
            dd = dt.find_next_sibling("dd")
            kv[_t(dt)] = _t(dd)
    home = kv.get("홈페이지") or ""
    staff = re.search(r"\d+", kv.get("직원 수", "") or kv.get("직원수", ""))
    return {"page": page, "ceo": kv.get("대표") or kv.get("대표자"), "founded_date": norm_date(kv.get("설립일")),
            "url": home if home.startswith("http") or "." in home else None,
            "headcount": int(staff.group()) if staff else None,
            "service_summary": _t(s.select_one("div.summury_wrap"))[:300] or None}


def run_dhnet(con) -> None:
    items = dhnet()
    print(f"디지털헬스넷 기업 {len(items)}곳 (상세 읽는 중)")
    with db.run_log(con, "inc:dhnet") as st:
        for it in items:
            x = {"name": re.sub(r"\(주\)|㈜|주식회사", "", it["name"]).strip(), **dhnet_detail(it["no"]),
                 "kws": [k.strip() for k in re.split(r"[,#]", it.get("keyword") or "") if k.strip()][:4]}
            if not x["service_summary"]:
                x["service_summary"] = it.get("svc")
            _save(con, "inc:dhnet", f"{DHNET}/core/?cid=97", x, "상", "디지털헬스산업협회 디렉토리")
            st["facts"] += 1
            if st["facts"] % 20 == 0:
                con.commit()
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── KHIDI 의료기기 임상시험계획 승인 (소프트웨어) ───────────────────────────────
TRIALS = "https://www.khidi.or.kr/mdtis/clncTestPlanAprv/clncTestPlanAprvList"


def trials(keyword="소프트웨어", max_pages=60) -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        s = _soup(TRIALS, params={"pageNum": page, "rowCnt": 10, "menuId": "MENU03607", "schType": 3,
                                   "schText": keyword})
        rows = s.select("table.tstyle_list tbody tr")
        new = 0
        for tr in rows:
            td = [_t(x) for x in tr.find_all("td")]
            if len(td) < 5 or td[2] in seen:
                continue
            seen.add(td[2])
            new += 1
            out.append({"sponsor": td[1], "no": td[2], "date": norm_date(td[3]), "product": td[4]})
        if not new:
            break
    return out


_BIG = re.compile(r"병원|의료원|대학교|대학|재단|연구원|센터|삼성|엘지|LG|SK|GE|지멘스|필립스|메드트로닉|코리아$|유한회사|\(유\)")


def run_trials(con) -> None:
    rows = trials()
    by = {}
    for r in rows:
        by.setdefault(r["sponsor"], []).append(r)
    print(f"소프트웨어 의료기기 임상시험 승인 {len(rows)}건, 의뢰사 {len(by)}곳")
    with db.run_log(con, "inc:trials") as st:
        for sponsor, rs in by.items():
            name = re.sub(r"\(주\)|㈜|주식회사", "", sponsor).strip()
            if not name or _BIG.search(name):  # 병원·대학·대기업·외국계는 시드로 쓰지 않음
                cid = db.find_company(con, name)
                if cid is None:
                    continue
            else:
                cid = db.get_or_create_company(con, name, source_url=TRIALS)
            for r in rs:
                db.upsert_row(con, "regulatory", {"company_id": cid, "product": r["product"], "kind": "임상시험승인",
                                                  "status": "승인", "event_date": r["date"], "source_url": TRIALS})
            if not con.execute("SELECT service_summary FROM companies WHERE id=?", (cid,)).fetchone()[0]:
                db.set_field(con, cid, "service_summary", "; ".join(sorted({r["product"] for r in rs}))[:200],
                             "inc:trials", TRIALS, "중")
            db.add_tag(con, cid, "program", "SW 의료기기 임상시험 승인", TRIALS)
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 분당서울대병원 창업보육센터 ─────────────────────────────────────────────
SNUBH = "https://bri.snubh.org/bizIncuCenter/conts/102011002000000.do"


def run_snubh(con) -> None:
    rows = []
    for tr in _soup(SNUBH).select("table tbody tr"):
        td = tr.find_all("td")
        if len(td) >= 4:
            a = tr.select_one("a.btn_sty03")
            rows.append({"name": re.sub(r"\(주\)|㈜|주식회사", "", _t(td[1])).strip(), "ceo": _t(td[2]),
                         "service_summary": _t(td[3]), "url": a["href"] if a and a.get("href", "").startswith("http") else None})
    with db.run_log(con, "inc:snubh") as st:
        for x in rows:
            _save(con, "inc:snubh", SNUBH, x, "상", "분당서울대병원 창업보육센터")
            st["facts"] += 1
        con.commit()
    print(f"분당서울대병원 창업보육센터 {st['facts']}곳 반영")


# ── KHIDI 연구중심병원 창업기업 ─────────────────────────────────────────────
RDH = "https://www.khidi.or.kr/rdh/krhNetwork/networkEstablish"
RDH_VIEW = "https://www.khidi.or.kr/rdh/krhNetwork/networkEstablishView"


def rdh(max_pages=40) -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        s = _soup(RDH, params={"pageNum": page, "rowCnt": 10, "menuId": "MENU01936", "rdh_krh_seq_sub": "",
                                "est_classify": "", "schType": 0, "schText": ""})
        new = 0
        for tr in s.select("table tbody tr"):
            td = tr.find_all("td")
            m = re.search(r"goView\('?(\d+)", str(tr))
            if len(td) < 3 or not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            new += 1
            out.append({"seq": m.group(1), "name": _t(td[0]), "ceo": _t(td[1]), "biz": _t(td[2])})
        if not new:
            break
    return out


def rdh_detail(seq) -> dict:
    page = f"{RDH_VIEW}?menuId=MENU01936&est_seq={seq}"
    kv = {}
    for th in _soup(page).select("th"):
        td = th.find_next_sibling("td")
        if td:
            kv[_t(th)] = _t(td)
    return {"page": page, "hospital": kv.get("병원명"), "cls": kv.get("분류"),
            "founded_date": norm_date(kv.get("창립(설립)년월일") or kv.get("설립년월일"))}


def run_rdh(con) -> None:
    items = rdh()
    # 제약·식품·화장품 분류는 상세를 봐야 알 수 있어, 사업내용 키워드로 1차 거른 뒤 상세를 읽는다
    cand = [x for x in items if is_health(x["biz"], x["name"])]
    print(f"연구중심병원 창업기업 {len(items)}곳 중 헬스케어 후보 {len(cand)}곳")
    with db.run_log(con, "inc:rdh") as st:
        for x in cand:
            d = rdh_detail(x["seq"])
            if d.get("cls") in ("식품", "화장품"):
                continue
            y = {"name": re.sub(r"\(주\)|㈜|주식회사", "", x["name"]).strip(), "ceo": x["ceo"],
                 "service_summary": x["biz"], **d}
            cid = _save(con, "inc:rdh", RDH, y, "상", f"연구중심병원 창업 ({d.get('hospital') or '병원'})")
            db.set_field(con, cid, "affiliation", d.get("hospital"), "inc:rdh", d["page"], "상")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 연세대 창업지원단 입주기업 ──────────────────────────────────────────────
YONSEI = "https://venture.yonsei.ac.kr/eco/tenants"


def run_yonsei(con) -> None:
    rows = []
    for d in _soup(YONSEI).select("div.tenants__company"):
        info = d.select_one("p.tenants__company-info")
        cat = _t(info.select_one("span.blue")) if info else ""
        desc = _t(info).replace(cat, "").strip() if info else ""
        name = re.sub(r"\(주\)|㈜|주식회사", "", _t(d.select_one("p.tenants__company-name"))).strip()
        if name and ("헬스" in cat or "바이오" in cat or is_health(desc)):
            rows.append({"name": name, "service_summary": desc})
    with db.run_log(con, "inc:yonsei") as st:
        for x in rows:
            cid = _save(con, "inc:yonsei", YONSEI, x, "중", "연세대 창업지원단 입주")
            db.set_field(con, cid, "affiliation", "연세대학교", "inc:yonsei", YONSEI, "중")
            st["facts"] += 1
        con.commit()
    print(f"연세대 창업지원단 입주 헬스케어 {st['facts']}곳 반영")


# ── 중기부 창업보육센터 입주기업 현황 (data.go.kr 15122804, odcloud, DATA_GO_KR_KEY + 활용신청) ─────────
BI_API = "https://api.odcloud.kr/api/15122804/v1/uddi:b600784f-b42e-45bc-bb10-e0b5054b1b44"
BI_SRC = "https://www.data.go.kr/data/15122804/fileData.do"
_NOT_HEALTH = re.compile(r"화장품|스킨케어|코스메틱|뷰티|반려|애견|펫|식품|음료|건강기능식품|농|축산|수산")


def run_bi(con, recent_years: int = 6) -> None:
    """전국 창업보육센터(대학·병원 BI 포함) 입주기업 5,700곳 중 주생산품이 의료·건강인 최근 창업기업.
    uddi 는 데이터가 갱신되면 바뀐다 — https://infuser.odcloud.kr/oas/docs?namespace=15122804/v1 의 마지막 것."""
    from datetime import date

    from .. import config
    rows, page = [], 1
    while True:
        js = http_get(BI_API, check_robots=False, params={"page": page, "perPage": 1000,
                                                         "serviceKey": config.DATA_GO_KR_KEY}).json()
        rows += js.get("data") or []
        if page * 1000 >= int(js.get("matchCount") or 0):
            break
        page += 1
    cutoff = f"{date.today().year - recent_years}-01-01"
    keep = [x for x in rows if is_health(x.get("주생산품"), x.get("업종") if "의료" in (x.get("업종") or "") else None)
            and not _NOT_HEALTH.search(x.get("주생산품") or "") and (x.get("창업일") or "9999") >= cutoff]
    print(f"창업보육센터 입주기업 {len(rows)}곳 중 의료·건강 최근 창업 {len(keep)}곳")
    with db.run_log(con, "inc:bi") as st:
        for x in keep:
            name = re.sub(r"\(주\)|㈜|주식회사|\(유\)", "", x["기업명"]).strip()
            if not name:
                continue
            y = {"name": name, "ceo": x.get("대표자명"), "founded_date": norm_date(x.get("창업일")),
                 "service_summary": x.get("주생산품")}
            cid = _save(con, "inc:bi", BI_SRC, y, "상", f"{x.get('센터명')} 입주")
            if x.get("기업형태") == "예비창업자":
                con.execute("UPDATE companies SET is_pre_startup=1 WHERE id=?", (cid,))
            db.set_field(con, cid, "sido", x.get("소재지"), "inc:bi", BI_SRC, "중")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# snubh 는 파이썬에서 인증서 검증이 실패(중간 인증서 누락)해 기본에서 빼고, 브라우저로 읽은 config/snubh_bi.json 을
# `univ --only snubh_browser` 로 넣는다
SOURCES = {"dhnet": run_dhnet, "trials": run_trials, "rdh": run_rdh, "yonsei": run_yonsei, "bi": run_bi, "snubh": run_snubh}
DEFAULT_OFF = {"snubh"}


def run(con, only=None) -> None:
    for k, fn in SOURCES.items():
        if (only and k in only) or (not only and k not in DEFAULT_OFF):
            try:
                fn(con)
            except Exception as e:  # noqa: BLE001 — 한 곳이 바뀌어도 나머지는 계속
                print(f"{k}: 실패 {e!r}")
