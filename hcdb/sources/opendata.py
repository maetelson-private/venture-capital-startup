"""공공데이터 파일·공개 API → 초기 헬스케어 기업 (2026-11 조사).

data.go.kr 파일데이터는 로그인 없이 내려받을 수 있다(사이트 화면이 쓰는 경로). 받는 횟수가 많아지면
CAPTCHA 를 요구하는데, 그때는 멈추고 사람이 직접 받는다. 받은 파일은 data/opendata/{데이터셋번호}.csv.

  venture   중기부 벤처기업명단 (15084581) — 업종·주생산품 키워드, 신규 확인·예비벤처 표시
  nps       국민연금 가입사업장 (15083277) — 의료기기·의약 연구개발 업종 + 최근 5년 내 가입(적용일자≈설립)
            → 신규 시드, 그리고 기존 nps 모듈로 월별 인원 스냅샷
  techdev   중소기업 기술개발 과제정보 (3044610) — 산업기술분류 '바이오ㆍ의료' 주관기업
  labco     연구소기업 운영현황 (15089826) — 이름에 의료·바이오 단서가 있는 최근 등록 연구소기업
  kmedi     대구경북첨단의료산업진흥재단 입주기업 (15020969)
  aiv       AI바우처 공급기업 풀 (pms.ai-voucher.or.kr, 공개 JSON) — '의료'·'헬스케어' 솔루션, 일반·청년창업 바우처
"""
import csv
import json
import re
import time
from datetime import date
from pathlib import Path

from .. import config, db
from ..fetch import http_get, http_post_form
from ..normalize import norm_date
from .univ import is_health

DIR = config.DATA_DIR / "opendata"
_NOT = re.compile(r"화장품|코스메틱|뷰티|반려|애견|펫|식품|음료|건강기능식품|농업|축산|수산|사료")
_CORP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(재\)|재단법인|\(사\)")


def _rows(path: Path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("cp949", "ignore")
    return list(csv.DictReader(text.splitlines()))


def _name(s: str) -> str:
    return re.sub(r"\s+", " ", _CORP.sub("", s or "")).strip()


def _cutoff(years: int = 5) -> str:
    return f"{date.today().year - years}-{date.today():%m-%d}"


def fetch(dataset_id: str) -> Path:
    """data.go.kr 파일데이터 내려받기. CAPTCHA 가 필요하다고 나오면 멈춘다(사람이 직접 받아 DIR 에 넣기)."""
    DIR.mkdir(parents=True, exist_ok=True)
    page = http_get(f"https://www.data.go.kr/data/{dataset_id}/fileData.do").text
    m = re.search(r"fn_fileDataDown\('(\d+)',\s*'([^']+)',\s*'([^']*)',\s*'(\d+)',\s*'(\d+)'\)", page)
    if not m:
        raise SystemExit(f"{dataset_id}: 다운로드 버튼을 찾지 못함")
    pk, dpk, _, sn, _ = m.groups()
    meta = http_get("https://www.data.go.kr/tcs/dss/selectFileDataDownload.do", params={
        "publicDataDetailPk": dpk, "publicDataPk": pk, "atchFileId": "", "fileDetailSn": sn,
        "publicDataTyCode": "PR0051"}).json()
    a, fs = meta["atchFileId"], meta["fileDetailSn"]
    lim = http_post_form("https://www.data.go.kr/cmm/cmm/check-limit.json", {"atchFileId": a, "fileDetailSn": fs})
    if "true" in lim.text:
        raise SystemExit(f"{dataset_id}: data.go.kr 가 CAPTCHA 를 요구함 → 브라우저에서 직접 받아 {DIR} 에 넣으세요")
    r = http_get("https://www.data.go.kr/cmm/cmm/fileDownload.do",
                 params={"atchFileId": a, "fileDetailSn": fs, "dataNm": dataset_id})
    path = DIR / f"{dataset_id}.csv"
    path.write_bytes(r.content)
    print(f"{dataset_id}: {len(r.content):,} bytes → {path}")
    return path


def _file(dataset_id: str, download: bool) -> Path:
    path = DIR / f"{dataset_id}.csv"
    if download or not path.exists():
        path = fetch(dataset_id)
    return path


def _seed(con, label: str, src: str, x: dict, program: str, conf="중") -> int:
    cid = db.get_or_create_company(con, x["name"], x.get("biz_no"), src)
    vals = {k: x.get(k) for k in ("service_summary", "url", "sido") if x.get(k)}
    db.set_fields(con, cid, vals, label, src, conf)
    if x.get("founded_date"):  # 공공데이터의 날짜는 설립일의 근사(가입·확인일) → 신뢰도 하, 더 나은 값이 있으면 그쪽이 이긴다
        db.set_field(con, cid, "founded_date", x["founded_date"], label, src, "하")
    db.add_tag(con, cid, "program", program, src)
    if x.get("pre"):
        con.execute("UPDATE companies SET is_pre_startup=1 WHERE id=? AND founded_date IS NULL", (cid,))
    return cid


# ── 벤처기업명단 ─────────────────────────────────────────────────────────
def run_venture(con, download=False) -> None:
    src = "https://www.data.go.kr/data/15084581/fileData.do"
    rows = _rows(_file("15084581", download))
    keep = []
    for r in rows:
        text = f"{r.get('업종명(11차)', '')} {r.get('주생산품', '')}"
        if is_health(text) and not _NOT.search(text):
            keep.append(r)
    print(f"벤처기업명단 {len(rows):,}곳 중 의료·건강 {len(keep):,}곳")
    with db.run_log(con, "od:venture") as st:
        for r in keep:
            typ = r.get("벤처확인유형", "")
            x = {"name": _name(r["업체명"]), "service_summary": r.get("주생산품"), "sido": r.get("지역"),
                 "pre": "예비" in typ}
            if not x["name"]:
                continue
            _seed(con, "od:venture", src, x, f"벤처기업 확인 ({typ}, {r.get('신규_재확인', '')})")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 국민연금 가입사업장 ────────────────────────────────────────────────────
# 국민연금 업종코드(6자리, KSIC 아님). 의료기기 제조 / 의학·약학 연구개발 / 의약품
NPS_CODES = {"331100", "331101", "331102", "331103", "331104", "730003", "730001", "242301", "242303",
             "242305", "242309"}
NPS_SW = {"722000", "723000", "729000", "721000"}  # 응용 SW 등 → 사명에 의료 단서가 있을 때만
_NPS_NAME = re.compile(r"메디|메디컬|헬스|케어|닥터|큐어|테라|바이오|뉴로|진단|의료|클리닉|헬스케어|라이프|마인드|슬립|힐")


def run_nps(con, download=False) -> None:
    src = "https://www.data.go.kr/data/15083277/fileData.do"
    path = _file("15083277", download)
    cutoff = _cutoff(5)
    keep = []
    with open(path, encoding="cp949", errors="ignore", newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        ix = {k: next(i for i, c in enumerate(h) if k in c) for k in
              ("사업장명", "사업자등록번호", "가입상태", "형태구분", "업종코드", "업종코드명", "적용일자", "가입자수",
               "법정동주소광역시도코드")}
        for r in rd:
            code = r[ix["업종코드"]]
            if r[ix["가입상태"]] != "1" or r[ix["형태구분"]] != "1" or r[ix["적용일자"]] < cutoff:
                continue
            name = r[ix["사업장명"]]
            if code in NPS_CODES or (code in NPS_SW and _NPS_NAME.search(name)):
                if not _NOT.search(name):
                    keep.append(r)
        print(f"국민연금 가입사업장 중 의료 업종·최근 5년 가입 법인 {len(keep):,}곳")
    with db.run_log(con, "od:nps") as st:
        for r in keep:
            x = {"name": _name(r[ix["사업장명"]]), "founded_date": norm_date(r[ix["적용일자"]]),
                 "service_summary": r[ix["업종코드명"]]}
            if not x["name"]:
                continue
            cid = _seed(con, "od:nps", src, x, "국민연금 신규 사업장 (의료 업종)", "하")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영 → 인원 스냅샷은 `python -m hcdb nps {path}`")


# ── 중소기업 기술개발 과제 ───────────────────────────────────────────────
def run_techdev(con, download=False) -> None:
    src = "https://www.data.go.kr/data/3044610/fileData.do"
    rows = _rows(_file("3044610", download))
    keep = [r for r in rows if ("바이오ㆍ의료" in r.get("산업기술분류", "") or is_health(r.get("과제명")))
            and not _NOT.search(r.get("산업기술분류", "") + r.get("과제명", ""))]
    print(f"중소기업 기술개발 과제 {len(rows):,}건 중 바이오·의료 {len(keep):,}건")
    with db.run_log(con, "od:techdev") as st:
        for r in keep:
            x = {"name": _name(r.get("주관기관")), "service_summary": r.get("과제명")}
            if not x["name"] or re.search(r"대학|병원|연구원|재단|센터|협회", x["name"]):
                continue
            cid = _seed(con, "od:techdev", src, x, f"중기부 R&D {r.get('사업년도', '')} ({r.get('사업명', '')})")
            db.upsert_row(con, "grants", {"company_id": cid, "program": r.get("사업명"), "year":
                                          int(r["사업년도"]) if str(r.get("사업년도", "")).isdigit() else None,
                                          "operator": r.get("전문기관"), "source_url": src, "confidence": "상"})
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── 연구소기업 ──────────────────────────────────────────────────────────
def run_labco(con, download=False) -> None:
    src = "https://www.data.go.kr/data/15089826/fileData.do"
    rows = _rows(_file("15089826", download))
    y0 = date.today().year - 5
    keep = [r for r in rows if str(r.get("등록연도", "0")).isdigit() and int(r["등록연도"]) >= y0
            and _NPS_NAME.search(r.get("기업명", "")) and not _NOT.search(r.get("기업명", ""))]
    print(f"연구소기업 {len(rows):,}곳 중 최근 5년 등록·이름에 의료 단서 {len(keep):,}곳")
    with db.run_log(con, "od:labco") as st:
        for r in keep:
            x = {"name": _name(r["기업명"])}
            _seed(con, "od:labco", src, x, f"연구소기업 {r['등록연도']} ({r.get('현행특구', '')})")
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


# ── K-MEDI hub 입주 ─────────────────────────────────────────────────────
def run_kmedi(con, download=False) -> None:
    src = "https://www.data.go.kr/data/15020969/fileData.do"
    rows = _rows(_file("15020969", download))
    keep = [r for r in rows if not re.search(r"연구원|센터|대학|재단|병원|기관", r.get("기업 및 기관명", ""))
            and is_health(r.get("업종"), r.get("기업 및 기관명"))]
    with db.run_log(con, "od:kmedi") as st:
        for r in keep:
            _seed(con, "od:kmedi", src, {"name": _name(r["기업 및 기관명"]), "service_summary": r.get("업종")},
                  "대구경북첨복단지 입주")
            st["facts"] += 1
        con.commit()
    print(f"대구경북첨복단지 입주 {st['facts']}곳 반영")


# ── AI바우처 공급기업 풀 ─────────────────────────────────────────────────
AIV = "https://pms.ai-voucher.or.kr/supply/pool"


def run_aiv(con, download=False) -> None:
    year = http_get(f"{AIV}/years", check_robots=False).json()[-1]
    items, seen = [], set()
    for voucher in ("1", "4"):  # 1 일반 AI바우처, 4 AI 청년창업기업 바우처
        for kw in ("의료", "헬스케어"):
            page = 0
            while True:
                js = http_get(f"{AIV}/list", check_robots=False, params={
                    "area": "", "exptFld": "", "keyword": kw, "year": year, "approvalStatus": 1,
                    "applyVoucher": voucher, "page": page, "size": 100}).json()
                for it in js.get("content") or []:
                    if it["splyPoolNo"] not in seen:
                        seen.add(it["splyPoolNo"])
                        items.append((voucher, it))
                if js.get("last", True):
                    break
                page += 1
    y0 = date.today().year - 5
    keep = [(v, it) for v, it in items if str(it.get("fndnYmd") or "")[:4].isdigit()
            and int(str(it["fndnYmd"])[:4]) >= y0 and is_health(it.get("aiSolution"))]
    print(f"AI바우처 공급기업({year}) 의료·헬스케어 {len(items)}곳 중 최근 5년 설립 {len(keep)}곳")
    src = f"{AIV}/list"
    with db.run_log(con, "od:aiv") as st:
        for v, it in keep:
            sol = re.sub(r"\s+", " ", it.get("aiSolution") or "")
            x = {"name": _name(it["splyCmpNm"]), "url": it.get("splyCmpHmpg"), "sido": it.get("splyAddrSido"),
                 "service_summary": sol[:300], "founded_date": f"{str(it['fndnYmd'])[:4]}-07-01"}
            _seed(con, "od:aiv", src, x, "AI바우처 공급기업" + (" (청년창업)" if v == "4" else ""))
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


SOURCES = {"venture": run_venture, "nps": run_nps, "techdev": run_techdev, "labco": run_labco,
           "kmedi": run_kmedi, "aiv": run_aiv}


def run(con, only=None, download=False) -> None:
    if only == ["nps-founding"]:
        return fill_founding_from_nps(con)
    for k, fn in SOURCES.items():
        if not only or k in only:
            try:
                fn(con, download)
            except SystemExit as e:
                print(f"{k}: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"{k}: 실패 {e!r}")
            time.sleep(1)
    if (DIR / "15083277.csv").exists():
        fill_founding_from_nps(con)


def fill_founding_from_nps(con, path: Path | None = None) -> None:
    """설립일이 빈 회사를 국민연금 가입사업장의 최초 적용일자(≈설립 직후)로 채운다. 사명이 국민연금 파일에서
    한 곳에만 있을 때만(동명 회피). 신뢰도 하 — 더 정확한 출처가 나오면 그쪽이 이긴다."""
    from ..normalize import norm_name
    path = path or DIR / "15083277.csv"
    first: dict[str, str] = {}
    dup: set[str] = set()
    with open(path, encoding="cp949", errors="ignore", newline="") as f:
        rd = csv.reader(f)
        h = next(rd)
        i_nm = next(i for i, c in enumerate(h) if "사업장명" in c)
        i_dt = next(i for i, c in enumerate(h) if "적용일자" in c)
        i_ty = next(i for i, c in enumerate(h) if "형태구분" in c)
        for r in rd:
            if r[i_ty] != "1":
                continue
            k = norm_name(r[i_nm])
            if not k:
                continue
            if k in first and first[k] != r[i_dt]:
                dup.add(k)
            first.setdefault(k, r[i_dt])
    src = "https://www.data.go.kr/data/15083277/fileData.do"
    n = 0
    with db.run_log(con, "od:nps-founding") as st:
        for c in con.execute("SELECT id FROM companies WHERE founded_date IS NULL AND coalesce(is_pre_startup,0)=0").fetchall():
            keys = {r[0] for r in con.execute("SELECT alias_norm FROM company_aliases WHERE company_id=?", (c["id"],))}
            hits = {first[k] for k in keys if k in first and k not in dup}
            if len(hits) == 1:
                db.set_field(con, c["id"], "founded_date", norm_date(hits.pop()), "od:nps-founding", src, "하")
                n += 1
        st["facts"] = n
        con.commit()
    print(f"국민연금 적용일자로 설립일(근사) 채움 {n}곳")


# ── 지역 입주·기업 현황 파일 (열 이름이 제각각 → 이름·설명 열을 추정해서 읽음) ─────────────
EXTRA = {
    "15032487": "성남시 벤처기업", "15085792": "대구 의료R&D지구 입주", "15100806": "경기 광교비즈니스센터 입주",
    "15100814": "경기 테크노밸리(경기바이오센터 등) 입주", "15149071": "경북 바이오헬스기업",
}
_NAME_COL = re.compile(r"업체명|기업명|기업 명|회사명|사업장명")
_DESC_COL = re.compile(r"주생산품|주요생산품|업종|분야|품목|제품|아이템|종류|산업분류")
_YEAR_COL = re.compile(r"설립년도|설립연도|설립일")


def run_extra(con, download=False) -> None:
    for ds, label in EXTRA.items():
        try:
            rows = _rows(_file(ds, download))
        except SystemExit as e:
            print(f"{label}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"{label}: 받기 실패 {e!r}")
            continue
        if not rows:
            continue
        cols = list(rows[0].keys())
        nc = next((c for c in cols if _NAME_COL.search(c)), None)
        dcs = [c for c in cols if _DESC_COL.search(c)]
        yc = next((c for c in cols if _YEAR_COL.search(c)), None)
        if not nc:
            print(f"{label}: 회사명 열 없음 {cols}")
            continue
        all_health = ds == "15149071"  # 경북 바이오헬스는 전부 해당 분야
        src = f"https://www.data.go.kr/data/{ds}/fileData.do"
        n = 0
        with db.run_log(con, f"od:{ds}") as st:
            for r in rows:
                desc = " ".join(r.get(c) or "" for c in dcs)
                name = _name(r.get(nc))
                if not name or _NOT.search(desc + name) or re.search(r"재단|연구원|센터|대학|병원|기관$", name):
                    continue
                if not (all_health or is_health(desc, name)):
                    continue
                y = (r.get(yc) or "")[:10] if yc else ""
                x = {"name": name, "service_summary": desc.strip() or None,
                     "founded_date": norm_date(y if len(y) >= 8 else (f"{y[:4]}-07-01" if y[:4].isdigit() else ""))}
                _seed(con, f"od:{ds}", src, x, label)
                n += 1
            st["facts"] = n
            con.commit()
        print(f"{label}: {len(rows)}행 중 헬스케어 {n}곳 반영")


SOURCES["extra"] = run_extra


# ── 창업기업확인서 발급기업 (창업진흥원, data.go.kr 15125362, 활용신청 필요) ─────────────────
CERT = "https://apis.data.go.kr/B552735/kisedCertService01"
CERT_SRC = "https://www.data.go.kr/data/15125362/openapi.do"


def _cert_all(op: str) -> list[dict]:
    out, page = [], 1
    while True:
        js = http_get(f"{CERT}/{op}", check_robots=False, params={
            "serviceKey": config.DATA_GO_KR_KEY, "page": page, "perPage": 1000, "returnType": "json"}).json()
        out += js.get("data") or []
        if page * 1000 >= int(js.get("totalCount") or 0) or not js.get("data"):
            return out
        page += 1


def run_cert(con, download=False) -> None:
    """창업기업확인서 = 업력 7년 이하 공식 확인. (1) 등록 제품이 의료·헬스케어인 기업을 시드로,
    (2) 이미 DB 에 있는 기업은 사명이 한 곳과만 맞을 때 사업자번호를 채우고 '창업기업 확인' 태그."""
    from ..normalize import norm_name
    prods = _cert_all("getProductInformation01")
    by_brno: dict[str, dict] = {}
    for p in prods:
        if is_health(p.get("manu_nm")) and not _NOT.search(p.get("manu_nm") or ""):
            b = by_brno.setdefault(p["brno"], {**p, "items": []})
            b["items"].append(p.get("manu_nm"))
    print(f"창업기업 등록 제품 {len(prods):,}개 중 의료·헬스케어 기업 {len(by_brno)}곳")
    with db.run_log(con, "od:cert") as st:
        for b, p in by_brno.items():
            x = {"name": _name(p["ntrp_nm"]), "biz_no": b, "url": p.get("hmpg"), "sido": p.get("lwdg_nm"),
                 "service_summary": "; ".join(dict.fromkeys(p["items"]))[:300]}
            _seed(con, "od:cert", CERT_SRC, x, f"창업기업 확인 ({p.get('confmdoc_isu_dt', '')[:4]}, 7년 이하)", "상")
            st["facts"] += 1
        con.commit()
        corps = _cert_all("getCorporateInformation01")
        names: dict[str, list[dict]] = {}
        for c in corps:
            names.setdefault(norm_name(c.get("ntrp_nm")), []).append(c)
        hit = 0
        for row in con.execute("SELECT id FROM companies").fetchall():
            keys = {r[0] for r in con.execute("SELECT alias_norm FROM company_aliases WHERE company_id=?", (row["id"],))}
            cands = {c["brno"]: c for k in keys for c in names.get(k, [])}
            if len(cands) == 1:
                c = next(iter(cands.values()))
                db.set_field(con, row["id"], "biz_no", c["brno"], "od:cert", CERT_SRC, "중")
                db.add_tag(con, row["id"], "program", f"창업기업 확인 ({c.get('confmdoc_isu_dt', '')[:4]}, 7년 이하)", CERT_SRC)
                hit += 1
        st["docs"] = hit
        con.commit()
    print(f"제품 기준 {st['facts']}곳 반영, 확인기업 {len(corps):,}곳 중 DB 기업과 일치 {hit}곳 (사업자번호·7년 이하 표시)")


# ── 의료기기 GMP 지정현황 (식약처, data.go.kr 15058930, 활용신청 필요) ─────────────────────
GMP = "https://apis.data.go.kr/1471000/MdlpGmpAppnSttusInfoService03/getCompanyNameList03"
GMP_SRC = "https://www.data.go.kr/data/15058930/openapi.do"


def run_gmp(con, download=False, recent_years: int = 3) -> None:
    """GMP 적합인정은 품목허가보다 먼저 받는다 → 업체별 '첫 적합인정일'이 최근 N년인 국내 제조업체 = 신규 의료기기 회사."""
    rows, page = [], 1
    while True:
        js = http_get(GMP, check_robots=False, params={"serviceKey": config.DATA_GO_KR_KEY, "pageNo": page,
                                                        "numOfRows": 500, "type": "json"}).json()  # 최대 500
        items = (js.get("body") or {}).get("items") or []
        rows += [i.get("item", i) for i in items]
        if (js.get("header") or {}).get("resultCode") not in (None, "00"):
            raise RuntimeError(f"GMP API 오류: {(js.get('header') or {}).get('resultMsg')}")
        if page * 500 >= int((js.get("body") or {}).get("totalCount") or 0) or not items:
            break
        page += 1
    first: dict[str, dict] = {}
    for r in rows:
        if r.get("COB_FLAG_NAME") != "제조업" or not r.get("BRNO"):
            continue
        d = r.get("FIT_CERTF_DATE") or r.get("PERMIT_DATE") or ""
        if not d:
            continue
        cur = first.get(r["BRNO"])
        if cur is None or d < cur["_d"]:
            first[r["BRNO"]] = {**r, "_d": d}
    cutoff = f"{date.today().year - recent_years}{date.today():%m%d}"
    new = {b: r for b, r in first.items() if r["_d"] >= cutoff and "(유)" not in (r.get("ENTP_NAME") or "")}
    print(f"GMP 지정 {len(rows):,}건, 제조업체 {len(first):,}곳 중 첫 적합인정이 최근 {recent_years}년 {len(new)}곳")
    with db.run_log(con, "od:gmp") as st:
        for b, r in new.items():
            x = {"name": _name(r["ENTP_NAME"]), "biz_no": b, "sido": (r.get("ENTP_ADDR1") or "").split(" ")[0] or None}
            cid = _seed(con, "od:gmp", GMP_SRC, x, f"의료기기 GMP 첫 적합인정 {r['_d'][:4]}", "상")
            db.upsert_row(con, "regulatory", {"company_id": cid, "product": "의료기기 제조 GMP", "kind": "GMP적합인정",
                                              "status": r.get("PROGRESS_STATUS_NAME"), "event_date": norm_date(r["_d"]),
                                              "source_url": GMP_SRC})
            st["facts"] += 1
        con.commit()
    print(f"{st['facts']}곳 반영")


SOURCES["cert"] = run_cert
SOURCES["gmp"] = run_gmp


def gmp_items(con) -> None:
    """GMP 로 새로 들어온 회사(이름만 있음)에 GMP 품목명을 붙여 범위 판정이 가능하게 한다 (업체당 1회 호출)."""
    url = "https://apis.data.go.kr/1471000/MdlpGmpAppnSttusInfoService03/getItemGroupList03"
    rows = con.execute("""SELECT c.id, c.name FROM companies c JOIN company_tags t ON t.company_id=c.id
        WHERE t.value LIKE '의료기기 GMP 첫 적합인정%' AND c.service_summary IS NULL""").fetchall()
    n = 0
    with db.run_log(con, "od:gmp-items") as st:
        for r in rows:
            alias = con.execute("SELECT alias FROM company_aliases WHERE company_id=? ORDER BY length(alias) DESC",
                                (r["id"],)).fetchone()
            try:
                js = http_get(url, check_robots=False, params={"serviceKey": config.DATA_GO_KR_KEY, "pageNo": 1,
                                                               "numOfRows": 100, "type": "json",
                                                               "entp_name": alias[0] if alias else r["name"]}).json()
            except Exception:  # noqa: BLE001
                continue
            items = [i.get("item", i) for i in ((js.get("body") or {}).get("items") or [])]
            names = list(dict.fromkeys(f"{i.get('ITEM_GRP_NAME') or ''}/{i.get('ITEM_NAME') or ''}".strip("/")
                                       for i in items if i.get("ITEM_NAME")))
            if names:
                db.set_field(con, r["id"], "service_summary", "; ".join(names)[:300], "od:gmp", GMP_SRC, "상")
                n += 1
            if n % 50 == 0:
                con.commit()
        st["facts"] = n
        con.commit()
    print(f"GMP 신규 업체 {len(rows)}곳 중 품목명 붙임 {n}곳")


SOURCES["gmp_items"] = lambda con, download=False: gmp_items(con)
