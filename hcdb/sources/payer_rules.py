"""추정 지불자 규칙표 (2026-11-17 사용자 확정).

판정 묶음마다 기준이 달랐던 추정을 버리고 이 규칙을 일괄 적용한다. 근거 값·manual 값이 있는 기업은 건드리지 않는다.
1) 사용자 유형이 돈을 쓰는 조직이면 그 조직 → basis '추정' (표시: 추정(사용자 유형))
2) 아니면 과제가 구매자를 정하는 묶음만 기본값 → basis '기본값' (표시: 기본값(과제 묶음))
3) 둘 다 없으면 '근거 없음'
소비자 직접·치매 공공·질환명 묶음의 병의원 기본값, 환자·보호자 예외 규칙은 두지 않는다.
"""
import re
from collections import Counter

from .. import db

UNKNOWN = "근거 없음"
SOURCE = "payer-rule"
BASIS_USER, BASIS_GROUP = "추정", "기본값"

# 사용자 유형 판별 (customer 태그 문구 + customer_type 태그). 위에서부터 먼저 맞는 것
USER_TYPES = [
    ("약국·약사", r"약국|약사|약제"),
    ("요양·돌봄 기관·인력", r"요양|돌봄|간병|방문간호|주간보호|복지관|복지시설|사회복지|케어기버"),
    ("의료진·병원", r"의사|의료진|간호|병원|의원|전문의|임상|치료사|클리닉|진료|방사선사|의료기관|검사실|수술|외과|판독의|전공의|의과"),
    ("연구자·제약사", r"연구|제약|바이오|신약|CRO|의료기기 개발사|소프트웨어 의료기기"),
    ("공공·학교", r"보건소|지자체|공공|학교|교사"),
    ("기업·직장인", r"기업|직장|임직원|근로자|사무직|선원|운전자"),
]

USER_RULES = {  # 사용자가 곧 돈을 쓰는 조직인 경우
    "치과": ("병의원 예산", "치과 병의원·치과의사가 쓰는 제품이라 치과가 산다고 봄"),
    "약국·약사": ("약국 예산", "약국·약사가 쓰는 제품이라 약국이 산다고 봄"),
    "요양·돌봄 기관·인력": ("요양기관 예산", "요양·돌봄 기관과 인력이 쓰는 제품이라 기관이 산다고 봄"),
    "의료진·병원": ("병의원 예산", "의료진이 진료 현장에서 쓰는 제품이라 병의원이 산다고 봄"),
    "연구자·제약사": ("제약·기기사", "연구·개발 조직이 쓰는 서비스라 제약·기기사가 산다고 봄"),
    "공공·학교": ("공공·지자체", "보건소·학교 등 공공 현장에서 쓰여 공공 예산으로 산다고 봄"),
    "기업·직장인": ("기업 복지", "직장인·근로자 대상이라 기업이 복지로 산다고 봄"),
}

GROUP_RULES = {  # 과제가 구매자를 정하는 묶음만
    "영상·검사 판독": "병의원 예산", "진료 기록 작성": "병의원 예산", "의료기관 운영·청구 비효율": "병의원 예산",
    "시술·수술 정밀도·안전": "병의원 예산", "의료인 술기 교육": "병의원 예산", "응급·입원 중 악화 대응": "병의원 예산",
    "신약·연구개발 효율": "제약·기기사", "약국·의약품 유통 운영": "약국 예산", "돌봄·요양기관 운영·인력": "요양기관 예산",
}


def user_type(customer: str | None, ctype: list[str]) -> str:
    if "치과" in ctype:
        return "치과"
    if not customer or customer in (UNKNOWN, "미상"):
        return "사용자 미상"
    for name, rx in USER_TYPES:
        if re.search(rx, customer):
            return name
    return "환자·소비자·기타"


def rule(customer: str | None, ctype: list[str], group: str | None) -> tuple[str, str, str] | None:
    """(지불자, basis, 이유) 또는 None(근거 없음)."""
    u = user_type(customer, ctype)
    if u in USER_RULES:
        v, why = USER_RULES[u]
        return v, BASIS_USER, f"[사용자 유형] '{customer or u}' → {why}"
    if group in GROUP_RULES:
        return GROUP_RULES[group], BASIS_GROUP, f"[과제 묶음 기본값] '{group}' → {GROUP_RULES[group]}"
    return None


# ───────────── 적용 ─────────────
def _kept(e) -> bool:
    """규칙으로 바꾸지 않고 남기는 지불자 근거: manual 전부, 근거(basis 근거 또는 이전 기사 근거) 값."""
    if e["source"] == "manual":
        return True
    if e["value"] in (UNKNOWN, "미상"):
        return False
    return e["basis"] not in ("추정", BASIS_GROUP)


def _tags(con, cid, kind) -> list[str]:
    return [r[0] for r in con.execute("SELECT value FROM tag_evidence WHERE company_id=? AND kind=? AND applied=1",
                                      (cid, kind))]


def _state(evs) -> list[tuple[str, str]]:
    out = []
    for e in evs:
        if e["value"] in (UNKNOWN, "미상"):
            continue
        b = "근거" if _kept(e) and e["basis"] != "추정" else ("기본값" if e["basis"] == BASIS_GROUP else "추정")
        out.append((e["value"], b))
    return out


def apply(con, dry_run=False) -> dict:
    ids = [r[0] for r in con.execute("SELECT id FROM companies WHERE hard_filter='통과' AND scope='포함'")]
    before, after, log = Counter(), Counter(), []
    for cid in ids:
        evs = con.execute("""SELECT id, value, basis, source, note FROM tag_evidence
                             WHERE company_id=? AND kind='payer' AND applied=1""", (cid,)).fetchall()
        old = _state(evs)
        for v, b in old or [(UNKNOWN, "")]:
            before[(v, b)] += 1
        if any(_kept(e) for e in evs):
            for v, b in old:
                after[(v, b)] += 1
            log.append({"id": cid, "old": old, "new": old, "kept": True})
            continue
        cust = (_tags(con, cid, "customer") or [None])[0]
        r = rule(cust, _tags(con, cid, "customer_type"), (_tags(con, cid, "task_group") or [None])[0])
        new = [(r[0], r[1])] if r else []
        for v, b in new or [(UNKNOWN, "")]:
            after[(v, b)] += 1
        log.append({"id": cid, "old": old, "new": new, "kept": False})
        if dry_run:
            continue
        if not r and not old:
            continue  # 이미 '근거 없음' 이고 규칙도 없음 → 기존 표시(no_source 등) 유지
        con.execute("UPDATE tag_evidence SET applied=0 WHERE company_id=? AND kind='payer' AND applied=1", (cid,))
        con.execute("DELETE FROM company_tags WHERE company_id=? AND kind='payer'", (cid,))
        v, basis, why = r if r else (UNKNOWN, None, None)
        con.execute("""INSERT INTO tag_evidence(company_id, kind, value, source, confidence, note, basis, reason, applied)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (cid, "payer", v, SOURCE, "하" if r else None, "rule" if r else "rule_none", basis, why))
        db.add_tag(con, cid, "payer", v, None)
    if not dry_run:
        con.commit()
    return {"before": before, "after": after, "log": log}


def summary(res: dict) -> str:
    log = res["log"]
    payers = sorted({v for c in (res["before"], res["after"]) for v, _ in c},
                    key=lambda v: (v == UNKNOWN, -res["after"].get((v, "근거"), 0) - res["after"].get((v, "추정"), 0)))
    L = ["| 지불자 | 전 근거 | 전 추정 | 후 근거 | 후 추정(사용자 유형) | 후 기본값(과제 묶음) |", "|---|---|---|---|---|---|"]
    for v in payers:
        b, a = res["before"], res["after"]
        if v == UNKNOWN:
            L.append(f"| {v} | {b[(v, '')]} | | {a[(v, '')]} | | |")
        else:
            L.append(f"| {v} | {b[(v, '근거')]} | {b[(v, '추정')]} | {a[(v, '근거')]} | {a[(v, '추정')]} | {a[(v, '기본값')]} |")
    changed = [x for x in log if not x["kept"]]
    lost = [x for x in changed if x["old"] and not x["new"]]
    same = [x for x in changed if x["old"] and x["new"] and x["old"][0][0] == x["new"][0][0]]
    diff = [x for x in changed if x["old"] and x["new"] and x["old"][0][0] != x["new"][0][0]]
    gained = [x for x in changed if not x["old"] and x["new"]]
    L += ["", f"- 롱리스트 {len(log)}곳, 근거·manual 이 있어 그대로 둔 곳 {len(log) - len(changed)}곳",
          f"- 추정이 사라지는 곳 {len(lost)} · 값이 같은 곳 {len(same)} · 값이 바뀌는 곳 {len(diff)} · 새로 붙는 곳 {len(gained)}"]
    lost_by = Counter(v for x in lost for v, _ in x["old"])
    L.append("- 사라지는 추정 값별: " + ", ".join(f"{v} {n}" for v, n in lost_by.most_common()))
    return "\n".join(L)
