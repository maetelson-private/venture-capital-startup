"""하드 필터 판정: 업력(딥테크 연장) · 영업상태 · 추정 밸류."""
from . import config


def judge(row) -> tuple[str, str]:
    if row["is_pre_startup"]:
        return "통과", "예비창업팀"
    reasons, hold = [], []
    if row["biz_status"] in ("휴업", "폐업"):
        return "탈락", f"영업상태 {row['biz_status']}"
    limit = config.MAX_AGE_YEARS + (config.DEEPTECH_EXTRA_YEARS if row["is_deeptech"] else 0)
    if row["age_years"] is None:
        hold.append("설립일 미상")
    elif row["age_years"] > limit:
        reasons.append(f"업력 {row['age_years']}년 > {limit}년")
    if row["est_valuation_krw"] is None:
        hold.append("밸류 미상")
    elif row["est_valuation_krw"] > config.MAX_VALUATION_KRW:
        reasons.append(f"추정 밸류 {row['est_valuation_krw'] / 1e8:.0f}억 > {config.MAX_VALUATION_KRW / 1e8:.0f}억")
    if reasons:
        return "탈락", "; ".join(reasons)
    if hold:
        return "보류", "; ".join(hold)
    return "통과", f"업력 {row['age_years']}년, 밸류 {row['est_valuation_krw'] / 1e8:.0f}억"


def run(con) -> None:
    cnt = {"통과": 0, "탈락": 0, "보류": 0}
    for r in con.execute("SELECT id, is_pre_startup, biz_status, is_deeptech, age_years, est_valuation_krw "
                         "FROM v_companies").fetchall():
        res, why = judge(r)
        con.execute("UPDATE companies SET hard_filter=?, hard_filter_reason=? WHERE id=?", (res, why, r["id"]))
        cnt[res] += 1
    con.commit()
    print(cnt)
