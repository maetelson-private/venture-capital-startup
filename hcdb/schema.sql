-- 디지털 헬스케어 스타트업 DB (SQLite)
-- 원칙
--  1) companies 는 "현재 채택된 값" 한 줄. 모든 값의 근거는 field_evidence 에 이력으로 남김
--  2) 1:N 정보(라운드·지원금·특허 등)는 자식 테이블. 행마다 source_url·confidence 보유
--  3) 업력·누적투자금·팁스 여부처럼 계산 가능한 값은 저장하지 않고 VIEW 에서 계산
PRAGMA foreign_keys = ON;

-- ───────────────────────── 1. 식별·수집 관리 ─────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY,
    biz_no              TEXT UNIQUE,              -- 사업자등록번호 10자리(숫자만). 중복 판정 1순위 키
    corp_reg_no         TEXT,                     -- 법인등록번호
    dart_corp_code      TEXT,
    name                TEXT NOT NULL,            -- 대표 사명
    name_en             TEXT,

    -- 2. 기본 정보·하드 필터
    url                 TEXT,
    ceo                 TEXT,
    founded_date        TEXT,                     -- YYYY-MM-DD. 업력은 VIEW 에서 계산
    sido                TEXT,
    sigungu             TEXT,
    lab_sido            TEXT,                     -- 연구소 소재지
    lab_sigungu         TEXT,
    lab_note            TEXT,                     -- 예: OO대병원 내 연구소
    is_registered       INTEGER,                  -- 사업자등록 여부 0/1
    biz_status          TEXT CHECK (biz_status IN ('계속','휴업','폐업','미상')),
    biz_status_checked  TEXT,
    is_deeptech         INTEGER,                  -- 0/1
    est_valuation_krw   INTEGER,                  -- NULL = 미상
    valuation_basis     TEXT,                     -- 산정 근거(라운드 금액/지분율)
    hard_filter         TEXT CHECK (hard_filter IN ('통과','탈락','보류')),
    hard_filter_reason  TEXT,

    -- 3. 분류 (FIRST 체계)
    cb_category         TEXT,                     -- 8개 중 1
    cb_market           TEXT,                     -- 243개 중 주 Market 1
    theme_id            TEXT,                     -- T01~T24
    target_area         TEXT,                     -- GTCM Mash-up 결과
    scope               TEXT CHECK (scope IN ('포함','제외','보류')),
    scope_reason        TEXT,
    bm_summary          TEXT,
    service_summary     TEXT,
    -- 고객·지불주체·키워드는 다중값이라 company_tags 에 저장

    -- 5. 팀
    headcount           INTEGER,                  -- 최신 인원(추이는 headcount_snapshots)
    founder_background  TEXT,                     -- 의사/연구자/엔지니어/업계출신 (대표 창업자 기준)
    tech_origin         TEXT CHECK (tech_origin IN ('대학실험실','병원','기업분사','자체') OR tech_origin IS NULL),

    -- 7. 기술·제도 요약 (상세는 regulatory)
    regulatory_status   TEXT,                     -- 미대상/임상중/식약처허가/FDA·CE
    device_class        TEXT,                     -- 의료기기 등급 1~4

    -- 8. FIRST 판정 연결 (팀 판정값)
    g1_same_market_cnt  INTEGER,
    route               TEXT CHECK (route IN ('A','B') OR route IS NULL),
    gap_cause           TEXT CHECK (gap_cause IN ('시차형','우회형','국내기원형') OR gap_cause IS NULL),
    g3_si_note          TEXT,

    -- 9. 예비창업팀 전용
    is_pre_startup      INTEGER NOT NULL DEFAULT 0,
    affiliation         TEXT,                     -- 대학·연구실·병원·재직중
    expected_founding   TEXT,
    contact_channel     TEXT,                     -- 공개된 창구만

    confidence          TEXT CHECK (confidence IN ('상','중','하')),
    collected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    notes               TEXT
);

-- 사명 이형 표기 / 영문명. alias_norm 으로 매칭
CREATE TABLE IF NOT EXISTS company_aliases (
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_norm  TEXT NOT NULL,
    source_url  TEXT,
    PRIMARY KEY (company_id, alias_norm)
);
CREATE INDEX IF NOT EXISTS ix_alias_norm ON company_aliases(alias_norm);

-- 칸별 출처: companies 의 모든 단일값 칸은 여기 근거가 쌓이고, 우선순위 규칙으로 채택됨
CREATE TABLE IF NOT EXISTS field_evidence (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field        TEXT NOT NULL,
    value        TEXT,
    source       TEXT NOT NULL,                   -- news / nts / dart / kipris / nps / lab / manual ...
    source_url   TEXT,
    confidence   TEXT CHECK (confidence IN ('상','중','하')),
    collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    applied      INTEGER NOT NULL DEFAULT 0       -- 현재 companies 에 반영된 근거 = 1
);
CREATE INDEX IF NOT EXISTS ix_evidence_cf ON field_evidence(company_id, field);

-- 다중값 태그: customer(고객) / payer(지불주체) / kw_task / kw_means / kw_customer / program(참여 프로그램)
CREATE TABLE IF NOT EXISTS company_tags (
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    source_url  TEXT,
    PRIMARY KEY (company_id, kind, value)
);

-- ───────────────────────── 4. 자금 ─────────────────────────
CREATE TABLE IF NOT EXISTS investors (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_norm  TEXT NOT NULL UNIQUE,
    type       TEXT,                              -- VC/AC/CVC/SI/엔젤/정책금융 ... (FIRST 10종)
    grp        TEXT,                              -- G1/G2/G3 (FIRST 투자사 장부 연결)
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS funding_rounds (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    round          TEXT,                          -- 프리시드/시드/프리A/A/B/브릿지 ...
    announced_date TEXT,
    amount_krw     INTEGER,
    post_val_krw   INTEGER,
    equity_pct     REAL,
    source_url     TEXT,
    confidence     TEXT,
    collected_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (company_id, round, announced_date)
);

CREATE TABLE IF NOT EXISTS round_investors (
    round_id    INTEGER NOT NULL REFERENCES funding_rounds(id) ON DELETE CASCADE,
    investor_id INTEGER NOT NULL REFERENCES investors(id),
    is_lead     INTEGER DEFAULT 0,
    PRIMARY KEY (round_id, investor_id)
);

-- 지원금 이력. 팁스도 여기에 program='TIPS', operator=운영사 로 저장
CREATE TABLE IF NOT EXISTS grants (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    program     TEXT NOT NULL,                    -- 예비창업패키지/초기창업패키지/청년창업사관학교/TIPS ...
    year        INTEGER,
    amount_krw  INTEGER,
    operator    TEXT,
    source_url  TEXT,
    confidence  TEXT,
    UNIQUE (company_id, program, year)
);

CREATE TABLE IF NOT EXISTS financials (
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fiscal_year       INTEGER NOT NULL,
    revenue_krw       INTEGER,
    op_income_krw     INTEGER,
    total_equity_krw  INTEGER,
    source            TEXT NOT NULL,              -- dart / 기업공시 / 기사 / 수기
    source_url        TEXT,
    PRIMARY KEY (company_id, fiscal_year, source)
);

-- ───────────────────────── 5. 팀 ─────────────────────────
CREATE TABLE IF NOT EXISTS founders (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT,
    role        TEXT,
    background  TEXT,                             -- 의사/연구자/엔지니어/업계출신
    affiliation TEXT,
    source_url  TEXT,
    UNIQUE (company_id, name)
);

-- ───────────────────────── 6. 기울기 신호 ─────────────────────────
CREATE TABLE IF NOT EXISTS headcount_snapshots (     -- 국민연금 가입자 수 월별
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    month       TEXT NOT NULL,                    -- YYYY-MM
    headcount   INTEGER NOT NULL,
    new_hires   INTEGER,
    leavers     INTEGER,
    source      TEXT NOT NULL DEFAULT 'nps',
    PRIMARY KEY (company_id, month, source)
);

CREATE TABLE IF NOT EXISTS job_posting_snapshots (
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observed_date TEXT NOT NULL,
    window_months INTEGER NOT NULL DEFAULT 6,
    postings      INTEGER NOT NULL,
    source        TEXT NOT NULL,                  -- wanted / saramin / jobkorea / manual
    source_url    TEXT,
    PRIMARY KEY (company_id, observed_date, source)
);

CREATE TABLE IF NOT EXISTS service_metrics (
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observed_date TEXT NOT NULL,
    metric        TEXT NOT NULL,                  -- app_downloads / mau / web_visits ...
    value         REAL,
    source_url    TEXT,
    PRIMARY KEY (company_id, observed_date, metric)
);

CREATE TABLE IF NOT EXISTS partners (               -- PoC·파트너·고객사
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    partner_name TEXT NOT NULL,
    relation     TEXT,                            -- PoC/파트너/고객사/공동연구/MOU
    partner_type TEXT,                            -- 병원/보험사/제약/기업/공공
    started_date TEXT,
    source_url   TEXT,
    confidence   TEXT,
    UNIQUE (company_id, partner_name, relation)
);

-- ───────────────────────── 7. 기술·제도 ─────────────────────────
CREATE TABLE IF NOT EXISTS regulatory (
    id           INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    product      TEXT,
    kind         TEXT NOT NULL,                   -- 식약처허가/임상/FDA/CE/혁신의료기기/규제샌드박스
    status       TEXT,
    device_class TEXT,
    event_date   TEXT,
    source_url   TEXT,
    UNIQUE (company_id, product, kind)
);

CREATE TABLE IF NOT EXISTS patents (
    application_no  TEXT PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title           TEXT,
    status          TEXT,                         -- 출원/공개/등록/거절/소멸
    is_pct          INTEGER DEFAULT 0,
    filed_date      TEXT,
    registered_date TEXT,
    source_url      TEXT
);

CREATE TABLE IF NOT EXISTS awards (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT,                             -- 수상/데모데이
    event_date  TEXT,
    source_url  TEXT,
    UNIQUE (company_id, name)
);

-- ───────────────────────── 수집 원문·로그 ─────────────────────────
CREATE TABLE IF NOT EXISTS raw_documents (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    title        TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    http_status  INTEGER,
    content_hash TEXT,
    body         TEXT,                            -- 본문 텍스트(HTML 아님)
    processed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doc_company_links (
    doc_id     INTEGER NOT NULL REFERENCES raw_documents(id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, company_id)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    args        TEXT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    n_docs      INTEGER DEFAULT 0,
    n_facts     INTEGER DEFAULT 0,
    error       TEXT
);

-- ───────────────────────── 계산 VIEW ─────────────────────────
DROP VIEW IF EXISTS v_companies;
CREATE VIEW v_companies AS
SELECT
    c.*,
    CASE WHEN c.founded_date IS NOT NULL
         THEN round((julianday('now') - julianday(c.founded_date)) / 365.25, 1) END   AS age_years,
    (SELECT group_concat(alias, ' | ') FROM company_aliases a WHERE a.company_id = c.id)  AS aliases,
    (SELECT group_concat(value, ', ') FROM company_tags t WHERE t.company_id = c.id AND kind='customer') AS customers,
    (SELECT group_concat(value, ', ') FROM company_tags t WHERE t.company_id = c.id AND kind='payer')    AS payers,
    (SELECT group_concat(kind || ':' || value, ', ') FROM company_tags t
        WHERE t.company_id = c.id AND kind LIKE 'kw_%')                                   AS keywords,
    (SELECT count(*) FROM funding_rounds r WHERE r.company_id = c.id)                     AS n_rounds,
    (SELECT min(announced_date) FROM funding_rounds r WHERE r.company_id = c.id)          AS first_invest_date,
    (SELECT round FROM funding_rounds r WHERE r.company_id = c.id
        ORDER BY announced_date DESC LIMIT 1)                                             AS latest_round,
    (SELECT sum(amount_krw) FROM funding_rounds r WHERE r.company_id = c.id)              AS total_funding_krw,
    (SELECT group_concat(DISTINCT i.name) FROM funding_rounds r
        JOIN round_investors ri ON ri.round_id = r.id
        JOIN investors i ON i.id = ri.investor_id WHERE r.company_id = c.id)              AS investor_names,
    (SELECT group_concat(DISTINCT i.type) FROM funding_rounds r
        JOIN round_investors ri ON ri.round_id = r.id
        JOIN investors i ON i.id = ri.investor_id WHERE r.company_id = c.id)              AS investor_types,
    EXISTS (SELECT 1 FROM grants g WHERE g.company_id = c.id AND g.program = 'TIPS')      AS tips_selected,
    (SELECT operator FROM grants g WHERE g.company_id = c.id AND g.program = 'TIPS' LIMIT 1) AS tips_operator,
    (SELECT headcount FROM headcount_snapshots h WHERE h.company_id = c.id
        ORDER BY month DESC LIMIT 1)                                                      AS nps_headcount,
    (SELECT postings FROM job_posting_snapshots j WHERE j.company_id = c.id
        ORDER BY observed_date DESC LIMIT 1)                                              AS job_postings_6m,
    (SELECT count(*) FROM partners p WHERE p.company_id = c.id)                           AS n_partners,
    (SELECT count(*) FROM patents p WHERE p.company_id = c.id AND p.status = '등록')       AS n_patents_registered,
    (SELECT count(*) FROM patents p WHERE p.company_id = c.id)                            AS n_patents_total,
    (SELECT count(*) FROM patents p WHERE p.company_id = c.id AND p.is_pct = 1)           AS n_pct,
    EXISTS (SELECT 1 FROM regulatory g WHERE g.company_id = c.id AND g.kind = '혁신의료기기') AS innovative_device,
    EXISTS (SELECT 1 FROM regulatory g WHERE g.company_id = c.id AND g.kind = '규제샌드박스') AS sandbox,
    (SELECT count(*) FROM awards w WHERE w.company_id = c.id)                             AS n_awards
FROM companies c;

-- 인원 추이(월별 증감) — 기울기 확인용
DROP VIEW IF EXISTS v_headcount_trend;
CREATE VIEW v_headcount_trend AS
SELECT h.company_id, c.name, h.month, h.headcount,
       h.headcount - lag(h.headcount) OVER (PARTITION BY h.company_id ORDER BY h.month) AS delta
FROM headcount_snapshots h JOIN companies c ON c.id = h.company_id;
