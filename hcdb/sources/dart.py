"""OpenDART: 기업개황(사업자번호·설립일·대표·홈페이지) + 단일회사 주요계정(매출·영업이익·자본총계).
시드 단계 비상장사는 대부분 없음 → 외감 대상(자산 120억↑ 등)만 잡힌다고 보면 됨."""
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import date

from .. import config, db
from ..fetch import http_get
from ..normalize import norm_name

BASE = "https://opendart.fss.or.kr/api"
_ACC = {"매출액": "revenue_krw", "수익(매출액)": "revenue_krw", "영업이익": "op_income_krw",
        "영업이익(손실)": "op_income_krw", "자본총계": "total_equity_krw"}


def _corp_codes(con) -> dict[str, str]:
    """corpCode.xml (전체 기업 고유번호) → {정규화사명: corp_code}. data/ 에 캐시."""
    cache = config.DATA_DIR / "dart_corpcode.xml"
    if not cache.exists():
        r = http_get(f"{BASE}/corpCode.xml", params={"crtfc_key": config.DART_KEY}, check_robots=False)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            cache.write_bytes(z.read(z.namelist()[0]))
    out: dict[str, str] = {}
    for el in ET.parse(cache).getroot().iter("list"):
        key = norm_name(el.findtext("corp_name"))
        # 동명이면 매칭하지 않도록 표시
        out[key] = "DUP" if key in out else el.findtext("corp_code")
    return out


def _num(s):
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def run(con, years: int = 3) -> None:
    if not config.DART_KEY:
        raise SystemExit("DART_KEY 가 .env 에 없습니다 (opendart.fss.or.kr 무료 발급)")
    codes = _corp_codes(con)
    with db.run_log(con, "dart") as st:
        for c in con.execute("SELECT id, name, dart_corp_code FROM companies WHERE is_pre_startup=0").fetchall():
            code = c["dart_corp_code"]
            if not code:
                keys = [norm_name(a["alias"]) for a in
                        con.execute("SELECT alias FROM company_aliases WHERE company_id=?", (c["id"],))]
                code = next((codes[k] for k in keys if codes.get(k) not in (None, "DUP")), None)
                if not code:
                    continue
                con.execute("UPDATE companies SET dart_corp_code=? WHERE id=?", (code, c["id"]))
            info = http_get(f"{BASE}/company.json", params={"crtfc_key": config.DART_KEY, "corp_code": code},
                            check_robots=False).json()
            src = f"https://dart.fss.or.kr/dsab001/main.do?autoSearch=true&textCrpCik={code}"
            if info.get("status") == "000":
                st["facts"] += db.set_fields(con, c["id"], {
                    "biz_no": info.get("bizr_no"), "corp_reg_no": info.get("jurir_no"),
                    "ceo": info.get("ceo_nm"), "url": info.get("hm_url"),
                    "founded_date": f"{info['est_dt'][:4]}-{info['est_dt'][4:6]}-{info['est_dt'][6:8]}"
                    if info.get("est_dt") else None,
                    "name_en": info.get("corp_name_eng"),
                }, "dart", src, "상")
            for y in range(date.today().year - years, date.today().year):
                fin = http_get(f"{BASE}/fnlttSinglAcnt.json", check_robots=False, params={
                    "crtfc_key": config.DART_KEY, "corp_code": code, "bsns_year": y, "reprt_code": "11011"}).json()
                if fin.get("status") != "000":
                    continue
                row = {"company_id": c["id"], "fiscal_year": y, "source": "dart", "source_url": src}
                ofs = [x for x in fin["list"] if x.get("fs_div") == "OFS"] or fin["list"]
                for x in ofs:
                    if x["account_nm"] in _ACC:
                        row[_ACC[x["account_nm"]]] = _num(x.get("thstrm_amount"))
                db.upsert_row(con, "financials", row)
                st["facts"] += 1
            st["docs"] += 1
            con.commit()
    print(f"DART 매칭 {st['docs']}개사, fact {st['facts']}건")
