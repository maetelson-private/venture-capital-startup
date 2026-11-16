"""지불자(payer) × 과제(task) × 사용자(customer) 태그 — 근거 문장이 있을 때만 채운다.

근거 원천은 두 가지뿐:
  1) raw_documents 수집 원문 — 기업에 연결된 문서 전체 + 연결 안 된 문서 중 사명이 나오는 부분
  2) 기업 홈페이지 — fetch.fetch_doc 그대로(robots·호스트 2초 간격·30일 캐시), 첫 화면 + 회사소개·서비스 하위 3쪽
한 줄 소개(service_summary 등)는 근거로 쓰지 않는다.

Claude 가 돌려준 근거 문장은 원문에 실제로 있는지(공백 무시 부분일치) 확인하고, 없으면 버린다 → '미상'.
결과는 company_tags(kind=payer/task/customer)에, 근거는 tag_evidence 에 쌓는다.
manual 근거가 반영된 kind 는 건드리지 않는다.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .. import config, db, fetch, llm

PAYERS = ["건보 급여", "비급여 본인부담", "실손·민간보험", "장기요양보험", "병의원 예산", "기업 복지",
          "제약·기기사", "공공·지자체", "소비자 직접", "미상"]
UNKNOWN = "미상"
KINDS = ("payer", "task", "customer")
SOURCE = "payer-llm"

# 기존 payer 값 → 새 목록. None 이면 원문을 다시 읽어 판정(B2B 는 병의원·기업복지·제약 중 무엇인지 모름)
LEGACY_MAP = {"건보 급여": "건보 급여", "비급여": "비급여 본인부담", "실손·민간보험": "실손·민간보험",
              "소비자 직접": "소비자 직접", "B2G": "공공·지자체", "B2B": None}
B2B_FAMILY = {"병의원 예산", "기업 복지", "제약·기기사"}

SKIP_HOSTS = ("instagram.com", "facebook.com", "linkedin.com", "youtube.com", "blog.naver.com",
              "notion.site", "linktr.ee", "play.google.com", "apps.apple.com")
SUBPAGE = re.compile(r"about|company|intro|overview|service|product|solution|business|"
                     r"회사|소개|기업|서비스|제품|솔루션|사업", re.I)
NOT_SUBPAGE = re.compile(r"polic|terms|privacy|약관|개인정보|recruit|career|채용|login|signup|회원|notice|공지|"
                         r"news|뉴스|press|blog|faq|contact|문의|\.pdf$", re.I)
# 과제 구절에 들어가면 안 되는 '수단' 단어 (검사·표시용)
MEANS = re.compile(r"AI|인공지능|딥러닝|머신러닝|알고리즘|플랫폼|솔루션|앱|어플|소프트웨어|SW|디지털|웨어러블|"
                   r"센서|기기|디바이스|로봇|빅데이터|데이터 분석|클라우드|메타버스|VR|AR", re.I)

DOC_CHARS = 7000        # 문서 하나 최대 글자
WIN = 700               # 사명 언급 앞뒤 글자
TOTAL_CHARS = 30000     # 한 기업 프롬프트 원문 합계


# ───────────── 근거 원문 모으기 ─────────────
def _clean_name(s: str) -> str:
    return re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s+", "", s or "")


def _names(con, cid: int, name: str) -> list[str]:
    out = {_clean_name(name)}
    for r in con.execute("SELECT alias FROM company_aliases WHERE company_id=?", (cid,)):
        out.add(_clean_name(r["alias"]))
    # 너무 짧은 이름(2글자 이하 한글, 3글자 이하 영문)은 다른 단어에 섞여 오탐이 많아 뺀다
    return [n for n in out if (len(n) >= 3 and re.search(r"[가-힣]", n)) or len(n) >= 4]


def _windows(body: str, names: list[str]) -> str:
    spans = []
    for n in names:
        for m in re.finditer(r"(?<![가-힣A-Za-z0-9])" + re.escape(n), body):
            spans.append((max(0, m.start() - WIN), m.end() + WIN))
            if len(spans) >= 4:
                break
    if not spans:
        return ""
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return "\n…\n".join(body[a:b] for a, b in merged)[:DOC_CHARS]


def _norm_url(u: str | None) -> str | None:
    u = (u or "").strip()
    if not u or " " in u:
        return None
    if not u.startswith("http"):
        u = "http://" + u
    host = urlparse(u).netloc.lower()
    if not host or any(h in host for h in SKIP_HOSTS):
        return None
    return u


def homepage_docs(con, cid: int, url: str, dry_run: bool) -> list[dict]:
    """첫 화면 + 회사소개·서비스 하위 최대 3쪽. 캐시 적중 시엔 지난번 연결해둔 하위 쪽을 쓴다."""
    links: list = []
    home = fetch.fetch_doc(con, url, "homepage", links=links)
    if not home:
        return []
    docs = [home]
    if links:
        subs = [u for u, t in links if u.rstrip("/") != url.rstrip("/") and (SUBPAGE.search(t) or SUBPAGE.search(u))
                and not NOT_SUBPAGE.search(t + " " + u)]
        for u in subs[:3]:
            d = fetch.fetch_doc(con, u, "homepage")
            if d:
                docs.append(d)
    else:  # 캐시 적중 → 예전에 연결한 홈페이지 하위 쪽
        host = urlparse(url).netloc.removeprefix("www.")
        for r in con.execute("""SELECT d.* FROM raw_documents d JOIN doc_company_links l ON l.doc_id=d.id
                                WHERE l.company_id=? AND d.source='homepage'""", (cid,)):
            if urlparse(r["url"]).netloc.removeprefix("www.") == host and r["id"] != home["id"]:
                docs.append(dict(r))
    if not dry_run:
        for d in docs:
            con.execute("INSERT OR IGNORE INTO doc_company_links(doc_id, company_id) VALUES (?,?)", (d["id"], cid))
        con.commit()
    return docs


class Corpus:
    """연결 안 된 원문에서 사명 검색용으로 본문을 한 번만 읽어 둔다."""

    def __init__(self, con):
        self.docs = [dict(r) for r in con.execute(
            "SELECT id, url, title, body FROM raw_documents WHERE body IS NOT NULL AND source!='homepage'")]

    def mentions(self, names: list[str], exclude: set[int]) -> list[dict]:
        out = []
        for d in self.docs:
            if d["id"] in exclude:
                continue
            w = _windows(d["body"], names)
            if w:
                out.append({**d, "body": w})
        return out


def gather(con, corpus: Corpus, cid: int, name: str, url: str | None, fetch_home=True,
           dry_run=False) -> list[dict]:
    """[{id,url,title,body,kind}] — kind: article(연결 원문) / mention(사명 언급 부분) / homepage"""
    sources = []
    linked = [dict(r) for r in con.execute(
        """SELECT d.id, d.url, d.title, d.body, d.source FROM raw_documents d
           JOIN doc_company_links l ON l.doc_id=d.id WHERE l.company_id=? AND d.body IS NOT NULL""", (cid,))]
    for d in linked:
        if d["source"] != "homepage":
            sources.append({**d, "body": d["body"][:DOC_CHARS], "kind": "article"})
    names = _names(con, cid, name)
    for d in corpus.mentions(names, {d["id"] for d in linked})[:5]:
        sources.append({**d, "kind": "mention"})
    u = _norm_url(url)
    if u and fetch_home:
        for d in homepage_docs(con, cid, u, dry_run):
            if d.get("body") and len(d["body"]) > 40:
                sources.append({**d, "body": d["body"][:DOC_CHARS], "kind": "homepage"})
    # 같은 URL 중복 제거 + 전체 길이 제한
    seen, out, total = set(), [], 0
    for s in sources:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        if total + len(s["body"]) > TOTAL_CHARS:
            s = {**s, "body": s["body"][:max(0, TOTAL_CHARS - total)]}
        if len(s["body"]) < 40:
            continue
        out.append(s)
        total += len(s["body"])
    return out


# ───────────── Claude 판정 ─────────────
SYSTEM = f"""너는 헬스케어 스타트업의 '누가 돈을 내는가 / 누구의 어떤 문제인가 / 누가 실제로 쓰는가'를 원문 근거로만 판정하는 조사원이다.

payer (1~2개): 아래 목록에서만 고른다.
{' / '.join(PAYERS)}
- 건보 급여: 건강보험 수가·급여 적용(신의료기술·혁신의료기술 수가 포함)이 원문에 적혀 있을 때
- 비급여 본인부담: 비급여로 환자가 병원에서 내는 구조
- 병의원 예산: 병원·의원이 구매·구독·도입 비용을 낸다 (병원에 판매·공급·도입)
- 기업 복지: 일반 기업이 임직원 복지로 산다
- 제약·기기사: 제약사·의료기기사·CRO 가 계약·구매한다
- 공공·지자체: 정부·지자체·보건소·공공기관이 사업비로 산다 (R&D 과제비·정부 지원금은 지불자가 아니다)
- 소비자 직접: 개인이 앱 구독·제품 구매로 직접 낸다
- 실손·민간보험, 장기요양보험: 그 보험이 비용을 낸다고 적혀 있을 때
- 판단 근거 문장이 원문에 없으면 반드시 "{UNKNOWN}". 서비스 설명만 보고 '병원용이니 병원이 내겠지' 식으로 짐작하지 않는다.
  판매·도입·공급·계약·구독·수가·납품·구매처럼 돈의 흐름을 말하는 문장이 있어야 한다.

task (1개): 누구의 어떤 문제를 푸는지 한 구절(25자 안팎). 수단(AI, 앱, 플랫폼, 기기, 웨어러블, 데이터 분석 등)을 넣지 않는다.
  나쁜 예: "AI 영상 분석", "디지털 치료제 플랫폼"  좋은 예: "응급실 뇌출혈 판독 지연", "소아 ADHD 치료 지속률 저하"
  원문에서 문제·대상 질환·대상자를 확인할 수 없으면 "{UNKNOWN}".

customer (1개): 돈 내는 쪽이 아니라 실제로 쓰는 사람·조직 (예: "영상의학과 전문의", "당뇨 환자", "요양보호사"). 근거 없으면 "{UNKNOWN}".

근거 규칙:
- quote 는 원문 문장을 한 글자도 바꾸지 말고 그대로 복사한다 (요약·번역·연결 금지, 120자 이내로 잘라도 됨).
- source 는 그 문장이 있는 원문 번호 (예: "S2").
- 원문에 여러 회사가 나오면 대상 회사에 관한 문장만 쓴다. 투자사·협력사·다른 회사 이야기는 근거가 아니다.
- confidence: 상 = 문장이 그 값을 직접 말함 / 중 = 돈의 흐름·사용 장면이 분명히 드러나지만 표현이 간접적 / 하 = 약한 단서.
- "{UNKNOWN}" 이면 quote·source 는 빈 문자열, confidence 도 빈 문자열."""

SKELETON = json.dumps({
    "payer": [{"value": "", "quote": "", "source": "", "confidence": ""}],
    "task": {"value": "", "quote": "", "source": "", "confidence": ""},
    "customer": {"value": "", "quote": "", "source": "", "confidence": ""},
}, ensure_ascii=False)


def _prompt(name: str, sources: list[dict], only_payer: set | None = None) -> str:
    parts = [f"[S{i}] ({s['kind']}) {s['url']}\n{s.get('title') or ''}\n{s['body']}" for i, s in enumerate(sources, 1)]
    extra = ""
    if only_payer:
        extra = (f"\n이 기업의 이전 지불자 값은 'B2B' 였다. payer 를 {' / '.join(sorted(only_payer))} 중에서 근거가 있을 때만 고르고, "
                 f"근거가 없으면 \"{UNKNOWN}\".")
    return (f"대상 회사: {name}\n\n<sources>\n" + "\n\n".join(parts) + "\n</sources>\n" + extra +
            f"\n\n아래 키를 가진 JSON 객체 하나만 출력해.\n{SKELETON}")


def _ask(name: str, sources: list[dict], only_payer: set | None = None) -> dict | None:
    client = llm._client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY 가 없습니다")
    resp = client.messages.create(model=llm.MODEL, max_tokens=2000, system=SYSTEM,
                                  output_config={"effort": "low"},
                                  messages=[{"role": "user", "content": _prompt(name, sources, only_payer)}])
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _squash(s: str) -> str:
    return re.sub(r"[\s\"'“”‘’·•\-–—]", "", s or "")


def _quote_ok(quote: str, body: str) -> bool:
    """근거 문장이 원문에 실제로 있는가. 말줄임(…/...)으로 나눈 조각이 모두 있어야 한다."""
    parts = [p for p in re.split(r"…|\.\.\.", quote or "") if len(_squash(p)) >= 6]
    if not parts:
        return False
    b = _squash(body)
    return all(_squash(p) in b for p in parts)


def _check(item: dict | None, sources: list[dict], allowed: set | None = None) -> dict:
    """한 값 검증 → {value, quote, url, confidence, note}"""
    unk = {"value": UNKNOWN, "quote": "", "url": None, "confidence": None, "note": "no_evidence"}
    if not item:
        return unk
    v = (item.get("value") or "").strip()
    if not v or v == UNKNOWN or (allowed is not None and v not in allowed):
        return unk
    m = re.search(r"\d+", item.get("source") or "")
    if not m or not (1 <= int(m.group()) <= len(sources)):
        return {**unk, "note": "bad_source"}
    src = sources[int(m.group()) - 1]
    q = (item.get("quote") or "").strip()
    if not _quote_ok(q, src["body"]):
        return {**unk, "note": "quote_not_found"}
    conf = item.get("confidence") if item.get("confidence") in ("상", "중", "하") else "하"
    return {"value": v, "quote": q, "url": src["url"], "confidence": conf, "note": src["kind"]}


def judge(name: str, sources: list[dict], only_payer: set | None = None, raw: dict | None = None) -> dict:
    """{payer:[…], task:{…}, customer:{…}} — 검증을 통과한 값만, 나머지는 미상.
    raw 를 주면 API 를 부르지 않고 그 답(세션에서 판정한 JSON)을 같은 규칙으로 검증한다."""
    if not sources:
        u = {"value": UNKNOWN, "quote": "", "url": None, "confidence": None, "note": "no_source"}
        return {"payer": [u], "task": u, "customer": u}
    if raw is None:
        raw = _ask(name, sources, only_payer) or {}
    allowed = (only_payer or set(PAYERS)) - {UNKNOWN}
    payers = [_check(p, sources, allowed) for p in (raw.get("payer") or [])[:2]]
    payers = [p for p in payers if p["value"] != UNKNOWN] or [
        payers[0] if payers else {"value": UNKNOWN, "quote": "", "url": None, "confidence": None, "note": "no_evidence"}]
    dedup = {p["value"]: p for p in payers}
    return {"payer": list(dedup.values()), "task": _check(raw.get("task"), sources),
            "customer": _check(raw.get("customer"), sources)}


# ───────────── 저장 ─────────────
def _manual_kinds(con, cid: int) -> set[str]:
    return {r["kind"] for r in con.execute(
        "SELECT DISTINCT kind FROM tag_evidence WHERE company_id=? AND applied=1 AND source='manual'", (cid,))}


def write(con, cid: int, res: dict, source=SOURCE) -> None:
    skip = _manual_kinds(con, cid)
    for kind in KINDS:
        if kind in skip:
            continue
        vals = res[kind] if isinstance(res[kind], list) else [res[kind]]
        # 지금 붙어 있는 값 중 근거 기록이 없는 것(뉴스 추출 등 이전 값)은 legacy 로 남겨 둔다
        for t in con.execute("SELECT value, source_url FROM company_tags WHERE company_id=? AND kind=?", (cid, kind)):
            has = con.execute("SELECT 1 FROM tag_evidence WHERE company_id=? AND kind=? AND value=?",
                              (cid, kind, t["value"])).fetchone()
            if not has:
                con.execute("""INSERT INTO tag_evidence(company_id, kind, value, source, source_url, note, applied)
                               VALUES (?,?,?,?,?,?,0)""", (cid, kind, t["value"], "legacy-news", t["source_url"], "legacy"))
        con.execute("DELETE FROM company_tags WHERE company_id=? AND kind=?", (cid, kind))
        con.execute("UPDATE tag_evidence SET applied=0 WHERE company_id=? AND kind=?", (cid, kind))
        for v in vals:
            con.execute("""INSERT INTO tag_evidence(company_id, kind, value, quote, source, source_url, confidence, note, applied)
                           VALUES (?,?,?,?,?,?,?,?,1)""",
                        (cid, kind, v["value"], v["quote"] or None, source, v["url"], v["confidence"], v["note"]))
            db.add_tag(con, cid, kind, v["value"], v["url"])


# ───────────── 대상·실행 ─────────────
def targets(con, ids: list[int] | None = None, redo=False) -> list[dict]:
    where = "c.hard_filter='통과' AND c.scope='포함'"
    if ids:
        where = f"c.id IN ({','.join(str(int(i)) for i in ids)})"
    elif not redo:
        where += f" AND c.id NOT IN (SELECT company_id FROM tag_evidence WHERE kind='payer' AND source='{SOURCE}')"
    rows = con.execute(f"""SELECT c.id, c.name, c.url,
              (SELECT count(*) FROM funding_rounds r WHERE r.company_id=c.id) AS n_rounds,
              (SELECT count(*) FROM doc_company_links l WHERE l.company_id=c.id) AS n_docs
            FROM companies c WHERE {where}
            ORDER BY (n_rounds > 0), (c.url IS NULL OR c.url=''), n_docs DESC, c.id""").fetchall()
    return [dict(r) for r in rows]


def run(con, limit: int | None = None, dry_run=False, ids: list[int] | None = None, redo=False,
        workers: int = 4, only_sourced=False, fetch_home=True) -> list[dict]:
    corpus = Corpus(con)
    rows = targets(con, ids, redo)
    print(f"대상 {len(rows)}곳 (투자 이력 없음 {sum(not r['n_rounds'] for r in rows)}곳)")
    jobs = []
    for r in rows:
        if limit and len(jobs) >= limit:
            break
        srcs = gather(con, corpus, r["id"], r["name"], r["url"], fetch_home, dry_run)
        if only_sourced and not srcs:
            continue
        jobs.append((r, srcs))
        print(f"  · {r['name']}: 원문 {len(srcs)}건 "
              f"({', '.join(sorted({s['kind'] for s in srcs})) or '없음'})", flush=True)
    results = []
    with ThreadPoolExecutor(workers) as ex:
        futs = [(r, srcs, ex.submit(judge, r["name"], srcs)) for r, srcs in jobs]
        for r, srcs, f in futs:
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001 — 한 곳 실패해도 계속
                print(f"  ! {r['name']}: {e}")
                continue
            results.append({"company": r, "n_sources": len(srcs),
                            "source_kinds": sorted({s["kind"] for s in srcs}), **res})
    if not dry_run:
        with db.run_log(con, SOURCE, f"n={len(results)}") as st:
            for x in results:
                write(con, x["company"]["id"], x)
                st["docs"] += 1
                st["facts"] += sum(1 for k in KINDS for v in (x[k] if isinstance(x[k], list) else [x[k]])
                                   if v["value"] != UNKNOWN)
            con.commit()
    return results


# ───────────── 기존 payer 값 옮기기 ─────────────
def _legacy_rows(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT t.company_id, c.name, t.value, t.source_url FROM company_tags t JOIN companies c ON c.id=t.company_id "
        f"WHERE t.kind='payer' AND t.value NOT IN ({','.join('?' * len(PAYERS))}) ORDER BY t.company_id", PAYERS)]


def _b2b_sources(con, url: str) -> list[dict]:
    doc = con.execute("SELECT id, url, title, body FROM raw_documents WHERE url=?", (url,)).fetchone()
    if not doc or not doc["body"]:
        return []
    return [{**dict(doc), "body": doc["body"][:DOC_CHARS], "kind": "article"}]


def migrate(con, dry_run=False, answers: dict | None = None) -> list[dict]:
    """기존 payer 태그(B2B·B2G·비급여…)를 새 목록으로. B2B 는 그 태그의 원문 기사를 다시 읽어 판정.
    answers 를 주면 API 대신 세션 판정 답(키 'b2b|기업ID|기사URL')을 같은 규칙으로 검증해 쓴다."""
    rows = _legacy_rows(con)
    log = []

    def one(r):
        new = LEGACY_MAP.get(r["value"], "")
        if new:
            return r, {"value": new, "quote": "", "url": r["source_url"], "confidence": "하", "note": "migrate:rename"}
        srcs = _b2b_sources(con, r["source_url"])
        if not srcs:
            return r, {"value": UNKNOWN, "quote": "", "url": r["source_url"], "confidence": None, "note": "migrate:no_source"}
        raw = None if answers is None else (answers.get(f"b2b|{r['company_id']}|{r['source_url']}") or {})
        res = judge(r["name"], srcs, only_payer=B2B_FAMILY, raw=raw)
        p = res["payer"][0]
        return r, {**p, "url": p["url"] or r["source_url"], "note": f"migrate:{p['note']}"}

    results = [one(r) for r in rows]  # B2B 기사 판정은 수십 건이라 순차로 충분
    for r, new in results:
        log.append({"company_id": r["company_id"], "name": r["name"], "old": r["value"], "new": new["value"],
                    "quote": new["quote"], "url": new["url"], "note": new["note"]})
        if dry_run:
            continue
        cid = r["company_id"]
        if "payer" in _manual_kinds(con, cid):
            continue
        con.execute("""INSERT INTO tag_evidence(company_id, kind, value, source, source_url, note, applied)
                       VALUES (?,?,?,?,?,?,0)""", (cid, "payer", r["value"], "legacy-news", r["source_url"], "legacy"))
        con.execute("DELETE FROM company_tags WHERE company_id=? AND kind='payer' AND value=?", (cid, r["value"]))
        exists = con.execute("SELECT 1 FROM company_tags WHERE company_id=? AND kind='payer' AND value=?",
                             (cid, new["value"])).fetchone()
        if new["value"] == UNKNOWN and con.execute(
                "SELECT 1 FROM company_tags WHERE company_id=? AND kind='payer'", (cid,)).fetchone():
            continue  # 다른 값이 이미 있으면 미상은 붙이지 않음
        if not exists:
            con.execute("""INSERT INTO tag_evidence(company_id, kind, value, quote, source, source_url, confidence, note, applied)
                           VALUES (?,?,?,?,?,?,?,?,1)""",
                        (cid, "payer", new["value"], new["quote"] or None, "migrate", new["url"], new["confidence"], new["note"]))
            db.add_tag(con, cid, "payer", new["value"], new["url"])
    if not dry_run:
        con.execute("DELETE FROM company_tags WHERE kind='payer' AND value=? AND company_id IN "
                    "(SELECT company_id FROM company_tags WHERE kind='payer' AND value!=?)", (UNKNOWN, UNKNOWN))
        con.commit()
    return log


# ───────────── API 없이: 원문 내보내기 → 세션에서 판정 → 넣기 ─────────────
def dump(con, path: Path, limit=None, ids=None, redo=False, only_sourced=False, fetch_home=True,
         with_b2b=False) -> int:
    """판정할 원문을 JSON 으로. 키: 기업 ID 문자열 / 'b2b|기업ID|기사URL'(이관용)."""
    corpus = Corpus(con)
    out = {"system": SYSTEM, "skeleton": SKELETON, "items": {}}
    n = 0
    for r in targets(con, ids, redo):
        if limit and n >= limit:
            break
        srcs = gather(con, corpus, r["id"], r["name"], r["url"], fetch_home, dry_run=False)
        if only_sourced and not srcs:
            continue
        n += 1
        out["items"][str(r["id"])] = {
            "name": r["name"], "n_rounds": r["n_rounds"], "mode": "full",
            "sources": [{"S": f"S{i}", **{k: s.get(k) for k in ("kind", "url", "title", "body")}}
                        for i, s in enumerate(srcs, 1)]}
    if with_b2b:
        for r in _legacy_rows(con):
            if r["value"] == "B2B":
                srcs = _b2b_sources(con, r["source_url"])
                if srcs:
                    out["items"][f"b2b|{r['company_id']}|{r['source_url']}"] = {
                        "name": r["name"], "mode": "b2b", "allowed_payer": sorted(B2B_FAMILY),
                        "sources": [{"S": "S1", **{k: srcs[0].get(k) for k in ("kind", "url", "title", "body")}}]}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{n}곳 (+이관 {len(out['items']) - n}건) → {path}")
    return n


def load(con, dump_path: Path, answers_path: Path, dry_run=False) -> list[dict]:
    """세션에서 쓴 답 {키: {payer, task, customer}} 를 API 판정과 똑같이 검증(근거 문장 원문 대조)해서 넣는다."""
    d = json.loads(dump_path.read_text(encoding="utf-8"))["items"]
    ans = json.loads(answers_path.read_text(encoding="utf-8"))
    results = []
    for key, it in d.items():
        if it["mode"] != "full" or key not in ans:
            continue
        srcs = it["sources"]
        res = judge(it["name"], srcs, raw=ans[key])
        r = con.execute("SELECT id, name, url, (SELECT count(*) FROM funding_rounds f WHERE f.company_id=c.id) n_rounds "
                        "FROM companies c WHERE id=?", (int(key),)).fetchone()
        results.append({"company": dict(r), "n_sources": len(srcs),
                        "source_kinds": sorted({s["kind"] for s in srcs}), **res})
    if not dry_run:
        with db.run_log(con, SOURCE + "-session", f"n={len(results)}") as st:
            for x in results:
                write(con, x["company"]["id"], x)
                st["docs"] += 1
            con.commit()
    return results


def fill_no_source(con, dry_run=False) -> int:
    """근거 원천(연결 원문·사명 언급·홈페이지 URL)이 하나도 없는 곳은 판정 없이 미상(no_source)."""
    corpus = Corpus(con)
    n = 0
    for r in targets(con):
        # 홈페이지는 캐시(30일)에서 다시 읽음. 글자가 거의 없는 JS 페이지도 원천 없음으로 친다
        if gather(con, corpus, r["id"], r["name"], r["url"], fetch_home=True, dry_run=True):
            continue  # 쓸 만한 원문이 있는데 아직 판정 전 → dump/load 대상
        n += 1
        if not dry_run:
            res = judge(r["name"], [])
            if _norm_url(r["url"]):  # URL 은 있으나 robots 금지·접속 실패·빈 페이지
                res = {k: ([{**v[0], "note": "homepage_failed"}] if isinstance(v, list) else {**v, "note": "homepage_failed"})
                       for k, v in res.items()}
            write(con, r["id"], res)
    if not dry_run:
        con.commit()
    print(f"원천 없음 → 미상 {n}곳" + (" (dry-run)" if dry_run else ""))
    return n


# ───────────── 표 출력 ─────────────
def _cell(v: dict) -> str:
    if v["value"] == UNKNOWN:
        return f"미상 ({v['note']})"
    return f"{v['value']} [{v['confidence']}]"


def dry_table(results: list[dict]) -> str:
    lines = ["| # | 기업 | 투자 | 원문 | payer | task | customer | 근거 (payer · task) |",
             "|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(results, 1):
        c = x["company"]
        p = "<br>".join(_cell(v) for v in x["payer"])
        t = _cell(x["task"]) + (" ⚠수단" if x["task"]["value"] != UNKNOWN and MEANS.search(x["task"]["value"]) else "")
        ev = []
        for v in x["payer"] + [x["task"]]:
            if v["quote"]:
                ev.append(f"“{v['quote'][:90]}” ({v['url']})")
        lines.append(f"| {i} | {c['name']} | {c['n_rounds'] or '-'} | {x['n_sources']} {'/'.join(x['source_kinds'])} | "
                     f"{p} | {t} | {_cell(x['customer'])} | {'<br>'.join(ev).replace('|', '/')} |")
    return "\n".join(lines)


def save_report(text: str, name: str) -> Path:
    p = config.DATA_DIR / name
    p.write_text(text, encoding="utf-8")
    return p


def cli(con, a) -> None:
    if a.step == "dump":
        dump(con, config.DATA_DIR / "payer_task_sources.json", a.limit, a.ids, a.redo, a.only_sourced,
             not a.no_fetch, a.with_b2b)
        return
    if a.step == "load":
        res = load(con, config.DATA_DIR / "payer_task_sources.json", Path(a.file), a.dry_run)
        t = dry_table(res)
        p = save_report(t, "payer_task_dryrun.md" if a.dry_run else "payer_task_last.md")
        print(t)
        print(f"→ {p}")
        return
    if a.step == "nosource":
        fill_no_source(con, a.dry_run)
        return
    if a.step == "migrate":
        answers = json.loads(Path(a.file).read_text(encoding="utf-8")) if a.file else None
        log = migrate(con, a.dry_run, answers)
        lines = ["| 기업 | 이전 | 새 값 | 판정 | 근거 |", "|---|---|---|---|---|"]
        lines += [f"| {x['name']} | {x['old']} | {x['new']} | {x['note']} | "
                  f"{(x['quote'][:80] + ' ') if x['quote'] else ''}{x['url'] or ''} |" for x in log]
        p = save_report("\n".join(lines), "payer_migration.md")
        print("\n".join(lines))
        print(f"\n{len(log)}건 → {p}" + (" (dry-run)" if a.dry_run else ""))
    elif a.step == "run":
        res = run(con, a.limit, a.dry_run, a.ids, a.redo, a.workers, a.only_sourced, not a.no_fetch)
        t = dry_table(res)
        p = save_report(t, "payer_task_dryrun.md" if a.dry_run else "payer_task_last.md")
        print(t if a.dry_run or len(res) <= 40 else f"{len(res)}곳 판정")
        print(f"→ {p}")
    elif a.step == "group":
        from .payer_task_report import group
        group(con, a.dry_run, a.file)
    elif a.step == "report":
        from .payer_task_report import report
        report(con)
