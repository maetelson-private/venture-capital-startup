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


def collect(con) -> list[dict]:
    out = []
    rows = con.execute("""SELECT * FROM v_companies WHERE hard_filter='통과' AND scope='포함'""").fetchall()
    for r in rows:
        cid = r["id"]
        tags = con.execute("SELECT kind, value FROM company_tags WHERE company_id=?", (cid,)).fetchall()
        kws, programs, ptc = [], [], {"payer": [], "task": [], "customer": [], "task_group": []}
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
        out.append({
            "id": cid,
            "name": _display(r["name"]),
            "founded": (r["founded_date"] or "")[:7],
            "age": r["age_years"],
            "sido": (r["sido"] or "").replace("특별시", "").replace("광역시", "").replace("특별자치도", "").replace("도", "")[:4],
            "one": r["one_liner"] or _first_sentence(r["service_summary"]) or _first_sentence(r["bm_summary"]),
            "kws": kws[:5],
            # 지불자·과제·사용자 (tags 명령). 없으면 미상
            "payer": ptc["payer"] or ["미상"],
            "task": (ptc["task"] or [""])[0] if (ptc["task"] or [""])[0] != "미상" else "",
            "tgroup": (ptc["task_group"] or ["미상"])[0],
            "user": (ptc["customer"] or [""])[0] if (ptc["customer"] or [""])[0] != "미상" else "",
            "signals": signals,
            "investors": [x for x in (r["investor_names"] or "").split(",") if x][:4],
            "rounds": r["n_rounds"] or 0,
            "url": r["url"] or "",
            "ceo": r["ceo"] or "",
            "conf": r["confidence"] or "",
            # 밸류 미상은 거의 전부라 표시하지 않고, 설립일 미상만 경고
            "check": not r["founded_date"],
            "reason": "설립일 미상 — 업력 확인 필요",
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
.warn{font-size:11px;color:var(--warn);background:var(--warn-soft);border-radius:6px;padding:2px 7px;align-self:flex-start}
.foot{margin-top:auto;font-size:12px;color:var(--sub)}.empty{grid-column:1/-1;text-align:center;color:var(--sub);padding:40px}
</style></head><body>
<header><h1>디지털 헬스케어 롱리스트</h1>
<div class="meta">하드필터 통과 + 범위 포함 · <span id="count"></span> · 생성 __DATE__</div></header>
<div class="bar"><div class="controls">
<input type="search" id="q" placeholder="회사명·설명·키워드 검색">
<label class="tg"><input type="checkbox" id="first" checked> 첫 투자 후보만 (투자 이력 없음)</label>
<label class="tg"><input type="checkbox" id="dated"> 설립일 확인된 곳만</label>
<label class="tg"><input type="checkbox" id="thin"> 정보 부족한 곳도 보기</label>
<select id="payer"><option value="">지불자 전체</option></select>
<select id="tgroup"><option value="">과제 묶음 전체</option></select>
<select id="sort"><option value="new">최근 설립순</option><option value="sig">신호 많은 순</option><option value="name">이름순</option></select>
</div><div class="kwbar" id="kwbar"></div></div>
<main id="grid"></main>
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
  <div class="chips">${d.payer.map(p=>`<span class="pay${p==='미상'?' unk':''}">${p==='미상'?'지불자 미상':'💳 '+esc(p)}</span>`).join('')}</div>
  ${d.task || d.user ? `<div class="ptc">${d.task ? `<span>과제 <b>${esc(d.task)}</b>${d.tgroup!=='미상' ? ' · '+esc(d.tgroup) : ''}</span>` : ''}${d.user ? `<span>사용자 <b>${esc(d.user)}</b></span>` : ''}</div>` : ''}
  ${d.signals.length ? `<div class="chips">${d.signals.map(s=>`<span class="sig ${s.k}">${esc(s.t)}</span>`).join('')}</div>` : ''}
  ${d.check ? `<span class="warn">${esc(d.reason)}</span>` : ''}
  <div class="foot">${d.investors.length ? '투자: ' + esc(d.investors.join(', ')) : ''}</div></article>`; }
function render(){ const q = $('#q').value.trim().toLowerCase();
  let xs = DATA.filter(d => ($('#thin').checked || !d.thin) && (!$('#first').checked || !d.rounds) && (!$('#dated').checked || d.founded)
    && (!activeKw || d.kws.includes(activeKw))
    && (!$('#payer').value || d.payer.includes($('#payer').value)) && (!$('#tgroup').value || d.tgroup === $('#tgroup').value)
    && (!q || [d.name, d.one, d.task, d.user, d.payer.join(' '), d.kws.join(' '), d.signals.map(s=>s.t).join(' ')].join(' ').toLowerCase().includes(q)));
  const s = $('#sort').value;
  xs.sort(s === 'name' ? (a,b)=>a.name.localeCompare(b.name,'ko') : s === 'sig' ? (a,b)=>b.signals.length-a.signals.length
       : (a,b)=>(b.founded||'0').localeCompare(a.founded||'0'));
  $('#count').textContent = `${xs.length} / ${DATA.length}곳`;
  $('#grid').innerHTML = xs.length ? xs.map(card).join('') : '<div class="empty">조건에 맞는 회사가 없어요</div>'; }
['q','first','dated','thin','sort','payer','tgroup'].forEach(id => $('#'+id).addEventListener('input', render));
fillSel('payer', DATA.flatMap(d => d.payer)); fillSel('tgroup', DATA.map(d => d.tgroup));
kwBar(); render();
</script></body></html>"""


def run(con, out: str | None = None) -> Path:
    data = collect(con)
    path = Path(out or config.DATA_DIR / "longlist.html")
    body = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    body = body.replace("__DATE__", html.escape(f"{datetime.now():%Y-%m-%d %H:%M}"))
    path.write_text(body, encoding="utf-8")
    print(f"카드 {len(data)}곳 → {path}")
    return path
