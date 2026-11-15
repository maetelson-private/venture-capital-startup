"""국세청 사업자등록 상태조회 (공공데이터포털 '국세청_사업자등록정보 진위확인 및 상태조회 서비스').
사업자번호가 있는 기업만 대상. 100건씩 일괄 조회."""
from .. import config, db
from ..fetch import http_post_json

URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
_MAP = {"01": "계속", "02": "휴업", "03": "폐업"}


def run(con) -> None:
    if not config.DATA_GO_KR_KEY:
        raise SystemExit("DATA_GO_KR_KEY 가 .env 에 없습니다 (data.go.kr 에서 무료 발급)")
    rows = con.execute("SELECT id, biz_no FROM companies WHERE biz_no IS NOT NULL").fetchall()
    by_no = {r["biz_no"]: r["id"] for r in rows}
    with db.run_log(con, "nts") as st:
        nos = list(by_no)
        for i in range(0, len(nos), 100):
            res = http_post_json(URL, {"b_no": nos[i:i + 100]}, params={"serviceKey": config.DATA_GO_KR_KEY})
            for d in res.get("data", []):
                cid = by_no.get(d.get("b_no"))
                status = _MAP.get(d.get("b_stt_cd"), "미상")
                if cid:
                    db.set_field(con, cid, "biz_status", status, "nts", URL, "상")
                    db.set_field(con, cid, "is_registered", 1 if status != "미상" else 0, "nts", URL, "상")
                    con.execute("UPDATE companies SET biz_status_checked=date('now') WHERE id=?", (cid,))
                    st["facts"] += 1
            st["docs"] += 1
        con.commit()
    print(f"영업상태 {st['facts']}건 갱신")
