"""롱리스트 카드 HTML (한 파일, 오프라인에서 열림).

대상: 하드필터 '통과' + 범위 '포함'. 지불자·과제 묶음 필터(tags 명령 결과)도 있다. 카드마다 이름·설립·지역·한 줄 설명·키워드·신호(혁신의료기기·식약처 허가·
샌드박스·프로그램·투자사)를 보여주고, 검색·키워드·'첫 투자 후보만' 필터와 정렬을 브라우저에서 한다.
"""
import html
import json
import re
from datetime import datetime
from pathlib import Path

from . import config

_CORP = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사")


def _display(name: str) -> str:
    return re.sub(r"\s+", " ", _CORP.sub("", name)).strip() or name


def _first_sentence(s: str | None, n: int = 70) -> str:
    if not s:
        return ""
    # TIPS 소개문 "OO는 △△ 분야에서 '과제명'을 전개하는 기업입니다" → 과제명만
    m = re.search(r"분야에서\s*[‘'\"“](.+?)[’'\"”]\s*[을를]\s*전개", s)
    if m:
        s = m.group(1)
    s = re.split(r"(?<=[.다])\s", s.strip())[0]
    return s if len(s) <= n else s[: n - 1] + "…"


# 출처 표시: field_evidence.source → 카드에 보일 이름. 판정·보강용(사람·Claude 판단, 규칙)은 '출처'가 아니라서 뺀다
_NOT_SOURCE = {"claude-session", "rule", "llm-scope", "manual", "web-llm", "nts", "nps", "od:nps-founding",
               "kst-browser", "gist-browser", "snubh-browser", "enrich-web", "naver-news",
               "web-contact", "naver-web", "naver-local"}
SOURCE_LABELS = {
    "tips": "TIPS", "kbic": "KBIC", "dart": "DART", "dart-holdings": "기술지주 감사보고서(DART)",
    "news-llm": "언론 기사", "holding-news": "언론 기사", "briefing": "정책브리핑 보도자료",
    "notice:innov": "혁신의료기기 보도자료", "notice:sandbox": "규제샌드박스 보도자료", "notice:program": "선정 공고",
    "mfds-sw": "식약처 SW 의료기기 허가", "mfds-mnft": "식약처 제조업 허가", "mfds-innov": "식약처 혁신의료기기",
    "od:venture": "벤처기업명단", "od:nps": "국민연금 사업장", "od:cert": "창업기업확인서", "od:gmp": "의료기기 GMP",
    "od:techdev": "중기부 R&D 과제", "od:labco": "연구소기업", "od:kmedi": "대구경북첨복단지", "od:aiv": "AI바우처 공급기업",
    "od:15032487": "성남시 벤처기업", "od:15085792": "대구 의료R&D지구", "od:15149071": "경북 바이오헬스기업",
    "inc:bi": "창업보육센터 입주기업", "inc:dhnet": "디지털헬스산업협회", "inc:trials": "SW 의료기기 임상시험 승인",
    "inc:rdh": "연구중심병원 창업", "inc:yonsei": "연세대 창업지원단", "inc:snubh": "분당서울대병원 창업보육",
    "portfolio:dhp": "DHP 포트폴리오", "portfolio:dcamp": "D.CAMP", "portfolio:d2sf": "네이버 D2SF",
    "portfolio:futureplay": "퓨처플레이", "portfolio:lotte": "롯데벤처스", "portfolio:springcamp": "스프링캠프",
    "portfolio:mashup": "매쉬업벤처스", "portfolio:kakao": "카카오벤처스", "portfolio:iaccel": "인포뱅크 아이엑셀",
    "portfolio:clab": "삼성 C-Lab", "portfolio:kiac": "한국투자AC", "portfolio:etri": "ETRI홀딩스",
    "portfolio:theventures": "더벤처스", "portfolio:ccvc": "쿨리지코너", "portfolio:signite": "시그나이트",
    "portfolio:kingo": "킹고스프링", "cl:hyu": "한양대 스타트업", "cl:gbia": "김해의생명산업진흥원",
    "cl:kustartup": "고려대 KU Startup", "cl:kmedi_web": "K-MEDI hub", "cl:khu": "경희대 창업보육센터",
    "cl:gjtp": "광주TP", "cl:ewha": "이화여대 창업지원단", "cl:jbf": "전남바이오진흥원", "cl:knu": "경북대 창업지원단",
    "cl:gwtp": "강원TP", "cl:erica": "한양대 ERICA 창업보육",
}


def _source_label(code: str) -> str | None:
    if code in _NOT_SOURCE:
        return None
    if code in SOURCE_LABELS:
        return SOURCE_LABELS[code]
    if code.startswith("univ:"):  # 'univ:서울대기술지주 포트폴리오' 처럼 라벨이 코드에 들어 있음
        return code.split(":", 1)[1]
    return code


def _sources(con, cid: int) -> list[dict]:
    """처음 발견한 순서대로 [{t: 이름, u: 링크(있으면)}]. 같은 이름은 한 번만."""
    rows = con.execute("""SELECT source, min(id) first,
                                 max(CASE WHEN source_url LIKE 'http%' THEN source_url END) url
                          FROM field_evidence WHERE company_id=? GROUP BY source ORDER BY first""", (cid,)).fetchall()
    out, seen = [], set()
    for r in rows:
        lab = _source_label(r["source"])
        if lab and lab not in seen:
            seen.add(lab)
            out.append({"t": lab, "u": r["url"] or ""})
    return out


def _contacts(s: str | None) -> list[dict]:
    """contact_channel 요약 한 줄('메일 a, b · 전화 x · 문의 url · linkedin url')을 링크 목록으로."""
    out = []
    for part in (s or "").split(" · "):
        kind, _, rest = part.partition(" ")
        for v in ([x.strip() for x in rest.split(",")] if kind in ("메일", "전화") else [rest.strip()]):
            if not v:
                continue
            if kind == "메일":
                out.append({"k": "메일", "t": v, "u": "mailto:" + v})
            elif kind == "전화":
                out.append({"k": "전화", "t": v, "u": "tel:" + v.replace("-", "")})
            elif kind == "문의":
                out.append({"k": "문의", "t": "문의 페이지", "u": v})
            elif v.startswith("http"):
                out.append({"k": kind, "t": {"linkedin": "LinkedIn", "kakao": "카카오채널"}.get(kind, kind), "u": v})
    return out


def collect(con) -> list[dict]:
    out = []
    rows = con.execute("""SELECT * FROM v_companies WHERE hard_filter='통과' AND scope='포함'""").fetchall()
    for r in rows:
        cid = r["id"]
        tags = con.execute("SELECT kind, value FROM company_tags WHERE company_id=?", (cid,)).fetchall()
        kws, programs, ptc = [], [], {"payer": [], "task": [], "customer": [], "task_group": [], "customer_type": []}
        for t in tags:
            if t["kind"] in ptc:
                ptc[t["kind"]].append(t["value"])
            elif t["kind"].startswith("kw") and t["value"] not in kws:
                kws.append(t["value"])
            elif t["kind"] == "program":
                programs.append(t["value"])
        reg = con.execute("SELECT kind, count(*) n, min(event_date) d FROM regulatory WHERE company_id=? GROUP BY kind",
                          (cid,)).fetchall()
        signals = [{"t": f"{g['kind']}" + (f" {g['n']}건" if g["n"] > 1 else ""), "k": "reg"} for g in reg]
        signals += [{"t": p, "k": "prog"} for p in programs[:3]]
        # 지불자: 태그 근거(tag_evidence)로 상태를 나눈다 — 미처리 / 근거 없음 / 값(근거·추정(사용자 유형)·기본값(과제 묶음))
        pev = con.execute("""SELECT value, basis FROM tag_evidence WHERE company_id=? AND kind='payer' AND applied=1""",
                          (cid,)).fetchall()
        if not pev:
            payers = [{"v": "미처리", "b": "none"}]
        elif all(p["value"] in ("근거 없음", "미상") for p in pev):
            payers = [{"v": "근거 없음", "b": "none"}]
        else:
            payers = [{"v": p["value"], "b": p["basis"] if p["basis"] in ("추정", "기본값") else "근거"}
                      for p in pev if p["value"] not in ("근거 없음", "미상")]
        unknown = ("근거 없음", "미상")
        age_unverified = not r["is_pre_startup"] and (r["age_years"] is None or r["age_years"] < 0)
        out.append({
            "id": cid,
            "name": _display(r["name"]),
            "founded": (r["founded_date"] or "")[:7],
            # 설립일이 미래면(예정일이 설립일 칸에 들어간 경우) 음수 업력을 보이지 않는다
            "age": r["age_years"] if r["age_years"] is None or r["age_years"] >= 0 else None,
            "sido": (r["sido"] or "").replace("특별시", "").replace("광역시", "").replace("특별자치도", "").replace("도", "")[:4],
            "one": r["one_liner"] or _first_sentence(r["service_summary"]) or _first_sentence(r["bm_summary"]),
            "kws": kws[:5],
            # 지불자·과제·사용자 (tags 명령)
            "payer": payers,
            "task": next((v for v in ptc["task"] if v not in unknown), ""),
            "tgroup": (ptc["task_group"] or ["미분류"])[0],
            "user": next((v for v in ptc["customer"] if v not in unknown), ""),
            "pre": bool(r["is_pre_startup"]),
            "ctype": ptc["customer_type"],  # 고객 유형 (예: 치과) — 과제 묶음과 별개로 거름
            "unverified": age_unverified,
            "signals": signals,
            "src": _sources(con, cid),
            "investors": [x for x in (r["investor_names"] or "").split(",") if x][:4],
            "rounds": r["n_rounds"] or 0,
            "url": r["url"] or "",
            "contact": _contacts(r["contact_channel"]),
            "ceo": r["ceo"] or "",
            "conf": r["confidence"] or "",
            # 밸류 미상은 거의 전부라 표시하지 않고, 업력을 알 수 없을 때만 경고 (예비창업팀은 업력 판단 대상 아님)
            "check": age_unverified,
            # 경고가 붙는 카드에만 문구를 둔다 (예전엔 과거 설립일 카드에도 '미래' 문구가 데이터에 들어갔음)
            "reason": ("" if not age_unverified
                       else "업력 미확인 — 설립일 없음 (국민연금 대조로도 못 찾음)" if r["age_years"] is None
                       else f"설립일 {r['founded_date']} 이 미래 — 예정일로 보임, 확인 필요"),
            # 서비스 정보가 전혀 없는 곳(빈 KBIC 프로필 등)은 기본으로 숨김
            "thin": not (r["one_liner"] or r["service_summary"] or r["bm_summary"] or reg),
        })
    return out


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>헬스케어 롱리스트</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--sub:#6b6b66;--line:#e4e3de;--chip:#efeee9;--accent:#2f6f5e;--accent-soft:#e3f0ec;
--warn:#9a6a12;--warn-soft:#f8efdc;--reg:#3b5ba5;--reg-soft:#e6ecf8;--inv:#8a3b5a;--inv-soft:#f6e6ed}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151514;--card:#1f1f1d;--ink:#ecebe6;--sub:#a3a29b;--line:#33332f;--chip:#2a2a27;
--accent:#7cc4ae;--accent-soft:#1f3a33;--warn:#e0b25b;--warn-soft:#3a3020;--reg:#9db4ef;--reg-soft:#232c42;--inv:#e39ab8;--inv-soft:#3d2430}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"Apple SD Gothic Neo","Pretendard","Malgun Gothic",sans-serif}
header{padding:28px 16px 8px;max-width:1240px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}.meta{color:var(--sub);font-size:13px}
.bar{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line)}
.controls{max-width:1240px;margin:0 auto;padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search],select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1 1 220px;min-width:0}label.tg{display:flex;gap:6px;align-items:center;font-size:14px;color:var(--sub);cursor:pointer}
.kwbar{max-width:1240px;margin:0 auto;padding:0 16px 12px;display:flex;flex-wrap:wrap;gap:6px}
.kwbar button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--card);color:var(--sub);border-radius:999px;padding:3px 10px;cursor:pointer}
.kwbar button.on{background:var(--accent);border-color:var(--accent);color:var(--card)}
main{max-width:1240px;margin:0 auto;padding:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:8px}
.top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.name{font-weight:700;font-size:16px}
.name a{color:inherit;text-decoration:none}.name a:hover{text-decoration:underline}
.badge{font-size:11px;border-radius:6px;padding:2px 7px;white-space:nowrap}.first{background:var(--accent-soft);color:var(--accent)}
.inv{background:var(--inv-soft);color:var(--inv)}.facts{font-size:12px;color:var(--sub)}.one{font-size:14px}
.chips{display:flex;flex-wrap:wrap;gap:5px}.chip{font-size:12px;background:var(--chip);border-radius:6px;padding:2px 7px}
.sig{font-size:11px;border-radius:6px;padding:2px 7px}.sig.reg{background:var(--reg-soft);color:var(--reg)}.sig.prog{background:var(--chip);color:var(--sub)}
.ptc{font-size:12px;color:var(--sub);display:flex;flex-direction:column;gap:2px}.ptc b{color:var(--ink);font-weight:600}
.pay{font-size:11px;border-radius:6px;padding:2px 7px;background:var(--accent-soft);color:var(--accent)}.pay.unk{background:var(--chip);color:var(--sub)}
.pay.est{background:transparent;border:1px dashed var(--accent);color:var(--accent)}
.pay.def{background:transparent;border:1px dotted var(--sub);color:var(--sub)}
.warn{font-size:11px;color:var(--warn);background:var(--warn-soft);border-radius:6px;padding:2px 7px;align-self:flex-start}
.ct{font-size:12px;color:var(--sub);word-break:break-all}.ct a{color:var(--accent)}
.foot{margin-top:auto;font-size:12px;color:var(--sub)}
.src{font-size:11px;color:var(--sub);border-top:1px dashed var(--line);padding-top:6px}.src a{color:var(--sub)}.src a:hover{color:var(--ink)}.empty{grid-column:1/-1;text-align:center;color:var(--sub);padding:40px}
.views{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.views button{font:inherit;font-size:13px;border:0;background:var(--card);color:var(--sub);padding:7px 12px;cursor:pointer}
.views button.on{background:var(--accent);color:var(--card)}
.dl{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:7px 12px;cursor:pointer}
#sheet{display:none;padding:0 16px 24px}
.xl{overflow:auto;max-height:calc(100vh - 210px);border:1px solid #c8c8c8;background:var(--card)}
.xl table{border-collapse:collapse;font:12px/1.35 "Malgun Gothic","Apple SD Gothic Neo",sans-serif;color:var(--ink)}
.xl th,.xl td{border:1px solid #d4d4d4;padding:3px 6px;white-space:nowrap;max-width:320px;overflow:hidden;text-overflow:ellipsis;vertical-align:top}
.xl thead th{position:sticky;background:#f3f3f3;color:#444;font-weight:600;z-index:2}
.xl thead tr.col th{top:0;text-align:center;font-weight:400;color:#777;height:20px}
.xl thead tr.hd th{top:21px;cursor:pointer;text-align:left}
.xl thead tr.hd th:hover{background:#e8efe9}
.xl .rn{position:sticky;left:0;background:#f3f3f3;color:#777;text-align:center;min-width:42px;z-index:1}
.xl thead .rn{z-index:3}
.xl .nm{position:sticky;left:42px;background:var(--card);font-weight:600;z-index:1;border-right:2px solid #bfbfbf}
.xl thead .nm{background:#f3f3f3;z-index:3}
.xl tbody tr:hover td{background:#eef5f1}
.xl a{color:#1a56b0}.xl td.num{text-align:right}.xl .wrap{white-space:normal;min-width:260px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .xl thead th,:root:not([data-theme="light"]) .xl .rn{background:#2a2a27;color:#bbb}
:root:not([data-theme="light"]) .xl th,:root:not([data-theme="light"]) .xl td{border-color:#3d3d39}
:root:not([data-theme="light"]) .xl a{color:#8ab4f8}:root:not([data-theme="light"]) .xl tbody tr:hover td{background:#26332e}}
</style></head><body>
<header><h1>디지털 헬스케어 롱리스트</h1>
<div class="meta">하드필터 통과 + 범위 포함 · <span id="count"></span> · 생성 __DATE__ · 업력 미확인(설립일 없음)은 기본으로 숨김 · 💳 칩: 실선 = 근거, 점선 = 추정(사용자 유형), 회색 점 = 기본값(과제 묶음)</div></header>
<div class="bar"><div class="controls">
<input type="search" id="q" placeholder="회사명·설명·키워드 검색">
<label class="tg"><input type="checkbox" id="first" checked> 첫 투자 후보만 (투자 이력 없음)</label>
<label class="tg"><input type="checkbox" id="unver"> 업력 미확인도 보기</label>
<label class="tg"><input type="checkbox" id="thin"> 정보 부족한 곳도 보기</label>
<select id="payer"><option value="">지불자 전체</option></select>
<select id="pbasis"><option value="">지불자 단계 전체</option><option value="근거+추정">근거+추정 (기본값 제외)</option><option value="근거">근거만</option><option value="추정">추정(사용자 유형)만</option><option value="기본값">기본값(과제 묶음)만</option></select>
<select id="tgroup"><option value="">과제 묶음 전체</option></select>
<select id="ctype"><option value="">고객 유형 전체</option></select>
<span class="views"><button id="vCard" class="on">카드</button><button id="vSheet">표 (엑셀)</button></span>
<button class="dl" id="csv" title="지금 보이는 행 그대로, 엑셀에서 바로 열리는 CSV">CSV 내려받기</button>
<select id="sort"><option value="new">최근 설립순</option><option value="sig">신호 많은 순</option><option value="name">이름순</option></select>
</div><div class="kwbar" id="kwbar"></div></div>
<main id="grid"></main>
<section id="sheet"><div class="xl"><table id="xl"></table></div></section>
<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s); let activeKw = null;
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function kwBar(){ const cnt = {}; DATA.forEach(d => d.kws.forEach(k => cnt[k] = (cnt[k]||0)+1));
  const top = Object.entries(cnt).sort((a,b)=>b[1]-a[1]).slice(0,18);
  $('#kwbar').innerHTML = top.map(([k,n]) => `<button data-k="${esc(k)}">${esc(k)} ${n}</button>`).join('');
  $('#kwbar').onclick = e => { const b = e.target.closest('button'); if(!b) return;
    activeKw = activeKw === b.dataset.k ? null : b.dataset.k;
    document.querySelectorAll('#kwbar button').forEach(x => x.classList.toggle('on', x.dataset.k === activeKw)); render(); }; }
function fillSel(id, vals){ const cnt = {}; vals.forEach(v => cnt[v] = (cnt[v]||0)+1);
  const el = $('#'+id); Object.entries(cnt).sort((a,b)=>(a[0]==='미상')-(b[0]==='미상') || b[1]-a[1])
    .forEach(([v,n]) => el.insertAdjacentHTML('beforeend', `<option value="${esc(v)}">${esc(v)} ${n}</option>`)); }
function card(d){ const facts = [d.founded ? `${d.founded} 설립` : '설립일 미상', d.age != null ? `${d.age}년차` : '', d.sido, d.ceo && `대표 ${d.ceo}`].filter(Boolean).join(' · ');
  const name = d.url ? `<a href="${esc(d.url.startsWith('http')?d.url:'http://'+d.url)}" target="_blank" rel="noopener">${esc(d.name)}</a>` : esc(d.name);
  const badge = d.rounds ? `<span class="badge inv">투자 ${d.rounds}회</span>` : `<span class="badge first">첫 투자 후보</span>`;
  return `<article class="card"><div class="top"><div class="name">${name}</div>${badge}</div>
  <div class="facts">${esc(facts)}</div><div class="one">${esc(d.one || '공개된 서비스 정보 없음')}</div>
  ${d.kws.length ? `<div class="chips">${d.kws.map(k=>`<span class="chip">#${esc(k)}</span>`).join('')}</div>` : ''}
  <div class="chips">${d.payer.map(p=>p.b==='none' ? `<span class="pay unk">지불자 ${esc(p.v)}</span>`
     : `<span class="pay${p.b==='추정'?' est':p.b==='기본값'?' def':''}" title="${PB[p.b]}">💳 ${esc(p.v)}${p.b==='근거'?'':' ('+PB[p.b]+')'}</span>`).join('')}</div>
  ${d.task || d.user ? `<div class="ptc">${d.task ? `<span>과제 <b>${esc(d.task)}</b>${d.tgroup!=='미분류' ? ' · '+esc(d.tgroup) : ''}</span>` : ''}${d.user ? `<span>사용자 <b>${esc(d.user)}</b></span>` : ''}</div>` : ''}
  ${d.signals.length ? `<div class="chips">${d.signals.map(s=>`<span class="sig ${s.k}">${esc(s.t)}</span>`).join('')}</div>` : ''}
  ${d.check ? `<span class="warn">${esc(d.reason)}</span>` : ''}
  ${d.contact.length ? `<div class="ct">연락 ${d.contact.map(x => `<a href="${esc(x.u)}"${x.u.startsWith('http')?' target="_blank" rel="noopener"':''}>${esc(x.t)}</a>`).join(' · ')}</div>` : ''}
  <div class="foot">${d.investors.length ? '투자: ' + esc(d.investors.join(', ')) : ''}</div>
  ${d.src.length ? `<div class="src">출처: ${d.src.map(x => x.u ? `<a href="${esc(x.u)}" target="_blank" rel="noopener">${esc(x.t)}</a>` : esc(x.t)).join(' · ')}</div>` : ''}</article>`; }
const PB = {'근거':'근거', '추정':'추정·사용자 유형', '기본값':'기본값·과제 묶음'};
function payOk(d){ const v = $('#payer').value, b = $('#pbasis').value;
  if (!v && !b) return true;
  return d.payer.some(p => (!v || p.v === v) && (!b || (b === '근거+추정' ? p.b === '근거' || p.b === '추정' : p.b === b))); }
function filtered(){ const q = $('#q').value.trim().toLowerCase();
  let xs = DATA.filter(d => ($('#thin').checked || !d.thin) && (!$('#first').checked || !d.rounds) && ($('#unver').checked || !d.unverified)
    && (!activeKw || d.kws.includes(activeKw))
    && payOk(d) && (!$('#tgroup').value || d.tgroup === $('#tgroup').value)
    && (!$('#ctype').value || d.ctype.includes($('#ctype').value))
    && (!q || [d.name, d.one, d.task, d.user, d.payer.map(p=>p.v).join(' '), d.kws.join(' '), d.signals.map(s=>s.t).join(' '), d.src.map(x=>x.t).join(' '), d.contact.map(x=>x.t).join(' ')].join(' ').toLowerCase().includes(q)));
  const s = $('#sort').value;
  xs.sort(s === 'name' ? (a,b)=>a.name.localeCompare(b.name,'ko') : s === 'sig' ? (a,b)=>b.signals.length-a.signals.length
       : (a,b)=>(b.founded||'0').localeCompare(a.founded||'0'));
  if (sheetSort) { const c = COLS[sheetSort.i]; xs.sort((a,b) => {
      const x = c.v(a), y = c.v(b);
      if ((x === '' || x == null) !== (y === '' || y == null)) return (x === '' || x == null) ? 1 : -1;  // 빈 칸은 항상 아래로
      const r =(typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y), 'ko');
      return sheetSort.dir * r; }); }
  return xs; }
// 표(엑셀) 보기: 화면 표와 CSV 가 같은 열 정의를 쓴다
const url = d => d.url ? (d.url.startsWith('http') ? d.url : 'http://' + d.url) : '';
const COLS = [
  {h:'회사명', v:d=>d.name, cls:'nm'}, {h:'투자 이력', v:d=>d.rounds ? `투자 ${d.rounds}회` : '첫 투자 후보'},
  {h:'설립', v:d=>d.founded || (d.pre ? '예비창업' : '')}, {h:'업력(년)', v:d=>d.age ?? '', cls:'num'},
  {h:'지역', v:d=>d.sido}, {h:'대표', v:d=>d.ceo}, {h:'한 줄 설명', v:d=>d.one, cls:'wrap'},
  {h:'키워드', v:d=>d.kws.join(', ')},
  {h:'지불자', v:d=>d.payer.map(p=>p.b==='근거'||p.b==='none'?p.v:`${p.v}(${p.b})`).join(', ')},
  {h:'과제', v:d=>d.task}, {h:'과제 묶음', v:d=>d.tgroup}, {h:'사용자', v:d=>d.user},
  {h:'신호', v:d=>d.signals.map(s=>s.t).join(', ')}, {h:'투자사', v:d=>d.investors.join(', ')},
  {h:'출처', v:d=>d.src.map(x=>x.t).join(', '), link:d=>d.src}, {h:'홈페이지', v:url, href:url},
  {h:'연락처', v:d=>d.contact.map(x=>x.k==='문의'||x.k==='linkedin'||x.k==='kakao'?`${x.t} ${x.u}`:x.t).join(', '), link:d=>d.contact, csv:true},
];
let view = 'card', sheetSort = null;
const colName = i => { let s = ''; i++; while (i) { const m = (i - 1) % 26; s = String.fromCharCode(65 + m) + s; i = (i - m - 1) / 26; } return s; };
function sheet(xs){
  const nmc = c => c.cls === 'nm' ? 'nm' : '';
  const head = `<thead><tr class="col"><th class="rn"></th>${COLS.map((c,i)=>`<th class="${nmc(c)}">${colName(i)}</th>`).join('')}</tr>
    <tr class="hd"><th class="rn">1</th>${COLS.map((c,i)=>`<th data-i="${i}" class="${nmc(c)}">${esc(c.h)}${sheetSort&&sheetSort.i===i?(sheetSort.dir>0?' ▲':' ▼'):''}</th>`).join('')}</tr></thead>`;
  const body = xs.map((d,r)=>`<tr><td class="rn">${r+2}</td>${COLS.map(c=>{ const v = c.v(d);
      let inner = esc(v);
      if (c.href && v) inner = `<a href="${esc(c.href(d))}" target="_blank" rel="noopener">${esc(v)}</a>`;
      if (c.link) inner = c.link(d).map(x=>x.u?`<a href="${esc(x.u)}" target="_blank" rel="noopener">${esc(x.t)}</a>`:esc(x.t)).join(', ');
      return `<td class="${c.cls||''}" title="${esc(v)}">${inner}</td>`; }).join('')}</tr>`).join('');
  $('#xl').innerHTML = head + `<tbody>${body}</tbody>`; }
function render(){ const xs = filtered();
  $('#count').textContent = `${xs.length} / ${DATA.length}곳`;
  $('#grid').style.display = view === 'card' ? '' : 'none'; $('#sheet').style.display = view === 'sheet' ? 'block' : 'none';
  if (view === 'sheet') sheet(xs);
  else $('#grid').innerHTML = xs.length ? xs.map(card).join('') : '<div class="empty">조건에 맞는 회사가 없어요</div>'; }
$('#xl').addEventListener('click', e => { const th = e.target.closest('tr.hd th[data-i]'); if (!th) return;
  const i = +th.dataset.i;
  sheetSort = sheetSort && sheetSort.i === i ? (sheetSort.dir > 0 ? {i, dir:-1} : null) : {i, dir:1}; render(); });
function setView(v){ view = v; $('#vCard').classList.toggle('on', v==='card'); $('#vSheet').classList.toggle('on', v==='sheet');
  try { localStorage.setItem('longlistView', v); } catch(e) {} render(); }
$('#vCard').onclick = () => setView('card'); $('#vSheet').onclick = () => setView('sheet');
$('#csv').onclick = () => {
  const cell = x => { const t = String(x ?? ''); return /[",\\n\\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; };
  const rows = [COLS.map(c=>c.h)].concat(filtered().map(d => COLS.map(c =>
    c.csv ? c.v(d) : c.link ? c.link(d).map(x => x.t + (x.u ? ` (${x.u})` : '')).join(', ') : c.v(d))));
  const csv = '\\ufeff' + rows.map(r => r.map(cell).join(',')).join('\\r\\n');  // BOM: 엑셀이 한글을 UTF-8 로 읽게
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'}));
  a.download = `헬스케어_롱리스트_${new Date().toISOString().slice(0,10)}.csv`; a.click(); URL.revokeObjectURL(a.href); };
['q','first','unver','thin','sort','payer','pbasis','tgroup','ctype'].forEach(id => $('#'+id).addEventListener('input', render));
fillSel('payer', DATA.flatMap(d => d.payer.map(p => p.v))); fillSel('ctype', DATA.flatMap(d => d.ctype)); fillSel('tgroup', DATA.map(d => d.tgroup));
kwBar(); try { view = localStorage.getItem('longlistView') || 'card'; } catch(e) {} setView(view);
</script></body></html>"""


def run(con, out: str | None = None, public: bool = False) -> Path:
    """public=True: 외부 공개용 — 내부 신뢰도 표시는 빼고, 검색엔진 색인을 막는다(주소를 아는 사람만). 대표자 이름은 보인다.
    site/index.html 로 쓰고, main 에 커밋·푸시하면 .github/workflows/pages.yml 이 GitHub Pages 로 배포한다."""
    data = collect(con)
    if public:
        for d in data:
            d["conf"] = ""
    # 공개용은 저장소의 site/index.html (GitHub Actions 가 이 폴더를 Pages 로 배포), 개인용은 data/longlist.html
    path = Path(out or (config.ROOT / "site" / "index.html" if public else config.DATA_DIR / "longlist.html"))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    body = body.replace("__DATE__", html.escape(f"{datetime.now():%Y-%m-%d %H:%M}"))
    if public:
        body = body.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="robots" content="noindex, nofollow">', 1)
    path.write_text(body, encoding="utf-8")
    print(f"카드 {len(data)}곳 → {path}")
    return path
