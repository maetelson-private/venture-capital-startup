"""과제(task) 묶기 + payer × 과제 묶음 교차표.

group: 판정된 과제 구절을 Claude 가 15~25개 묶음으로 나눈다. 묶음마다 '기준'(무엇으로 같은 묶음이라 봤는지)을 함께 받아
       data/task_groups.json 에 남기고, 기업마다 company_tags(kind='task_group') 로 붙인다.
report: 하드필터 통과 + 범위 포함 기업으로 교차표·미상 비율·신뢰도 분포 → data/payer_task_report.md
"""
import json
import re
from collections import Counter, defaultdict

from .. import config, db, llm
from .payer_task import PAYERS, UNKNOWN

BASE = "c.hard_filter='통과' AND c.scope='포함'"

GROUP_SYSTEM = (
    "너는 헬스케어 스타트업들이 푸는 '과제' 구절을 묶는다. 수단(AI·앱·기기)이 아니라 '누구의 어떤 문제'가 같은지로 묶는다. "
    "묶음 수는 15~25개. 묶음 이름은 12자 안팎 명사구, 기준(criterion)은 '무엇이 같아서 한 묶음인지' 한 문장. "
    "묶음끼리 기준이 겹치지 않게 하고, 모든 구절을 정확히 한 묶음에 넣는다. 어디에도 안 맞는 소수는 '기타' 묶음 하나로."
)


def group(con, dry_run=False, groups_file: str | None = None) -> dict:
    """groups_file 을 주면 API 대신 그 묶음([{name, criterion, members:[과제 구절]}])을 쓴다(세션에서 묶은 결과)."""
    tasks = Counter(r["value"] for r in con.execute(
        f"SELECT t.value FROM company_tags t JOIN companies c ON c.id=t.company_id "
        f"WHERE t.kind='task' AND t.value NOT IN (?, '미상') AND {BASE}", (UNKNOWN,)))
    if not tasks:
        print("판정된 과제가 없어요")
        return {}
    items = list(tasks)
    if groups_file:
        given = json.loads(open(groups_file, encoding="utf-8").read())
        data = {"groups": [{"name": g["name"], "criterion": g["criterion"],
                            "members": [f"T{items.index(m)}" for m in g["members"] if m in items]} for g in given]}
        return _apply(con, dry_run, items, tasks, data)
    listing = "\n".join(f"T{i}: {t} ({tasks[t]})" for i, t in enumerate(items))
    skel = json.dumps({"groups": [{"name": "", "criterion": "", "members": ["T0"]}]}, ensure_ascii=False)
    client = llm._client()
    resp = client.messages.create(
        model=llm.MODEL, max_tokens=16000, system=GROUP_SYSTEM, output_config={"effort": "medium"},
        messages=[{"role": "user", "content": f"과제 구절 목록 (괄호는 기업 수):\n{listing}\n\n"
                                              f"아래 형식의 JSON 객체 하나만 출력해. members 는 T번호.\n{skel}"}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    return _apply(con, dry_run, items, tasks, data)


def _apply(con, dry_run, items, tasks, data) -> dict:
    assign, groups = {}, []
    for g in data["groups"]:
        mem = []
        for m in g["members"]:
            k = re.search(r"\d+", str(m))
            if k and int(k.group()) < len(items) and items[int(k.group())] not in assign:
                assign[items[int(k.group())]] = g["name"]
                mem.append(items[int(k.group())])
        groups.append({"name": g["name"], "criterion": g["criterion"], "members": mem})
    # 어느 묶음에도 넣지 않은 구절은 '미분류'로 둔다(묶음을 임의로 만들지 않음). 많으면 묶음 추가를 사람에게 제안
    missing = [t for t in items if t not in assign]
    if missing:
        n_co = sum(tasks[t] for t in missing)
        print(f"묶음에 안 맞는 과제 {len(missing)}종 / {n_co}곳 → 미분류" + (" — 20곳 초과, 묶음 추가 검토 필요" if n_co > 20 else ""))
        (config.DATA_DIR / "task_unassigned.json").write_text(
            json.dumps({t: tasks[t] for t in missing}, ensure_ascii=False, indent=1), encoding="utf-8")
    path = config.DATA_DIR / "task_groups.json"
    path.write_text(json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"묶음 {len(groups)}개 → {path}")
    for g in groups:
        print(f"- {g['name']} ({sum(tasks[t] for t in g['members'])}곳): {g['criterion']}")
    if not dry_run:
        crit = {g["name"]: g["criterion"] for g in groups}
        con.execute("DELETE FROM company_tags WHERE kind='task_group'")
        con.execute("UPDATE tag_evidence SET applied=0 WHERE kind='task_group' AND source='group-llm'")
        for r in con.execute(f"SELECT t.company_id, t.value FROM company_tags t JOIN companies c ON c.id=t.company_id "
                             f"WHERE t.kind='task' AND t.value NOT IN (?, '미상') AND {BASE}", (UNKNOWN,)).fetchall():
            gname = assign.get(r["value"])
            if not gname:
                continue  # 미분류: 묶음 태그 없음
            db.add_tag(con, r["company_id"], "task_group", gname)
            con.execute("""INSERT INTO tag_evidence(company_id, kind, value, quote, source, note, applied)
                           VALUES (?,?,?,?,?,?,1)""",
                        (r["company_id"], "task_group", gname, r["value"], "group-llm", crit.get(gname)))
        con.commit()
    return {"groups": groups, "assign": assign}


UNK = (UNKNOWN, "미상")
TIERS = ("근거", "추정", "기본값")
TIER_LABEL = {"근거": "근거", "추정": "추정(사용자 유형)", "기본값": "기본값(과제 묶음)"}


def _companies(con) -> dict:
    """롱리스트 기업별 지불자(값·basis)·과제·묶음·사용자와 상태."""
    out = {}
    for r in con.execute(f"""SELECT c.id, c.name,
            (SELECT count(*) FROM funding_rounds f WHERE f.company_id=c.id) n_rounds
          FROM companies c WHERE {BASE}"""):
        d = {"name": r["name"], "first": not r["n_rounds"], "payer": [], "pnote": None,
             "task": None, "tnote": None, "customer": None, "cnote": None, "group": None}
        for e in con.execute("""SELECT kind, value, basis, note, confidence FROM tag_evidence
                                WHERE company_id=? AND applied=1""", (r["id"],)):
            if e["kind"] == "payer":
                if e["value"] in UNK:
                    d["pnote"] = e["note"]
                else:
                    d["payer"].append((e["value"], e["basis"] if e["basis"] in TIERS[1:] else "근거", e["confidence"]))
            elif e["kind"] in ("task", "customer"):
                k = e["kind"]
                if e["value"] in UNK:
                    d["tnote" if k == "task" else "cnote"] = e["note"]
                else:
                    d[k] = (e["value"], e["confidence"])
            elif e["kind"] == "task_group":
                d["group"] = e["value"]
        tiers = {b for _, b, _ in d["payer"]}
        d["tier"] = next((t for t in TIERS if t in tiers), None)
        d["pstate"] = (TIER_LABEL[d["tier"]] if d["tier"] else "근거 없음" if d["pnote"] else "미처리")
        out[r["id"]] = d
    return out


def _groups():
    p = config.DATA_DIR / "task_groups.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _gname(d):
    return d["group"] or ("과제 근거 없음" if d["tnote"] or not d["task"] else "미분류")


def _xtab(cos, ids, tiers):
    """기업 수 교차표. tiers 에 든 basis 의 값만 센다. 지불자 2개면 두 행에 모두 셈."""
    cell = Counter()
    for i in ids:
        d = cos[i]
        for v in {v for v, b, _ in d["payer"] if b in tiers}:
            cell[(v, _gname(d))] += 1
    return cell


def _table(cell, rows, cols, title, extra=None):
    """extra: {행이름: Counter(묶음→수)} — 합계 행에서 빼는 별도 표기 행."""
    L = [f"### {title}", "", "| 지불자 / 과제 묶음 | " + " | ".join(cols) + " | 합계 |", "|" + "---|" * (len(cols) + 2)]
    for p in rows:
        vals = [cell.get((p, g), 0) for g in cols]
        if not any(vals):
            continue
        L.append(f"| **{p}** | " + " | ".join(str(v) if v else "·" for v in vals) + f" | {sum(vals)} |")
    L.append("| **합계** | " + " | ".join(str(sum(cell.get((p, g), 0) for p in rows)) for g in cols) +
             f" | {sum(cell.get((p, g), 0) for p in rows for g in cols)} |")
    for name, c in (extra or {}).items():
        L.append(f"| _{name}_ | " + " | ".join(str(c.get(g, 0)) if c.get(g) else "·" for g in cols) + f" | _{sum(c.values())}_ |")
    return L


def report(con) -> str:
    cos = _companies(con)
    allids = list(cos)
    first = [i for i in allids if cos[i]["first"]]
    groups = _groups()
    gnames = [g["name"] for g in groups] + ["미분류", "과제 근거 없음"]
    payers = [p for p in PAYERS if p != UNKNOWN]
    L = ["# payer × 과제 묶음 교차표", "",
         f"대상: 하드필터 통과 + 범위 포함 {len(allids)}곳, 그중 투자 이력 없는 곳 {len(first)}곳. "
         "지불자가 2개인 기업은 두 행에 모두 들어가서 행 합이 기업 수보다 클 수 있음. "
         f"'미분류' = 과제는 있으나 {len(groups)}개 묶음 어디에도 넣지 않음, '과제 근거 없음' = 과제를 정하지 못함.", "",
         "지불자 근거 단계 (2026-11-17 규칙표):",
         "- **근거**: 원문 문장 + URL 이 있는 값, 사용자 결정(manual) 값",
         "- **추정(사용자 유형)**: 사용자가 돈을 쓰는 조직(병의원·치과·약국·요양기관·연구조직·공공·기업)이라 그 조직을 지불자로 봄. "
         "사용자 결정으로 근거에서 내린 추정 2건(메디아크 실손, 핀스케어 기업 복지)도 여기 포함",
         "- **기본값(과제 묶음)**: 사용자로는 못 정하고, 과제가 구매자를 정하는 9개 묶음(판독·기록·병원 운영·시술·술기 교육·응급입원·"
         "신약·약국·요양기관)이라 붙인 값. **합계·밀집 칸 계산에 넣지 않고 따로 표기**",
         "- 한 기업은 가장 높은 단계로 셈 (근거 > 추정 > 기본값)", ""]

    # 1. 상태
    cols = [TIER_LABEL[t] for t in TIERS] + ["근거 없음", "미처리"]
    L += ["## 1. 상태 (기업 단위, 가장 높은 단계)", "", "| 항목 | " + " | ".join(cols) + " | 투자 없는 곳 중 근거+추정 |",
          "|" + "---|" * (len(cols) + 2)]
    st = Counter(cos[i]["pstate"] for i in allids)
    stf = sum(1 for i in first if cos[i]["tier"] in ("근거", "추정"))
    L.append("| 지불자 | " + " | ".join(str(st[c]) for c in cols) + f" | {stf} / {len(first)} |")
    for k, nk in (("task", "tnote"), ("customer", "cnote")):
        has = sum(1 for i in allids if cos[i][k]); none = sum(1 for i in allids if not cos[i][k] and cos[i][nk])
        L.append(f"| {k} | {has} | – | – | {none} | {len(allids) - has - none} | {sum(1 for i in first if cos[i][k])} / {len(first)} |")
    notes = Counter(cos[i]["pnote"] for i in allids if cos[i]["pstate"] == "근거 없음")
    L += ["", "지불자 '근거 없음'의 사유: " + ", ".join(f"{k} {v}" for k, v in notes.most_common()) +
          " (no_source = 원천 없음, homepage_failed = 홈페이지 수집 실패, no_evidence = 원천은 있으나 근거·추정 불가, "
          "rule_none = 규칙표로 정할 수 없어 이전 추정을 내림)"]

    # 2. 신뢰도
    L += ["", "## 2. 신뢰도 분포 (값 단위)", "", "| 항목 | 상 | 중 | 하 |", "|---|---|---|---|"]
    for t in TIERS:
        c = Counter(cf for i in allids for _, b, cf in cos[i]["payer"] if b == t)
        L.append(f"| 지불자 {TIER_LABEL[t]} | {c['상']} | {c['중']} | {c['하'] + c[None]} |")
    for k in ("task", "customer"):
        c = Counter(cos[i][k][1] for i in allids if cos[i][k])
        L.append(f"| {k} | {c['상']} | {c['중']} | {c['하']} |")

    # 3. 교차표 (투자 이력 없는 곳)
    L += ["", "## 3. 투자 이력 없는 곳: 지불자 × 과제 묶음", ""]
    for tiers, title in ((("근거",), "근거만"), (("추정",), "추정(사용자 유형)만"), (("기본값",), "기본값(과제 묶음)만")):
        L += _table(_xtab(cos, first, tiers), payers, gnames, title) + [""]
    only_def = Counter(_gname(cos[i]) for i in first if cos[i]["tier"] == "기본값")
    none = Counter(_gname(cos[i]) for i in first if cos[i]["tier"] is None)
    L += _table(_xtab(cos, first, ("근거", "추정")), payers, gnames,
                "근거+추정 (합계는 이 두 단계만. 아래 기울임 행은 합계 밖)",
                {"기본값만 있는 곳": only_def, "근거 없음·미처리": none}) + [""]

    # 4. 밀집·빈 칸 (근거+추정만)
    cell = _xtab(cos, first, ("근거", "추정"))
    dcell = _xtab(cos, first, ("기본값",))
    real_g = [g["name"] for g in groups]
    dense = sorted(((p, g, cell[(p, g)]) for p in payers for g in real_g if cell.get((p, g), 0) >= 5), key=lambda x: -x[2])
    sparse = [(p, g, cell.get((p, g), 0)) for p in payers for g in real_g if cell.get((p, g), 0) <= 1]
    L += ["## 4. 기업이 5곳 이상 몰린 칸 (투자 이력 없음, 근거+추정. 괄호는 같은 칸의 기본값 수)", ""]
    L += [f"- {p} × {g}: {n}곳" + (f" (+기본값 {dcell[(p, g)]})" if dcell.get((p, g)) else "") for p, g, n in dense] or ["- 없음"]
    L += ["", f"## 5. 0~1곳인 칸 (투자 이력 없음, 근거+추정) — {len(sparse)}칸 / {len(payers) * len(real_g)}칸", ""]
    by_p = defaultdict(list)
    for p, g, n in sparse:
        by_p[p].append(f"{g}({n})" if n else g)
    L += [f"- **{p}**: " + ", ".join(v) for p, v in by_p.items()]

    if groups:
        L += ["", "## 6. 과제 묶음 기준", "", "| 묶음 | 기준 | 기업 수(전체) | 투자 없음 |", "|---|---|---|---|"]
        cnt = Counter(cos[i]["group"] for i in allids); cntf = Counter(cos[i]["group"] for i in first)
        for g in groups:
            L.append(f"| {g['name']} | {g['criterion']} | {cnt.get(g['name'], 0)} | {cntf.get(g['name'], 0)} |")
    text = "\n".join(L)
    path = config.DATA_DIR / "payer_task_report.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n→ {path}")
    return text
