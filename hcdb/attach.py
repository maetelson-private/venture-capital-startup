"""공고·보도자료 첨부파일(HWP/HWPX/PDF/XLSX/DOCX) 다운로드 + 텍스트 추출.

정부 공고 명단은 대부분 첨부파일 안의 표로 나온다. 여기서는 '표 구조를 최대한 살린 텍스트'까지만 만들고,
기업명·대표·소속을 칸으로 나누는 일은 llm.py 가 한다.
다운로드 파일은 data/attachments/ 에 해시 이름으로 저장하고, 실행하지 않고 읽기만 한다.
"""
import hashlib
import io
import re
import struct
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from . import config
from .fetch import http_get

ATTACH_DIR = config.DATA_DIR / "attachments"
EXTS = (".hwp", ".hwpx", ".pdf", ".xlsx", ".xls", ".docx", ".csv")


# ───────────── 첨부 링크 찾기 ─────────────
def find_attachments(page_url: str, html: str) -> list[tuple[str, str]]:
    """게시판 상세 페이지에서 (파일명, 다운로드 URL) 목록. 확장자 또는 download 류 링크를 잡는다."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href, label = a["href"], a.get_text(" ", strip=True)
        if href.startswith("javascript"):
            # 예: javascript:fn_download('123') — 사이트별 처리가 필요해 일단 건너뜀
            continue
        url = urljoin(page_url, href)
        name = label or unquote(Path(urlparse(url).path).name)
        hit = any(e in (name + url).lower() for e in EXTS) or re.search(r"down|file|atch", href, re.I)
        if hit and url not in seen and not re.search(r"미리보기|바로보기|preview", label):
            seen.add(url)
            out.append((name, url))
    return out


def download(url: str, referer: str | None = None) -> tuple[Path, str]:
    """첨부 다운로드 → (저장 경로, 원래 파일명). 같은 URL 은 다시 받지 않음."""
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    hit = list(ATTACH_DIR.glob(key + ".*"))
    if hit:
        return hit[0], hit[0].name
    r = http_get(url, headers={"Referer": referer} if referer else None)
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", cd, re.I)
    raw = (m.group(1) or m.group(2)) if m else Path(urlparse(url).path).name
    try:
        fname = unquote(raw.encode("latin-1").decode("utf-8") if m and not m.group(1) else raw)
    except (UnicodeDecodeError, UnicodeEncodeError):
        fname = unquote(raw)
    ext = Path(fname).suffix.lower() or _sniff(r.content)
    path = ATTACH_DIR / f"{key}{ext}"
    path.write_bytes(r.content)
    return path, fname


def _sniff(b: bytes) -> str:
    if b[:4] == b"%PDF":
        return ".pdf"
    if b[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".hwp"  # OLE: HWP5 (또는 구형 xls/doc)
    if b[:2] == b"PK":
        names = zipfile.ZipFile(io.BytesIO(b)).namelist()
        if any(n.startswith("Contents/") for n in names):
            return ".hwpx"
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        if any(n.startswith("word/") for n in names):
            return ".docx"
        return ".zip"
    return ".bin"


# ───────────── 텍스트 추출 ─────────────
def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".hwp":
        return hwp5_text(path)
    if ext == ".hwpx":
        return hwpx_text(path)
    if ext == ".pdf":
        return pdf_text(path)
    if ext in (".xlsx", ".xlsm"):
        return xlsx_text(path)
    if ext == ".docx":
        return docx_text(path)
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp949"):
            try:
                return path.read_text(encoding=enc)
            except UnicodeDecodeError:
                pass
    if ext == ".zip":
        out = []
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if Path(n).suffix.lower() in EXTS:
                    sub = ATTACH_DIR / (path.stem + "_" + hashlib.sha1(n.encode()).hexdigest()[:8] + Path(n).suffix.lower())
                    sub.write_bytes(z.read(n))
                    out.append(f"### {n}\n{extract_text(sub)}")
        return "\n\n".join(out)
    raise ValueError(f"지원하지 않는 형식: {path.name}")


# HWP5: OLE 컨테이너 안의 BodyText/SectionN 스트림. 레코드 단위로 PARA_TEXT(태그 67)만 모은다.
_HWPTAG_PARA_TEXT = 67
_CTRL_8 = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}  # 8 wchar 짜리 제어문자


def hwp5_text(path: Path) -> str:
    import olefile
    ole = olefile.OleFileIO(str(path))
    header = ole.openstream("FileHeader").read()
    compressed = bool(header[36] & 1)
    sections = sorted((e for e in ole.listdir() if e[0] == "BodyText"), key=lambda e: int(e[1][7:]))
    paras = []
    for sec in sections:
        data = ole.openstream(sec).read()
        if compressed:
            data = zlib.decompress(data, -15)
        i = 0
        while i + 4 <= len(data):
            h = struct.unpack_from("<I", data, i)[0]
            tag, size = h & 0x3FF, (h >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = struct.unpack_from("<I", data, i)[0]
                i += 4
            if tag == _HWPTAG_PARA_TEXT:
                paras.append(_hwp_chars(data[i:i + size]))
            i += size
    ole.close()
    return "\n".join(p for p in paras if p.strip())


def _hwp_chars(b: bytes) -> str:
    out, i = [], 0
    while i + 2 <= len(b):
        c = struct.unpack_from("<H", b, i)[0]
        if c in _CTRL_8:
            i += 16
            continue
        if c < 32:
            out.append("\t" if c == 9 else "")
        else:
            out.append(chr(c))
        i += 2
    return "".join(out)


def hwpx_text(path: Path) -> str:
    """HWPX: zip 안 Contents/section*.xml. 표는 행마다 ' | ' 로 셀을 이어 한 줄로 만든다."""
    from xml.etree import ElementTree as ET
    lines = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(x for x in z.namelist() if re.match(r"Contents/section\d+\.xml", x)):
            root = ET.fromstring(z.read(n))
            _hwpx_walk(root, lines)
    return "\n".join(lines)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _hwpx_walk(node, lines):
    for ch in node:
        t = _local(ch.tag)
        if t == "tr":
            cells = []
            for tc in ch.iter():
                if _local(tc.tag) == "tc":
                    cells.append(" ".join("".join(x.itertext()) for x in tc.iter() if _local(x.tag) == "t").strip())
            lines.append(" | ".join(cells))
        elif t == "p" and not any(_local(x.tag) == "tbl" for x in ch.iter()):
            s = "".join("".join(x.itertext()) for x in ch.iter() if _local(x.tag) == "t").strip()
            if s:
                lines.append(s)
        else:
            _hwpx_walk(ch, lines)


def pdf_text(path: Path) -> str:
    """표는 pdfplumber 로 행 단위, 나머지 본문은 PyMuPDF."""
    import pdfplumber
    import pymupdf as fitz
    out = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            for tbl in page.extract_tables():
                for row in tbl:
                    out.append(" | ".join((c or "").replace("\n", " ").strip() for c in row))
    tables = "\n".join(out)
    with fitz.open(path) as doc:
        body = "\n".join(p.get_text() for p in doc)
    return (f"[표]\n{tables}\n\n[본문]\n{body}" if tables else body)


def xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"### 시트: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                out.append(" | ".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def docx_text(path: Path) -> str:
    from xml.etree import ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for p in root.iter():
        if _local(p.tag) == "p":
            s = "".join(x.text or "" for x in p.iter() if _local(x.tag) == "t")
            if s.strip():
                lines.append(s)
    return "\n".join(lines)
