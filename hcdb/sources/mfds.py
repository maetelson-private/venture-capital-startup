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
