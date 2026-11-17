"""이름 중복 후보 찾기 (병합은 하지 않음 — 사람이 골라 `merge 남길ID 지울ID`).

같은 묶음으로 보는 기준:
  exact   : 정규화 사명(norm_name)이 같은 별칭을 가진 서로 다른 기업
  variant : 정규화는 다르지만 variant_key(ㅐ/ㅔ, '코리아·글로벌' 접미어)가 같은 기업
묶음마다 사업자번호·대표·홈페이지·설립일이 같은지/다른지를 보여 주고,
다른 값이 있으면 '충돌'(다른 회사일 가능성)로 표시한다.
남길 쪽 제안 = 근거(field_evidence)가 많은 쪽.
"""
from collections import defaultdict
from urllib.parse import urlparse

from . import config
from .normalize import norm_name, variant_key

FIELDS = ("biz_no", "ceo", "host", "founded_date")
LABEL = {"biz_no": "사업자번호", "ceo": "대표", "host": "홈페이지", "founded_date": "설립일"}


def _host(u):
    if not u:
        return None
    u = u if u.startswith("http") else "http://" + u
    return urlparse(u).netloc.lower().removeprefix("www.") or None


def find(con) -> list[dict]:
    comp = {r["id"]: dict(r) for r in con.execute(
        """SELECT c.id, c.name, c.biz_no, c.ceo, c.url, c.founded_date, c.hard_filter, c.scope,
                  (SELECT count(*) FROM funding_rounds f WHERE f.company_id=c.id) n_rounds,
                  (SELECT count(*) FROM field_evidence e WHERE e.company_id=c.id) n_ev
           FROM companies c""")}
    for c in comp.values():
        c["host"] = _host(c["url"])
    keys = defaultdict(set)  # (kind, key) → company ids
    for r in con.execute("SELECT company_id, alias FROM company_aliases"):
        n = norm_name(r["alias"])
        if len(n) >= 2:
            keys[("exact", n)].add(r["company_id"])
        v = variant_key(r["alias"])
        if v:
            keys[("variant", v)].add(r["company_id"])
    # 같은 기업 묶음은 한 번만 (exact 가 있으면 exact 로)
    groups, seen = [], set()
    for (kind, key), ids in sorted(keys.items(), key=lambda x: x[0][0]):
        ids = frozenset(i for i in ids if i in comp)
        if len(ids) < 2 or ids in seen:
            continue
        seen.add(ids)
        members = sorted((comp[i] for i in ids), key=lambda c: -c["n_ev"])
        cmp, conflict = {}, False
        for f in FIELDS:
            vals = {m[f] for m in members if m[f]}
            if not vals:
                cmp[f] = "없음"
            elif len(vals) == 1:
                cmp[f] = "같음" if sum(1 for m in members if m[f]) > 1 else "한쪽만"
            else:
                cmp[f] = "다름"
                conflict = conflict or f in ("biz_no", "ceo")
        same = sum(v == "같음" for v in cmp.values())
        verdict = "충돌" if conflict else ("강함" if same else "약함")
        groups.append({"kind": kind, "key": key, "members": members, "cmp": cmp, "verdict": verdict,
                       "longlist": any(m["hard_filter"] == "통과" and m["scope"] == "포함" for m in members)})
    order = {"강함": 0, "약함": 1, "충돌": 2}
    groups.sort(key=lambda g: (not g["longlist"], order[g["verdict"]], g["kind"]))
    return groups


def run(con) -> list[dict]:
    groups = find(con)
    L = ["# 이름 중복 후보", "",
         "병합은 하지 않았음. 고른 묶음만 `python -m hcdb merge 남길ID 지울ID` 로 합친다.",
         "판정: 강함 = 사업자번호·대표·홈페이지·설립일 중 하나 이상 같고 다른 값 없음 / "
         "약함 = 비교할 값이 없음 / 충돌 = 사업자번호나 대표가 다름(다른 회사일 가능성).", "",
         "| # | 종류 | 판정 | 롱리스트 | 남길 쪽 (근거 많은 쪽) | 지울 쪽 | 사업자번호 | 대표 | 홈페이지 | 설립일 |",
         "|---|---|---|---|---|---|---|---|---|---|"]

    def desc(m):
        return (f"{m['id']} {m['name']} ({m['hard_filter']}/{m['scope']}, 근거 {m['n_ev']}"
                + (f", 투자 {m['n_rounds']}" if m["n_rounds"] else "") + ")")
    for i, g in enumerate(groups, 1):
        keep, drops = g["members"][0], g["members"][1:]
        L.append(f"| {i} | {g['kind']} | {g['verdict']} | {'예' if g['longlist'] else ''} | {desc(keep)} | "
                 + "<br>".join(desc(d) for d in drops) + " | "
                 + " | ".join(g["cmp"][f] for f in FIELDS) + " |")
    path = config.DATA_DIR / "dupe_candidates.md"
    path.write_text("\n".join(L), encoding="utf-8")
    n_ll = sum(g["longlist"] for g in groups)
    print(f"중복 후보 {len(groups)}묶음 (롱리스트 관련 {n_ll}) → {path}")
    return groups
