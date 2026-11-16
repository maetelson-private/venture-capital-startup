# 디지털 헬스케어 스타트업 DB (hcdb)

SQLite 한 파일(`data/startups.db`) + Python 스크립트. 서버 없이 로컬에서 돌리고, 보는 건 엑셀로.

## 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env          # API 키 입력 (없는 소스는 건너뜀, 발급 방법은 아래)
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

## API 키 발급

모두 무료예요. Claude API만 사용량만큼 과금돼요. 키는 `.env` 에만 두고 커밋하지 않아요(`.gitignore` 에 포함).

| 키 | 발급처 | 방법 | 쓰는 곳 |
|---|---|---|---|
| `DATA_GO_KR_KEY` | data.go.kr | 회원가입 → 아래 API 를 하나씩 검색해 **활용신청** → 마이페이지 > 개발계정 > '일반 인증키(Decoding)' 복사. 대부분 자동 승인이지만 실제로 호출되기까지 1~2시간 걸리기도 함 | 국세청 상태조회, 국민연금 가입사업장, K-Startup 공고, 식약처 의료기기 허가·임상 |
| `KIPRIS_KEY` | plus.kipris.or.kr | 회원가입 → 서비스 신청에서 '특허·실용신안 정보' 계열 상품 신청 → 승인 후 마이페이지에서 키 확인. 무료 호출 한도가 있으니 신청 화면에서 확인 | 특허 출원인·발명자 |
| `NTIS_KEY` | ntis.go.kr | 회원가입 → 국가R&D 정보 Open API 활용 신청(사용 목적 기재) → 승인 후 발급 | 국가R&D 과제 수행기관 |
| `DART_KEY` | opendart.fss.or.kr | 인증키 신청 → 이메일 인증 → 즉시 발급 | 기업개황·결산 |
| `NAVER_CLIENT_ID/SECRET` | developers.naver.com | Application 등록 → 사용 API '검색' 선택 → Client ID/Secret 발급 (하루 25,000회) | 뉴스 검색 |
| `ANTHROPIC_API_KEY` | console.anthropic.com | 결제수단 등록 → API Keys → Create Key | 기사·HWP·PDF 명단 추출 (선택) |

data.go.kr 에서 활용신청할 API (검색창에 아래 이름을 넣으면 나와요. 정확한 상품명은 포털에서 다시 확인하세요):
- `국세청_사업자등록정보 진위확인 및 상태조회 서비스`
- `국민연금공단_국민연금 가입 사업장 내역` (API 와 월별 파일데이터 둘 다 있음)
- `창업진흥원_K-Startup` 조회서비스 (사업공고)
- `식품의약품안전처_의료기기 품목허가 정보`, 의료기기 임상시험 승인 관련 API

### 키 없이 가져오는 원천
API 가 없어서 페이지 크롤링이나 첨부파일 파싱으로 가져와요.
- **공고·보도자료 첨부(HWP·PDF)**: 실험실창업탐색팀, 학생창업유망팀, 보건산업혁신창업센터, 서울바이오허브, 식약처 혁신의료기기 지정 공고, 규제샌드박스 승인과제. 명단이 대부분 첨부파일에 있어 첨부파일 파서가 필요해요(아직 미구현).
- **HTML 페이지**: 병원 인큐베이터, 넥스트유니콘 공고 목록, DHP 프로그램, 업계 언론(메디게이트뉴스·데일리팜·메디칼타임즈·벤처스퀘어·와우테일). `labs` 설정 방식으로 붙여요.
- **파일 다운로드**: 벤처확인기업 목록(벤처확인종합관리시스템), 기업부설연구소 현황(한국산업기술진흥협회).
- **수기·필터용**: THE VC, 혁신의숲, 스타트업레시피는 공개 API 가 없어서 이미 투자받았는지 확인하는 용도로만 써요. 수기 `import` 로 넣어요. 원티드·로켓펀치 채용 공고는 검색 결과 개수만 월 1회 기록해요.

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
| **공고·보도자료 첨부 명단** (실험실창업탐색팀, 학생창업유망팀, 혁신창업멤버스, 규제샌드박스, 혁신의료기기 보도자료 등) | `notice URL --kind program\|sandbox\|innov --program 이름 --year 연도` | 프로그램 선정·지원금 / 규제샌드박스 / 혁신의료기기 | ANTHROPIC_API_KEY |
| **게시판 자동 순회** (식약처 보도자료·공지, 과기정통부 보도자료, 산업부 보도자료) | `boards [--pages 3] [--only 식약처] [--dry-run]` | 위 `notice` 와 같음. 선정·지정·승인 공고만 골라 자동 처리 | ANTHROPIC_API_KEY |
| **업계 언론 RSS** (벤처스퀘어·스타트업레시피·와우테일·플래텀·히트뉴스·모비인사이드) | `feeds [매체…]` | 기업·라운드·투자사·BM·고객·지불주체·PoC·프로그램 (신뢰도 중) | ANTHROPIC_API_KEY |
| **KBIC 보건산업혁신창업센터 창업기업** (~300개사, 실명) | `kbic [--all]` | 대표·설립일·주소·인원·매출·정부지원사업 (신뢰도 상). 기본은 디지털헬스케어·의료기기만 | 없음 |
| **TIPS 선정기업** (jointips.or.kr 공개 API, 헬스케어·바이오의료 ~1,100곳) | `tips` | 설립일·대표·홈페이지·소개·운영사. 팁스 = 운영사가 이미 투자 → '이미 투자받음' 표시 | 없음 |
| **포트폴리오·보육** (DHP, D.CAMP 바이오/헬스케어, NAVER D2SF 헬스케어) | `portfolio [--only dhp\|dcamp\|d2sf]` | DHP 투자연도·분야 / D.CAMP 사업자번호·설립일·투자 여부 / D2SF 투자 단계·연도·창업자·홈페이지 | 없음 |
| **대학 기술지주 자회사·창업지원단 동문기업** (고려대·서울대·미래과학(KAIST·UNIST·GIST·DGIST)·POSTECH·UNIST·한양·이화·충남·전남·강원·전북연합·가톨릭·중앙 기술지주, 서울대 동문기업) | `univ [--only ku snu sth mirae …]` | 소개·홈페이지·설립일·소속 대학, program 태그. 기술지주 출자는 VC 라운드가 아니라서 '첫 투자 후보'로 남김 | 없음 |
| **기술지주 감사보고서 (DART)** — 한국과학기술지주·고려대·미래과학 등 DART 에 감사보고서를 내는 곳 | `dart-holdings [--only corp_code…]` | 주석의 매도가능증권·지분법투자주식·투자유가증권 표에서 투자기업 이름 → DB 에 있으면 '○○ 출자' 태그, 이름에 의료 단서가 있으면 새 기업. 나머지는 `data/dart_holdings_review.json` | DART_KEY |
| **정책브리핑(korea.kr) 보도자료 첨부 명단** (아기유니콘, 예비유니콘, 초격차, 혁신기업 국가대표, 딥테크 챌린지 등) | `briefing [--q 검색어…] [--since 2019-01-01] [--dry-run]` | 선정 명단 표를 LLM 없이 규칙으로 읽음. 헬스케어 행은 새 기업, 나머지는 DB 에 있는 기업에만 프로그램 태그. 처리한 보도자료는 다음에 건너뜀 | 없음 |
| 식약처 의료기기 제조·수입업 허가 | `mfds --only mnft` | DB 에 있는 기업에만 허가일·휴폐업을 붙임(업체명만으론 디지털 여부를 몰라 시드로는 안 씀) | DATA_GO_KR_KEY |
| **롱리스트 카드 페이지** | `cards` | `data/longlist.html` — 검색·키워드 필터·첫 투자 후보만 보기 | – |
| 업계 언론 과거 기사 | `feeds --archive --pages 60 --since 2023-11-01` | 헬스케어 태그 피드까지 과거 페이지를 훑음 | ANTHROPIC_API_KEY |
| **빈칸 보강 (Claude 웹 검색)** | `enrich [--limit N]` | 설립일이 빈 기업의 설립일·대표·홈페이지·시도 (출처 URL 기록, 신뢰도 중) | ANTHROPIC_API_KEY |
| 식약처 혁신의료기기 지정 현황 API | `mfds` | 혁신의료기기 지정 | DATA_GO_KR_KEY + 이 API 활용신청 |
| 뉴스 검색 (네이버 API, 없으면 구글뉴스 RSS 제목) | `news`, `article URL…` | 위와 같음. 키가 없으면 규칙 기반(신뢰도 하) | NAVER_* (선택) |
| 국세청 사업자 상태조회 | `nts` | 영업 상태·사업자등록 여부 | DATA_GO_KR_KEY |
| OpenDART | `dart` | 사업자번호·설립일·대표·홈페이지·결산 | DART_KEY |
| KIPRIS Plus | `kipris` | 특허 출원·등록·PCT | KIPRIS_KEY |
| 국민연금 가입사업장 CSV (월별 파일) | `nps 파일.csv` | 월별 인원·신규·상실 | 없음 (data.go.kr 파일 다운로드) |
| 연구실·창업 프로그램 페이지 | `labs --config config/labs.json` | 예비창업팀·소속·서비스 | 없음 |
| 수기 (LinkedIn·원티드·IR·데모데이 자료 등) | `import 파일` | 아무 칸 | 없음 |

### 공고 명단 넣기 (`notice`)
게시글 URL 을 주면 본문과 첨부(HWP·HWPX·PDF·XLSX·DOCX·ZIP)를 받아 텍스트로 바꾸고, Claude 가 명단 표에서 기업·팀명·대표·소속·아이템을 뽑는다. 헬스케어 관련 행만 저장한다(`--all` 이면 전부). 먼저 `--dry-run` 으로 결과를 확인하고 넣는 걸 권장. 첨부를 직접 받은 경우 URL 대신 파일 경로를 줘도 된다. 가려진 대표 이름(김**)은 저장하지 않는다.

```bash
python -m hcdb notice "https://www.mfds.go.kr/brd/m_99/view.do?seq=49659" --kind innov --year 2025 --dry-run
python -m hcdb notice 학생창업유망팀_선정결과.hwp --kind program --program 학생창업유망팀 --year 2026
```

`javascript:` 로 내려받는 게시판(첨부 링크가 스크립트 함수인 곳)은 자동으로 못 받는다. 그런 곳은 파일을 받아 경로로 넣는다.

### 예비창업·실험실 팀 명단 구하는 법 (2026-11 조사)
| 프로그램 | 공식 명단 | 현실적인 경로 |
|---|---|---|
| 실험실창업탐색팀 (과기정통부·COMPA, 2026년부터 TeX-Corps) | **비공개**. 발표는 팀 수만 공개 | 과학기술 창업경진대회(8월)·실험실창업 페스티벌(12월)·SPRINT Unicorn's Day 수상·결선 기사 → `article URL` |
| 학생창업유망팀 300+ (교육부·연구재단·청년기업가정신재단) | 공식 게시 없음 | 매년 6~8월 대학 홍보기사에 팀명·아이템이 나옴 → 뉴스 검색 "학생 창업유망팀 선정" → `article`. u300.kr '온라인 전시관'에 팀 목록이 있을 수 있음(직접 확인 필요) |
| 예비창업패키지·초기창업패키지 | **비공개** (개별 통보). 보건·바이오 특화 트랙 없음 | 주관기관 데모데이 기사 정도 |
| KBIC 혁신창업멤버스 | **공개** (창업기업소개 300개사) | `kbic` |

### 게시판 자동 순회 (`boards`)
게시판별 최근 N페이지와(검색되는 곳은) 키워드 검색 결과를 훑어, 제목 규칙에 맞는 선정·지정·승인 공고만 `notice` 로 넘긴다. 한 번 처리한 게시글은 다시 처리하지 않으니 주 1회 돌리면 새 공고만 들어온다. 제목 규칙(`RULES`)이 kind 와 프로그램명을 정한다(예: '실험실창업' → program/실험실창업탐색팀, '혁신의료기기…지정' → innov, '규제특례…승인' → sandbox).

```bash
python -m hcdb boards --dry-run      # 무엇을 처리할지 먼저 확인
python -m hcdb boards                # 실제 처리
```

### 연구실 페이지 추가하기
사이트마다 구조가 달라 CSS 선택자 설정으로 붙인다. `config/labs.example.json` 을 `config/labs.json` 으로 복사해서 항목을 추가하고, 먼저 `labs --dry-run` 으로 추출 결과를 확인한다. JS 로 그려지는 페이지는 `"render": true` (최초 1회 `playwright install chromium` 필요).

### LinkedIn
자동 크롤링은 넣지 않았다. LinkedIn 약관이 스크래핑을 금지하고, 로그인 계정이 바로 제한된다. 창업자 배경·인원은 직접 보고 수기 시트로 `import` 하거나, 국민연금 인원·KIPRIS 발명자 같은 공개 데이터로 대체한다.

### 대학 기술지주 (`univ`) 메모 (2026-11 조사)
- 키: `ku` 고려대, `snu` 서울대 동문기업, `sth` 서울대기술지주(바이오 분류), `mirae` 미래과학기술지주(바이오·의료), `catholic` 가톨릭대, `postech`, `unist`, `hanyang`, `ewha`, `cnu` 충남대, `jnu` 전남대, `kangwon` 강원대, `jb` 전북지역대학연합, `cau` 중앙대.
- 분류를 주는 곳(서울대·미래·가톨릭·고려대)은 그 분류로, 나머지는 회사 소개의 의료·바이오 키워드로 고른다 → 이후 `scope` 판정이 한 번 더 거른다.
- 브라우저로 읽어 `config/*.json` 에 둔 곳: `kst` 한국과학기술지주(JS 렌더링, 186곳 중 헬스케어 38곳), `gist_browser` GIST(파이썬 인증서 검증 실패). 목록이 바뀌면 파일을 갱신.
- 기본 실행에서 뺀 곳: `pnu` 부산대(인증서가 다른 주소용이라 브라우저로도 안 열림)·`gist`(직접 수집판).
- DART 감사보고서: 감사보고서를 내는 기술지주는 한국과학기술지주(2025)·고려대·미래과학(2019)·가톨릭(2022, 회사명 없이 합계만)뿐. 연세·성균관·포항공대·울산대·전북연합은 감사보고서 공시가 없음.
- 넣지 않은 곳: 연세대기술지주(robots.txt 가 Claude 계열 봇을 이름으로 차단), 연세 바이오헬스(robots 해석이 갈림), 한국과학기술지주(Firestore 직접 조회 필요), KAIST 청년창업투자지주(406 차단), 경북대(도메인이 다른 사이트로 바뀜), 성균관대·인하대(공개 목록 없음).

### 크롤링 매너 (config.py)
robots.txt 준수, 같은 호스트에 2초 간격, 같은 URL 은 30일 캐시. 구글뉴스 기사 링크는 robots 로 막혀 있어 제목·요약만 저장한다.

## 다음에 붙이면 좋은 것
- **게시판 추가**: `hcdb/sources/boards.py` 의 `BOARDS` 에 한 줄 추가하면 된다. 교육부(학생창업유망팀)는 게시글을 JS 함수로 열어 직접 URL 을 못 만들고, K-Startup 은 자동 접속을 끊어서 아직 빠져 있다. 과기정통부는 검색이 안 돼 최근 N페이지만 본다.
- **채용공고 수**: 원티드·사람인 공개 검색 결과 개수를 `job_posting_snapshots` 에 월 1회 저장.
- **FIRST 연결**: FIRST 투자사 장부를 `investors.grp/type` 에, G1 딜 시트를 별도 테이블로 넣고 `cb_market` 으로 조인하면 `g1_same_market_cnt` 를 자동 계산할 수 있다.
- **하드 필터 기준**: `config.py` 의 `MAX_AGE_YEARS` / `DEEPTECH_EXTRA_YEARS` / `MAX_VALUATION_KRW` 를 실제 기준에 맞게 수정.
