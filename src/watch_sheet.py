"""
구글 시트 폴링 기반 TC 자동 생성 스크립트

[동작 방식]
1. 구글 시트 '티켓 입력' 탭의 A열(URL/키)을 스캔
2. C열(상태)이 "TC 생성" 또는 "QA 확인"인 행만 '미처리'로 인식 — 비어있는 행(URL/제목을
   아직 작성 중인 초안)은 처리하지 않는다. 드롭다운에서 명시적으로 선택해야 다음 폴링에서
   처리가 시작된다(Jira 티켓 행/기획서 URL 행 모두 동일).
3. 기획서 URL 행은 먼저 QA Analysis(정책 불명확성 분석)를 수행:
   - 불명확한 정책이 없으면 → 기존처럼 바로 TC 생성
   - 있으면 → TC 생성을 보류하고 이 기획서 전용 "{제목} QA Review" 탭에 확인 질문을
     남긴 뒤 상태를 "QA 확인 필요 (N건)"로 표시. 사람이 그 탭에 답변을 채운 뒤 '티켓 입력'
     시트의 상태를 "QA 확인"으로 직접 바꾸면, 다음 폴링에서 원본 기획서 + 확정 답변을
     합쳐 TC를 생성한다(2차 실행). Jira 티켓 행은 이 게이트 없이 기존과 동일하게 처리.
4. Groq AI로 TC 생성 후 "{제목}" 시트에 저장
5. C열 → '생성 완료', D열 → 처리 시각으로 업데이트
6. Slack 알림 발송

[시트 구조 - '티켓 입력' 탭]
  A열: 티켓 URL/이슈 키(예: MKQA-1, Jira URL) 또는 기획서 Confluence 페이지 URL
       (.../wiki/spaces/.../pages/숫자ID/... 형태면 자동으로 기획서 기반 파이프라인으로 처리됨,
        Jira 티켓 없이도 항상 "에픽" 기준 최소 TC 수량 적용)
  B열: 기획서 제목(선택) — A열이 기획서 URL일 때만 의미 있음. 값을 넣으면 Confluence에서
       자동 추출한 제목(본문 첫 줄) 대신 이 제목을 그대로 사용(결과 시트 탭 이름/헤더에 표시됨).
       비워두면 기존처럼 자동 추출. Jira 티켓 행에서는 무시됨.
  C열: 상태 — 아래 값 중 하나
       (빈 값) 작성 중, 아직 처리 안 함 / "TC 생성"(드롭다운, 1차 실행 트리거) / "생성 완료"
       / "QA 확인 필요 (N건)" / "QA 확인"(드롭다운, 2차 실행 트리거) / "오류: ..." 계열
  D열: 처리 시각 (자동 기입)

  기존에 A/상태/처리시각 3열 스키마로 쓰던 시트는 처음 실행될 때 B열에 "기획서 제목" 컬럼을
  자동으로 끼워넣는 1회성 마이그레이션이 실행된다(기존 데이터·기존 뒤쪽 컬럼은 오른쪽으로 밀리기만
  하고 값은 그대로 보존됨).

[시트 구조 - "{기획서/티켓 제목} QA Review" 탭 (해당 기획서에서 정책 불명확성이 발견됐을 때만
  생성됨 — TC 결과 시트("{제목}")와 마찬가지로 기획서/티켓 하나당 하나씩 생긴다)]
  A열: Question ID (Q-001, Q-002, ...)
  B열: 관련 요구사항/컨텍스트
  C열: 확인 필요 사항 (AI가 생성한 질문)
  D열: 답변 (사람이 직접 입력)
  E열: 상태 ("미확정" / "답변완료" — 답변 입력 후 다음 폴링에서 자동으로 갱신됨)

[실행]
  python src/watch_sheet.py
  python src/watch_sheet.py --sheet-id YOUR_SHEET_ID  # 시트 ID 직접 지정
"""

import sys
import io
import os
import re
import json
import argparse
from datetime import datetime

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv
from utils import rate_limit_wait_seconds
from tc_core import (
    DailyTokenLimitError,
    extract_issue_key, fetch_issue, load_context,
    augment_ticket_spec, analyze_spec_for_plan, generate_test_cases,
    sanitize_tc, filter_tc_list, dedupe_tc_list, _get_gspread_client,
    _sanitize_text,
)

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH = os.path.join(ROOT_DIR, os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"))
JIRA_URL = os.getenv("JIRA_URL", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

INPUT_SHEET_NAME = "티켓 입력"
OUTPUT_SHEET_NAME = "매뉴얼 TC"  # 사용 안 함 (티켓별 시트로 분리)
QA_REVIEW_SHEET_SUFFIX = " QA Review"  # 기획서/티켓 제목 + 이 접미사로 전용 탭을 만든다 (TC 결과 시트와 동일한 명명 규칙)
QA_REVIEW_HEADERS = ["Question ID", "관련 요구사항", "확인 필요 사항", "답변", "상태"]
# '티켓 입력' 상태를 이 값으로 바꾸면(드롭다운 선택) 다음 폴링에서 처리를 시작한다 — 상태가
# 비어있는 행(작성 중인 초안)은 자동 처리되지 않는다.
GENERATE_TRIGGER_STATUS = "TC 생성"
# '티켓 입력' 상태를 이 값으로 직접 바꾸면 QA Review 답변을 반영한 2차 TC 생성이 트리거된다.
RETRY_TRIGGER_STATUS = "QA 확인"


# ── gspread 클라이언트 ────────────────────────────────────────────────

def get_or_create_worksheet(sh, title: str, rows=1000, cols=10):
    """시트가 없으면 생성, 있으면 반환."""
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        print(f"  '{title}' 시트 새로 생성")
        return ws


# ── 미처리 행 스캔 ────────────────────────────────────────────────────

def scan_pending_rows(ws_input) -> list:
    """
    '티켓 입력' 시트에서 C열(상태)이 GENERATE_TRIGGER_STATUS("TC 생성") 또는
    RETRY_TRIGGER_STATUS("QA 확인")인 행을 반환. 상태가 비어있는 행(URL/제목을 아직
    작성 중인 초안)은 처리하지 않는다 — 드롭다운에서 명시적으로 트리거를 선택해야 다음
    폴링에서 처리가 시작된다. 후자는 QA Review 답변을 확정하고 2차 TC 생성을 요청한
    행이다 — status 값을 같이 돌려줘서 호출부가 1차/2차 실행을 구분할 수 있게 한다.
    반환: [{"row_idx": 2, "raw_value": "MKQA-1", "title": "", "status": "TC 생성"}, ...]
    """
    all_values = ws_input.get_all_values()  # 전체 행 리스트

    pending = []
    for i, row in enumerate(all_values):
        if i == 0:  # 헤더 스킵
            continue
        a_val = row[0].strip() if len(row) > 0 else ""
        b_val = row[1].strip() if len(row) > 1 else ""  # 기획서 제목(선택)
        c_val = row[2].strip() if len(row) > 2 else ""  # 상태
        if a_val and c_val in (GENERATE_TRIGGER_STATUS, RETRY_TRIGGER_STATUS):
            pending.append({"row_idx": i + 1, "raw_value": a_val, "title": b_val, "status": c_val})  # 1-based

    return pending


# ── Jira ─────────────────────────────────────────────────────────────



def is_confluence_spec_url(raw: str) -> bool:
    """'티켓 입력' A열 값이 Jira 티켓이 아니라 기획서(Confluence 페이지) URL인지 판별한다.

    Jira 티켓 URL(/browse/KEY-123)과는 경로 형태가 다르므로(/wiki/.../pages/숫자ID),
    둘을 혼동할 일은 없다.
    """
    return bool(re.search(r"atlassian\.net/wiki/.+/pages/\d+", raw))


def slugify_spec_key(text: str) -> str:
    """기획서 URL/제목에서 결과 시트 식별용 키를 만든다."""
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", text).strip("_")
    return (slug[:40] or "SPEC").upper()


def build_pseudo_issue_from_spec(spec: str, key: str, url: str) -> dict:
    """기획서 원문을 이 파일의 augment_ticket_spec/generate_test_cases가 기대하는
    issue 딕셔너리 형태로 감싼다. issue_type을 "에픽"으로 고정하는 이유: 기획서는
    티켓 하나보다 범위가 넓은 문서이므로 analyze_spec_for_plan()의 유형별 최소 TC
    기준 중 가장 넓은 Epic 기준을 적용받도록 한다.
    """
    first_line = next((line.strip("# ").strip() for line in spec.splitlines() if line.strip()), "")
    return {
        "key": key,
        "summary": first_line[:200] or "기획서",
        "status": "기획",
        "description": spec,
        "issue_type": "에픽",
        "url": url,
    }




# ── Groq TC 생성 ─────────────────────────────────────────────────────



def extract_spec_url(issue: dict) -> str:
    """Jira 티켓 설명에서 Notion 또는 Confluence URL을 추출합니다."""
    description = issue.get('description', '') or ''
    notion_match = re.search(r'https://(?:www\.)?notion\.so/\S+', description)
    if notion_match:
        return notion_match.group(0).rstrip(')')
    confluence_match = re.search(r'https://\S+atlassian\.net/wiki/\S+', description)
    if confluence_match:
        return confluence_match.group(0).rstrip(')')
    return ""


def fetch_notion_page(url: str) -> str:
    """Notion 페이지 내용을 텍스트로 fetch합니다."""
    notion_api_key = os.getenv("NOTION_API_KEY")
    if not notion_api_key:
        print("  [경고] NOTION_API_KEY 없음 — Notion fetch 건너뜀")
        return ""

    page_id_match = re.search(r'([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', url)
    if not page_id_match:
        print(f"  [경고] Notion 페이지 ID 추출 실패: {url}")
        return ""

    raw_id = page_id_match.group(1).replace('-', '')
    page_id = f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"
    headers = {
        "Authorization": f"Bearer {notion_api_key}",
        "Notion-Version": "2022-06-28",
    }

    try:
        resp = requests.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [경고] Notion API 오류: {resp.status_code}")
            return ""

        lines = []
        for block in resp.json().get('results', []):
            btype = block.get('type', '')
            rich_text = block.get(btype, {}).get('rich_text', [])
            text = ''.join(t.get('plain_text', '') for t in rich_text)
            if not text:
                continue
            if btype == 'heading_1':
                lines.append(f"# {text}")
            elif btype == 'heading_2':
                lines.append(f"## {text}")
            elif btype == 'heading_3':
                lines.append(f"### {text}")
            elif btype in ('bulleted_list_item', 'numbered_list_item'):
                lines.append(f"- {text}")
            else:
                lines.append(text)

        return '\n'.join(lines)
    except Exception as e:
        print(f"  [경고] Notion fetch 오류: {e}")
        return ""


def fetch_confluence_page(url: str) -> str:
    """Confluence 페이지 내용을 텍스트로 fetch합니다."""
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    if not email or not token:
        print("  [경고] Confluence 인증 정보 없음")
        return ""

    page_id_match = re.search(r'/pages/(\d+)', url)
    domain_match = re.search(r'https://([^/]+)', url)
    if not page_id_match or not domain_match:
        print(f"  [경고] Confluence URL 파싱 실패: {url}")
        return ""

    page_id = page_id_match.group(1)
    domain = domain_match.group(1)

    try:
        resp = requests.get(
            f"https://{domain}/wiki/rest/api/content/{page_id}",
            params={"expand": "body.view"},
            auth=(email, token),
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [경고] Confluence API 오류: {resp.status_code}")
            return ""

        body = resp.json().get('body', {}).get('view', {}).get('value', '')
        text = re.sub(r'<[^>]+>', '\n', body)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    except Exception as e:
        print(f"  [경고] Confluence fetch 오류: {e}")
        return ""


def fetch_spec_from_link(issue: dict) -> str:
    """Jira 티켓에서 Notion/Confluence 링크를 감지하고 기획서를 fetch합니다."""
    url = extract_spec_url(issue)
    if not url:
        return ""

    print(f"  기획서 링크 감지: {url[:70]}...")
    if 'notion.so' in url:
        print("  Notion 페이지 fetch 중...")
        return fetch_notion_page(url)
    elif 'atlassian.net/wiki' in url:
        print("  Confluence 페이지 fetch 중...")
        return fetch_confluence_page(url)
    return ""


def auto_generate_spec(groq_client: Groq, issue: dict) -> str:
    """티켓 기반 기능 기획서를 AI로 자동 생성하고 contexts/에 저장합니다."""
    import time

    prompt = f"""다음 Jira 티켓을 기반으로 QA 테스트에 필요한 기능 기획서를 작성하세요.

[티켓 정보]
티켓 키: {issue['key']}
티켓 유형: {issue['issue_type']}
티켓 제목: {issue['summary']}
티켓 설명: {issue['description']}

아래 항목을 작성하세요:
## 1. 기능 개요 및 목적
## 2. 화면 흐름 (각 단계별 버튼 텍스트, 입력 필드명 포함)
## 3. 입력값 유효성 규칙 (에러 메시지 문구 포함)
## 4. 핵심 정책 (타이머, 횟수 제한, 글자 수 제한 등 수치 포함)
## 5. 예외/에러 케이스 10개 이상 (조건, 에러 메시지, 시스템 동작)
## 6. 회귀 체크 포인트"""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 시니어 서비스 기획자입니다. "
                            "QA 엔지니어가 TC를 작성할 수 있도록 구체적인 기획서를 작성합니다. "
                            "정확한 문구, 수치, 조건을 사용하세요. 한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=3000,
                reasoning_effort="low",
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"  [Rate Limit/분당] spec 생성 {wait}초 대기...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        print("  [오류] spec 자동 생성 실패 — 티켓 정보만 사용")
        return ""

    content = response.choices[0].message.content.strip()

    ticket_key_lower = issue['key'].lower().replace('-', '_')
    spec_path = os.path.join(ROOT_DIR, 'contexts', f'{ticket_key_lower}_spec.md')
    with open(spec_path, 'w', encoding='utf-8') as f:
        f.write(f"---\nticket: {issue['key']}\nfeature: {issue['summary']}\ntype: 기능 기획서 (AI 자동 생성)\ngenerated: {datetime.now().strftime('%Y-%m-%d')}\n---\n\n{content}\n")
    print(f"  spec 저장: contexts/{ticket_key_lower}_spec.md")
    return content


def get_or_generate_spec(groq_client: Groq, issue: dict, service_context: str = "") -> str:
    """
    spec 컨텍스트를 아래 우선순위로 반환합니다.
    1순위: Jira 티켓에 Notion/Confluence 링크 → 실제 기획서 fetch
    2순위: contexts/{ticket}_spec.md 존재 → 로컬 파일 사용
    3순위: 없으면 AI mock 자동 생성
    4순위: 생성 실패 시 서비스 컨텍스트 반환
    """
    # 1순위: 링크 fetch
    fetched = fetch_spec_from_link(issue)
    if fetched:
        return fetched

    # 2순위: 로컬 spec 파일
    ticket_key_lower = issue['key'].lower().replace('-', '_')
    spec_path = os.path.join(ROOT_DIR, 'contexts', f'{ticket_key_lower}_spec.md')
    if os.path.exists(spec_path):
        print(f"  기존 spec 사용: contexts/{ticket_key_lower}_spec.md")
        with open(spec_path, encoding='utf-8') as f:
            return f.read().strip()

    # 3순위: AI mock 자동 생성
    print("  spec 없음 → AI 자동 생성...")
    generated = auto_generate_spec(groq_client, issue)
    return generated if generated else service_context



# ── 구글 시트 결과 저장 (append) ──────────────────────────────────────

def create_ticket_sheet(sh, issue: dict, tc_list: list, generated_at: str):
    """티켓 키 이름으로 시트를 생성(또는 초기화)하고 TC를 기입."""
    import gspread

    sheet_title = issue["summary"][:100]
    headers = [
        "TC ID", "대분류", "소분류", "테스트 유형", "우선순위",
        "테스트 시나리오(목적)", "사전 조건", "테스트 단계", "기대 결과",
        "실제 결과", "테스트 상태", "비고 / 버그 링크",
        "위험도", "자동화 가능여부", "Quality 판정", "판정 사유",
        "요구사항 근거", "요구사항 상태", "실행 가능성", "필요 도구", "실행 메모", "설계 기법",
    ]
    priority_colors = {
        "High":   {"red": 1.0,  "green": 0.8,  "blue": 0.8},
        "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "Low":    {"red": 0.85, "green": 0.92, "blue": 0.85},
    }
    quality_colors = {
        "PASS":   {"red": 0.85, "green": 0.92, "blue": 0.85},
        "REVIEW": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "REJECT": {"red": 1.0,  "green": 0.8,  "blue": 0.8},
    }
    col_widths = [
        100, 110, 120, 110, 90, 280, 200, 320, 260, 220, 100, 160,
        90, 110, 90, 240, 130, 120, 140, 160, 220, 120,
    ]
    last_col_letter = "V"

    try:
        ws = sh.worksheet(sheet_title)
        ws.clear()
        print(f"  '{sheet_title}' 시트 초기화")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_title, rows=200, cols=len(headers))
        print(f"  '{sheet_title}' 시트 생성")

    # 행 1: 티켓 URL 정보 + 검수 안내 배너
    # issue에 "url"이 있으면(기획서 URL 직접 입력 경로) 그걸 쓰고, 없으면 기존처럼 Jira 링크로 조립
    ticket_url = issue.get("url", f"{JIRA_URL}/browse/{issue['key']}")
    ws.update(
        [[f"{issue['key']}  |  {issue['summary']}  |  {ticket_url}  |  생성: {generated_at}"
          f"  |  ⚠️ AI 자동 생성 TC — 실행 전 QA 검수·보완 필요"]],
        "A1"
    )
    ws.format("A1", {
        "backgroundColor": {"red": 1.0, "green": 0.949, "blue": 0.8},
        "textFormat": {"bold": True, "foregroundColor": {"red": 0.6, "green": 0.35, "blue": 0.0}},
        "horizontalAlignment": "LEFT",
    })
    ws.merge_cells(f"A1:{last_col_letter}1")

    # 행 2: 헤더
    ws.update([headers], "A2")
    ws.format(f"A2:{last_col_letter}2", {
        "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        "horizontalAlignment": "CENTER",
    })

    # 행 3~: TC 데이터
    rows_to_add = []
    for tc in tc_list:
        rows_to_add.append([
            tc.get("tc_id", ""),
            tc.get("대분류", ""),
            tc.get("소분류", ""),
            tc.get("테스트유형", ""),
            tc.get("우선순위", ""),
            tc.get("테스트시나리오", ""),
            tc.get("사전조건", ""),
            tc.get("테스트단계", ""),
            tc.get("기대결과", ""),
            "",  # 실제 결과 - 테스터 입력
            "",  # 테스트 상태 - 테스터 입력
            "",  # 연결 버그/비고 - 테스터 입력
            tc.get("위험도", ""),
            tc.get("자동화가능여부", ""),
            tc.get("quality_status", ""),
            tc.get("quality_reason", ""),
            ", ".join(tc.get("requirement_refs", [])),
            tc.get("requirement_status", ""),
            tc.get("execution_type", ""),
            ", ".join(tc.get("required_tools", [])),
            tc.get("execution_note", ""),
            tc.get("test_design_technique", ""),
        ])

    if rows_to_add:
        ws.update(rows_to_add, "A3")
        end_row = 3 + len(rows_to_add)

        # 데이터 셀 정렬: 세로=가운데, 가로=왼쪽
        ws.format(f"A3:{last_col_letter}{end_row - 1}", {
            "verticalAlignment": "MIDDLE",
            "horizontalAlignment": "LEFT",
            "wrapStrategy": "WRAP",
        })

        # 우선순위(E열)/Quality 판정(O열) 색상 — 셀 하나당 ws.format() 호출 1번씩 하면 TC가 많을 때
        # (기획서 URL 직접 입력처럼 40~50개 이상 나오는 경우) 분당 쓰기 요청 할당량(429)을
        # 초과한다. generate_tc.py의 save_to_sheets에서 실제로 재현/수정한 것과 같은 버그라
        # 여기도 동일하게 batch_format으로 모아서 한 번에 보낸다.
        color_requests = []
        for i, tc in enumerate(tc_list):
            color = priority_colors.get(tc.get("우선순위", ""))
            if color:
                color_requests.append({"range": f"E{3 + i}", "format": {"backgroundColor": color}})
            q_color = quality_colors.get(tc.get("quality_status", ""))
            if q_color:
                color_requests.append({"range": f"O{3 + i}", "format": {"backgroundColor": q_color}})
        if color_requests:
            ws.batch_format(color_requests)

        # 기존 드롭다운 초기화 후 테스트 상태(K열)/Quality 판정(O열) 드롭다운 재설정
        sh.batch_update({"requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 2,
                        "endRowIndex": end_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": 22,
                    },
                }
            },
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 2,
                        "endRowIndex": end_row,
                        "startColumnIndex": 10,
                        "endColumnIndex": 11,
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
                        "startRowIndex": 2,
                        "endRowIndex": end_row,
                        "startColumnIndex": 14,
                        "endColumnIndex": 15,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "PASS"},
                                {"userEnteredValue": "REVIEW"},
                                {"userEnteredValue": "REJECT"},
                            ],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            },
        ]})

        # 자동화가능여부(N, index13) + 요구사항/실행성/설계기법 메타데이터(Q~V, index16~21)는
        # 수동 테스트 수행 시 방해되지 않도록 기본 숨김 처리 (Quality 판정/사유는 O/P로 노출 유지) —
        # generate_tc.py의 save_to_sheets와 동일한 숨김 규칙
        hidden_ranges = [(13, 14), (16, 22)]
        sh.batch_update({"requests": [
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": start, "endIndex": end},
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            } for start, end in hidden_ranges
        ]})

    # 열 너비 설정
    requests_body = [{"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
        "properties": {"pixelSize": px},
        "fields": "pixelSize",
    }} for i, px in enumerate(col_widths)]
    sh.batch_update({"requests": requests_body})

    print(f"  '{sheet_title}' 시트에 TC {len(tc_list)}개 저장 완료")


# ── QA Analysis (정책 불명확성 분석) ───────────────────────────────────

def run_qa_analysis(groq_client: Groq, issue: dict, context: str = "") -> dict:
    """기획서를 분석해서, 테스트 케이스의 정확한 기대 결과를 결정하는 데 필요한데
    기획서에 명시되지 않은 정책을 찾아낸다. AI가 빠진 정책을 스스로 확정하지 않고
    (예: "31일인데 2월이면 말일 처리"처럼 그럴듯한 기본값이라도 근거가 없으면 확정 금지),
    전부 질문 형태로만 남긴다. 문서 스타일/오탈자 같은 사소한 문제는 다루지 않는다.

    반환: {"questions": [{"id": "Q-001", "context": "...", "question": "..."}]}
    빈 리스트면 불명확한 정책이 없다는 뜻 — 호출부는 바로 TC 생성으로 진행하면 된다.
    """
    import time
    context_block = f"\n\n[서비스 컨텍스트]\n{context}" if context else ""

    prompt = f"""다음 기획서를 분석하세요.

[티켓 유형] {issue['issue_type']} | [제목] {issue['summary']}

[기획서 원문]
{issue['description']}{context_block}

기획서에 실제로 명시된 요구사항과, 명시돼 있지 않아 정책이 불명확한 부분을 구분하세요.

[불명확 판단 기준]
- 경계값/상태전이 등 테스트 케이스의 정확한 기대 결과를 결정해야 하는데, 기획서에 처리 규칙이 없으면 불명확으로 분류하세요.
- 기획서에 없는 처리 방식을 스스로 확정하지 마세요. 합리적으로 보이는 기본값이라도 기획서에 근거가 없으면 확정하지 말고 질문으로 남기세요.
- 문서 스타일, 오탈자, 사소한 표현 문제는 다루지 마세요 — 테스트 가능한 정책/동작 규칙의 공백만 다루세요.
- 이미 기획서에 명시된 내용은 다시 질문으로 만들지 마세요.

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{"questions": [{{"id": "Q-001", "context": "관련 요구사항 요약", "question": "확인이 필요한 질문"}}]}}

불명확한 정책이 전혀 없으면 questions를 빈 배열로 응답하세요."""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 시니어 QA 엔지니어입니다. 기획서를 분석해서, 테스트 케이스의 "
                            "정확한 기대 결과를 결정하는 정책 중 기획서에 명시되지 않은 부분을 찾아냅니다. "
                            "기획서에 없는 정책을 스스로 확정하지 않고, 반드시 질문 형태로만 남깁니다. "
                            "사소한 문서 스타일 문제는 다루지 않고, 실제로 테스트 가능한 정책 공백만 다룹니다. "
                            "반드시 순수한 한국어로만 작성하세요. 한국어, 숫자, 영문 외 다른 언어는 절대 사용하지 마세요. "
                            "JSON만 출력하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2800,
                reasoning_effort="medium",
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"    [Rate Limit/분당] QA Analysis {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        print("  [오류] QA Analysis Rate Limit 재시도 소진 — 불명확 없음으로 간주하고 진행")
        return {"questions": []}

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        result = json.loads(raw)
        questions = result.get("questions", [])
        for q in questions:
            q["context"] = _sanitize_text(q.get("context", ""))
            q["question"] = _sanitize_text(q.get("question", ""))
        return {"questions": questions}
    except json.JSONDecodeError:
        print(f"  [경고] QA Analysis 파싱 실패 (응답: {raw[:150]}) — 불명확 없음으로 간주하고 진행")
        return {"questions": []}


def qa_review_sheet_title(issue: dict) -> str:
    """기획서/티켓 제목 기반 QA Review 탭 이름을 만든다. 구글시트 탭 이름 100자 제한을
    지키기 위해 접미사(" QA Review")가 들어갈 자리를 남기고 제목을 자른다."""
    max_title_len = 100 - len(QA_REVIEW_SHEET_SUFFIX)
    return issue["summary"][:max_title_len] + QA_REVIEW_SHEET_SUFFIX


def get_or_create_qa_review_sheet(sh, issue: dict):
    """이 기획서/티켓 전용 QA Review 탭을 확보한다 — `create_ticket_sheet`가 TC 결과 시트를
    제목으로 만드는 것과 동일한 규칙으로, TC 시트 바로 옆에서 찾을 수 있게 한다."""
    ws = get_or_create_worksheet(sh, qa_review_sheet_title(issue), rows=200, cols=len(QA_REVIEW_HEADERS))
    first_row = ws.row_values(1)
    if not first_row or first_row[0] != QA_REVIEW_HEADERS[0]:
        ws.update([QA_REVIEW_HEADERS], "A1")
        ws.format(f"A1:{chr(64 + len(QA_REVIEW_HEADERS))}1", {
            "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER",
        })
    return ws


def append_qa_questions(ws_qa, questions: list):
    """QA Analysis가 찾아낸 질문들을 이 기획서 전용 QA Review 탭에 추가한다. 같은 질문
    본문이 이미 있으면 건너뛴다(재실행으로 인한 중복 방지).

    dedup 키로 AI가 매긴 id(예: "Q-001")를 쓰지 않는다 — id는 매 호출마다 AI가 Q-001부터
    새로 매기므로, 재분석하면 실제 질문 내용이 달라도 id가 우연히 겹쳐서 새 질문이 전부
    걸러지는 버그가 있었다(같은 URL을 다시 넣으면 QA Review에 아무것도 추가되지 않던
    증상의 원인). 질문 본문으로 dedup하고, 시트에 보일 ID는 이 탭 안에서 이어지는
    순번으로 여기서 새로 매긴다."""
    existing = ws_qa.get_all_values()
    existing_rows = existing[1:]
    existing_questions = {row[2].strip() for row in existing_rows if len(row) > 2}
    next_seq = len(existing_rows) + 1

    rows_to_add = []
    for q in questions:
        question_text = q["question"].strip()
        if question_text in existing_questions:
            continue
        rows_to_add.append([f"Q-{next_seq:03d}", q.get("context", ""), question_text, "", "미확정"])
        existing_questions.add(question_text)
        next_seq += 1

    if rows_to_add:
        ws_qa.append_rows(rows_to_add, value_input_option="RAW")


def read_qa_answers(ws_qa) -> tuple:
    """QA Review 탭의 질문/답변 행을 전부 읽는다(탭 자체가 이미 기획서 하나 전용이라
    기획서 키로 다시 걸러낼 필요가 없다). 답변이 새로 채워진 행은 상태를 "답변완료"로 갱신한다.
    반환: (행 목록, 모든 질문에 답변이 채워졌는지 여부)
    """
    all_values = ws_qa.get_all_values()
    rows = []
    status_updates = []
    for i, row in enumerate(all_values):
        if i == 0 or len(row) < 1:
            continue
        answer = row[3].strip() if len(row) > 3 else ""
        status = row[4].strip() if len(row) > 4 else ""
        if answer and status != "답변완료":
            status_updates.append((i + 1, "답변완료"))
            status = "답변완료"
        rows.append({
            "row_idx": i + 1,
            "id": row[0].strip() if len(row) > 0 else "",
            "context": row[1].strip() if len(row) > 1 else "",
            "question": row[2].strip() if len(row) > 2 else "",
            "answer": answer,
            "status": status,
        })

    for row_idx, status in status_updates:
        ws_qa.update_cell(row_idx, 5, status)

    all_answered = bool(rows) and all(r["answer"] for r in rows)
    return rows, all_answered


CONFIRMED_QA_MARKER = "[QA 검토에서 확정된 추가 정책]"


def build_confirmed_spec(spec: str, qa_rows: list) -> str:
    """원본 기획서 + QA Review에서 사람이 확정한 답변을 하나의 텍스트로 합친다.
    사람의 답변은 원본 요구사항을 보완하는 확정 정책으로 취급되어, 기존
    augment_ticket_spec/generate_test_cases 파이프라인에 issue["description"]로 그대로 흘러간다
    (TC 생성 프롬프트 자체는 손대지 않는다)."""
    answered = [q for q in qa_rows if q["answer"]]
    if not answered:
        return spec
    answers_block = "\n".join(f"- {q['question']}\n  → 확정: {q['answer']}" for q in answered)
    return f"{spec}\n\n{CONFIRMED_QA_MARKER}\n{answers_block}"


def extract_confirmed_qa_block(description: str) -> str:
    """issue['description']에 build_confirmed_spec()이 붙인 확정 QA 블록이 있으면 그대로 추출한다.

    augment_ticket_spec()은 부실한 Jira 티켓을 보완하려고 만든 함수라, "기능 요구사항 3~5개/
    예외 케이스 2~3개"처럼 고정된 작은 틀로 요약한다. 기획서 기반 플로우에서 이 함수에 QA
    Review 확정 답변(특히 날짜/경계값처럼 기존 3~5개 틀에 안 들어가는 항목)까지 같이 넣으면,
    요약 과정에서 통째로 잘려나가는 걸 실제로 재현 확인함(2026-08-16, payday-budget Phase 2
    스펙으로 재현 — 확정 답변 7개 중 날짜 관련 4개가 augmented_spec에서 전부 사라져서
    analyze_spec_for_plan이 경계값 TC를 0개로 판단함). 요약에 정확성을 맡기지 않고, 이 블록을
    원문 그대로 augmented_spec 뒤에 코드로 강제 첨부해 TC 플랜 단계가 반드시 보게 만든다.
    """
    idx = description.find(CONFIRMED_QA_MARKER)
    return description[idx:] if idx != -1 else ""


def generate_and_save_tc(sh, ws_input, groq_client, issue: dict, context: str, row_idx: int, timestamp: str):
    """요구사항 추론 → TC 생성 → 필터/중복제거 → 시트 저장 → 입력행 상태 업데이트.

    Jira 티켓 경로와 기획서 URL 직접 입력 경로가 issue 딕셔너리만 다르게 만들어서
    이 함수로 합류한다(이후 로직은 완전히 동일).

    반환: (processed 요약 dict 또는 None, 일일 토큰 한도로 이후 처리를 중단해야 하면 True)
    """
    print("  요구사항 추론 중...")
    try:
        augmented_spec = augment_ticket_spec(groq_client, issue, context)
    except DailyTokenLimitError:
        print(f"  [일일 한도 초과] Groq 일일 토큰 소진 — 이후 항목 처리 중단")
        ws_input.update_cell(row_idx, 3, "오류: 일일 토큰 한도 초과")
        ws_input.update_cell(row_idx, 4, timestamp)
        return None, True

    # QA Review 확정 답변은 augment_ticket_spec의 압축 요약을 못 믿고 원문 그대로 재첨부한다
    # (extract_confirmed_qa_block 설명 참고 — 요약 과정에서 날짜/경계값류가 누락되는 걸 확인함).
    confirmed_qa_block = extract_confirmed_qa_block(issue.get("description", ""))
    if confirmed_qa_block:
        augmented_spec = f"{augmented_spec}\n\n{confirmed_qa_block}"

    print(f"  TC 생성 중...")
    # analyze_spec_for_plan(TC 플랜 결정 단계)도 augment_ticket_spec과 별도로 Groq를 호출하고
    # 별도로 DailyTokenLimitError를 던질 수 있는데, 이 호출은 여태 감싸지 않고 있었다 —
    # 실제 GitHub Actions 실행(2026-08-10)에서 이걸로 크래시(uncaught exception, exit 1)가
    # 재현됨. 위와 동일하게 처리한다. (이전엔 문자열 매칭(`"per day" in str(e)`)으로 판별했는데,
    # DailyTokenLimitError 자신의 메시지("Groq 일일 토큰 한도 초과")엔 그 영문 substring이 없어서
    # 실제로는 안 걸리고 그대로 재발생(raise)하던 것도 같이 바로잡음 — 타입으로 직접 잡는다.
    try:
        tc_list = generate_test_cases(groq_client, issue, augmented_spec, context)
    except DailyTokenLimitError:
        print(f"  [일일 한도 초과] Groq 일일 토큰 소진 — 이후 항목 처리 중단")
        ws_input.update_cell(row_idx, 3, "오류: 일일 토큰 한도 초과")
        ws_input.update_cell(row_idx, 4, timestamp)
        return None, True
    tc_list = filter_tc_list(tc_list)
    tc_list = dedupe_tc_list(tc_list)
    # 필터/중복제거로 사라진 TC가 있어도 tc_id가 빠짐없이 이어지도록 최종 순서로 재번호 매김
    # (sanitize_tc는 이미 생성 단계에서 한 번 적용됐으므로 여기서는 seq 재부여만 idempotent하게 반복)
    tc_list = [sanitize_tc(tc, tc.get("테스트유형", ""), i) for i, tc in enumerate(tc_list, start=1)]
    print(f"  생성된 TC: {len(tc_list)}개")
    for tc in tc_list:
        print(f"    [{tc.get('tc_id')}] [{tc.get('대분류', '-')}] [{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트시나리오', '')}")

    create_ticket_sheet(sh, issue, tc_list, timestamp)
    mark_row_review_pending(ws_input, row_idx, timestamp)
    print(f"  상태 업데이트: 검수 대기")

    return {"key": issue["key"], "summary": issue["summary"], "tc_count": len(tc_list)}, False


def mark_row_review_pending(ws_input, row_idx: int, timestamp: str):
    """입력 시트 해당 행의 C열=생성 완료(AI 생성 완료, 사람 확인 필요), D열=처리시각으로 업데이트."""
    ws_input.update_cell(row_idx, 3, "생성 완료")
    ws_input.update_cell(row_idx, 4, timestamp)


# ── Slack 알림 ────────────────────────────────────────────────────────

def notify_slack(processed: list, sheet_id: str, needs_qa: list = None):
    """처리 완료된 티켓 목록 + QA 확인이 필요해진 기획서 목록을 Slack으로 알림."""
    if not SLACK_WEBHOOK_URL:
        return
    needs_qa = needs_qa or []
    if not processed and not needs_qa:
        return

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    lines = []
    if processed:
        lines.append(f"*[TC 자동 생성 완료 — 검수 요청]* {len(processed)}개 티켓 처리됨")
        for item in processed:
            lines.append(f"  • `{item['key']}` {item['summary']} — TC {item['tc_count']}개 (검수 대기)")
    if needs_qa:
        lines.append(f"\n*[QA 확인 필요]* {len(needs_qa)}건")
        for item in needs_qa:
            lines.append(
                f"  • `{item['key']}` {item['summary']} — 미답변 질문 {item['question_count']}건. "
                f"'QA Review' 탭에서 답변 후 '티켓 입력' 상태를 '{RETRY_TRIGGER_STATUS}'로 바꿔주세요."
            )
    if processed:
        lines.append("\n⚠️ AI가 생성한 초안입니다. QA 담당자 검수·보완 후 사용해주세요.")
    lines.append(f"<{sheet_url}|구글 시트에서 확인>")

    payload = {"text": "\n".join(lines)}
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            print("  Slack 알림 발송 완료")
        else:
            print(f"  [경고] Slack 알림 실패: {resp.status_code}")
    except Exception as e:
        print(f"  [경고] Slack 알림 오류: {e}")


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="구글 시트 폴링 기반 TC 자동 생성")
    parser.add_argument("--sheet-id", default=SPREADSHEET_ID, help="구글 스프레드시트 ID")
    parser.add_argument("--context", default=os.getenv("CONTEXT_NAME", ""), help="서비스 컨텍스트 이름 (예: kream)")
    args = parser.parse_args()

    context = load_context(args.context)
    if context:
        print(f"  컨텍스트 로드됨: contexts/{args.context}.md")

    sheet_id = args.sheet_id
    if not sheet_id:
        print("[오류] SPREADSHEET_ID 환경변수 또는 --sheet-id 인자가 필요합니다.")
        sys.exit(1)

    print(f"\n=== 구글 시트 폴링 시작 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")

    # 클라이언트 초기화
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)

    # 입력 시트 확인
    ws_input = get_or_create_worksheet(sh, INPUT_SHEET_NAME)

    # 헤더 확인 / 마이그레이션
    first_row = ws_input.row_values(1)
    new_header = ["티켓 URL 또는 이슈 키", "기획서 제목(선택)", "상태", "처리 시각"]
    header_format = {
        "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        "horizontalAlignment": "CENTER",
    }

    if not first_row or first_row[0] != "티켓 URL 또는 이슈 키":
        # 시트가 비어있거나 헤더가 아예 없는 경우 — 새로 만든다.
        ws_input.insert_row(new_header, index=1)
        ws_input.format("A1:D1", header_format)
        print(f"  '{INPUT_SHEET_NAME}' 헤더 추가 완료")
    elif len(first_row) < 2 or first_row[1] != "기획서 제목(선택)":
        # 기존 3열(URL/상태/처리시각) 스키마 → B열에 "기획서 제목" 컬럼을 끼워넣는 1회성 마이그레이션.
        # 기존 데이터가 아래로 밀리지 않도록 행이 아니라 열을 삽입해서, 기존 값은 오른쪽으로만
        # 이동시킨다(예: 기존 B/C/D열의 상태·처리시각·검수완료여부 데이터는 그대로 C/D/E로 이동).
        sh.batch_update({"requests": [{
            "insertDimension": {
                "range": {"sheetId": ws_input.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                "inheritFromBefore": False,
            }
        }]})
        ws_input.update([new_header], "A1:D1")
        ws_input.format("A1:D1", header_format)
        print(f"  '{INPUT_SHEET_NAME}' 기존 3열 스키마 감지 → B열에 '기획서 제목' 컬럼 삽입 (마이그레이션 완료)")

    # C열(상태) 드롭다운: GENERATE_TRIGGER_STATUS/RETRY_TRIGGER_STATUS를 직접 타이핑하다
    # 오타 나면 실행이 영원히 트리거 안 되는 문제를 막는다. strict=False라 스크립트가 쓰는
    # 다른 상태값(생성 완료/QA 확인 필요 (N건)/오류: ... 등)은 그대로 자유롭게 쓸 수 있다.
    sh.batch_update({"requests": [{
        "setDataValidation": {
            "range": {
                "sheetId": ws_input.id,
                "startRowIndex": 1,
                "endRowIndex": ws_input.row_count,
                "startColumnIndex": 2,
                "endColumnIndex": 3,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": GENERATE_TRIGGER_STATUS},
                        {"userEnteredValue": RETRY_TRIGGER_STATUS},
                    ],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    }]})

    # 미처리 행 스캔
    pending = scan_pending_rows(ws_input)

    if not pending:
        print("  미처리 티켓 없음 - 종료")
        return

    print(f"  미처리 티켓 {len(pending)}개 발견: {[p['raw_value'] for p in pending]}")

    # Jira / Groq 클라이언트
    jira = JIRA(
        server=os.getenv("JIRA_URL"),
        basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
    )
    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    processed = []
    needs_qa = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in pending:
        raw = item["raw_value"]
        row_idx = item["row_idx"]
        title = item.get("title", "")  # B열: 기획서 제목(선택), Jira 티켓 행에서는 항상 빈 문자열
        is_retry = item.get("status", "") == RETRY_TRIGGER_STATUS

        print(f"\n처리 중: {raw} (행 {row_idx})" + (" [2차: QA 검토 완료]" if is_retry else ""))

        if is_confluence_spec_url(raw):
            # ── 기획서(Confluence 페이지) URL 직접 입력 경로
            print("  기획서(Confluence) URL로 인식 — 기획서 기반 파이프라인 사용")
            spec = fetch_confluence_page(raw)
            if not spec:
                print(f"  [건너뜀] 기획서 페이지를 가져오지 못했습니다: {raw}")
                ws_input.update_cell(row_idx, 3, "오류: 기획서 조회 실패")
                ws_input.update_cell(row_idx, 4, timestamp)
                continue

            key_source = title or raw
            spec_key = slugify_spec_key(key_source)
            issue = build_pseudo_issue_from_spec(spec, spec_key, raw)
            if title:
                # B열에 사람이 직접 적은 제목이 있으면, Confluence 본문 첫 줄 추출보다 우선한다
                # (본문 구조가 지저분하면 첫 줄이 실제 제목과 다를 수 있어서).
                issue["summary"] = title
            print(f"  제목: {issue['summary']}")
            spec_context = context

            if is_retry:
                # ── 2차 실행: QA Review 답변이 다 채워졌는지 확인 후, 원본 기획서 + 확정 답변으로 진행
                ws_qa = get_or_create_qa_review_sheet(sh, issue)
                qa_rows, all_answered = read_qa_answers(ws_qa)
                if qa_rows and not all_answered:
                    unanswered = sum(1 for q in qa_rows if not q["answer"])
                    print(f"  [보류] QA Review 미답변 질문 {unanswered}건 남음 — TC 생성 보류")
                    ws_input.update_cell(row_idx, 3, f"QA 확인 필요 ({unanswered}건)")
                    ws_input.update_cell(row_idx, 4, timestamp)
                    needs_qa.append({"key": issue["key"], "summary": issue["summary"], "question_count": unanswered})
                    continue
                if qa_rows:
                    print(f"  QA Review 답변 {len(qa_rows)}건 전부 확인 — 원본 기획서에 반영")
                    issue["description"] = build_confirmed_spec(spec, qa_rows)
                # qa_rows가 비어있으면(질문 자체가 없었던 행) 원본 기획서 그대로 진행
            else:
                # ── 1차 실행: QA Analysis로 정책 불명확성 먼저 확인
                print("  QA Analysis 수행 중...")
                try:
                    qa_result = run_qa_analysis(groq_client, issue, spec_context)
                except DailyTokenLimitError:
                    print(f"  [일일 한도 초과] Groq 일일 토큰 소진 — 이후 항목 처리 중단")
                    ws_input.update_cell(row_idx, 3, "오류: 일일 토큰 한도 초과")
                    ws_input.update_cell(row_idx, 4, timestamp)
                    break
                questions = qa_result["questions"]
                if questions:
                    print(f"  QA 확인 필요 질문 {len(questions)}건 발견 — TC 생성 보류")
                    ws_qa = get_or_create_qa_review_sheet(sh, issue)
                    append_qa_questions(ws_qa, questions)
                    ws_input.update_cell(row_idx, 3, f"QA 확인 필요 ({len(questions)}건)")
                    ws_input.update_cell(row_idx, 4, timestamp)
                    needs_qa.append({"key": issue["key"], "summary": issue["summary"], "question_count": len(questions)})
                    continue
                print("  불명확한 정책 없음 — 바로 TC 생성 진행")

        else:
            # ── 기존 Jira 티켓 경로
            try:
                issue_key = extract_issue_key(raw)
            except ValueError as e:
                print(f"  [건너뜀] {e}")
                ws_input.update_cell(row_idx, 3, "오류: 유효하지 않은 티켓")
                ws_input.update_cell(row_idx, 4, timestamp)
                continue

            try:
                issue = fetch_issue(jira, issue_key)
            except Exception as e:
                print(f"  [건너뜀] Jira 조회 실패: {e}")
                ws_input.update_cell(row_idx, 3, "오류: Jira 조회 실패")
                ws_input.update_cell(row_idx, 4, timestamp)
                continue

            print(f"  제목: {issue['summary']} | 상태: {issue['status']}")

            # spec 파일 자동 탐색 또는 생성
            spec_context = get_or_generate_spec(groq_client, issue, context)

        result, hit_daily_limit = generate_and_save_tc(sh, ws_input, groq_client, issue, spec_context, row_idx, timestamp)
        if hit_daily_limit:
            break
        if result:
            processed.append(result)

    # 슬랙 알림
    notify_slack(processed, sheet_id, needs_qa)

    print(
        f"\n=== 완료: {len(processed)}개 티켓 처리 / {sum(p['tc_count'] for p in processed)}개 TC 생성"
        f" / QA 확인 필요 {len(needs_qa)}건 ==="
    )


if __name__ == "__main__":
    main()
