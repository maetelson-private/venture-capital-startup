"""사명·금액·날짜 정규화."""
import re
from datetime import date

_CORP_TOKENS = [
    r"\(주\)", r"㈜", r"주식회사", r"\(유\)", r"유한회사", r"\(재\)", r"재단법인", r"\(사\)", r"사단법인",
    r"\binc\.?", r"\bco\.?,?\s*ltd\.?", r"\bltd\.?", r"\bcorp\.?", r"\bcorporation\b", r"\bllc\b", r"\bgmbh\b",
]
_CORP_RE = re.compile("|".join(_CORP_TOKENS), re.I)


def norm_name(name: str) -> str:
    """비교키. '(주)루닛'·'주식회사 루닛' → '루닛', 'Lunit Inc.' → 'lunit'."""
    if not name:
        return ""
    s = _CORP_RE.sub("", name)
    s = re.sub(r"[\s\-_·.,&'\"()\[\]]", "", s)
    return s.lower()


def norm_biz_no(s: str | None) -> str | None:
    if not s:
        return None
    d = re.sub(r"\D", "", str(s))
    return d if len(d) == 10 else None


_UNIT = {"조": 1_0000_0000_0000, "억": 1_0000_0000, "천만": 1000_0000, "백만": 100_0000, "만": 1_0000}


def parse_krw(text: str) -> int | None:
    """'120억 원', '1조 2,000억', '30억원', '5,000만원' → 원 단위 정수."""
    if not text:
        return None
    total, found = 0, False
    for num, unit in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(조|억|천만|백만|만)", text):
        total += float(num.replace(",", "")) * _UNIT[unit]
        found = True
    if found:
        return int(total)
    m = re.fullmatch(r"\s*(\d[\d,]*)\s*원?\s*", text)
    return int(m.group(1).replace(",", "")) if m else None


def norm_date(s: str | None) -> str | None:
    """'2021.3.5', '2021-03', '20210305', '2021년 3월 5일' → ISO. 일자 없으면 -01."""
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})\s*[.\-/년]?\s*(\d{1,2})?\s*[.\-/월]?\s*(\d{1,2})?", s)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2) or "1", m.group(3) or "1"
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None
