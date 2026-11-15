"""식약처 혁신의료기기 지정 현황 (data.go.kr OpenAPI, DATA_GO_KR_KEY + 이 API 활용신청 필요).
응답 칸: BIZ_IPLA_NM(업소명) PRDLST_NM(품목명) MODL_NM(모델명) DSGN_DT(지정일) …"""
from .. import config, db
from ..fetch import http_get
from ..normalize import norm_date

URL = "https://apis.data.go.kr/1471000/InnoMdeqDsgnStusService/getInnoMdeqDsgnStusService"
SRC = "https://www.data.go.kr/data/15117147/openapi.do"


def _items(js: dict) -> tuple[list[dict], int]:
    if "OpenAPI_ServiceResponse" in js:
        h = js["OpenAPI_ServiceResponse"]["cmmMsgHeader"]
        raise SystemExit(f"식약처 API 오류: {h.get('returnAuthMsg')} — data.go.kr 에서 '식품의약품안전처_혁신의료기기 지정 현황' 활용신청 필요")
    body = js.get("body", {})
    items = body.get("items") or []
    if isinstance(items, dict):  # 1건일 때 dict 로 오는 경우
        items = items.get("item", [])
        items = [items] if isinstance(items, dict) else items
    return items, int(body.get("totalCount") or 0)


PRMSN_URL = "https://apis.data.go.kr/1471000/MdlpPrdlstPrmisnInfoService06/getMdlpPrdlstPrmisnItem05"
PRMSN_SRC = "https://www.data.go.kr/data/15057456/openapi.do"
MNFT_URL = "https://apis.data.go.kr/1471000/MdeqMnftPrmsnDtlService01/getMdeqMnftPrmsnDtlInq01"


def _get_json(url: str, params: dict, tries: int = 4) -> dict:
    """data.go.kr 는 간헐적으로 503·DNS 실패가 나서 몇 번 재시도한다."""
    import time

    import requests
    for k in range(tries):
        try:
            return http_get(url, check_robots=False, params=params).json()
        except (requests.ConnectionError, requests.HTTPError, requests.Timeout):
            if k == tries - 1:
                raise RuntimeError(f"data.go.kr 응답 없음: {url.rsplit('/', 1)[-1]}") from None
            time.sleep(5 * (k + 1))


def _pages(url: str, params: dict, rows: int = 100, max_pages: int = 100):
    page = 1
    while page <= max_pages:
        js = _get_json(url, {
            "serviceKey": config.DATA_GO_KR_KEY, "type": "json", "numOfRows": rows, "pageNo": page, **params})
        items, total = _items(js)
        yield [i.get("item", i) for i in items]
        if page * rows >= total or not items:
            break
        page += 1


def run_sw(con, keywords=("소프트웨어",), recent_years: int = 5) -> None:
    """식약처 품목허가 중 품목명에 키워드(기본 '소프트웨어')가 들어간 것 → 업체별로 묶어
    '첫 SW 허가'가 최근 N년 안인 국내 업체를 시드로 추가. 기존 기업이면 인허가 이력만 붙인다.
    사업자등록번호(BRNO)가 함께 와서 국세청 조회·중복 판정이 정확해진다."""
    from datetime import date
    cutoff = f"{date.today().year - recent_years}{date.today():%m%d}"
    by_co: dict[str, dict] = {}
    for kw in keywords:
        # 상세조회(Item05)는 대문자 PRDUCT 로 걸러야 한다 (소문자는 무시되어 전체 21만 건이 나옴)
        for batch in _pages(PRMSN_URL, {"PRDUCT": kw}):
            for it in batch:
                name = (it.get("ENTRPS") or "").strip()
                if not name or it.get("RTRCN_DSCTN_DIVS_CD"):  # 취소·취하된 허가 제외
                    continue
                c = by_co.setdefault(name, {"brno": it.get("BRNO"), "items": [], "imported": False})
                c["items"].append(it)
                # 수입품(해외 제조원 표기)·외국계 유한회사는 국내 스타트업이 아님
                if it.get("MANUF_NM") or "(유)" in name or "유한회사" in name:
                    c["imported"] = True
    new = existing = 0
    with db.run_log(con, "mfds-sw", repr(keywords)) as st:
        for name, c in by_co.items():
            first = min(i.get("PRMISN_DT") or "99999999" for i in c["items"])
            cid = db.find_company(con, name, c["brno"])
            if cid is None:
                if c["imported"] or first < cutoff:
                    continue
                cid = db.get_or_create_company(con, name, c["brno"], PRMSN_SRC)
                con.execute("UPDATE companies SET confidence=coalesce(confidence,'중') WHERE id=?", (cid,))
                new += 1
            else:
                existing += 1
                if c["brno"]:
                    db.set_field(con, cid, "biz_no", c["brno"], "mfds-sw", PRMSN_SRC, "상")
            for it in c["items"]:
                db.upsert_row(con, "regulatory", {
                    "company_id": cid, "product": f"{it.get('PRDUCT')} ({it.get('TYPE_NAME') or ''})".strip(),
                    "kind": "식약처허가", "status": "허가", "device_class": it.get("GRADE"),
                    "event_date": norm_date(it.get("PRMISN_DT")), "source_url": PRMSN_SRC})
                st["facts"] += 1
            grade = max((i.get("GRADE") or "" for i in c["items"]), default="")
            db.set_fields(con, cid, {"regulatory_status": "식약처 허가", "device_class": grade or None},
                          "mfds-sw", PRMSN_SRC, "상")
            if not con.execute("SELECT service_summary FROM companies WHERE id=?", (cid,)).fetchone()[0]:
                db.set_field(con, cid, "service_summary",
                             "; ".join(sorted({i.get('PRDUCT') for i in c['items']}))[:200], "mfds-sw", PRMSN_SRC, "중")
            st["docs"] += 1
        con.commit()
    print(f"SW 의료기기 허가 업체 {len(by_co)}곳 → 신규 {new}곳 추가, 기존 {existing}곳에 허가 이력 연결")


def run(con) -> None:
    if not config.DATA_GO_KR_KEY:
        raise SystemExit("DATA_GO_KR_KEY 가 없습니다")
    with db.run_log(con, "mfds-innov") as st:
        page, rows = 1, 100
        while True:
            js = http_get(URL, check_robots=False, params={
                "serviceKey": config.DATA_GO_KR_KEY, "type": "json", "numOfRows": rows, "pageNo": page}).json()
            items, total = _items(js)
            for it in items:
                name = (it.get("BIZ_IPLA_NM") or "").strip()
                if not name:
                    continue
                # 혁신의료기기 업체는 대기업·외국계도 많아 '발굴 후보'가 아니라 신호로만 붙인다:
                # 이미 DB 에 있는 기업이면 연결, 없으면 새로 만들되 신뢰도 '하'로 표시
                cid = db.get_or_create_company(con, name, source_url=SRC)
                con.execute("UPDATE companies SET confidence=coalesce(confidence,'하') WHERE id=?", (cid,))
                db.upsert_row(con, "regulatory", {
                    "company_id": cid, "product": " / ".join(filter(None, [it.get("PRDLST_NM"), it.get("MODL_NM")])),
                    "kind": "혁신의료기기", "status": "지정", "event_date": norm_date(it.get("DSGN_DT")),
                    "source_url": SRC})
                st["facts"] += 1
            st["docs"] += 1
            if page * rows >= total or not items:
                break
            page += 1
        con.commit()
    print(f"혁신의료기기 지정 {st['facts']}건 반영")
