# 디지털 헬스케어 스타트업 DB (hcdb)

SQLite 한 파일(`data/startups.db`) + Python 스크립트. 서버 없이 로컬에서 돌리고, 보는 건 엑셀로.

## 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env          # API 키 입력 (없는 소스는 건너뜀)
python -m hcdb init
python -m hcdb news --limit 50               # 뉴스 검색 → 기사 수집 → 기업·라운드 추출
python -m hcdb import 내가정리한파일.xlsx      # 수기 입력분 반영
python -m hcdb nts                            # 국세청 영업상태
python -m hcdb dart && python -m hcdb kipris  # 결산·특허
python -m hcdb nps 국민연금_가입사업장_202610.csv
python -m hcdb filter                         # 하드 필터 판정
python -m hcdb export                         # data/startups_YYYYMMDD_HHMM.xlsx
python -m hcdb stats
```

DB 를 직접 보고 싶으면 [DB Browser for SQLite](https://sqlitebrowser.org/) 또는 `pip install datasette && datasette data/startups.db`.

## DB 구성

```
companies ─┬─ company_aliases      사명 이형 표기·영문명 (매칭 키)
 (1행=1사) ├─ field_evidence       칸별 출처·신뢰도 이력 ← companies 의 모든 단일값 근거
           ├─ company_tags         고객 / 지불주체 / 키워드(과제·수단·고객) / 참여프로그램 (다중값)
           ├─ funding_rounds ── round_investors ── investors (유형·G1~G3)
           ├─ grants               지원금 + 팁스(program='TIPS', operator=운영사)
           ├─ financials           연도별 매출·영업이익·자본총계 (원천별)
           ├─ founders             창업자 배경
           ├─ headcount_snapshots  국민연금 월별 인원 ← 기울기
           ├─ job_posting_snapshots / service_metrics / partners(PoC·고객사)
           ├─ regulatory           인허가·혁신의료기기·규제샌드박스
           ├─ patents / awards
           └─ doc_company_links ── raw_documents  수집 원문(본문 텍스트) 캐시
crawl_runs   수집 실행 로그
v_companies  업력·누적투자금·첫 투자일·팁스·특허 수 등 계산 칸을 붙인 VIEW (엑셀 '기업' 시트)
```

설계 원칙 세 가지:

1. **칸별 출처.** `companies` 의 값은 `set_field()` 로만 바꾼다. 매번 `field_evidence` 에 (값, 원천, URL, 신뢰도)가 쌓이고, 기존 채택값보다 신뢰도가 같거나 높을 때만 반영된다. 사람이 넣은 값(`source='manual'`)은 자동 수집값에 덮이지 않는다.
2. **계산값은 저장하지 않음.** 업력·누적투자금·기투자 횟수·팁스 여부는 VIEW 에서 계산해서 시간이 지나도 틀리지 않는다.
3. **중복 판정.** 사업자등록번호가 있으면 그것, 없으면 정규화 사명(`(주)`, `Inc.`, 공백 제거)이 이형 표기 테이블과 맞는지로 판정한다. 동명 기업이 둘 이상이면 자동으로 붙이지 않는다. 잘못 갈라진 기업은 `python -m hcdb merge 남길ID 지울ID` 로 합친다.

## 엑셀 왕복

`export` 의 '기업' 시트를 고친 뒤 `import 파일.xlsx --sheet 기업` 으로 다시 넣으면 **바뀐 칸만** manual 근거로 반영된다. 안 바꾼 칸은 그대로 둔다. '출처 URL', '신뢰도' 열을 채우면 그 행 근거로 기록된다. `export --template` 은 빈 입력 양식만 만든다.

## 수집 소스

| 소스 | 명령 | 채우는 칸 | 키 |
|---|---|---|---|
| 뉴스 (네이버 검색 API, 없으면 구글뉴스 RSS 제목) | `news`, `article URL…` | 사명·대표·라운드·금액·투자사 (신뢰도 하) | NAVER_* (선택) |
| 국세청 사업자 상태조회 | `nts` | 영업 상태·사업자등록 여부 | DATA_GO_KR_KEY |
| OpenDART | `dart` | 사업자번호·설립일·대표·홈페이지·결산 | DART_KEY |
| KIPRIS Plus | `kipris` | 특허 출원·등록·PCT | KIPRIS_KEY |
| 국민연금 가입사업장 CSV (월별 파일) | `nps 파일.csv` | 월별 인원·신규·상실 | 없음 (data.go.kr 파일 다운로드) |
| 연구실·창업 프로그램 페이지 | `labs --config config/labs.json` | 예비창업팀·소속·서비스 | 없음 |
| 수기 (LinkedIn·원티드·IR·데모데이 자료 등) | `import 파일` | 아무 칸 | 없음 |

### 연구실 페이지 추가하기
사이트마다 구조가 달라 CSS 선택자 설정으로 붙인다. `config/labs.example.json` 을 `config/labs.json` 으로 복사해서 항목을 추가하고, 먼저 `labs --dry-run` 으로 추출 결과를 확인한다. JS 로 그려지는 페이지는 `"render": true` (최초 1회 `playwright install chromium` 필요).

### LinkedIn
자동 크롤링은 넣지 않았다. LinkedIn 약관이 스크래핑을 금지하고, 로그인 계정이 바로 제한된다. 창업자 배경·인원은 직접 보고 수기 시트로 `import` 하거나, 국민연금 인원·KIPRIS 발명자 같은 공개 데이터로 대체한다.

### 크롤링 매너 (config.py)
robots.txt 준수, 같은 호스트에 2초 간격, 같은 URL 은 30일 캐시. 구글뉴스 기사 링크는 robots 로 막혀 있어 제목·요약만 저장한다.

## 다음에 붙이면 좋은 것
- **LLM 추출**: 규칙 기반 뉴스 추출은 "OO(대표 XXX)" 표기와 제목 패턴만 잡는다. `raw_documents.body` 를 Claude API 에 넘겨 칸별 JSON 으로 받아 `set_field(..., source='llm', confidence='중')` 으로 넣으면 BM·고객·지불주체·PoC 까지 채울 수 있다.
- **채용공고 수**: 원티드·사람인 공개 검색 결과 개수를 `job_posting_snapshots` 에 월 1회 저장.
- **FIRST 연결**: FIRST 투자사 장부를 `investors.grp/type` 에, G1 딜 시트를 별도 테이블로 넣고 `cb_market` 으로 조인하면 `g1_same_market_cnt` 를 자동 계산할 수 있다.
- **하드 필터 기준**: `config.py` 의 `MAX_AGE_YEARS` / `DEEPTECH_EXTRA_YEARS` / `MAX_VALUATION_KRW` 를 실제 기준에 맞게 수정.
