"""
Jira 티켓 기반 매뉴얼 TC 자동 생성

사용법:
  # 단일 티켓 (티켓 키 또는 URL)
  python src/generate_tc.py PROJ-123
  python src/generate_tc.py https://yourcompany.atlassian.net/browse/PROJ-123

  # 서비스 컨텍스트 적용
  python src/generate_tc.py PROJ-123 --context kream

  # 엑셀 일괄 처리 (A열에 티켓 URL/키 목록)
  python src/generate_tc.py tickets.xlsx --context kream

  # 구글 스프레드시트 일괄 처리
  python src/generate_tc.py https://docs.google.com/spreadsheets/d/SHEET_ID/edit

  # 입력용 템플릿 엑셀 생성
  python src/generate_tc.py --template
"""

import sys
import io
import re
import os
import json
import argparse
from datetime import datetime
from difflib import SequenceMatcher

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv
from src.utils import rate_limit_wait_seconds


class DailyTokenLimitError(Exception):
    pass

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH = os.path.join(ROOT_DIR, os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"))
JIRA_URL = os.getenv("JIRA_URL", "")


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


# ── Groq ─────────────────────────────────────────────────────────────

def load_context(context_name: str) -> str:
    """contexts/{name}.md 파일을 읽어 반환합니다. 없으면 빈 문자열."""
    if not context_name:
        return ""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "contexts", f"{context_name.lower()}.md")
    if not os.path.exists(path):
        print(f"  [경고] 컨텍스트 파일 없음: {path}")
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()

# 티켓 설명 보완 및 요구사항 추론
def augment_ticket_spec(groq_client: Groq, issue: dict, context: str = "") -> str:
    """부실한 티켓 설명을 AI로 보완해 테스트 관점 요구사항을 추론합니다."""
    import time
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
2. 주요 기능 요구사항 (3~5개)
3. 예외/비정상 케이스 (2~3개)
4. 보안·권한 고려사항 (해당 시)
5. 확인이 필요한 질문 (기획서/티켓에 명시되지 않았지만 QA 관점에서 기획자·개발자에게 반드시 확인해야 하는 것 — 예: 경계값 처리 기준, 동시 요청 시 우선순위, 상태 전이 실패 시 롤백 여부 등. 해당 없으면 "없음")

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
        raise RuntimeError("augment_ticket_spec Rate Limit 재시도 소진")

    return _sanitize_text(response.choices[0].message.content.strip())

# TC 생성 (단일 유형)
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
- 기대결과: 기본은 "1. ...됨" 한 줄. 여러 줄을 쓸 수 있는 경우는 원칙 8을 반드시 따를 것
- 기대결과 각 항목: "~됨" 또는 "~함" 으로 끝낼 것 (예: "노출됨", "표시됨" — "나타남" 같은 표현은 사용 금지)
- 위험도: 이 케이스가 실패(버그로 이어짐)했을 때 비즈니스/사용자에게 미치는 영향 크기. High(결제·인증·데이터 유실 등 치명적) / Medium(핵심 기능 저하) / Low(경미한 불편)
- 우선순위: 위험도와 발생 가능성(자주 타는 경로인지)을 함께 고려해 결정 — 위험도가 낮아도 자주 발생하는 경로면 우선순위는 높을 수 있음
- 자동화가능여부: UI/API 자동화 도구(Selenium/Playwright/Appium 등)로 결정적으로 검증 가능하면 "가능", 육안 판단(디자인 정합성, 문구 뉘앙스 등)이나 외부 요인(실제 PG 결제 등)이 필요하면 "불가능"

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
8. TC 하나는 검증 목적 하나만 가진다. 기대결과에 서로 독립적으로 성공/실패할 수 있는 검증 항목을 여러 개 나열하지 않는다 — 그런 경우 기대결과를 합치지 말고 TC 자체를 분리해서 각각 별도로 작성한다.
   같은 동작·같은 계산 결과에서 함께 파생되는, 분리해서 검증할 실익이 없는 값들만 예외적으로 기대결과 안에 여러 줄로 적을 수 있다.
   예: "계산 로직이 UI와 분리됨" / "Vitest 테스트 통과" / "Production Build 성공"은 서로 다른 이유로 독립적으로 실패할 수 있으므로 TC 3개로 분리한다.
   반면 "초과 금액이 0원으로 계산됨" / "일일 사용 가능 금액이 0원으로 계산됨"처럼 같은 시나리오(남은 예산 0원)에서 함께 파생되는 값은 하나의 TC에 여러 줄로 적어도 된다.
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
    "기대결과": "1. ...됨"
  }}
]"""

    import time
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
                            "TC 하나는 검증 목적 하나만 가지세요 — 서로 독립적으로 실패할 수 있는 검증 항목이 여러 개면 기대결과에 나열하지 말고 TC를 분리하세요. "
                            "사전조건은 테스트 시작 전에 이미 성립돼 있어야 할 구체적 상태만 적고, '~확인함'/'~검증함'처럼 테스트 자체가 할 검증 행위를 사전조건으로 적지 마세요 — "
                            "구체적 선행 상태를 특정할 수 없다면 그 TC 자체를 작성하지 마세요. "
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


# 1단계: 스펙 분석 → TC 플랜 결정
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

    import time
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


# 2단계: 유형별 분리 호출 → 합산
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


# ── 엑셀 입력/출력 ────────────────────────────────────────────────────

def create_template(output_path: str = "tickets_template.xlsx"):
    """입력용 티켓 목록 템플릿 엑셀을 생성합니다."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "티켓목록"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")

    ws.cell(row=1, column=1, value="티켓 URL 또는 이슈 키").font = header_font
    ws.cell(row=1, column=1).fill = header_fill
    ws.cell(row=1, column=1).alignment = header_align
    ws.cell(row=1, column=2, value="메모 (선택)").font = header_font
    ws.cell(row=1, column=2).fill = header_fill
    ws.cell(row=1, column=2).alignment = header_align
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 30
    ws.row_dimensions[1].height = 22

    examples = [
        ("MKQA-1", "로그인 기능 TC"),
        ("MKQA-2", "회원가입 TC"),
        (f"{JIRA_URL}/browse/MKQA-3", "URL 형식 예시"),
    ]
    for r, (key, memo) in enumerate(examples, start=2):
        ws.cell(row=r, column=1, value=key).font = Font(color="808080", italic=True)
        ws.cell(row=r, column=2, value=memo).font = Font(color="808080", italic=True)

    wb.save(output_path)
    return output_path


def read_keys_from_excel(file_path: str) -> list:
    """엑셀 A열에서 티켓 URL/키 목록을 읽어옵니다. (헤더 제외, 빈 셀 스킵)"""
    wb = openpyxl.load_workbook(file_path)
    ws = wb.active
    keys = []
    for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
        val = row[0]
        if val and str(val).strip():
            keys.append(str(val).strip())
    return keys


def save_excel(results: list, output_path: str):
    """결과를 엑셀 파일로 저장합니다. 티켓마다 별도 시트 생성."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # 기본 빈 시트 제거

    headers = [
        "TC ID", "대분류", "소분류", "테스트 유형",
        "테스트 시나리오(목적)", "사전 조건", "테스트 단계", "기대 결과",
        "테스트 상태", "비고 / 버그 링크", "우선순위", "위험도", "자동화 가능여부",
    ]
    col_widths = [14, 14, 16, 12, 35, 28, 45, 35, 12, 20, 10, 10, 14]
    last_col_letter = "M"

    header_font  = Font(bold=True, color="FFFFFF")
    header_fill  = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_align   = Alignment(horizontal="left", vertical="center", wrap_text=True)
    note_align   = Alignment(horizontal="left", vertical="top", wrap_text=True)

    level_fills = {
        "High":   PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid"),
        "Medium": PatternFill(start_color="FFF3CC", end_color="FFF3CC", fill_type="solid"),
        "Low":    PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid"),
    }

    for item in results:
        sheet_title = re.sub(r'[\\/*?:\[\]]', '', item["summary"])[:31]
        ws = wb.create_sheet(title=sheet_title)

        # 행 1: 티켓 URL 정보 (하이퍼링크)
        # item에 "url"이 명시되어 있으면(예: 기획서 기반 생성의 Confluence 페이지) 그걸 우선 쓰고,
        # 없으면 기존처럼 Jira 티켓 링크로 조립한다. 링크가 없는 경우(로컬 기획서 파일 등)는 하이퍼링크를 걸지 않는다.
        ticket_url = item.get("url", f"{JIRA_URL}/browse/{item['key']}")
        info_cell = ws.cell(row=1, column=1, value=f"{item['key']}  |  {item['summary']}")
        if ticket_url:
            info_cell.hyperlink = ticket_url
        info_cell.font = Font(bold=True, color="0563C1", underline="single", size=11)
        info_cell.fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        info_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.merge_cells(f"A1:{last_col_letter}1")
        ws.row_dimensions[1].height = 22

        # 행 2: 요구사항 분석 (확인 필요한 질문 포함)
        note_cell = ws.cell(row=2, column=1, value=item.get("augmented_spec", ""))
        note_cell.font = Font(size=10, color="444444")
        note_cell.alignment = note_align
        ws.merge_cells(f"A2:{last_col_letter}2")
        ws.row_dimensions[2].height = 140

        # 행 3: 컬럼 헤더
        for col, (header, width) in enumerate(zip(headers, col_widths), start=1):
            cell = ws.cell(row=3, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            ws.column_dimensions[cell.column_letter].width = width
        ws.row_dimensions[3].height = 25
        # 자동화가능여부(M열)는 수동 테스트 수행 시 방해되지 않도록 기본 숨김 처리
        # (자동화 대상 TC를 고를 때만 펼쳐서 확인)
        ws.column_dimensions["M"].hidden = True

        # 행 4~: TC 데이터
        last_row = 3 + len(item["test_cases"])
        for r_idx, tc in enumerate(item["test_cases"], start=4):
            ws.cell(row=r_idx, column=1,  value=tc.get("tc_id", "")).alignment = data_align
            ws.cell(row=r_idx, column=2,  value=tc.get("대분류", "")).alignment = data_align
            ws.cell(row=r_idx, column=3,  value=tc.get("소분류", "")).alignment = data_align
            ws.cell(row=r_idx, column=4,  value=tc.get("테스트유형", "")).alignment = data_align
            ws.cell(row=r_idx, column=5,  value=tc.get("테스트시나리오", "")).alignment = data_align
            ws.cell(row=r_idx, column=6,  value=tc.get("사전조건", "")).alignment = data_align
            ws.cell(row=r_idx, column=7,  value=tc.get("테스트단계", "")).alignment = data_align
            ws.cell(row=r_idx, column=8,  value=tc.get("기대결과", "")).alignment = data_align
            ws.cell(row=r_idx, column=9,  value="").alignment = data_align   # 테스트 상태
            ws.cell(row=r_idx, column=10, value="").alignment = data_align   # 연결 버그/비고
            priority = tc.get("우선순위", "")
            p_cell = ws.cell(row=r_idx, column=11, value=priority)
            p_cell.alignment = data_align
            if priority in level_fills:
                p_cell.fill = level_fills[priority]
            risk = tc.get("위험도", "")
            r_cell = ws.cell(row=r_idx, column=12, value=risk)
            r_cell.alignment = data_align
            if risk in level_fills:
                r_cell.fill = level_fills[risk]
            ws.cell(row=r_idx, column=13, value=tc.get("자동화가능여부", "")).alignment = data_align
            ws.row_dimensions[r_idx].height = 70

        # TC가 0개(예: 한도 초과로 생성 중단)면 드롭다운을 걸 데이터 범위 자체가 없으므로 스킵
        if item["test_cases"]:
            # 테스트 상태 드롭다운 (I열): P / F / B / N/A
            dv_status = DataValidation(type="list", formula1='"P,F,B,N/A"', allow_blank=True, showDropDown=False)
            dv_status.sqref = f"I4:I{last_row}"
            ws.add_data_validation(dv_status)

            # 자동화 가능여부 드롭다운 (M열): 가능 / 불가능
            dv_auto = DataValidation(type="list", formula1='"가능,불가능"', allow_blank=True, showDropDown=False)
            dv_auto.sqref = f"M4:M{last_row}"
            ws.add_data_validation(dv_auto)

    wb.save(output_path)


# ── 구글 스프레드시트 입력/출력 ──────────────────────────────────────

def _get_gspread_client():
    """gspread 클라이언트를 반환합니다."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[오류] gspread 또는 google-auth 패키지가 없습니다.")
        print("  pip install gspread google-auth")
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)
    return gspread.authorize(creds)


def extract_sheet_id(url: str) -> str:
    """구글 스프레드시트 URL에서 spreadsheet ID를 추출합니다."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    # ID 직접 입력된 경우 (URL이 아닌 순수 ID)
    if re.fullmatch(r"[a-zA-Z0-9_-]{30,}", url):
        return url
    raise ValueError(f"유효한 구글 스프레드시트 URL이 아닙니다: {url}")


def extract_gid(url: str) -> str | None:
    """구글 스프레드시트 URL에서 특정 탭의 gid를 추출합니다. 없으면 None."""
    match = re.search(r"[#&]gid=(\d+)", url)
    return match.group(1) if match else None


def read_keys_from_sheets(sheet_id: str, gid: str | None = None) -> list:
    """구글 스프레드시트 A열에서 티켓 URL/키를 읽습니다. (헤더 제외)

    URL에 gid가 명시되어 있으면 그 탭을, 없으면 첫 번째 탭을 읽습니다.
    """
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)
    if gid is not None:
        ws = sh.get_worksheet_by_id(int(gid))
    else:
        ws = sh.get_worksheet(0)
    all_values = ws.col_values(1)  # A열 전체
    # 헤더(1행) 제외, 빈 값 스킵
    return [v.strip() for v in all_values[1:] if v and v.strip()]


def save_to_sheets(results: list, sheet_id: str):
    """생성된 TC를 구글 스프레드시트에 티켓별 시트로 저장합니다."""
    import gspread

    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)

    headers = [
        "TC ID", "대분류", "소분류", "테스트 유형",
        "테스트 시나리오(목적)", "사전 조건", "테스트 단계", "기대 결과",
        "테스트 상태", "비고 / 버그 링크", "우선순위", "위험도", "자동화 가능여부",
    ]
    level_colors = {
        "High":   {"red": 1.0,  "green": 0.8,  "blue": 0.8},
        "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "Low":    {"red": 0.85, "green": 0.92, "blue": 0.85},
    }
    col_widths = [100, 110, 120, 110, 280, 200, 320, 260, 100, 160, 90, 90, 110]
    total_tc = 0

    for item in results:
        sheet_title = item["summary"][:100]
        # save_excel과 동일한 규칙: item["url"]이 있으면(기획서/Confluence 기반) 그걸 쓰고, 없으면 Jira 링크로 조립
        ticket_url = item.get("url", f"{JIRA_URL}/browse/{item['key']}")

        try:
            ws = sh.worksheet(sheet_title)
            ws.clear()
            print(f"  '{sheet_title}' 시트 초기화")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_title, rows=200, cols=len(headers))
            print(f"  '{sheet_title}' 시트 생성")

        # 행 1: 티켓 URL 정보
        header_text = f"{item['key']}  |  {item['summary']}  |  {ticket_url}" if ticket_url else f"{item['key']}  |  {item['summary']}"
        ws.update([[header_text]], "A1")
        ws.format("A1", {
            "backgroundColor": {"red": 0.922, "green": 0.953, "blue": 0.984},
            "textFormat": {"bold": True, "foregroundColor": {"red": 0.02, "green": 0.34, "blue": 0.71}},
            "horizontalAlignment": "LEFT",
        })
        ws.merge_cells("A1:M1")

        # 행 2: 요구사항 분석 (확인 필요한 질문 포함)
        ws.update([[item.get("augmented_spec", "")]], "A2")
        ws.format("A2", {
            "verticalAlignment": "TOP",
            "horizontalAlignment": "LEFT",
            "wrapStrategy": "WRAP",
            "textFormat": {"fontSize": 9, "foregroundColor": {"red": 0.27, "green": 0.27, "blue": 0.27}},
        })
        ws.merge_cells("A2:M2")

        # 행 3: 컬럼 헤더
        ws.update([headers], "A3")
        ws.format("A3:M3", {
            "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER",
        })

        # 행 4~: TC 데이터
        rows_data = []
        for tc in item["test_cases"]:
            rows_data.append([
                tc.get("tc_id", ""),
                tc.get("대분류", ""),
                tc.get("소분류", ""),
                tc.get("테스트유형", ""),
                tc.get("테스트시나리오", ""),
                tc.get("사전조건", ""),
                tc.get("테스트단계", ""),
                tc.get("기대결과", ""),
                "",  # 테스트 상태
                "",  # 연결 버그/비고
                tc.get("우선순위", ""),
                tc.get("위험도", ""),
                tc.get("자동화가능여부", ""),
            ])
        if rows_data:
            ws.update(rows_data, "A4")
            end_row = 4 + len(rows_data)

            # 데이터 셀 정렬: 세로=가운데, 가로=왼쪽
            ws.format(f"A4:M{end_row - 1}", {
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "LEFT",
                "wrapStrategy": "WRAP",
            })

            # 우선순위(K열)/위험도(L열) 색상 — 셀 하나당 ws.format() 호출 1번씩 하면 TC가 많을 때
            # (기획서 기반 생성처럼 40~50개 이상 나오는 경우) 분당 쓰기 요청 할당량(429)을 바로 초과한다.
            # 실제로 --sheet 옵션 실전 검증 중 57개 TC로 재현됨. batch_format으로 모아서 한 번에 보낸다.
            color_requests = []
            for i, tc in enumerate(item["test_cases"]):
                p_color = level_colors.get(tc.get("우선순위", ""))
                if p_color:
                    color_requests.append({"range": f"K{4 + i}", "format": {"backgroundColor": p_color}})
                r_color = level_colors.get(tc.get("위험도", ""))
                if r_color:
                    color_requests.append({"range": f"L{4 + i}", "format": {"backgroundColor": r_color}})
            if color_requests:
                ws.batch_format(color_requests)

            # 기존 드롭다운 초기화 후 테스트 상태(I열)/자동화 가능여부(M열) 드롭다운 재설정
            sh.batch_update({"requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 3,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 13,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 3,
                            "endRowIndex": end_row,
                            "startColumnIndex": 8,
                            "endColumnIndex": 9,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": "P"},
                                    {"userEnteredValue": "F"},
                                    {"userEnteredValue": "B"},
                                    {"userEnteredValue": "N/A"},
                                ],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 3,
                            "endRowIndex": end_row,
                            "startColumnIndex": 12,
                            "endColumnIndex": 13,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": [
                                    {"userEnteredValue": "가능"},
                                    {"userEnteredValue": "불가능"},
                                ],
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                },
            ]})

            # 열 너비 설정
            requests_body = [{"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }} for i, px in enumerate(col_widths)]
            sh.batch_update({"requests": requests_body})

            # 자동화가능여부(M열, index 12)는 수동 테스트 수행 시 방해되지 않도록 기본 숨김 처리
            sh.batch_update({"requests": [{
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 12, "endIndex": 13},
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }]})

        total_tc += len(item["test_cases"])
        print(f"  '{sheet_title}' — TC {len(item['test_cases'])}개 저장")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    print(f"\n  총 {total_tc}개 TC 저장 완료")
    print(f"  {sheet_url}")
    return sheet_url


# ── 처리 공통 로직 ────────────────────────────────────────────────────

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
    """테스트시나리오가 서로 유사한 중복 TC를 제거합니다 (먼저 나온 것을 우선 유지)."""
    kept = []
    kept_scenarios = []
    for tc in tc_list:
        scenario = (tc.get("테스트시나리오") or "").strip()
        if any(SequenceMatcher(None, scenario, s).ratio() >= threshold for s in kept_scenarios):
            print(f"    [중복 제외] {tc.get('tc_id')} - {scenario}")
            continue
        kept.append(tc)
        kept_scenarios.append(scenario)
    return kept


def process_keys(jira: JIRA, groq_client: Groq, issue_keys: list, context: str = "") -> list:
    """티켓 키 목록을 순서대로 처리하여 결과를 반환합니다."""
    results = []
    total = len(issue_keys)

    for idx, key in enumerate(issue_keys, start=1):
        print(f"\n[{idx}/{total}] {key} 처리 중...")

        try:
            issue = fetch_issue(jira, key)
        except Exception as e:
            print(f"  [건너뜀] 티켓 조회 실패: {e}")
            continue

        print(f"  제목: {issue['summary']} | 상태: {issue['status']}")
        print(f"  요구사항 추론 중...")
        try:
            augmented_spec = augment_ticket_spec(groq_client, issue, context)
        except DailyTokenLimitError as e:
            print(f"  [일일 한도 초과] {e}")
            print(f"  지금까지 처리된 {len(results)}개 티켓 결과로 저장합니다.")
            break
        print(f"  --- 요구사항 분석 ---\n{augmented_spec}\n  ---")
        print(f"  TC 생성 중...")
        try:
            tc_list = generate_test_cases(groq_client, issue, augmented_spec, context)
        except DailyTokenLimitError as e:
            print(f"  [일일 한도 초과] {e}")
            print(f"  지금까지 처리된 {len(results)}개 티켓 결과로 저장합니다.")
            break
        tc_list = filter_tc_list(tc_list)
        tc_list = dedupe_tc_list(tc_list)
        print(f"  생성된 TC: {len(tc_list)}개")
        for tc in tc_list:
            print(f"    [{tc.get('tc_id')}] [{tc.get('대분류', '-')}] [{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트시나리오', '')}")

        results.append({
            "key": issue["key"],
            "summary": issue["summary"],
            "status": issue["status"],
            "augmented_spec": augmented_spec,
            "test_cases": tc_list,
        })

        if idx < total:
            import time
            print(f"  다음 티켓까지 30초 대기...")
            time.sleep(30)

    return results


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("input", nargs="?", default=None)
    parser.add_argument("--context", default="", help="서비스 컨텍스트 이름 (예: kream, kurly)")
    parser.add_argument("--template", action="store_true")
    args, _ = parser.parse_known_args()

    if not args.input and not args.template:
        print("사용법:")
        print("  python src/generate_tc_from_url.py MKQA-1")
        print("  python src/generate_tc_from_url.py MKQA-1 --context kream")
        print("  python src/generate_tc_from_url.py tickets.xlsx --context kurly")
        print("  python src/generate_tc_from_url.py --template")
        sys.exit(1)

    # 컨텍스트 로드
    context = load_context(args.context)
    if context:
        print(f"  컨텍스트 로드됨: contexts/{args.context}.md")

    input_str = args.input

    # ── 템플릿 생성 모드
    if input_str == "--template":
        path = create_template("tickets_template.xlsx")
        print(f"템플릿 생성 완료: {path}")
        print("A열에 티켓 URL 또는 이슈 키를 입력한 후 실행하세요.")
        return

    # ── 구글 스프레드시트 모드
    if "docs.google.com/spreadsheets" in input_str or re.fullmatch(r"[a-zA-Z0-9_-]{44}", input_str.strip()):
        try:
            sheet_id = extract_sheet_id(input_str)
        except ValueError as e:
            print(f"[오류] {e}")
            sys.exit(1)
        gid = extract_gid(input_str)

        print(f"\n=== 구글 스프레드시트에서 티켓 목록 읽는 중... ===")
        if gid:
            print(f"  대상 탭: gid={gid}")
        raw_keys = read_keys_from_sheets(sheet_id, gid)
        if not raw_keys:
            print("[오류] 스프레드시트 A열(2행~)에 티켓 URL/키가 없습니다.")
            sys.exit(1)

        issue_keys = []
        for raw in raw_keys:
            try:
                issue_keys.append(extract_issue_key(raw))
            except ValueError:
                print(f"  [건너뜀] 유효하지 않은 값: {raw}")

        if not issue_keys:
            print("[오류] 유효한 이슈 키가 없습니다.")
            sys.exit(1)

        print(f"  읽어온 티켓: {len(issue_keys)}개 → {', '.join(issue_keys)}")
        print(f"\n=== TC 생성 시작 ===")
        label = f"sheets_{sheet_id[:8]}"
        use_sheets = True

    # ── 엑셀 파일 모드
    elif input_str.endswith(".xlsx") or input_str.endswith(".xls"):
        if not os.path.exists(input_str):
            print(f"[오류] 파일을 찾을 수 없습니다: {input_str}")
            sys.exit(1)

        raw_keys = read_keys_from_excel(input_str)
        if not raw_keys:
            print("[오류] 엑셀 A열에 티켓 URL/키가 없습니다.")
            sys.exit(1)

        issue_keys = []
        for raw in raw_keys:
            try:
                issue_keys.append(extract_issue_key(raw))
            except ValueError:
                print(f"  [건너뜀] 유효하지 않은 값: {raw}")

        if not issue_keys:
            print("[오류] 유효한 이슈 키가 없습니다.")
            sys.exit(1)

        print(f"\n=== 엑셀 일괄 TC 생성 시작: {len(issue_keys)}개 티켓 ===")
        label = f"batch_{os.path.splitext(os.path.basename(input_str))[0]}"
        sheet_id = None
        use_sheets = False

    # ── 단일 티켓 모드
    else:
        try:
            issue_key = extract_issue_key(input_str)
        except ValueError as e:
            print(f"[오류] {e}")
            sys.exit(1)
        issue_keys = [issue_key]
        print(f"\n=== 티켓 TC 자동 생성 시작: {issue_key} ===")
        label = issue_key.replace("/", "_")
        sheet_id = None
        use_sheets = False

    # 클라이언트 초기화
    jira = JIRA(
        server=os.getenv("JIRA_URL"),
        basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
    )
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    # TC 생성
    results = process_keys(jira, groq_client, issue_keys, context)

    if not results:
        print("\n[오류] 생성된 TC가 없습니다.")
        sys.exit(1)

    # 결과 저장
    print(f"\n=== 결과 저장 중... ===")
    os.makedirs("reports", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = f"reports/tc_{label}_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  JSON 저장 완료: {json_path}")

    xlsx_path = f"reports/tc_{label}_{timestamp}.xlsx"
    save_excel(results, xlsx_path)
    print(f"  엑셀 저장 완료: {xlsx_path}")

    # 구글 시트 모드일 경우 시트에도 저장
    if use_sheets:
        save_to_sheets(results, sheet_id)

    total_tc = sum(len(r["test_cases"]) for r in results)
    print(f"\n=== 완료: {len(results)}개 티켓 / {total_tc}개 TC 생성 ===")


if __name__ == "__main__":
    main()
