"""설정값. API 키는 .env 에 두고 여기서 읽는다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("HCDB_PATH", DATA_DIR / "startups.db"))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.split("#", 1)[0].strip()  # 줄 끝 주석 제거
            if v:
                os.environ.setdefault(k.strip(), v)


_load_dotenv()

# 공공데이터포털(data.go.kr) 인증키 — 국세청 사업자 상태조회
DATA_GO_KR_KEY = os.environ.get("DATA_GO_KR_KEY", "")
# OpenDART (opendart.fss.or.kr)
DART_KEY = os.environ.get("DART_KEY", "")
# KIPRIS Plus (plus.kipris.or.kr)
KIPRIS_KEY = os.environ.get("KIPRIS_KEY", "")
# NTIS 국가R&D 과제 (ntis.go.kr)
NTIS_KEY = os.environ.get("NTIS_KEY", "")
# Claude API — 기사·첨부파일 명단 추출 (선택)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# 네이버 검색 API (developers.naver.com) — 뉴스 검색
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

# 크롤링 매너
USER_AGENT = "hcdb-research/0.1 (personal research; low-rate)"
MIN_DELAY_SEC = 2.0          # 같은 호스트 연속 요청 간격
CACHE_TTL_DAYS = 30          # 같은 URL 재수집 주기
RESPECT_ROBOTS = True

# 하드 필터 기준 — 블루포인트/FIRST 기준에 맞게 조정
MAX_AGE_YEARS = 5            # 기본 업력 한도 (2026-11-15 결정)
DEEPTECH_EXTRA_YEARS = 5     # 딥테크 연장. 롱리스트 단계에선 자동 판정 안 함 — 엑셀에서 직접 '딥테크 여부'=1 로 표시한 곳만 적용
# 값이 없을 때: 롱리스트 단계에선 떨어뜨리지 않고 '통과(확인 필요)'로 둔다
PASS_IF_VALUATION_UNKNOWN = True
PASS_IF_FOUNDED_UNKNOWN = True
MAX_VALUATION_KRW = 5_000_000_000  # 추정 밸류 50억

# 신뢰도 우선순위. manual(사람이 확인한 값)은 항상 우선
CONF_RANK = {"상": 3, "중": 2, "하": 1, None: 0}
SOURCE_OVERRIDE = {"manual"}
