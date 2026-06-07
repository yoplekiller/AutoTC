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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv


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
                model="llama-3.3-70b-versatile",
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
                wait = 65 * (attempt + 1)
                print(f"  [Rate Limit/분당] augment {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        raise RuntimeError("augment_ticket_spec Rate Limit 재시도 소진")

    return response.choices[0].message.content.strip()

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
        "보안":     "인증 우회, 권한 상승, 토큰 탈취, SQL Injection 등 보안 취약점",
        "UI/UX":    "버튼 활성화 상태, 에러 메시지 문구, 화면 전환, 로딩 표시 등 UI 동작",
        "네트워크": "느린 네트워크, 연결 끊김, 타임아웃 상황에서의 동작",
    }
    context_block = f"\n\n[기획서/서비스 컨텍스트]\n{context}" if context else ""

    prompt = f"""다음 티켓에 대해 [{test_type}] 유형 TC를 정확히 {count}개 작성하세요.

[테스트 유형 설명]
{type_guide.get(test_type, test_type)}

[티켓 정보]
티켓 키: {issue['key']} | 유형: {issue['issue_type']} | 제목: {issue['summary']}

[요구사항]
{augmented_spec}{context_block}

[작성 지침]
- tc_id: 반드시 "TC_{{3자리}}" 형식 고정 (모듈 구분 없이), {start_idx:03d}번부터 시작
- 테스트유형: 반드시 "{test_type}" 으로 고정
- 사전조건/테스트단계: 번호 매겨서 구체적으로 작성
- 기대결과: "~됨" 또는 "~함" 으로 끝낼 것 (예: "노출됨", "표시됨" — "나타남" 같은 표현은 사용 금지)
- {count}개를 반드시 모두 작성할 것 — 개수 미달 시 불합격

JSON 배열만 출력하세요. 마크다운 없이.

[
  {{
    "tc_id": "TC_{start_idx:03d}",
    "대분류": "...",
    "소분류": "...",
    "테스트유형": "{test_type}",
    "우선순위": "High/Medium/Low",
    "테스트시나리오": "...",
    "사전조건": "1. ...\\n2. ...",
    "테스트단계": "1. ...\\n2. ...\\n3. ...",
    "기대결과": "...됨"
  }}
]"""

    import time
    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "지정된 테스트 유형과 개수를 반드시 지켜서 TC를 작성합니다. "
                            "각 TC는 서로 중복되지 않는 고유한 시나리오여야 합니다. "
                            "테스트 단계는 UI 기준으로 원자적이고 명확하게 작성하세요. "
                            "기대결과는 눈으로 판별 가능한 팩트로, '~됨' 또는 '~함'으로 끝내세요. "
                            "한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=6000,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            is_daily = "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str
            if is_daily:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
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
    """기획서/스펙 복잡도를 분석해 테스트 유형별 적정 TC 수를 결정합니다."""
    type_map = {
        "Bug": "Bug", "버그": "Bug",
        "Story": "Story", "스토리": "Story",
        "Task": "Task", "작업": "Task",
        "Epic": "Epic", "에픽": "Epic",
    }
    issue_type = type_map.get(issue["issue_type"], "Story")

    type_guides = {
        "Bug":   "기능 5개 이상, 예외처리 5개 이상, 경계값 4개 이상, 회귀 5개 이상",
        "Story": "기능 6개 이상, 예외처리 6개 이상, 경계값 5개 이상, 회귀 5개 이상, 보안 3개 이상(인증/결제 포함 시 5개 이상), UI/UX 3개 이상",
        "Task":  "기능 5개 이상, 예외처리 4개 이상, 경계값 4개 이상, 회귀 3개 이상",
        "Epic":  "기능 8개 이상, 예외처리 7개 이상, 경계값 6개 이상, 회귀 7개 이상, 보안 5개 이상",
    }
    type_guide_str = type_guides.get(issue_type, "기능 5개 이상, 예외처리 4개 이상, 경계값 4개 이상, 회귀 4개 이상")
    context_block = f"\n\n[기획서/스펙]\n{context}" if context else ""

    prompt = f"""다음 티켓과 기획서를 분석해서 테스트 유형별로 몇 개의 TC가 필요한지 결정하세요.

[티켓]
유형: {issue['issue_type']} | 제목: {issue['summary']}

[추론된 요구사항]
{augmented_spec}{context_block}

[유형별 목표 수량 — 아래는 최소 기준이며, 티켓이 복잡할수록 더 많이 배정할 것]
{type_guide_str}

복잡도에 따른 추가 배정 기준:
- 화면/입력 필드 3개 이상 → 경계값 +3~5, 예외처리 +3
- 정책/비즈니스 룰 2개 이상 → 기능 +3~5, 예외처리 +3
- 연관 기능/화면 2개 이상 → 회귀 +3~5
- 인증·권한·결제 포함 → 보안 5~8
- 사용자 입력 UI 있음 → UI/UX 3~6
- 빠진 케이스가 없도록 충분히 많이 배정하세요

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{"기능": 숫자, "예외처리": 숫자, "경계값": 숫자, "회귀": 숫자, "보안": 숫자, "UI/UX": 숫자}}

보안/UI/UX가 완전히 불필요하면 0으로 설정하세요."""

    import time
    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "기획서 복잡도를 분석해 테스트 유형별 TC 수를 결정합니다. "
                            "빠진 케이스가 없도록 최대한 촘촘하게, 각 유형별로 충분히 많이 배정하세요. "
                            "JSON만 출력하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
                print(f"    [Rate Limit/분당] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

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
        plan = []
        for t, n in plan_dict.items():
            n = max(int(n), 1)  # 0개 방어만 유지
            if n > 0:
                plan.append((t, n))
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
        "테스트 상태", "비고 / 버그 링크", "우선순위",
    ]
    col_widths = [14, 14, 16, 12, 35, 28, 45, 35, 12, 20, 10]
    last_col_letter = "K"

    header_font  = Font(bold=True, color="FFFFFF")
    header_fill  = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_align   = Alignment(horizontal="left", vertical="center", wrap_text=True)

    priority_fills = {
        "High":   PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid"),
        "Medium": PatternFill(start_color="FFF3CC", end_color="FFF3CC", fill_type="solid"),
        "Low":    PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid"),
    }

    for item in results:
        sheet_title = re.sub(r'[\\/*?:\[\]]', '', item["summary"])[:31]
        ws = wb.create_sheet(title=sheet_title)

        # 행 1: 티켓 URL 정보 (하이퍼링크)
        ticket_url = f"{JIRA_URL}/browse/{item['key']}"
        info_cell = ws.cell(row=1, column=1, value=f"{item['key']}  |  {item['summary']}")
        info_cell.hyperlink = ticket_url
        info_cell.font = Font(bold=True, color="0563C1", underline="single", size=11)
        info_cell.fill = PatternFill(start_color="EBF3FB", end_color="EBF3FB", fill_type="solid")
        info_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.merge_cells(f"A1:{last_col_letter}1")
        ws.row_dimensions[1].height = 22

        # 행 2: 컬럼 헤더
        for col, (header, width) in enumerate(zip(headers, col_widths), start=1):
            cell = ws.cell(row=2, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            ws.column_dimensions[cell.column_letter].width = width
        ws.row_dimensions[2].height = 25

        # 행 3~: TC 데이터
        last_row = 2 + len(item["test_cases"])
        for r_idx, tc in enumerate(item["test_cases"], start=3):
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
            if priority in priority_fills:
                p_cell.fill = priority_fills[priority]
            ws.row_dimensions[r_idx].height = 70

        # 테스트 상태 드롭다운 (I열): P / F / B / N/A
        dv = DataValidation(type="list", formula1='"P,F,B,N/A"', allow_blank=True, showDropDown=False)
        dv.sqref = f"I3:I{last_row}"
        ws.add_data_validation(dv)

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


def read_keys_from_sheets(sheet_id: str) -> list:
    """구글 스프레드시트 첫 번째 시트 A열에서 티켓 URL/키를 읽습니다. (헤더 제외)"""
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)
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
        "테스트 상태", "비고 / 버그 링크", "우선순위",
    ]
    priority_colors = {
        "High":   {"red": 1.0,  "green": 0.8,  "blue": 0.8},
        "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "Low":    {"red": 0.85, "green": 0.92, "blue": 0.85},
    }
    col_widths = [100, 110, 120, 110, 280, 200, 320, 260, 100, 160, 90]
    total_tc = 0

    for item in results:
        sheet_title = item["summary"][:100]
        ticket_url = f"{JIRA_URL}/browse/{item['key']}"

        try:
            ws = sh.worksheet(sheet_title)
            ws.clear()
            print(f"  '{sheet_title}' 시트 초기화")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=sheet_title, rows=200, cols=len(headers))
            print(f"  '{sheet_title}' 시트 생성")

        # 행 1: 티켓 URL 정보
        ws.update([[f"{item['key']}  |  {item['summary']}  |  {ticket_url}"]], "A1")
        ws.format("A1", {
            "backgroundColor": {"red": 0.922, "green": 0.953, "blue": 0.984},
            "textFormat": {"bold": True, "foregroundColor": {"red": 0.02, "green": 0.34, "blue": 0.71}},
            "horizontalAlignment": "LEFT",
        })
        ws.merge_cells("A1:K1")

        # 행 2: 컬럼 헤더
        ws.update([headers], "A2")
        ws.format("A2:K2", {
            "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER",
        })

        # 행 3~: TC 데이터
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
            ])
        if rows_data:
            ws.update(rows_data, "A3")
            end_row = 3 + len(rows_data)

            # 데이터 셀 정렬: 세로=가운데, 가로=왼쪽
            ws.format(f"A3:K{end_row - 1}", {
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "LEFT",
                "wrapStrategy": "WRAP",
            })

            # 우선순위 색상 (K열만)
            for i, tc in enumerate(item["test_cases"]):
                color = priority_colors.get(tc.get("우선순위", ""))
                if color:
                    ws.format(f"K{3 + i}", {"backgroundColor": color})

            # 기존 드롭다운 초기화 후 테스트 상태(I열) 드롭다운 재설정
            sh.batch_update({"requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 2,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": 11,
                        },
                    }
                },
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 2,
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
            ]})

            # 열 너비 설정
            requests_body = [{"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }} for i, px in enumerate(col_widths)]
            sh.batch_update({"requests": requests_body})

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
_FOREIGN_SCRIPT_PATTERN = re.compile(r"[一-鿿㐀-䶿぀-ヿ]+")
_TC_ID_PATTERN = re.compile(r"^TC_(?:[A-Z]+_)?(\d+)$")


def _sanitize_text(value):
    """문자열에 섞인 한자/일본어 가나 오타·금지 표현을 교정하고, 매핑이 없는 외래 문자는 제거합니다."""
    if not isinstance(value, str):
        return value
    for pattern, replacement in _TEXT_FIXES:
        value = pattern.sub(replacement, value)
    if _FOREIGN_SCRIPT_PATTERN.search(value):
        value = _FOREIGN_SCRIPT_PATTERN.sub("", value)
        value = re.sub(r"[ \t]{2,}", " ", value).strip()
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
        print(f"  TC 생성 중...")
        tc_list = generate_test_cases(groq_client, issue, augmented_spec, context)
        tc_list = filter_tc_list(tc_list)
        tc_list = dedupe_tc_list(tc_list)
        print(f"  생성된 TC: {len(tc_list)}개")
        for tc in tc_list:
            print(f"    [{tc.get('tc_id')}] [{tc.get('대분류', '-')}] [{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트시나리오', '')}")

        results.append({
            "key": issue["key"],
            "summary": issue["summary"],
            "status": issue["status"],
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

        print(f"\n=== 구글 스프레드시트에서 티켓 목록 읽는 중... ===")
        raw_keys = read_keys_from_sheets(sheet_id)
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
