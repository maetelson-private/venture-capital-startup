"""대학 기술지주·창업지원단의 자회사/동문기업 목록 → 기업 + '대학 출신' 표시.

1단계(연구) 신호에 가장 가깝다: 교원·실험실 창업은 기술지주 자회사로 먼저 잡히고, 투자사 DB엔 한참 뒤에 나온다.
기술지주 출자는 VC 라운드가 아니므로 투자 라운드로 넣지 않고 program 태그 + affiliation 으로만 표시한다
(→ 카드에선 여전히 '첫 투자 후보'로 남는다).

각 사이트는 robots.txt 가 허용하는 목록 페이지만 읽는다. 헬스케어 판단:
  - 사이트가 분야 구분을 주면 그 구분으로 (예: 고려대 'Medical Health', 서울대 '의학 및 약학 연구개발업')
  - 구분이 없거나 넓으면 회사 소개 문장의 키워드(_HEALTH)로
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment

from .. import db
from ..fetch import http_get, http_post_form
from ..normalize import norm_date

_HEALTH = re.compile(
    r"헬스|의료|의학|메디|메딕|병원|환자|진단|치료|임상|질환|질병|건강|바이오|신약|약물|제약|의약|수술|재활|돌봄|요양|치매|수면|"
    r"심리|정신|멘탈|우울|영양|피부|치과|구강|안과|혈당|당뇨|심장|뇌|암\b|항암|유전체|세포|면역|디지털치료|DTx|"
    r"medical|health|bio|clinic|care|therap|diagnos", re.I)


def is_health(*texts: str | None) -> bool:
    return bool(_HEALTH.search(" ".join(t for t in texts if t)))


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _save(con, src: str, label: str, univ: str, x: dict, conf="중") -> None:
    """x: name, desc?, url?, founded?, ceo?, kws?, category?"""
    cid = db.get_or_create_company(con, x["name"], source_url=x.get("page") or src)
    db.set_fields(con, cid, {
        "service_summary": x.get("desc"), "url": x.get("url"), "founded_date": x.get("founded"),
        "ceo": x.get("ceo"), "affiliation": univ,
    }, f"univ:{label}", x.get("page") or src, conf)
    db.add_tag(con, cid, "program", label, src)
    for k in x.get("kws") or []:
        db.add_tag(con, cid, "kw", k, src)


# ── 고려대학교기술지주 (그누보드 갤러리) ─────────────────────────────────────

KU_URL = "https://kuholdings.co.kr/bbs/board.php"
KU_CATS = {"Medical Health": "헬스케어", "의료기술지주": "의료", "Bio": "바이오"}


def gallery(url: str, params: dict, max_pages: int = 30) -> list[dict]:
    """그누보드 갤러리 스킨(li.gall_li → h4 이름, p 소개, 주석 속 홈페이지)."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = http_get(url, params={**params, "page": page})
        r.encoding = "utf-8"
        items = BeautifulSoup(r.text, "html.parser").select("li.gall_li")
        new = 0
        for li in items:
            h = li.select_one("h4")
            if not h or not h.get_text(strip=True):
                continue
            name = _clean(h.get_text())
            if name in seen:
                continue
            seen.add(name)
            new += 1
            p = li.select_one("p")
            m = re.search(r'href="(https?://[^"]+)"[^>]*title="Homepage"', str(li))
            out.append({"name": name, "desc": _clean(p.get_text()) if p else None, "url": m.group(1) if m else None})
        if not new:
            break
    return out


def run_ku(con) -> None:
    rows = []
    for cat, kw in KU_CATS.items():
        for x in gallery(KU_URL, {"bo_table": "portfolio", "sca": cat}):
            x["kws"] = [kw]
            rows.append(x)
    print(f"고려대기술지주 {', '.join(KU_CATS)} {len(rows)}곳")
    src = KU_URL + "?bo_table=portfolio"
    with db.run_log(con, "univ:ku") as st:
        for x in rows:
            _save(con, src, "고려대기술지주 자회사", "고려대학교", x)
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 서울대학교 창업지원단 동문기업 ──────────────────────────────────────────

SNU_URL = "https://startup.snu.ac.kr/bbs/board.php"
# 분야만으로 헬스케어가 확실한 것
SNU_HEALTH_CATS = re.compile(r"의학|약학|바이오|의료|건강")
# 분야가 넓어(소프트웨어·정보통신 등) 상세의 사업 아이템을 봐야 하는 것
SNU_MAYBE_CATS = re.compile(r"소프트웨어|정보|통신|데이터|전문, 과학|제조|서비스업$|연구개발")


def snu_list(max_pages: int = 40) -> list[dict]:
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        r = http_get(SNU_URL, params={"bo_table": "sub6_2", "page": page})
        r.encoding = "utf-8"
        boxes = BeautifulSoup(r.text, "html.parser").select(".co_box .box")
        new = 0
        for b in boxes:
            a = b.select_one("h4 a")
            if not a:
                continue
            link = urljoin(SNU_URL, a["href"])
            if link in seen:
                continue
            seen.add(link)
            new += 1
            cat = b.select_one(".cont p")
            out.append({"name": _clean(a.get_text()), "category": _clean(cat.get_text()) if cat else "", "page": link})
        if not new:
            break
    return out


def snu_detail(url: str) -> dict:
    r = http_get(url)
    r.encoding = "utf-8"
    s = BeautifulSoup(r.text, "html.parser")
    lines = [l for l in s.get_text("\n", strip=True).split("\n") if l.strip()]
    out = {}
    for key, field in (("설립일", "founded"), ("사업 아이템", "desc"), ("기업 분야", "category")):
        if key in lines:
            out[field] = lines[lines.index(key) + 1]
    if "founded" in out:
        m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", out["founded"])
        out["founded"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    # 사업 아이템과 '홈페이지' 사이: 참여 연도·프로그램 다음의 해시태그
    if "참여 프로그램명" in lines:
        i = lines.index("참여 프로그램명") + 2
        j = lines.index("홈페이지") if "홈페이지" in lines else i
        out["kws"] = lines[i:j][:6]
    home = s.find("a", string=re.compile("홈페이지"))
    if home and home.get("href", "").startswith("http"):
        out["url"] = home["href"]
    return out


def run_snu(con) -> None:
    items = snu_list()
    sure = [x for x in items if SNU_HEALTH_CATS.search(x["category"])]
    maybe = [x for x in items if x not in sure and SNU_MAYBE_CATS.search(x["category"])]
    print(f"서울대 동문기업 {len(items)}곳: 분야로 확정 {len(sure)}곳, 상세 확인 {len(maybe)}곳")
    keep = []
    for x in sure + maybe:
        d = snu_detail(x["page"])
        if x in sure or is_health(d.get("desc"), " ".join(d.get("kws") or [])):
            keep.append({**x, **d})
    print(f"헬스케어 {len(keep)}곳")
    with db.run_log(con, "univ:snu") as st:
        for x in keep:
            _save(con, SNU_URL + "?bo_table=sub6_2", "서울대 동문기업", "서울대학교", x, "상")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 그 밖의 대학 기술지주 (정적 목록 / 사이트 자체 AJAX) ─────────────────────
# 각 함수는 [{name, desc, url?, category?}] 를 돌려준다. 헬스케어 판정은 HOLDINGS 의 filter 로.

def _soup(url: str, encoding: str = "utf-8", **kw) -> BeautifulSoup:
    r = http_get(url, **kw)
    r.encoding = encoding
    return BeautifulSoup(r.text, "html.parser")


def _t(node) -> str:
    return _clean(node.get_text(" ", strip=True)) if node else ""


def _href(a) -> str | None:
    return a["href"] if a is not None and a.get("href", "").startswith("http") else None


def sth() -> list[dict]:
    """서울대기술지주 (KBoard). 바이오 분류 목록 → 상세의 한줄소개·사업내용·홈페이지."""
    base = "https://snutechholdings.snu.ac.kr/portfolio/"
    out, seen = [], set()
    for page in range(1, 15):
        s = _soup(base, params={"mod": "list", "category1": "바이오", "pageid": page})
        new = [li for li in s.select("div.board-list li") if li.a and li.a["href"] not in seen]
        if not new:
            break
        for li in new:
            seen.add(li.a["href"])
            out.append({"name": _t(li.select_one(".tit")), "category": _t(li.select_one(".category")),
                        "page": urljoin(base, li.a["href"])})
    for x in out:
        kv = {}
        for tr in _soup(x["page"]).select("div.kboard-content table tr"):
            td = tr.find_all(["td", "th"])
            if len(td) >= 2:
                kv[_t(td[0])] = td[1]
        x["desc"] = " — ".join(filter(None, [_t(kv.get("한줄소개")), _t(kv.get("사업내용"))]))[:300] or None
        x["url"] = _href(kv["홈페이지"].find("a")) if kv.get("홈페이지") else None
    return out


def _imweb(url: str) -> list[dict]:
    """POSTECH·UNIST (imweb 갤러리): 첫 번째 _gallery_wrap 만 (두 번째는 모바일 복제)."""
    w = _soup(url).select("div._gallery_wrap")
    out = []
    for item in (w[0].select("div._item") if w else []):
        cap = item.select_one("[id^=caption_]")
        if cap and cap.h4:
            out.append({"name": _t(cap.h4), "desc": _t(cap.p), "url": _href(item.select_one("a.item_container"))})
    return out


def postech() -> list[dict]:
    return _imweb("https://postechholdings.com/22")


def unist() -> list[dict]:
    return _imweb("https://unistholdings.com/portfolio")


def mirae() -> list[dict]:
    """미래과학기술지주 (KAIST·UNIST·GIST·DGIST): 바이오·의료 분류 AJAX, 12건씩 data-num 으로 이어받음."""
    url, out, start = "https://www.miraeholding.com/ajax.portfolio.php", [], 0
    for _ in range(20):
        r = http_post_form(url, {"caname": "바이오·의료", "limit_start": start})
        r.encoding = "utf-8"
        s = BeautifulSoup(r.text, "html.parser")
        items = s.select("div.port_item")
        for it in items:
            out.append({"name": _t(it.select_one("span.title")), "desc": _t(it.select_one("span.desc")),
                        "category": "바이오·의료"})
        more = s.select_one("span.btn_port_more[data-num]")
        if not more or not items:
            break
        start = more["data-num"]
    return out


def hanyang() -> list[dict]:
    # 기본 Accept: */* 이면 406 → 브라우저와 같은 Accept 를 보낸다
    s = _soup("http://hyuholdings.com/html/sub03/sub03_0301.php",
              headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    out = []
    for h4 in s.select("h4.sub-in-bullelt-tit"):
        row = h4.find_next_sibling("div", class_="row")
        for b in (row.select("div.box") if row else []):
            ps = [_t(p) for p in b.select("div.dp-table-cell p")]
            if ps and ps[0]:
                out.append({"name": ps[0], "category": f"{_t(h4)} {ps[1] if len(ps) > 1 else ''}".strip(),
                            "desc": ps[2] if len(ps) > 2 else None})
    return out


def ewha() -> list[dict]:
    s = _soup("https://holdings.ewha.ac.kr/holdings/2250/subview.do")
    return [{"name": _t(c.select_one(".company-name")), "desc": _t(c.select_one(".company-desc")),
             "url": _href(c.select_one(".card-links a")), "category": " ".join(_t(t) for t in c.select(".tag"))}
            for c in s.select("div.card-wrap div.card")]


def cnu() -> list[dict]:
    out = []
    for path in ("introduce.do", "portfolio.do"):
        for b in _soup(f"https://holdings.cnu.ac.kr/holdings/portfolio/{path}").select("div.board-intro-box"):
            out.append({"name": _t(b.select_one(".board-intro-tit01")), "desc": _t(b.select_one(".board-intro-txt01")),
                        "url": _href(b.select_one("a.btn-board-intro"))})
    return out


def jnu() -> list[dict]:
    out = []
    # 자회사(sc_NN)와 피투자사(ic_NN) 블록. 이 사이트는 http + EUC-KR (https 는 인증서가 다른 호스트용)
    s = _soup("http://jnuholdings.com/default/investment/sub04.php", encoding="euc-kr")
    for d in s.select("div[id^=sc_], div[id^=ic_], section[id^=sc_], section[id^=ic_], li[id^=ic_], li[id^=sc_]"):
        kv = {_t(dt): _t(dt.find_next_sibling("dd")) for dt in d.select("dt")}
        ps = [_t(p) for p in d.find_all("p") if _t(p)]
        if ps:
            out.append({"name": ps[0], "ceo": kv.get("대표이사"), "url": kv.get("홈페이지") or None,
                        "founded": norm_date(kv.get("설립일자")) if kv.get("설립일자") else None,
                        "desc": " — ".join(filter(None, [kv.get("주요사업"), kv.get("기술내용")]))[:300]})
    return out


def pnu() -> list[dict]:
    """부산대기술지주: 자회사(cp0002) 전체 + 투자기업 중 바이오/헬스케어(cp0003, pf0005)."""
    out = []
    for cp, pf in (("cp0002", ""), ("cp0003", "pf0005")):
        r = http_post_form("https://pnuholdings.com/ajax/getData.php", {
            "act": "comp", "sitenum": "04", "cpcode": cp, "pfcode": pf, "page": 1, "listcnt": 1000, "vmain": 0,
            "listtype": "item", "ordertype": "desc", "target": ".cooper_box"})
        r.encoding = "utf-8"
        for it in BeautifulSoup(r.text, "html.parser").select("div.item"):
            tit = it.select_one("p.tit")
            if not tit:
                continue
            for em in tit.select("em"):
                em.decompose()
            out.append({"name": _t(tit), "desc": _t(it.select_one("p.desc")),
                        "url": _href(it.select_one(".btn_wrap a")), "category": "바이오/헬스케어" if pf else ""})
    return out


def gist() -> list[dict]:
    s = _soup("https://gistholdings.net/bbs/board.php", params={"bo_table": "portfolio"})
    out = []
    for h3 in s.find_all("h3"):
        ul = h3.find_next_sibling("ul")
        for li in (ul.select("li.gall_li") if ul else []):
            cnt = li.select_one(".bo_cnt")
            first = cnt.get_text("\n", strip=True).split("\n")[0] if cnt else ""
            out.append({"name": _t(li.select_one(".bo_tit")), "desc": first, "category": _t(h3)})
    return out


def kangwon() -> list[dict]:
    out = []
    for d in _soup("http://www.knuholdings.co.kr/holdings").select("div[onclick^=view]"):
        kv = {}
        for div in d.select(".info > div"):
            b = div.find("b")
            if b:
                key = _t(b)
                b.decompose()
                kv[key] = _t(div)
        out.append({"name": _t(d.select_one(".title_area")), "category": kv.get("사업분야", ""),
                    "desc": kv.get("기업소개")})
    return out


def jb() -> list[dict]:
    return [{"name": _t(a.select_one("font.tit")), "desc": _t(a.select_one("font.con")), "url": _href(a)}
            for a in _soup("http://jbth.co.kr/sub3/s1.htm").select("a.iticBan")]


def catholic() -> list[dict]:
    """가톨릭대기술지주(가톨릭중앙의료원): 이름이 HTML 주석 안에 있다. 6건씩 offset."""
    out = []
    for off in (0, 6, 12, 18):
        s = _soup("http://www.cukholdings.co.kr/sub/sub04_01.php", params={"offset": off} if off else None)
        new = 0
        for li in s.select("div.site-list > ul > li"):
            c = li.find(string=lambda x: isinstance(x, Comment))
            cs = BeautifulSoup(c, "html.parser") if c else None
            if not cs or not cs.dt or any(x["name"] == _t(cs.dt) for x in out):
                continue
            new += 1
            out.append({"name": _t(cs.dt), "desc": "; ".join(_t(x) for x in cs.select("ul li") if _t(x)) or None,
                        "url": _href(li.select_one("a")), "category": "의료"})
        if not new:
            break
    return out


def cau() -> list[dict]:
    lines = [l for l in _soup("http://cauholdings.com/company").get_text("\n", strip=True).split("\n") if l.strip()]
    """자유 서식 텍스트: '㈜이름' 줄부터 다음 '㈜' 줄까지가 한 회사 (대표이사 : / 설립일 : / 사업분야 : / 소개 문단)."""
    starts = [i for i, l in enumerate(lines) if l.startswith("㈜")] + [len(lines)]
    out = []
    for a, b in zip(starts, starts[1:]):
        block = lines[a + 1:min(b, a + 10)]
        kv = {k.strip(): v.strip() for k, _, v in (x.partition(":") for x in block) if _ and len(k) < 8}
        m = re.match(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", kv.get("설립일", ""))
        prose = max((x for x in block if ":" not in x and len(x) > 30), key=len, default="")
        out.append({"name": lines[a], "ceo": kv.get("대표이사"),
                    "founded": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None,
                    "desc": " — ".join(filter(None, [kv.get("사업분야"), prose[:200]])) or None})
    return out


# key: (라벨, 대학, 수집 함수, 판정)  — 'all' 은 사이트 분류가 이미 바이오·의료라 전부, 'kw' 는 소개 키워드로
HOLDINGS = {
    "sth": ("서울대기술지주", "서울대학교", sth, "all"),
    "mirae": ("미래과학기술지주", "KAIST·UNIST·GIST·DGIST", mirae, "all"),
    "catholic": ("가톨릭대기술지주", "가톨릭대학교", catholic, "all"),
    "postech": ("POSTECH홀딩스", "포항공과대학교", postech, "kw"),
    "unist": ("UNIST기술지주", "UNIST", unist, "kw"),
    "hanyang": ("한양대기술지주", "한양대학교", hanyang, "kw"),
    "ewha": ("이화여대기술지주", "이화여자대학교", ewha, "kw"),
    "cnu": ("충남대기술지주", "충남대학교", cnu, "kw"),
    "jnu": ("전남대기술지주", "전남대학교", jnu, "kw"),
    # 부산대·GIST 는 2026-11 현재 인증서 문제(호스트 불일치 / 중간 인증서 누락)로 기본 실행에서 뺌. --only pnu gist 로 재시도
    "pnu": ("부산대기술지주", "부산대학교", pnu, "kw"),
    "gist": ("GIST기술지주", "GIST", gist, "kw"),
    "kangwon": ("강원대기술지주", "강원대학교", kangwon, "kw"),
    "jb": ("전북지역대학연합기술지주", "전북지역 대학", jb, "kw"),
    "cau": ("중앙대기술지주", "중앙대학교", cau, "kw"),
}


def run_holding(con, key: str) -> None:
    label, univ, fn, mode = HOLDINGS[key]
    try:
        items = [x for x in fn() if x.get("name")]
    except Exception as e:  # noqa: BLE001 — 한 사이트가 바뀌어도 나머지는 계속
        print(f"{label}: 실패 {e!r}")
        return
    keep = [x for x in items if mode == "all" or (x.get("category") or "").upper().startswith(("바이오", "BIO"))
            or is_health(x.get("name"), x.get("desc"), x.get("category"))]
    print(f"{label} {len(items)}곳 중 헬스케어 {len(keep)}곳")
    with db.run_log(con, f"univ:{key}") as st:
        for x in keep:
            x["name"] = re.sub(r"^제\s*\d+\s*호\s*", "", x["name"])
            _save(con, x.get("page") or label, f"{label} 포트폴리오", univ, x)
            st["facts"] += 1
        con.commit()


SOURCES = {"ku": run_ku, "snu": run_snu, **{k: (lambda con, k=k: run_holding(con, k)) for k in HOLDINGS}}


DEFAULT_OFF = {"pnu", "gist"}


def run(con, only: list[str] | None = None) -> None:
    for key, fn in SOURCES.items():
        if (only and key in only) or (not only and key not in DEFAULT_OFF):
            fn(con)
