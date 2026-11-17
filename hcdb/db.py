"""DB 접근 + 기업 매칭 + 근거 기반 값 채택."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config
from .fields import COMPANY_FIELDS
from .normalize import norm_biz_no, norm_name, variant_key

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)  # 다른 수집 작업이 동시에 쓰고 있으면 잠시 기다림
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


# 기존 DB 에 나중에 추가된 칸: (테이블, 칸, 타입)
_MIGRATIONS = [
    ("patents", "ipc", "TEXT"),
    ("patents", "applicants", "TEXT"),
    ("companies", "one_liner", "TEXT"),
    ("company_aliases", "alias_variant", "TEXT"),   # normalize.variant_key — 표기 차이 매칭 보조
    ("tag_evidence", "basis", "TEXT"),              # 근거 / 추정 (지불자)
    ("tag_evidence", "reason", "TEXT"),             # 추정 이유 한 줄
]


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table, col, typ in _MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_alias_variant ON company_aliases(alias_variant)")
    todo = con.execute("SELECT rowid, alias FROM company_aliases WHERE alias_variant IS NULL").fetchall()
    for r in todo:
        con.execute("UPDATE company_aliases SET alias_variant=? WHERE rowid=?", (variant_key(r["alias"]), r["rowid"]))
    con.commit()


@contextmanager
def run_log(con, source: str, args: str = ""):
    """crawl_runs 에 실행 기록. with 블록 안에서 stats['docs'], stats['facts'] 증가."""
    cur = con.execute("INSERT INTO crawl_runs(source, args) VALUES (?,?)", (source, args))
    run_id, stats, err = cur.lastrowid, {"docs": 0, "facts": 0}, None
    try:
        yield stats
    except Exception as e:  # noqa: BLE001
        # 오류 메시지의 URL 에 API 키가 들어 있으면 가려서 저장
        import re
        err = re.sub(r"(?i)(servicekey|crtfc_key|api_key)=[^&'\" ]+", r"\1=***", repr(e))
        raise
    finally:
        con.execute(
            "UPDATE crawl_runs SET finished_at=datetime('now','localtime'), n_docs=?, n_facts=?, error=? WHERE id=?",
            (stats["docs"], stats["facts"], err, run_id),
        )
        con.commit()


# ───────────── 기업 매칭 ─────────────
def find_company(con, name: str | None = None, biz_no: str | None = None) -> int | None:
    """사업자번호 → 정규화 사명/이형 표기 순으로 매칭."""
    bn = norm_biz_no(biz_no)
    if bn:
        r = con.execute("SELECT id FROM companies WHERE biz_no=?", (bn,)).fetchone()
        if r:
            return r["id"]
    key = norm_name(name or "")
    if key:
        r = con.execute("SELECT company_id FROM company_aliases WHERE alias_norm=?", (key,)).fetchall()
        if len(r) == 1:
            return r[0]["company_id"]
        # 동명 기업이 여러 개면 자동 매칭하지 않음 → 새로 만들지 않고 None
        if len(r) > 1:
            return None
        # 표기 차이(ㅐ/ㅔ, '코리아·글로벌' 접미어)만 다른 곳이 정확히 한 곳이면 같은 회사로 본다
        vk = variant_key(name or "")
        if vk:
            r = con.execute("SELECT DISTINCT company_id FROM company_aliases WHERE alias_variant=?", (vk,)).fetchall()
            if len(r) == 1:
                return r[0]["company_id"]
    return None


def get_or_create_company(con, name: str, biz_no: str | None = None, source_url: str | None = None,
                          create: bool = True) -> int | None:
    cid = find_company(con, name, biz_no)
    if cid is None and create:
        cur = con.execute(
            "INSERT INTO companies(name, biz_no) VALUES (?,?)", (name.strip(), norm_biz_no(biz_no))
        )
        cid = cur.lastrowid
    if cid is not None:
        add_alias(con, cid, name, source_url)
        bn = norm_biz_no(biz_no)
        if bn:
            con.execute("UPDATE companies SET biz_no=? WHERE id=? AND biz_no IS NULL", (bn, cid))
    return cid


def add_alias(con, company_id: int, alias: str | None, source_url: str | None = None) -> None:
    if alias and norm_name(alias):
        con.execute(
            "INSERT OR IGNORE INTO company_aliases(company_id, alias, alias_norm, source_url, alias_variant) "
            "VALUES (?,?,?,?,?)",
            (company_id, alias.strip(), norm_name(alias), source_url, variant_key(alias)),
        )


_NO_FILL = {"hard_filter", "hard_filter_reason"}  # 병합 뒤 다시 판정하는 칸


def merge_companies(con, keep_id: int, drop_id: int) -> dict:
    """중복 기업 병합: drop 의 자식 행을 keep 으로 옮기고 drop 삭제.
    - keep 이 비어 있는 칸은 drop 값으로 채운다(근거 행도 함께 옮겨져 applied 로 둔다).
    - 둘 다 값이 있고 다르면 keep 값을 유지하고 drop 쪽 근거는 applied=0 으로 남긴다.
      단 설립일이 다르면 어느 쪽도 확정하지 않는다: 칸을 비우고 두 근거를 모두 남긴 뒤 메모.
    - 지불자·과제 태그는 다시 판정하도록 비운다(근거 이력은 남김).
    반환: {'filled': [...], 'kept': [...], 'founded_conflict': (keep값, drop값) | None}"""
    keep = dict(con.execute("SELECT * FROM companies WHERE id=?", (keep_id,)).fetchone())
    drop = dict(con.execute("SELECT * FROM companies WHERE id=?", (drop_id,)).fetchone())
    drop_ev = [r[0] for r in con.execute(
        "SELECT id FROM field_evidence WHERE company_id=? AND applied=1", (drop_id,))]
    tables = [r[0] for r in con.execute(
        "SELECT m.name FROM sqlite_master m WHERE type='table' AND sql LIKE '%company_id%'")]
    for t in tables:
        con.execute(f"UPDATE OR IGNORE {t} SET company_id=? WHERE company_id=?", (keep_id, drop_id))
        con.execute(f"DELETE FROM {t} WHERE company_id=?", (drop_id,))
    con.execute("DELETE FROM companies WHERE id=?", (drop_id,))
    out = {"filled": [], "kept": [], "founded_conflict": None}
    for f in COMPANY_FIELDS:
        if f in _NO_FILL or drop.get(f) in (None, ""):
            continue
        if keep.get(f) in (None, ""):
            con.execute(f"UPDATE companies SET {f}=? WHERE id=?", (drop[f], keep_id))
            out["filled"].append(f)
            continue  # drop 의 applied 근거가 그대로 이 값의 근거
        if str(keep[f]) == str(drop[f]):
            con.execute(f"UPDATE field_evidence SET applied=0 WHERE field=? AND id IN ({','.join('?' * len(drop_ev)) or 'NULL'})",
                        (f, *drop_ev))
            continue
        if f == "founded_date":
            con.execute("UPDATE companies SET founded_date=NULL WHERE id=?", (keep_id,))
            con.execute("UPDATE field_evidence SET applied=0 WHERE company_id=? AND field='founded_date'", (keep_id,))
            con.execute("UPDATE companies SET notes=coalesce(notes||' / ','')||? WHERE id=?",
                        (f"[설립일 충돌] 병합 전 {keep[f]} / {drop[f]} — 국민연금·국세청 대조로 정함", keep_id))
            out["founded_conflict"] = (keep[f], drop[f])
        else:
            con.execute(f"UPDATE field_evidence SET applied=0 WHERE field=? AND id IN ({','.join('?' * len(drop_ev)) or 'NULL'})",
                        (f, *drop_ev))
            out["kept"].append(f)
    # 태그 다시 판정 대상으로 (manual 근거는 건드리지 않음)
    for kind in ("payer", "task", "customer", "task_group"):
        if con.execute("SELECT 1 FROM tag_evidence WHERE company_id=? AND kind=? AND applied=1 AND source='manual'",
                       (keep_id, kind)).fetchone():
            continue
        con.execute("UPDATE tag_evidence SET applied=0 WHERE company_id=? AND kind=?", (keep_id, kind))
        con.execute("DELETE FROM company_tags WHERE company_id=? AND kind=?", (keep_id, kind))
    return out


# ───────────── 근거 기반 값 채택 ─────────────
def set_field(con, company_id: int, field: str, value, source: str, source_url: str | None = None,
              confidence: str | None = "중") -> bool:
    """근거를 쌓고, 기존 채택값보다 우선순위가 같거나 높으면 companies 에 반영. 반영 여부 반환."""
    if field not in COMPANY_FIELDS:
        raise KeyError(f"알 수 없는 칸: {field}")
    if value is None or value == "":
        return False
    if field == "biz_no":
        value = norm_biz_no(value)
        if not value:
            return False
    # 현재 값과 같으면 근거를 새로 쌓지 않음 (엑셀 왕복 시 자동수집값이 manual 로 둔갑하는 것 방지)
    cur_val = con.execute(f"SELECT {field} FROM companies WHERE id=?", (company_id,)).fetchone()[0]
    if cur_val is not None and str(cur_val) == str(value):
        return False
    cur = con.execute(
        "INSERT INTO field_evidence(company_id, field, value, source, source_url, confidence) VALUES (?,?,?,?,?,?)",
        (company_id, field, str(value), source, source_url, confidence),
    )
    ev_id = cur.lastrowid
    prev = con.execute(
        "SELECT source, confidence FROM field_evidence WHERE company_id=? AND field=? AND applied=1",
        (company_id, field),
    ).fetchone()
    if prev is not None:
        if prev["source"] in config.SOURCE_OVERRIDE and source not in config.SOURCE_OVERRIDE:
            return False
        if source not in config.SOURCE_OVERRIDE and \
                config.CONF_RANK.get(confidence, 0) < config.CONF_RANK.get(prev["confidence"], 0):
            return False
    con.execute("UPDATE field_evidence SET applied=0 WHERE company_id=? AND field=?", (company_id, field))
    con.execute("UPDATE field_evidence SET applied=1 WHERE id=?", (ev_id,))
    try:
        con.execute(
            f"UPDATE companies SET {field}=?, updated_at=datetime('now','localtime') WHERE id=?",
            (value, company_id),
        )
    except sqlite3.IntegrityError:
        # 사업자번호가 다른 기업에 이미 있음 → 중복 기업. 반영하지 않고 메모만 남김
        con.execute("UPDATE field_evidence SET applied=0 WHERE id=?", (ev_id,))
        con.execute("UPDATE companies SET notes=coalesce(notes||' / ','')||? WHERE id=?",
                    (f"[중복의심] {field}={value}", company_id))
        return False
    return True


def set_fields(con, company_id: int, values: dict, source: str, source_url=None, confidence="중") -> int:
    return sum(set_field(con, company_id, k, v, source, source_url, confidence)
               for k, v in values.items() if v not in (None, ""))


def add_tag(con, company_id: int, kind: str, value: str, source_url=None) -> None:
    if value and value.strip():
        con.execute("INSERT OR IGNORE INTO company_tags VALUES (?,?,?,?)",
                    (company_id, kind, value.strip(), source_url))


def upsert_row(con, table: str, row: dict) -> int:
    """자식 테이블 공통 upsert (UNIQUE 충돌 시 NULL 아닌 새 값으로 갱신)."""
    cols = list(row)
    ph = ",".join("?" * len(cols))
    upd = ",".join(f"{c}=coalesce(excluded.{c}, {table}.{c})" for c in cols)
    sql = f"INSERT INTO {table}({','.join(cols)}) VALUES ({ph}) ON CONFLICT DO UPDATE SET {upd}"
    try:
        cur = con.execute(sql, [row[c] for c in cols])
        return cur.lastrowid
    except sqlite3.OperationalError:
        # UNIQUE 제약이 없는 테이블
        cur = con.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({ph})", [row[c] for c in cols])
        return cur.lastrowid


def get_or_create_investor(con, name: str, inv_type: str | None = None) -> int:
    key = norm_name(name)
    r = con.execute("SELECT id FROM investors WHERE name_norm=?", (key,)).fetchone()
    if r:
        if inv_type:
            con.execute("UPDATE investors SET type=coalesce(type, ?) WHERE id=?", (inv_type, r["id"]))
        return r["id"]
    return con.execute("INSERT INTO investors(name, name_norm, type) VALUES (?,?,?)",
                       (name.strip(), key, inv_type)).lastrowid


def add_round(con, company_id: int, round_: str | None, date: str | None, amount: int | None,
              investors: list[str] = (), source_url=None, confidence="하") -> int:
    upsert_row(con, "funding_rounds", {
        "company_id": company_id, "round": round_, "announced_date": date, "amount_krw": amount,
        "source_url": source_url, "confidence": confidence,
    })
    rid = con.execute(
        "SELECT id FROM funding_rounds WHERE company_id=? AND round IS ? AND announced_date IS ?",
        (company_id, round_, date)).fetchone()["id"]
    for name in investors:
        iid = get_or_create_investor(con, name)
        con.execute("INSERT OR IGNORE INTO round_investors(round_id, investor_id) VALUES (?,?)", (rid, iid))
    return rid
