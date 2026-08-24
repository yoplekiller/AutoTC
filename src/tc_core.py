"""
generate_tc.py / watch_sheet.py 공통 핵심 로직 (Jira 조회, Groq TC 생성, 텍스트 정제/Quality Gate)

두 스크립트가 이 로직을 각자 복붙해서 3차례 반복 drift를 겪은 뒤(Windows 인코딩,
"~한다"체 정규화, TC Quality Gate 등이 한쪽에만 반영되던 문제) 이 모듈로 통합했다
(2026-08-25). 각 파일에만 필요한 로직(watch_sheet.py의 구글시트 폴링/QA Review 게이트,
generate_tc.py의 엑셀 입출력 등)은 이 모듈로 옮기지 않고 각 파일에 그대로 둔다.
"""

import re
import os
import json
import time
from difflib import SequenceMatcher

from jira import JIRA
from groq import Groq
from dotenv import load_dotenv
from utils import rate_limit_wait_seconds

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH = os.path.join(ROOT_DIR, os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"))


class DailyTokenLimitError(Exception):
    pass


# TC Quality Gate — 실행 가능성 분류. "수동 불가 = 삭제"하지 않고 분류만 한다.
EXECUTION_TYPES = {
    "MANUAL", "MANUAL_WITH_TOOL", "API_OR_MOCK_REQUIRED",
    "AUTOMATION_RECOMMENDED", "ENVIRONMENT_SETUP_REQUIRED",
    "NOT_EXECUTABLE", "REQUIREMENT_CLARIFICATION",
}
TEST_DESIGN_TECHNIQUES = {
    "boundary_value", "equivalence_partition", "state_transition",
    "decision_table", "error_guessing", "requirement_based",
}
QUALITY_STATUSES = {"PASS", "REVIEW", "REJECT"}
REQUIREMENT_STATUSES = {"OK", "NEEDS_CLARIFICATION"}

# 기대결과에 이 표현만 있고 구체적 관찰 대상(명사)이 없으면 PASS를 REVIEW로 강등한다.
# LLM이 자기 출력의 품질을 관대하게 평가하는 경향을 보완하는 규칙 기반 이중 체크.
_VAGUE_RESULT_PATTERN = re.compile(
    r"(정상적으로|올바르게|문제\s*없이|정상\s*처리|정상\s*화면|성공적으로)\s*(동작|처리|표시|작동|저장)?\s*(됨|함)?\s*\.?\s*$"
)


# ── Jira ─────────────────────────────────────────────────────────────

def extract_issue_key(input_str: str) -> str:
    """URL 또는 이슈 키에서 Jira 이슈 키를 추출합니다."""
    url_match = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", input_str)
    if url_match:
        return url_match.group(1)

    key_match = re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", input_str.strip())
    if key_match:
        return input_str.strip()

    raise ValueError(
        f"유효한 Jira 티켓 URL 또는 이슈 키를 입력해주세요.\n"
        f"  예) https://yourcompany.atlassian.net/browse/PROJ-123\n"
        f"  예) PROJ-123\n"
        f"  입력값: {input_str}"
    )


def fetch_issue(jira: JIRA, issue_key: str) -> dict:
    """Jira 이슈 정보를 가져옵니다."""
    issue = jira.issue(issue_key)
    return {
        "key": issue.key,
        "summary": issue.fields.summary,
        "status": issue.fields.status.name,
        "description": issue.fields.description or "설명 없음",
        "issue_type": issue.fields.issuetype.name,
    }


def load_context(context_name: str) -> str:
    """contexts/{name}.md 파일을 읽어 반환합니다. 없으면 빈 문자열."""
    if not context_name:
        return ""
    path = os.path.join(ROOT_DIR, "contexts", f"{context_name.lower()}.md")
    if not os.path.exists(path):
        print(f"  [경고] 컨텍스트 파일 없음: {path}")
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


# ── gspread 클라이언트 ────────────────────────────────────────────────

def _get_gspread_client():
    try:
        import sys
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        import sys
        print("[오류] gspread 또는 google-auth 패키지가 없습니다.")
        print("  pip install gspread google-auth")
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # GitHub Actions 환경: GOOGLE_CREDENTIALS_JSON 환경변수에서 직접 읽기
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)

    return gspread.authorize(creds)


# ── 텍스트 정제 ──────────────────────────────────────────────────────

# AI 응답에 가끔 섞이는 한자 오타 교정 (한국어 텍스트에 정상적으로 등장할 수 없는 패턴)
_TEXT_FIXES = [
    (re.compile(r"예外처리"), "예외처리"),
    (re.compile(r"\s*不存在"), " 존재하지 않음"),
    (re.compile(r"나타남"), "노출됨"),
]
# 화이트리스트 방식: 한글/영숫자/기본 문장부호/원화기호 외의 문자는 전부 제거 (한자·가나뿐 아니라 아랍·키릴 등 임의 외래문자 오염 방지)
_FOREIGN_SCRIPT_PATTERN = re.compile(r"[^\x20-\x7E가-힣ㄱ-ㅎㅏ-ㅣ₩\t\n\r]+")
_TC_ID_PATTERN = re.compile(r"^TC_(?:[A-Z]+_)?(\d+)$")

# 한글에 공백 없이 붙은 영단어(예: "사진을registered") — AI가 문장을 끝맺지 못하고 영어로 누락한 패턴
_DANGLING_LATIN_PATTERN = re.compile(r"([가-힣])([a-zA-Z]{2,})\b")
_TRAILING_PARTICLE_PATTERN = re.compile(r"(을|를|이|가|은|는|에|와|과|의|로|으로|도)$")
_KOREAN_ENDING_PATTERN = re.compile(r"(음|함|됨|임|완료)$")

# 테스트 단계/사전조건이 "~한다"체로 끝나는 경우 기존 "~함"체와 통일 (유형별 개별 호출로 인한 문체 혼재 보정)
# "있다/없다/이다"는 사전조건에 흔한 서술형 종결("~되어 있다", "~로그인되어 있다")인데 기존엔 빠져있었음
_DECLARATIVE_ENDING_PATTERN = re.compile(r"(한다|된다|않는다|간다|온다|본다|있다|없다|이다)\.?\s*$")
_DECLARATIVE_ENDING_MAP = {
    "한다": "함", "된다": "됨", "않는다": "않음",
    "간다": "감", "온다": "옴", "본다": "봄",
    "있다": "있음", "없다": "없음", "이다": "임",
}


def _fix_dangling_latin(line):
    """한글 뒤에 붙은 영단어를 제거하고, 조사로 끝나 미완성된 문장을 보정합니다."""
    if not _DANGLING_LATIN_PATTERN.search(line):
        return line
    line = _DANGLING_LATIN_PATTERN.sub(r"\1", line).rstrip()
    if not _KOREAN_ENDING_PATTERN.search(line):
        line = _TRAILING_PARTICLE_PATTERN.sub("", line).rstrip()
        line += "이 정상적으로 처리됨"
    return line


def _normalize_step_ending(line):
    """"~한다"체로 끝나는 줄을 "~함"체로 변환합니다."""
    m = _DECLARATIVE_ENDING_PATTERN.search(line)
    if not m:
        return line
    return line[: m.start(1)] + _DECLARATIVE_ENDING_MAP[m.group(1)]


def _sanitize_text(value):
    """문자열에 섞인 외래 문자·오타·금지 표현을 교정하고, "~한다"체를 "~함"체로 통일합니다."""
    if not isinstance(value, str):
        return value
    for pattern, replacement in _TEXT_FIXES:
        value = pattern.sub(replacement, value)
    if _FOREIGN_SCRIPT_PATTERN.search(value):
        value = _FOREIGN_SCRIPT_PATTERN.sub("", value)
        value = re.sub(r"[ \t]{2,}", " ", value).strip()
    if _DANGLING_LATIN_PATTERN.search(value) or _DECLARATIVE_ENDING_PATTERN.search(value):
        value = "\n".join(
            _normalize_step_ending(_fix_dangling_latin(line))
            for line in value.split("\n")
        )
    return value


# ── Groq: 요구사항 추론 ──────────────────────────────────────────────

def augment_ticket_spec(groq_client: Groq, issue: dict, context: str = "") -> str:
    """부실한 티켓 설명을 AI로 보완해 테스트 관점 요구사항을 추론합니다."""
    context_section = f"\n\n[서비스 컨텍스트]\n{context}" if context else ""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 시니어 QA 엔지니어입니다. "
                            "Jira 티켓 정보가 부족할 때 도메인 지식으로 테스트 관점의 요구사항을 추론합니다. "
                            "서비스 컨텍스트가 제공된 경우 이를 적극 반영하세요. "
                            "반드시 순수한 한국어로만 작성하세요."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"""아래 Jira 티켓 정보를 보고 테스트 관점의 요구사항을 추론해주세요.

티켓 유형: {issue['issue_type']}
티켓 제목: {issue['summary']}
티켓 설명: {issue['description']}{context_section}

다음 항목을 간결하게 작성하세요:
1. 기능 목적
2. 주요 기능 요구사항 (3~5개). 각 항목 맨 앞에 "REQ-1.", "REQ-2." 형식으로 고유 번호를 매길 것 (이 번호는 뒤에서 TC가 근거로 인용함)
3. 예외/비정상 케이스 (2~3개). 각 항목 맨 앞에 이어서 "REQ-4.", "REQ-5." 형식으로 번호를 계속 매길 것
4. 보안·권한 고려사항 (해당 시). 있다면 이어서 REQ 번호를 매길 것
5. 확인이 필요한 질문 (기획서/티켓에 명시되지 않았지만 QA 관점에서 기획자·개발자에게 반드시 확인해야 하는 것 — 예: 경계값 처리 기준, 동시 요청 시 우선순위, 상태 전이 실패 시 롤백 여부 등. 해당 없으면 "없음". 이 항목은 REQ 번호를 매기지 않음 — 아직 요구사항으로 확정되지 않았기 때문)

설명 없이 위 형식만 출력하세요.""",
                    },
                ],
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"  [Rate Limit/분당] augment {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        print("  [오류] augment_ticket_spec Rate Limit 재시도 소진 — 원본 설명 그대로 사용")
        return _sanitize_text(issue.get("description", ""))

    return _sanitize_text(response.choices[0].message.content.strip())


# ── Groq: TC 생성 (단일 유형) ────────────────────────────────────────

def _call_tc_api(groq_client: Groq, issue: dict, augmented_spec: str, context: str,
                 test_type: str, count: int, start_idx: int) -> list:
    """특정 테스트 유형의 TC를 생성합니다."""
    type_guide = {
        "기능":     "정상적인 사용 흐름(Happy Path) — 기능이 올바르게 작동하는 시나리오",
        "예외처리": "오류 입력, 권한 없음, 서버 오류, 네트워크 오류 등 비정상 흐름",
        "경계값":   (
            "입력 필드의 데이터 타입에 맞는 경계 조건만 작성 — "
            "텍스트/코드형(쿠폰코드, 닉네임 등): 글자 수 상한/하한, 공백, 특수문자, 대소문자, 다국어; "
            "숫자형(수량, 금액 등): 최솟값/최댓값/0/음수/소수점; "
            "날짜·시간형: 과거/미래/형식 오류/만료 시점; "
            "선택형(체크박스, 드롭다운 등): 미선택/중복 선택/전체 선택. "
            "필드의 실제 데이터 타입과 맞지 않는 케이스(예: 코드 문자열에 최솟값/최댓값)는 작성하지 말 것"
        ),
        "회귀":     "이 기능 변경으로 영향받을 수 있는 연관 기능의 정상 동작 검증",
        "보안":     (
            "티켓 요구사항에 실제로 존재하는 보안 표면만 검증한다 — "
            "서버 인증/API가 있는 경우: 인증 우회, 권한 상승, 토큰 탈취, SQL Injection 등; "
            "인증/서버 없는 클라이언트 전용 로직인 경우: 로컬 저장소 데이터 노출·변조, 사용자 입력값 기반 XSS/인젝션, 민감정보 평문 저장 등. "
            "요구사항에 이런 보안 표면 자체가 없다면(예: 인증·서버 통신이 없는 순수 클라이언트 계산 로직) 검증할 대상이 없는 것이므로 TC를 작성하지 않는다."
        ),
        "UI/UX":    "버튼 활성화 상태, 에러 메시지 문구, 화면 전환, 로딩 표시 등 UI 동작",
        "네트워크": "느린 네트워크, 연결 끊김, 타임아웃 상황에서의 동작",
        "상태전이": (
            "상태 값이 바뀌는 시나리오 — 정상적인 상태 A→B 전이, 허용되지 않는 역방향/우회 전이 시도, "
            "동일 상태에서 중복 액션(예: 이미 발급된 쿠폰 재발급), 상태별로 가능/불가능한 액션이 달라지는 지점, "
            "동시 요청으로 인한 상태 충돌(race condition)"
        ),
    }
    context_block = f"\n\n[기획서/서비스 컨텍스트]\n{context}" if context else ""

    prompt = f"""다음 티켓에 대해 [{test_type}] 유형 TC를 작성하세요.

[목표 개수 — {count}개는 상한이 아니라 참고치입니다]
이전 단계에서 이 유형에 배정된 목표는 {count}개입니다. 하지만 이 숫자를 억지로 채워야 하는 것은 아닙니다.
요구사항에서 실제로 검증 가능한, 서로 다른 조건의 수만큼만 작성하세요. 그 수가 {count}개보다 적다면 적은 개수만 작성하고,
개수를 채우기 위해 중복 케이스·추정 기능·임의의 예외/보안/UX 케이스를 만들지 마세요.

[테스트 유형 설명]
{type_guide.get(test_type, test_type)}

[티켓 정보]
티켓 키: {issue['key']} | 유형: {issue['issue_type']} | 제목: {issue['summary']}

[요구사항]
{augmented_spec}{context_block}

[작성 지침]
- tc_id: 반드시 "TC_{{3자리}}" 형식 고정 (모듈 구분 없이), {start_idx:03d}번부터 시작
- 테스트유형: 반드시 "{test_type}" 으로 고정
- 사전조건/테스트단계: 번호 매겨서 구체적으로 작성 ("1. ..." 형식, 항목이 여러 개면 "2.", "3."으로 이어서)
- 사전조건 각 항목: "~음"/"~함"체로 끝낼 것 (예: "로그인되어 있음", "예산이 설정되어 있음", "앱이 실행 중임" — "~있다", "~이다" 같은 서술형 종결 금지)
  단, "~확인함"/"~검증함"/"~판단함"처럼 테스트가 수행할 검증 행위 자체는 사전조건에 쓰지 않는다(원칙 9 참고)
- 기대결과: 반드시 "1. ...됨" 한 줄만 작성한다. 검증 항목이 여러 개 필요하면 기대결과에 줄을 추가하지 말고 TC 자체를 분리해서 각각 별도로 작성할 것(원칙 8 참고)
- 기대결과: "~됨" 또는 "~함" 으로 끝낼 것 (예: "노출됨", "표시됨" — "나타남" 같은 표현은 사용 금지)
- 위험도: 이 케이스가 실패(버그로 이어짐)했을 때 비즈니스/사용자에게 미치는 영향 크기. High(결제·인증·데이터 유실 등 치명적) / Medium(핵심 기능 저하) / Low(경미한 불편)
- 우선순위: 위험도와 발생 가능성(자주 타는 경로인지)을 함께 고려해 결정 — 위험도가 낮아도 자주 발생하는 경로면 우선순위는 높을 수 있음
- 자동화가능여부: UI/API 자동화 도구(Selenium/Playwright/Appium 등)로 결정적으로 검증 가능하면 "가능", 육안 판단(디자인 정합성, 문구 뉘앙스 등)이나 외부 요인(실제 PG 결제 등)이 필요하면 "불가능"

[requirement_refs / requirement_status — 요구사항 추적]
- [요구사항] 섹션에 있는 "REQ-N." 번호 중 이 TC가 실제로 근거로 삼은 항목의 번호만 배열로 적으세요 (예: ["REQ-2", "REQ-4"]).
- 근거로 삼을 수 있는 REQ 번호가 하나도 없는데도 이 TC를 작성해야 한다고 판단된다면, requirement_refs는 빈 배열로 두고 requirement_status를 "NEEDS_CLARIFICATION"으로 표시하세요. 근거가 명확하면 "OK"로 표시하세요.
- 요구사항에 없는 기능을 상상해서 TC를 만들지 마세요 — 그럴 바엔 애초에 이 TC를 작성하지 마세요(원칙 1).

[execution_type — 실행 가능성 분류. "수동 불가 = 작성 금지"가 아니라 분류가 목적입니다]
- MANUAL: 화면 조작, 값 입력, 앱 재실행 등 일반 Manual QA가 UI만으로 바로 수행 가능
- MANUAL_WITH_TOOL: Manual로 가능하지만 날짜 변경, 특정 기기/OS 등 보조 도구·환경이 필요
- API_OR_MOCK_REQUIRED: 서버 장애 강제 발생, HTTP 응답 변조, DB 직접 수정 등 API Tool/Mock/Proxy 없이는 Precondition을 만들 수 없음
- AUTOMATION_RECOMMENDED: 수동으로도 가능하지만 반복/정밀 검증이라 자동화가 더 적합
- ENVIRONMENT_SETUP_REQUIRED: 별도 빌드/배포/환경 구성이 선행되어야 수행 가능
- NOT_EXECUTABLE: 현재 일반적인 QA 환경에서 수행할 방법이 없음 (그래도 삭제하지 말고 이 값으로 표시)
- REQUIREMENT_CLARIFICATION: 요구사항 자체가 불분명해서 기획 확인 없이는 Preconditions/기대결과를 확정할 수 없음
- execution_type이 MANUAL이 아니면 required_tools(필요한 도구/환경, 배열, 없으면 빈 배열)와 execution_note(왜 이 분류인지 한 줄)를 채우세요. MANUAL이면 둘 다 빈 값으로 둡니다.

[test_design_technique — 판단 가능한 경우에만 표시, 억지로 붙이지 말 것]
boundary_value(경계값) / equivalence_partition(동등분할 대표값) / error_guessing(경험적 결함 추정) / state_transition(상태전이) / decision_table(조합 조건) / requirement_based(단순 요구사항 검증, 위 기법이 특별히 해당 안 되는 일반 케이스) 중 하나. 판단 근거가 약하면 requirement_based로 두세요.

[quality_status / quality_reason — Quality Gate]
아래 기준으로 스스로 판정하세요. 하나라도 명확히 실패하면 REJECT, 판단을 유보해야 하거나 도구/기획 확인이 필요하면 REVIEW, 전부 충족하면 PASS.
1. requirement_status가 NEEDS_CLARIFICATION이면 자동으로 REVIEW 이상 (REJECT 사유가 겹치면 REJECT)
2. Preconditions를 실제로 구성할 수 있는가 (Controllability)
3. Steps를 실행할 수 있는가 (execution_type이 NOT_EXECUTABLE이면 REVIEW, 도구 필요는 REVIEW)
4. 기대결과를 관찰로 판정할 수 있는가 (Observability) — "정상적으로/올바르게" 같은 추상 표현만 있으면 REVIEW
5. 다른 TC와 검증 목적이 사실상 동일한가 (Duplication) — 그러면 REJECT
6. 발생 확률이 낮다는 이유만으로 REJECT하지 마세요. 위험도(위 필드)가 낮은 것과 테스트 가치가 없는 것은 다릅니다. 경계값/이상입력/상태전이/데이터정합성/재진입/중복요청/네트워크오류/저장실패 케이스는 "일반 사용자가 잘 안 함"이라는 이유만으로 REJECT하지 마세요.
quality_reason에는 판정 이유를 한 문장으로 남기세요 (예: "REQ-3 기반, Preconditions/기대결과 모두 관찰 가능 — PASS" / "서버 응답 변조가 필요해 Mock 없이는 Precondition 구성 불가 — REVIEW").

[QA 관점의 테스트케이스 생성 원칙]
1. 요구사항에 명시되어 있거나 요구사항으로부터 합리적으로 검증 가능한 동작만 TC로 작성한다. 요구사항에 근거가 없는 기능·화면·정책을 임의로 만들어 TC를 작성하지 않는다.
2. TC는 이미 구현 완료된 결과물이 요구사항을 만족하는지 검증하는 관점으로 작성하고, 구현 방법(어떻게 만드는지)을 설명하지 않는다. 테스트 수행자가 기능을 새로 구현하거나, 설정을 새로 만들거나, 코드를 수정해야만 수행 가능한 절차는 TC로 작성하지 않는다.
   환경 구축/SDK 설정/Build/CI/Test Framework/설정 파일 관련 요구사항에는 다음을 적용한다:
   - 설정 파일을 생성하거나 수정하는 절차를 테스트 단계로 쓰지 않는다.
     잘못된 예: "설정 파일을 연다." / "값을 입력한다." / "설정 파일을 저장한다."
     올바른 예: "설정 파일이 존재하는지 확인한다." / "요구된 설정값이 적용되어 있는지 확인한다." / "관련 명령을 실행하여 정상 동작 여부를 확인한다."
   - 개발 작업을 테스트 절차로 변환하지 않는다.
     잘못된 예: "Vitest 환경을 구성한다."
     올바른 예: "Vitest 테스트 명령을 실행하고 정상적으로 실행되는지 확인한다."
   - 기술 Task(빌드/배포/환경설정 등)는 구현 절차가 아니라 빌드 성공 여부·설정값 적용 여부·실행 결과를 검증하는 절차로 작성한다.
3. 기대 결과는 Pass/Fail을 객관적으로 판단할 수 있는 형태로 작성한다 — 관찰 가능한 화면 요소·값·상태·메시지 등 사실 기반으로 서술한다.
4. '편리하다', '안정적이다', '원활하다'처럼 측정 기준이 없는 주관적 표현은 기대 결과에 사용하지 않는다.
5. 요구사항에 정의되지 않은 오류 메시지·오류 화면·오류 로그·경고 팝업·fallback 동작·자동 복구 동작을 기대결과로 임의로 생성하지 않는다. 명시되지 않았다면 "요구사항 미충족으로 판정됨"처럼 검증 결과로 표현한다.
6. 정상 요구사항을 단순히 반대로 뒤집어서 Negative TC를 생성하지 않는다.
   예: 요구사항 "SDK는 2.x를 사용해야 한다."
   잘못된 TC: "SDK가 1.x이면 오류 메시지가 출력된다."
   올바른 TC: "설치된 SDK가 2.x 계열인지 확인한다."
7. 목표 개수({count}개)를 채우기 위해 위 원칙(1~6)을 어기는 TC를 추가하지 않는다. 검증 가능한 조건이 목표보다 적으면 그만큼만 작성한다.
8. TC 하나는 검증 목적 하나, 기대결과도 하나만 가진다. 예외 없이 기대결과는 항상 "1. ...됨" 한 줄이다.
   검증하고 싶은 항목이 여러 개면(같은 시나리오에서 파생되는 값이라도) 기대결과에 줄을 추가하지 말고 TC 자체를 값 개수만큼 분리해서 각각 별도로 작성한다.
   예: 요구사항이 "사용 가능 생활비/남은 생활비/남은 일수/일일 사용 가능 금액/예산 초과 여부"를 계산한다면, 다섯 항목을 한 TC의 기대결과에 다섯 줄로 나열하지 말고 TC 5개(각 TC의 기대결과는 정확히 한 줄)로 분리한다. 사전조건과 테스트 단계(입력값)는 TC마다 동일하게 반복해도 된다 — 기대결과만 하나로 좁히는 것이 목적이다.
9. 사전조건은 테스트 시작 전에 이미 성립돼 있어야 할 구체적 상태만 적는다. "~확인함", "~검증함", "~판단함"처럼 이 테스트가 수행할 검증 행위 자체를 사전조건으로 적지 않는다 — 그건 사전조건이 아니라 테스트의 목적이다.
   예: 테스트 시나리오 "주요 로직에 대한 보안 취약점을 검증"
   잘못된 사전조건: "1. 계산 로직이 올바르게 동작하는 것을 확인함" / "2. 보안 취약점을 검증하기 위한 조건이 준비되어 있음"
   올바른 사전조건: 실제로 존재하는 구체적 선행 상태만 적는다(예: "1. localStorage에 예산 데이터가 저장되어 있음"). 그런 구체적 선행 상태를 특정할 수 없다면 애초에 이 TC를 작성하지 않는다(원칙 1).

JSON 배열만 출력하세요. 마크다운 없이.

[
  {{
    "tc_id": "TC_{start_idx:03d}",
    "대분류": "...",
    "소분류": "...",
    "테스트유형": "{test_type}",
    "우선순위": "High/Medium/Low",
    "위험도": "High/Medium/Low",
    "자동화가능여부": "가능/불가능",
    "테스트시나리오": "...",
    "사전조건": "1. ...\\n2. ...",
    "테스트단계": "1. ...\\n2. ...\\n3. ...",
    "기대결과": "1. ...됨",
    "requirement_refs": ["REQ-2"],
    "requirement_status": "OK/NEEDS_CLARIFICATION",
    "execution_type": "MANUAL/MANUAL_WITH_TOOL/API_OR_MOCK_REQUIRED/AUTOMATION_RECOMMENDED/ENVIRONMENT_SETUP_REQUIRED/NOT_EXECUTABLE/REQUIREMENT_CLARIFICATION",
    "required_tools": [],
    "execution_note": "",
    "test_design_technique": "boundary_value/equivalence_partition/state_transition/decision_table/error_guessing/requirement_based",
    "quality_status": "PASS/REVIEW/REJECT",
    "quality_reason": "..."
  }}
]"""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "지정된 테스트 유형에 맞는 TC를 작성합니다. 목표 개수는 상한이 아니라 참고치이며, 실제로 검증 가능한 조건보다 많이 만들지 않습니다. "
                            "각 TC는 서로 중복되지 않는 고유한 시나리오여야 합니다. "
                            "요구사항에 명시되어 있거나 합리적으로 검증 가능한 동작만 TC로 작성하고, 근거 없는 기능이나 화면을 지어내지 마세요. "
                            "테스트 단계는 UI 기준으로 원자적이고 명확하게 작성하세요. "
                            "테스트 수행자가 기능을 구현하거나 설정을 새로 만들거나 코드를 수정해야만 수행 가능한 절차는 TC로 작성하지 마세요. "
                            "기대결과는 Pass/Fail을 객관적으로 판단할 수 있는 사실 기반으로 작성하고, '~됨' 또는 '~함'으로 끝내세요. "
                            "'편리하다', '안정적이다', '원활하다'처럼 측정 기준이 없는 주관적 표현은 기대결과에 쓰지 마세요. "
                            "TC는 구현 방법이 아니라, 구현 완료된 결과물이 요구사항을 만족하는지 검증하는 관점으로 작성하세요. "
                            "환경/설정/빌드/CI 관련 요구사항과 기술 Task는 구현 절차가 아니라 빌드 성공 여부·설정값 적용·실행 결과를 확인하는 절차로 쓰세요. "
                            "요구사항에 정의되지 않은 오류 메시지·오류 화면·오류 로그·경고 팝업·fallback·자동 복구 동작을 상상해서 채우지 마세요. "
                            "정상 요구사항을 단순히 반대로 뒤집어 없는 오류 동작을 지어내는 Negative TC를 만들지 마세요. "
                            "TC 하나는 검증 목적 하나, 기대결과도 하나만 가지세요 — 기대결과는 예외 없이 '1. ...됨' 한 줄입니다. 검증 항목이 여러 개면(같은 시나리오에서 파생되는 값이라도) 기대결과에 나열하지 말고 값 개수만큼 TC를 분리하세요. "
                            "사전조건은 테스트 시작 전에 이미 성립돼 있어야 할 구체적 상태만 적고, '~확인함'/'~검증함'처럼 테스트 자체가 할 검증 행위를 사전조건으로 적지 마세요 — "
                            "구체적 선행 상태를 특정할 수 없다면 그 TC 자체를 작성하지 마세요. "
                            "각 TC마다 요구사항 근거(requirement_refs), 실행 가능성 분류(execution_type), Quality Gate 판정(quality_status/quality_reason)을 프롬프트의 기준표 그대로 스스로 채점해서 채우세요 — "
                            "수동으로 바로 실행할 수 없는 TC라고 삭제하지 말고 execution_type으로 분류만 하고, 발생 확률이 낮다는 이유만으로 REJECT하지 마세요. "
                            "한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=8000,
                reasoning_effort="low",
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            is_daily = "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str
            if is_daily:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"    [Rate Limit/분당] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        print(f"    [오류] {test_type} Rate Limit 재시도 소진 — 건너뜀")
        return []

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # JSON이 잘린 경우 복구 시도: 마지막 완전한 객체까지만 파싱
        last_brace = raw.rfind("},")
        if last_brace == -1:
            last_brace = raw.rfind("}")
        if last_brace > 0:
            recovered = raw[:last_brace + 1].rstrip(",") + "\n]"
            try:
                result = json.loads("[" + recovered if not recovered.startswith("[") else recovered)
                print(f"    [복구] {test_type} JSON 잘림 감지 — {len(result)}개 복구됨")
                return result
            except json.JSONDecodeError:
                pass
        print(f"    [경고] {test_type} JSON 파싱 실패 (응답: {raw[:200]}...)")
        return []


# ── Groq: 스펙 분석 → TC 플랜 결정 ───────────────────────────────────

def analyze_spec_for_plan(groq_client: Groq, issue: dict, augmented_spec: str, context: str = "") -> list:
    """기획서/스펙에서 실제로 검증 가능한 조건의 개수를 분석해 테스트 유형별 TC 수를 결정합니다."""
    context_block = f"\n\n[기획서/스펙]\n{context}" if context else ""

    prompt = f"""다음 티켓과 기획서를 분석해서, 테스트 유형별로 실제 요구사항에서 검증 가능한 조건이 몇 개인지 세어 TC 개수를 결정하세요.

[티켓]
유형: {issue['issue_type']} | 제목: {issue['summary']}

[추론된 요구사항]
{augmented_spec}{context_block}

[판단 기준 — 정해진 최소/목표 개수는 없습니다]
- 기능/예외처리/경계값/회귀/보안/UI/UX/상태전이 각 유형에 대해, 요구사항에 실제로 근거가 있거나 요구사항으로부터 합리적으로 검증 가능한 조건의 수만큼만 배정하세요.
- 화면/입력 필드, 정책, 연관 기능, 인증·권한·결제, 상태 값/워크플로우 등은 "그 유형을 배정할 근거가 있는지"를 판단하는 신호일 뿐, 정해진 배정 개수 공식이 아닙니다. 실제로 몇 개의 독립적인 검증 조건이 존재하는지 직접 세어 반영하세요.
- 요구사항에 근거가 부족한 유형은 억지로 채우지 말고 0으로 두세요. 개수를 채우기 위한 중복 케이스, 추정 기능, 임의의 예외/보안/UX 케이스는 절대 만들지 마세요.
- 개발환경/빌드/설정(예: 프로젝트 생성, 빌드 성공 여부, 환경변수·설정값 지정)에 관한 요구사항도 정상적으로 TC 개수에 포함하세요 — 다만 이런 TC는 설정 파일을 만드는 절차가 아니라 "설정값이 적용됐는지, 명령 실행이 성공하는지"를 확인하는 검증 관점으로 작성될 것이므로, 배정할 유형은 UI 조작이 필요 없는 기능/회귀 위주로 판단하세요
- 기능/예외처리/회귀 등 테스트 유형이 다르다는 이유만으로 동일한 검증 목적의 TC를 중복 배정하지 마세요 — 유형이 다르더라도 실질적으로 같은 검증 목적이면 하나의 유형에만 배정하세요

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{"기능": 숫자, "예외처리": 숫자, "경계값": 숫자, "회귀": 숫자, "보안": 숫자, "UI/UX": 숫자, "상태전이": 숫자}}

해당 유형에 검증 가능한 조건이 없으면 0으로 설정하세요."""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "기획서에서 실제로 검증 가능한 조건의 수를 세어 테스트 유형별 TC 수를 결정합니다. "
                            "정해진 최소/목표 개수는 없습니다 — 요구사항에 근거가 부족한 유형은 억지로 채우지 말고 0으로 두고, 개수를 채우기 위해 부풀리지 마세요. "
                            "환경/빌드/설정 요구사항도 검증 관점 TC로 정상 포함하세요(설정값·실행 결과 확인, UI 절차 아님). "
                            "동일한 검증 목적의 TC를 테스트 유형만 바꿔 중복 배정하지 마세요. "
                            "JSON만 출력하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=800,
                reasoning_effort="low",
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"    [Rate Limit/분당] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    # Groq 호출 자체가 실패했을 때만 쓰는 최후의 degrade 값 — 정상 경로의 목표치가 아니다.
    fallback = [("기능", 6), ("예외처리", 6), ("경계값", 5), ("회귀", 5), ("보안", 3), ("UI/UX", 3)]

    if response is None:
        print("  [오류] 플랜 분석 Rate Limit 재시도 소진 — 기본값 사용")
        return fallback

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        plan_dict = json.loads(raw)
        print(f"  [AI 플랜] {json.dumps(plan_dict, ensure_ascii=False)}")
        plan = [(t, int(n)) for t, n in plan_dict.items() if int(n) > 0]
        return plan if plan else fallback
    except (json.JSONDecodeError, ValueError):
        print(f"  [경고] 플랜 분석 실패 (응답: {raw[:100]}) — 기본값 사용")
        return fallback


# ── TC 정제 (Quality Gate) ───────────────────────────────────────────

def sanitize_tc(tc: dict, test_type: str, seq: int = None) -> dict:
    """TC 텍스트를 정제하고, 테스트유형과 tc_id 형식("TC_xxx")을 강제로 고정합니다."""
    for field, value in tc.items():
        tc[field] = _sanitize_text(value)
    tc["테스트유형"] = test_type
    if seq is not None:
        tc["tc_id"] = f"TC_{seq:03d}"
    else:
        m = _TC_ID_PATTERN.match(tc.get("tc_id", ""))
        if m:
            tc["tc_id"] = f"TC_{int(m.group(1)):03d}"
    expected = tc.get("기대결과")
    if isinstance(expected, str) and expected and not re.match(r"^\s*1\.", expected):
        tc["기대결과"] = f"1. {expected}"

    # ── Quality Gate 필드 정규화 (LLM이 스키마를 벗어난 값을 낼 수 있으므로 안전한 기본값으로 보정) ──
    tc["source_type"] = "requirement"

    refs = tc.get("requirement_refs")
    tc["requirement_refs"] = [str(r).strip() for r in refs if str(r).strip()] if isinstance(refs, list) else []

    # requirement_refs가 비어있으면 LLM이 뭐라 자평했든(설령 "OK"라 해도) 신뢰하지 않고 강제로
    # NEEDS_CLARIFICATION 처리한다 — "근거 없음"과 "OK"는 동시에 참일 수 없는 자기모순이기 때문.
    if not tc["requirement_refs"]:
        tc["requirement_status"] = "NEEDS_CLARIFICATION"
    else:
        req_status = str(tc.get("requirement_status", "")).strip().upper()
        tc["requirement_status"] = req_status if req_status in REQUIREMENT_STATUSES else "OK"

    exec_type = str(tc.get("execution_type", "")).strip().upper()
    tc["execution_type"] = exec_type if exec_type in EXECUTION_TYPES else "MANUAL"

    tools = tc.get("required_tools")
    tc["required_tools"] = [str(t).strip() for t in tools if str(t).strip()] if isinstance(tools, list) else []
    tc["execution_note"] = tc.get("execution_note") or ""

    technique = str(tc.get("test_design_technique", "")).strip().lower()
    tc["test_design_technique"] = technique if technique in TEST_DESIGN_TECHNIQUES else "requirement_based"

    status = str(tc.get("quality_status", "")).strip().upper()
    tc["quality_status"] = status if status in QUALITY_STATUSES else "REVIEW"
    tc["quality_reason"] = tc.get("quality_reason") or ""

    # requirement_status가 불명확한데 PASS로 자평했다면 규칙 기반으로 강등 (섹션 4 Traceability 원칙)
    if tc["requirement_status"] == "NEEDS_CLARIFICATION" and tc["quality_status"] == "PASS":
        tc["quality_status"] = "REVIEW"
        tc["quality_reason"] = (tc["quality_reason"] + " / " if tc["quality_reason"] else "") + \
            "요구사항 근거 불명확 (requirement_refs 없음) — 규칙 기반 자동 강등"

    # 기대결과가 관찰 불가능한 추상 표현뿐이면 PASS를 REVIEW로 강등 (LLM 자기평가를 신뢰하지 않는 이중 체크, 섹션 9)
    if tc["quality_status"] == "PASS" and isinstance(expected, str):
        content_lines = [
            re.sub(r"^\s*\d+\.\s*", "", ln).strip()
            for ln in expected.split("\n") if ln.strip()
        ] or [expected]
        if all(_VAGUE_RESULT_PATTERN.fullmatch(ln) for ln in content_lines):
            tc["quality_status"] = "REVIEW"
            tc["quality_reason"] = (tc["quality_reason"] + " / " if tc["quality_reason"] else "") + \
                "기대결과가 관찰 가능한 대상 없이 추상적 표현뿐임 — 규칙 기반 자동 강등"

    # 기대결과가 여러 줄이면 PASS를 REVIEW로 강등 (기대결과 1개 = TC 1개 원칙, 섹션 8) —
    # "같은 시나리오에서 파생되는 값은 묶어도 된다"는 예외를 프롬프트에서 없앴는데도 LLM이
    # 종종 다시 여러 줄로 합쳐서 내는 걸 실제로 확인함(2026-08-25) — 이중 체크로 보완한다.
    if tc["quality_status"] == "PASS" and isinstance(expected, str):
        line_count = len([ln for ln in expected.split("\n") if ln.strip()])
        if line_count > 1:
            tc["quality_status"] = "REVIEW"
            tc["quality_reason"] = (tc["quality_reason"] + " / " if tc["quality_reason"] else "") + \
                f"기대결과가 {line_count}줄 — 검증 항목당 TC 1개 원칙 위반, 분리 필요 — 규칙 기반 자동 강등"

    return tc


def filter_tc_list(tc_list: list) -> list:
    """필수 필드(테스트시나리오, 기대결과)가 없는 TC를 제거합니다."""
    valid = []
    for tc in tc_list:
        if not tc.get("테스트시나리오") or not tc.get("기대결과"):
            print(f"    [필터] {tc.get('tc_id')} 제외 - 필수 항목 누락")
            continue
        valid.append(tc)
    return valid


def dedupe_tc_list(tc_list: list, threshold: float = 0.82) -> list:
    """테스트 목적(시나리오+기대결과)이 서로 유사한 중복 TC를 제거합니다 (먼저 나온 것을 우선 유지).

    시나리오 문장만 비교하면 표현만 다르고 실제 검증 목적(기대결과)이 같은 TC를 놓칠 수 있어,
    기대결과까지 합친 문자열로 비교 범위를 넓힌다.
    """
    kept = []
    kept_signatures = []
    for tc in tc_list:
        scenario = (tc.get("테스트시나리오") or "").strip()
        expected = (tc.get("기대결과") or "").strip()
        signature = f"{scenario} {expected}"
        if any(SequenceMatcher(None, signature, s).ratio() >= threshold for s in kept_signatures):
            print(f"    [중복 제외] {tc.get('tc_id')} - {scenario}")
            continue
        kept.append(tc)
        kept_signatures.append(signature)
    return kept


def generate_test_cases(groq_client: Groq, issue: dict, augmented_spec: str, context: str = "") -> list:
    """스펙 복잡도 분석 후 유형별로 TC를 생성합니다."""
    print(f"  스펙 분석 중 (TC 플랜 결정)...")
    plan = analyze_spec_for_plan(groq_client, issue, augmented_spec, context)
    total = sum(n for _, n in plan)
    plan_str = " + ".join([f"{t} {n}개" for t, n in plan])
    print(f"  플랜 확정: {plan_str} = 총 {total}개")

    all_tcs = []
    idx = 1

    for test_type, count in plan:
        print(f"    [{test_type}] {count}개 생성 중...")
        try:
            batch = _call_tc_api(groq_client, issue, augmented_spec, context, test_type, count, idx)
        except DailyTokenLimitError as e:
            print(f"    [일일 한도 초과] {e}")
            print(f"    지금까지 생성된 {len(all_tcs)}개 TC로 저장합니다.")
            break
        batch = [sanitize_tc(tc, test_type, idx + i) for i, tc in enumerate(batch)]
        print(f"    [{test_type}] {len(batch)}개 완료")
        all_tcs.extend(batch)
        idx += len(batch)

    return all_tcs
