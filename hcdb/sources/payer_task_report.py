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
        f"WHERE t.kind='task' AND t.value!=? AND {BASE}", (UNKNOWN,)))
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
    missing = [t for t in items if t not in assign]
    if missing:  # 빠진 구절은 기타로
        other = next((g for g in groups if g["name"] == "기타"), None)
        if not other:
            other = {"name": "기타", "criterion": "다른 묶음 기준에 맞지 않는 소수 과제", "members": []}
            groups.append(other)
        for t in missing:
            assign[t] = "기타"
            other["members"].append(t)
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
                             f"WHERE t.kind='task' AND {BASE}").fetchall():
            gname = assign.get(r["value"], UNKNOWN)
            db.add_tag(con, r["company_id"], "task_group", gname)
            con.execute("""INSERT INTO tag_evidence(company_id, kind, value, quote, source, note, applied)
                           VALUES (?,?,?,?,?,?,1)""",
                        (r["company_id"], "task_group", gname, r["value"], "group-llm", crit.get(gname)))
        con.commit()
    return {"groups": groups, "assign": assign}


def _tags(con) -> dict:
    rows = con.execute(f"""SELECT c.id, c.name,
            (SELECT count(*) FROM funding_rounds r WHERE r.company_id=c.id) AS n_rounds
          FROM companies c WHERE {BASE}""").fetchall()
    out = {}
    for r in rows:
        d = {"name": r["name"], "first": not r["n_rounds"], "payer": [], "task": [], "customer": [], "task_group": []}
        for t in con.execute("SELECT kind, value FROM company_tags WHERE company_id=? AND kind IN "
                             "('payer','task','customer','task_group')", (r["id"],)):
            d[t["kind"]].append(t["value"])
        out[r["id"]] = d
    return out


def report(con) -> str:
    cos = _tags(con)
    n = len(cos)
    n_first = sum(d["first"] for d in cos.values())
    groups_path = config.DATA_DIR / "task_groups.json"
    groups = json.loads(groups_path.read_text(encoding="utf-8")) if groups_path.exists() else []
    gnames = [g["name"] for g in groups] + [UNKNOWN]
    payers = [p for p in PAYERS]

    cell = defaultdict(lambda: [0, 0])  # (payer, group) → [전체, 첫투자후보]
    for d in cos.values():
        ps = d["payer"] or [UNKNOWN]
        gs = d["task_group"] or [UNKNOWN]
        for p in ps:
            for g in gs:
                cell[(p, g)][0] += 1
                cell[(p, g)][1] += d["first"]

    L = [f"# payer × 과제 묶음 교차표", "",
         f"대상: 하드필터 통과 + 범위 포함 {n}곳 (투자 이력 없는 곳 {n_first}곳). "
         f"칸 값은 `전체 (투자 이력 없음)`. 지불자가 2개인 기업은 두 행에 모두 들어가서 행 합계가 기업 수보다 클 수 있음.", ""]
    head = "| payer \\ 과제 | " + " | ".join(gnames) + " | 합계 |"
    L += [head, "|" + "---|" * (len(gnames) + 2)]
    col_tot = defaultdict(lambda: [0, 0])
    for p in payers:
        row_tot = [0, 0]
        cells = []
        for g in gnames:
            a, b = cell.get((p, g), [0, 0])
            row_tot[0] += a
            row_tot[1] += b
            col_tot[g][0] += a
            col_tot[g][1] += b
            cells.append(f"{a} ({b})" if a else "·")
        L.append(f"| **{p}** | " + " | ".join(cells) + f" | {row_tot[0]} ({row_tot[1]}) |")
    L.append("| **합계** | " + " | ".join(f"{col_tot[g][0]} ({col_tot[g][1]})" for g in gnames) + " | |")

    # 지불자 확인된 기업만 본 교차표 (미상 행·열 제외) — 밀집·빈칸 판단용
    L += ["", "## 지불자·과제 둘 다 확인된 칸만", ""]
    known = [(p, g) for (p, g), v in cell.items() if p != UNKNOWN and g != UNKNOWN and v[0]]
    known.sort(key=lambda x: -cell[x][0])
    L += ["| payer | 과제 묶음 | 기업 수 | 투자 이력 없음 |", "|---|---|---|---|"]
    L += [f"| {p} | {g} | {cell[(p, g)][0]} | {cell[(p, g)][1]} |" for p, g in known]
    empty_cols = [g for g in gnames[:-1] if not any(cell.get((p, g), [0])[0] for p in payers if p != UNKNOWN)]
    L += ["", f"지불자가 하나도 확인되지 않은 과제 묶음: {', '.join(empty_cols) or '없음'}"]

    # 미상 비율·원인, 신뢰도 분포
    L += ["", "## 미상 비율", "", "| 항목 | 미상 | 비율 | 원천 없음 | 원천은 있으나 근거 문장 없음 | 투자 이력 없는 곳 중 미상 |",
          "|---|---|---|---|---|---|"]
    for kind in ("payer", "task", "customer"):
        unk = [cid for cid, d in cos.items() if not d[kind] or d[kind] == [UNKNOWN]]
        notes = Counter(r["note"] for r in con.execute(
            f"SELECT e.note FROM tag_evidence e JOIN companies c ON c.id=e.company_id "
            f"WHERE e.kind=? AND e.applied=1 AND e.value=? AND {BASE}", (kind, UNKNOWN)))
        nosrc = notes.get("no_source", 0)
        unk_first = sum(cos[c]["first"] for c in unk)
        L.append(f"| {kind} | {len(unk)} | {len(unk) / n:.0%} | {nosrc} | {len(unk) - nosrc} | "
                 f"{unk_first} / {n_first} ({unk_first / max(n_first, 1):.0%}) |")
    L += ["", "## 신뢰도 분포 (미상 제외, 값 단위)", "", "| 항목 | 상 | 중 | 하 |", "|---|---|---|---|"]
    for kind in ("payer", "task", "customer"):
        cc = Counter(r["confidence"] for r in con.execute(
            f"SELECT e.confidence FROM tag_evidence e JOIN companies c ON c.id=e.company_id "
            f"WHERE e.kind=? AND e.applied=1 AND e.value!=? AND {BASE}", (kind, UNKNOWN)))
        L.append(f"| {kind} | {cc.get('상', 0)} | {cc.get('중', 0)} | {cc.get('하', 0)} |")
    src = Counter(r["note"] for r in con.execute(
        f"SELECT e.note FROM tag_evidence e JOIN companies c ON c.id=e.company_id "
        f"WHERE e.kind='payer' AND e.applied=1 AND e.value!=? AND {BASE}", (UNKNOWN,)))
    L += ["", "지불자 근거가 나온 원천: " + ", ".join(f"{k} {v}" for k, v in src.most_common())]

    if groups:
        L += ["", "## 과제 묶음 기준", "", "| 묶음 | 기준 | 기업 수 | 예시 과제 |", "|---|---|---|---|"]
        cnt = Counter(g for d in cos.values() for g in d["task_group"])
        for g in groups:
            L.append(f"| {g['name']} | {g['criterion']} | {cnt.get(g['name'], 0)} | "
                     f"{' / '.join(g['members'][:3])} |")
    text = "\n".join(L)
    path = config.DATA_DIR / "payer_task_report.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n→ {path}")
    return text
