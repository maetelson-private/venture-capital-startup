"""기술지주회사 감사보고서(DART) → 출자·투자 기업 명단.

기술지주는 외부감사 대상이라 감사보고서 주석에 '매도가능증권', '지분법적용투자주식', '종속기업·관계기업',
'공정가치측정금융자산' 표로 투자한 회사 이름이 모두 나온다. 웹 포트폴리오를 막아 두었거나(연세·KAIST) JS 로만
보여주는 곳(한국과학기술지주)도 여기서는 명단을 얻을 수 있다. 다만 DART 에 감사보고서를 내는 곳만 해당.

이름만 나오므로:
  - 이미 DB 에 있는 회사 → '○○ 출자' program 태그 (헬스케어 여부는 이미 판정돼 있음)
  - 새 이름 → 이름에 의료·바이오 단서가 있으면 새 기업(신뢰도 하)으로, 아니면 명단 파일에만 남겨 사람이 본다
"""
import io
import json
import re
import zipfile

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .. import config, db
from ..fetch import http_get
from .univ import is_health

API = "https://opendart.fss.or.kr/api"
# DART 에 등록된 기술지주 (corpCode.xml 에서 '기술지주'로 찾은 것)
HOLDINGS = {
    "01074701": ("한국과학기술지주", "출연연"),
    "01312081": ("고려대기술지주", "고려대학교"),
    "01305425": ("미래과학기술지주", "KAIST·UNIST·GIST·DGIST"),
    "01664841": ("가톨릭대기술지주", "가톨릭대학교"),
    "01779587": ("연세대기술지주", "연세대학교"),
    "01675388": ("성균관대기술지주", "성균관대학교"),
    "01672071": ("포항공대기술지주", "포항공과대학교"),
    "01694790": ("울산대기술지주", "울산대학교"),
    "01708747": ("전북지역대학연합기술지주", "전북지역 대학"),
}
_SECTION = re.compile(r"\n\s*\d+\.\s*(매도가능증권|투자유가증권|장기투자자산|지분법적용투자주식|종속기업|관계기업|공정가치\s*측정\s*금융자산|"
                      r"당기손익-?공정가치|기타포괄손익-?공정가치|장기투자증권|투자주식|유가증권)[^\n]*")
_NEXT = re.compile(r"\n\s*\d+\.\s*[가-힣]")
_CORP = re.compile(r"\(\s*주\s*\)|㈜|주식회사")
_NOT_CO = re.compile(r"주식수|회사명|조합|펀드|Fund|투자회사|합계|소계|은행|증권$|Partners|파트너스")


def latest_audit(corp_code: str) -> tuple[str, str] | None:
    js = http_get(f"{API}/list.json", check_robots=False, params={
        "crtfc_key": config.DART_KEY, "corp_code": corp_code, "bgn_de": "20150101", "page_count": 100}).json()
    reps = [(i["rcept_no"], i["report_nm"].strip()) for i in js.get("list") or [] if "감사보고서" in i["report_nm"]]
    return max(reps) if reps else None


def report_text(rcept_no: str) -> str:
    r = http_get(f"{API}/document.xml", check_robots=False, params={"crtfc_key": config.DART_KEY, "rcept_no": rcept_no})
    out = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for n in z.namelist():
            raw = z.read(n)
            # 2019년 이전 보고서는 EUC-KR (XML 머리에 encoding 표기)
            enc = re.search(rb'encoding="([^"]+)"', raw[:200])
            enc = enc.group(1).decode() if enc else "utf-8"
            try:
                html = raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                html = raw.decode("cp949", "ignore")
            out.append(BeautifulSoup(html, "html.parser").get_text("\n", strip=True))
    return "\n".join(out)


def investees(text: str) -> list[str]:
    names = []
    for m in _SECTION.finditer(text):
        body = text[m.end():]
        nxt = _NEXT.search(body)
        for line in body[:nxt.start() if nxt else 20000].split("\n"):
            line = line.strip()
            if not _CORP.search(line) or _NOT_CO.search(line) or len(line) > 40 or "당사" in line:
                continue
            # '(주) 알앤오식품(*4)', '㈜이노즈(전 지디센트론)' → 알앤오식품 / 이노즈
            name = re.sub(r"\(\*\d+\)|\((전|구)\s*[^)]*\)|\(\s*주\s*\)|㈜|주식회사", " ", line)
            name = re.sub(r"\s+", " ", name).strip(" .,")
            if 2 <= len(name) <= 25 and not re.fullmatch(r"[\d,.\-%() ]+", name) and name not in names:
                names.append(name)
    return names


def run(con, only: list[str] | None = None) -> None:
    if not config.DART_KEY:
        raise SystemExit("DART_KEY 가 .env 에 없습니다")
    review = {}
    with db.run_log(con, "dart-holdings") as st:
        for code, (label, univ) in HOLDINGS.items():
            if only and code not in only and label not in only:
                continue
            rep = latest_audit(code)
            if not rep:
                print(f"{label}: 감사보고서 없음")
                continue
            rcept, title = rep
            src = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"
            names = investees(report_text(rcept))
            tag = f"{label} 출자 ({title.split('(')[-1].rstrip(')')})"
            hit = new = 0
            for n in names:
                cid = db.find_company(con, n)
                if cid is None and is_health(n):
                    cid = db.get_or_create_company(con, n, source_url=src)
                    con.execute("UPDATE companies SET confidence=coalesce(confidence,'하') WHERE id=?", (cid,))
                    db.set_field(con, cid, "affiliation", univ, "dart-holdings", src, "하")
                    new += 1
                if cid is None:
                    review.setdefault(label, []).append(n)
                    continue
                db.add_tag(con, cid, "program", tag, src)
                hit += 1
            st["docs"] += 1
            st["facts"] += hit
            con.commit()
            print(f"{label} {title}: 투자기업 {len(names)}곳 → DB 연결 {hit}곳 (새로 만든 곳 {new}), 미확인 {len(names) - hit}곳")
    path = config.DATA_DIR / "dart_holdings_review.json"
    if path.exists():  # --only 로 일부만 돌려도 다른 기술지주 명단은 남긴다
        review = {**json.loads(path.read_text(encoding="utf-8")), **review}
    path.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"이름만으로 판단 못 한 회사 → {path}")
