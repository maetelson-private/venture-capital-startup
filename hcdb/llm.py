"""Claude API 로 비정형 텍스트 → 칸별 구조화.

- extract_roster(): 공고·보도자료 첨부의 선정 명단 → 팀/기업 목록
- extract_article(): 뉴스 기사 → 기업·라운드·투자사·BM·고객·지불주체·PoC

구조화 출력(messages.parse + Pydantic)이라 JSON 파싱 실패가 없다.
ANTHROPIC_API_KEY 가 없으면 호출하지 않고 None 을 돌려준다(호출부가 규칙 기반으로 대체).
"""

from pydantic import BaseModel, Field

from . import config

MODEL = "claude-opus-5-5"
CHUNK_CHARS = 60_000  # 한 번에 넘기는 텍스트 길이. 넘으면 나눠서 호출 후 합침


class RosterEntry(BaseModel):
    name: str = Field(description="기업명 또는 팀명. 원문 표기 그대로")
    ceo: str = Field("", description="대표자·팀장 이름. 가려진 경우(김**) 그대로")
    affiliation: str = Field("", description="소속 대학·연구실·병원·기관")
    item: str = Field("", description="과제명·품목명·아이템·서비스 한 줄")
    region: str = Field("", description="시·도")
    is_pre_startup: bool = Field(description="법인 설립 전 예비창업팀이면 true")
    is_health: bool = Field(description="의료·헬스케어·바이오·의료기기·디지털헬스 관련이면 true")
    note: str = Field("", description="분야·트랙·선정 구분 등 기타")


class Roster(BaseModel):
    entries: list[RosterEntry]


class Article(BaseModel):
    is_startup_news: bool = Field(description="특정 스타트업(또는 예비창업팀)에 관한 기사인가")
    is_health: bool = Field(description="디지털 헬스케어·의료·바이오 분야인가")
    company: str = Field("", description="기사 주인공 기업명 (투자사·기관 아님)")
    company_en: str = ""
    ceo: str = ""
    founded_year: int = 0
    round: str = Field("", description="프리시드/시드/프리A/A/B/C/브릿지 중 하나로 정규화")
    amount_krw: int = Field(0, description="투자 금액(원). '30억' → 3000000000")
    investors: list[str] = Field(default_factory=list, description="이번 라운드 참여 투자사")
    lead_investor: str = ""
    bm_summary: str = Field("", description="수익 구조 한 줄")
    service_summary: str = Field("", description="서비스·제품 한 줄")
    customers: list[str] = Field(default_factory=list, description="환자·소비자 / 의료진·병원 / 보험·지불자 / 제약·기업 중에서만")
    payers: list[str] = Field(default_factory=list, description="건보 급여 / 비급여 / 실손·민간보험 / B2B / B2G / 소비자 직접 중에서만")
    partners: list[str] = Field(default_factory=list, description="PoC·공동연구·공급 계약 상대 기관명")
    programs: list[str] = Field(default_factory=list, description="선정된 지원사업·프로그램 (팁스, 예비창업패키지 등)")
    tips_operator: str = Field("", description="팁스 선정이면 운영사(추천 투자사)")
    headcount: int = 0
    regulatory: str = Field("", description="식약처 허가·임상·FDA·CE·혁신의료기기 지정 등 언급")


def _client():
    if not config.ANTHROPIC_API_KEY:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _parse(client, system: str, user: str, fmt, effort="low"):
    import anthropic
    try:
        resp = client.messages.parse(
            model=MODEL, max_tokens=16000, system=system,
            output_config={"effort": effort},
            messages=[{"role": "user", "content": user}],
            output_format=fmt,
        )
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Claude API 요청 한도 초과: {e.message}") from e
    except anthropic.AuthenticationError as e:
        raise RuntimeError("ANTHROPIC_API_KEY 가 올바르지 않습니다") from e
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


ROSTER_SYSTEM = (
    "너는 한국 정부·기관 공고문에서 선정 명단을 뽑는 도구다. 표에 실제로 적힌 행만 옮기고, 없는 값은 비워둔다. "
    "없는 값은 빈 문자열. 추측해서 채우지 않는다. 심사위원·담당자·연락처 표는 명단이 아니다. 선정 기업·팀이 하나도 없으면 빈 목록을 돌려준다."
)


def extract_roster(text: str, context: str = "") -> list[RosterEntry] | None:
    client = _client()
    if client is None:
        return None
    out: list[RosterEntry] = []
    for i in range(0, len(text), CHUNK_CHARS):
        chunk = text[i:i + CHUNK_CHARS]
        r = _parse(client, ROSTER_SYSTEM,
                   f"공고/자료: {context}\n\n아래 텍스트에서 선정된 기업·팀 명단을 모두 뽑아줘.\n\n<text>\n{chunk}\n</text>",
                   Roster)
        if r:
            out.extend(r.entries)
    return out


class ScopeItem(BaseModel):
    id: int
    scope: str = Field(description="포함 / 제외 / 보류 중 하나")
    reason: str = Field(description="한 줄 근거")


class ScopeBatch(BaseModel):
    items: list[ScopeItem]


SCOPE_SYSTEM = (
    "너는 디지털 헬스케어 스타트업 DB의 수집 범위를 판정한다. 기준:\n"
    "- 포함: 의료·건강 문제를 소프트웨어·AI·데이터·디바이스·플랫폼으로 푸는 회사. 디지털치료제, 의료 AI·영상진단, "
    "원격·비대면 진료, 의료기기(진단·모니터링·치료·재활 기기 포함), 웨어러블 건강 측정, 병원·보험·제약 대상 헬스 IT, "
    "정신건강·만성질환 관리 서비스, 의료 데이터·마이데이터, 시니어케어 테크.\n"
    "- 보류: 신약·바이오 소재처럼 디지털 요소가 없는 바이오, 웰니스·피트니스·영양제처럼 의료 경계에 있는 소비자 서비스, "
    "정보가 너무 적어 판단이 어려운 곳.\n"
    "- 제외: 반려동물, 화장품·스킨케어, 식품, 일반 IT·SaaS, 의료와 무관한 회사, 병원·대학·공공기관·대기업.\n"
    "주어진 정보만으로 판정하고, 근거는 한 줄로 쓴다."
)


def classify_scope(rows: list[dict]) -> list[ScopeItem] | None:
    """rows: [{id, name, info}] 를 한 번에 판정."""
    client = _client()
    if client is None:
        return None
    text = "\n".join(f"[{r['id']}] {r['name']} — {r['info'] or '(정보 없음)'}" for r in rows)
    r = _parse(client, SCOPE_SYSTEM, f"아래 회사들을 각각 판정해줘. id 를 그대로 돌려줘.\n\n{text}", ScopeBatch)
    return r.items if r else None


class CompanyFacts(BaseModel):
    found: bool = False
    founded_date: str = ""     # YYYY-MM-DD / YYYY-MM / YYYY
    ceo: str = ""
    url: str = ""
    sido: str = ""
    source_url: str = ""
    evidence: str = ""         # 근거 문장 (짧게)


FACTS_SYSTEM = (
    "너는 한국 기업의 설립일을 웹에서 확인하는 조사원이다. 회사 홈페이지(회사소개·연혁), 기업정보 사이트, "
    "기사에서 '설립', '창립', '법인 설립' 표기를 찾는다. 동명의 다른 회사와 혼동하지 않도록 힌트(제품·분야)와 맞는지 확인한다. "
    "확인하지 못하면 found=false 로 두고 추측하지 않는다."
)


def find_company_facts(name: str, hint: str = "") -> CompanyFacts | None:
    """웹 검색(서버 도구)으로 설립일·대표·홈페이지를 찾는다. 검색 3회 이내."""
    import json as _json
    import re as _re
    client = _client()
    if client is None:
        return None
    defaults = {bool: False, int: 0, str: ""}
    skel = _json.dumps({k: defaults.get(f.annotation, "") for k, f in CompanyFacts.model_fields.items()},
                       ensure_ascii=False)
    messages = [{"role": "user", "content":
                 f"회사: {name}\n힌트: {hint}\n\n이 회사의 설립일(법인 설립일)과 대표자, 홈페이지, 본사 시·도를 찾아줘. "
                 f"마지막에 아래 키를 가진 JSON 객체 하나만 출력해. source_url 은 설립일 근거 페이지.\n{skel}"}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    for _ in range(4):  # pause_turn 이어가기
        resp = client.messages.create(model=MODEL, max_tokens=8000, system=FACTS_SYSTEM, tools=tools,
                                      output_config={"effort": "low"}, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = None
    for m in _re.finditer(r"\{[^{}]*\}", text, _re.S):
        pass  # 마지막 JSON 객체
    if not m:
        return None
    try:
        return CompanyFacts.model_validate_json(m.group(0))
    except ValueError:
        return None


ARTICLE_SYSTEM = (
    "너는 한국 스타트업 투자 기사에서 사실을 칸별로 옮기는 도구다. 기사에 적힌 것만 쓰고, 없으면 빈 문자열·0·빈 목록으로 둔다. "
    "투자사와 주인공 기업을 혼동하지 않는다. 금액은 원 단위 정수로 쓴다."
)


def extract_article(title: str, body: str) -> Article | None:
    """Article 스키마는 칸이 많아 구조화 출력 문법 컴파일이 멈추는 문제가 있어(실측 160초+ 타임아웃),
    JSON 을 직접 요청하고 Pydantic 으로 검증한다. 검증 실패 시 None."""
    import json as _json
    import re as _re
    client = _client()
    if client is None:
        return None
    # 타입별 기본값을 채운 뼈대 + 칸 설명을 따로 줘서 타입을 맞추게 한다
    defaults = {bool: False, int: 0, str: ""}
    skel = {k: defaults.get(f.annotation, []) for k, f in Article.model_fields.items()}
    desc = "\n".join(f"- {k}: {f.description}" for k, f in Article.model_fields.items() if f.description)
    skeleton = _json.dumps(skel, ensure_ascii=False) + "\n\n칸 설명:\n" + desc
    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=ARTICLE_SYSTEM, output_config={"effort": "low"},
        messages=[{"role": "user", "content":
                   f"제목: {title}\n\n<article>\n{body[:CHUNK_CHARS]}\n</article>\n\n"
                   f"아래 키를 가진 JSON 객체 하나만 출력해. 설명은 키의 의미다. 불리언은 true/false, 목록은 배열, "
                   f"없는 문자열은 \"\", 없는 숫자는 0.\n{skeleton}"}])
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = _re.search(r"\{.*\}", text, _re.S)
    if not m:
        return None
    try:
        return Article.model_validate_json(m.group(0))
    except ValueError:
        return None
