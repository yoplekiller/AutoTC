"""
구글 시트 폴링 기반 TC 자동 생성 스크립트

[동작 방식]
1. 구글 시트 '티켓 입력' 탭의 A열(티켓 URL/키)을 스캔
2. B열(상태)이 비어있는 행을 '미처리'로 인식
3. Groq AI로 TC 생성 후 '매뉴얼 TC' 탭에 append
4. B열 → '완료', C열 → 처리 시각으로 업데이트
5. Slack 알림 발송

[시트 구조 - '티켓 입력' 탭]
  A열: 티켓 URL 또는 이슈 키 (예: MKQA-1 또는 Jira URL)
  B열: 상태 (비워두면 대기 → 완료로 자동 업데이트)
  C열: 처리 시각 (자동 기입)

[실행]
  python src/watch_sheet.py
  python src/watch_sheet.py --sheet-id YOUR_SHEET_ID  # 시트 ID 직접 지정
"""

import sys
import os
import re
import json
import argparse
from datetime import datetime

import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH = os.path.join(ROOT_DIR, os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"))
JIRA_URL = os.getenv("JIRA_URL", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

INPUT_SHEET_NAME = "티켓 입력"
OUTPUT_SHEET_NAME = "매뉴얼 TC"  # 사용 안 함 (티켓별 시트로 분리)


# ── gspread 클라이언트 ────────────────────────────────────────────────

def _get_gspread_client():
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

    # GitHub Actions 환경: GOOGLE_CREDENTIALS_JSON 환경변수에서 직접 읽기
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        import json as _json
        info = _json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)

    return gspread.authorize(creds)


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
    '티켓 입력' 시트에서 B열이 비어있는 행을 반환.
    반환: [{"row_idx": 2, "raw_value": "MKQA-1"}, ...]
    """
    all_values = ws_input.get_all_values()  # 전체 행 리스트

    pending = []
    for i, row in enumerate(all_values):
        if i == 0:  # 헤더 스킵
            continue
        a_val = row[0].strip() if len(row) > 0 else ""
        b_val = row[1].strip() if len(row) > 1 else ""
        if a_val and not b_val:
            pending.append({"row_idx": i + 1, "raw_value": a_val})  # 1-based

    return pending


# ── Jira ─────────────────────────────────────────────────────────────

def extract_issue_key(input_str: str) -> str:
    url_match = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", input_str)
    if url_match:
        return url_match.group(1)
    key_match = re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", input_str.strip())
    if key_match:
        return input_str.strip()
    raise ValueError(f"유효하지 않은 티켓: {input_str}")


def fetch_issue(jira: JIRA, issue_key: str) -> dict:
    issue = jira.issue(issue_key)
    return {
        "key": issue.key,
        "summary": issue.fields.summary,
        "status": issue.fields.status.name,
        "description": issue.fields.description or "설명 없음",
        "issue_type": issue.fields.issuetype.name,
    }


# ── Groq TC 생성 ─────────────────────────────────────────────────────

def load_context(context_name: str) -> str:
    """contexts/{name}.md 파일을 읽어 반환합니다. 없으면 빈 문자열."""
    if not context_name:
        return ""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "contexts", f"{context_name.lower()}.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def filter_tc_list(tc_list: list) -> list:
    """테스트항목/기대결과가 None이거나 비어있는 TC를 제거합니다."""
    return [tc for tc in tc_list if tc.get("테스트항목") and tc.get("기대결과")]


def augment_ticket_spec(groq_client: Groq, issue: dict, context: str = "") -> str:
    """부실한 티켓 설명을 AI로 보완해 테스트 관점 요구사항을 추론합니다."""
    context_section = f"\n\n[서비스 컨텍스트]\n{context}" if context else ""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 10년차 시니어 QA 엔지니어입니다. "
                    "Jira 티켓 정보가 부족할 때 도메인 지식으로 테스트 관점의 요구사항을 추론합니다. "
                    "서비스 컨텍스트가 제공된 경우 이를 적극 반영하세요. "
                    "한국어로 작성하세요."
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
    return response.choices[0].message.content.strip()


def generate_test_cases(groq_client: Groq, issue: dict, augmented_spec: str, context: str = "") -> list:
    type_hint = {
        "Bug":   "재현 시나리오, 수정 확인, 회귀 테스트 위주로 작성하세요.",
        "Story": "Happy Path, 예외 처리, 경계값 테스트를 골고루 작성하세요.",
        "Task":  "기능 동작 확인 및 예외 케이스 위주로 작성하세요.",
        "Epic":  "주요 흐름과 통합 테스트 관점으로 작성하세요.",
    }.get(issue["issue_type"], "기능 검증, 예외 처리, 경계값을 고려하여 작성하세요.")

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 10년차 시니어 QA 엔지니어입니다. "
                    "실무 수준의 매뉴얼 테스트 케이스를 작성합니다. "
                    "테스트 단계는 테스터가 그대로 따라할 수 있을 만큼 구체적으로 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 Jira 티켓에 대한 매뉴얼 테스트 케이스를 작성해주세요.

[티켓 정보]
티켓 키: {issue['key']}
티켓 유형: {issue['issue_type']}
티켓 제목: {issue['summary']}

[티켓 설명]
{issue['description']}

[추론된 요구사항]
{augmented_spec}{f'''

[서비스 컨텍스트]
{context}''' if context else ""}

[작성 지침]
- {type_hint}
- 테스트유형: 기능 / 예외처리 / 경계값 / 회귀 / 보안 중 적절한 것 선택
- 사전조건은 테스트 실행 전 필요한 상태를 명시 (예: 로그인 상태, 특정 데이터 존재 등)
- 테스트 단계는 번호 매겨서 UI 기준으로 구체적으로 작성
- 기대결과는 눈으로 확인 가능한 수준으로 작성하며 반드시 "~됨" 으로 끝낼 것 (예: "로그인 페이지로 이동됨", "에러 메시지가 표시됨")

반드시 아래 JSON 배열 형식으로만 응답하세요. 마크다운 없이 JSON만 출력하세요.

[
  {{
    "tc_id": "TC-001",
    "테스트유형": "기능",
    "테스트항목": "",
    "사전조건": "",
    "테스트단계": "1. 단계1\\n2. 단계2\\n3. 단계3",
    "기대결과": "",
    "우선순위": "High"
  }}
]""",
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [경고] JSON 파싱 실패")
        return [{"tc_id": "TC-ERROR", "테스트유형": "", "테스트항목": raw, "사전조건": "", "테스트단계": "", "기대결과": "", "우선순위": ""}]


# ── 구글 시트 결과 저장 (append) ──────────────────────────────────────

def create_ticket_sheet(sh, issue: dict, tc_list: list, generated_at: str):
    """티켓 키 이름으로 시트를 생성(또는 초기화)하고 TC를 기입."""
    import gspread

    sheet_title = issue["summary"][:100]  # 시트 이름 = 티켓 제목
    headers = ["TC ID", "테스트유형", "테스트 항목", "사전 조건", "테스트 단계", "기대 결과", "결과", "우선순위", "비고"]
    priority_colors = {
        "High":   {"red": 1.0,  "green": 0.8,  "blue": 0.8},
        "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "Low":    {"red": 0.85, "green": 0.92, "blue": 0.85},
    }

    # 시트 생성 또는 초기화
    try:
        ws = sh.worksheet(sheet_title)
        ws.clear()
        print(f"  '{sheet_title}' 시트 초기화")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_title, rows=200, cols=7)
        print(f"  '{sheet_title}' 시트 생성")

    # 행 1: 티켓 URL 정보
    ticket_url = f"{JIRA_URL}/browse/{issue['key']}"
    ws.update(
        [[f"{issue['key']}  |  {issue['summary']}  |  {ticket_url}  |  생성: {generated_at}"]],
        "A1"
    )
    ws.format("A1", {
        "backgroundColor": {"red": 0.922, "green": 0.953, "blue": 0.984},
        "textFormat": {"bold": True, "foregroundColor": {"red": 0.02, "green": 0.34, "blue": 0.71}},
        "horizontalAlignment": "LEFT",
    })
    ws.merge_cells("A1:I1")

    # 2행: 헤더
    ws.update([headers], "A2")
    ws.format("A2:I2", {
        "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
        "horizontalAlignment": "CENTER",
    })

    # 3행~: TC 데이터
    rows_to_add = []
    for tc in tc_list:
        rows_to_add.append([
            tc.get("tc_id", ""),
            tc.get("테스트유형", ""),
            tc.get("테스트항목", ""),
            tc.get("사전조건", ""),
            tc.get("테스트단계", ""),
            tc.get("기대결과", ""),
            "",                      # 결과 - 테스터 입력
            tc.get("우선순위", ""),
            "",                      # 비고 - 테스터 입력
        ])

    if rows_to_add:
        ws.update(rows_to_add, "A3")

        # 우선순위 색상 (H열)
        for i, tc in enumerate(tc_list):
            color = priority_colors.get(tc.get("우선순위", ""))
            if color:
                ws.format(f"H{3 + i}", {"backgroundColor": color})

        # 결과 드롭다운 (G열, 0-indexed: col 6)
        end_row = 3 + len(rows_to_add)
        sh.batch_update({"requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": 2,
                    "endRowIndex": end_row,
                    "startColumnIndex": 6,
                    "endColumnIndex": 7,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": "P"},
                            {"userEnteredValue": "F"},
                            {"userEnteredValue": "N/A"},
                        ],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }]})

    # 열 너비 설정
    col_widths = [90, 110, 200, 180, 280, 220, 100, 90, 150]
    requests_body = [{"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
        "properties": {"pixelSize": px},
        "fields": "pixelSize",
    }} for i, px in enumerate(col_widths)]
    sh.batch_update({"requests": requests_body})

    print(f"  '{sheet_title}' 시트에 TC {len(tc_list)}개 저장 완료")


def mark_row_done(ws_input, row_idx: int, timestamp: str):
    """입력 시트 해당 행의 B열=완료, C열=처리시각으로 업데이트."""
    ws_input.update_cell(row_idx, 2, "완료")
    ws_input.update_cell(row_idx, 3, timestamp)


# ── Slack 알림 ────────────────────────────────────────────────────────

def notify_slack(processed: list, sheet_id: str):
    """처리 완료된 티켓 목록을 Slack으로 알림."""
    if not SLACK_WEBHOOK_URL:
        return

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    lines = [f"*[TC 자동 생성 완료]* {len(processed)}개 티켓 처리됨"]
    for item in processed:
        tc_count = item["tc_count"]
        lines.append(f"  • `{item['key']}` {item['summary']} — TC {tc_count}개")
    lines.append(f"\n<{sheet_url}|구글 시트에서 확인>")

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

    # 헤더 확인 (1행이 비어있으면 헤더 추가)
    first_row = ws_input.row_values(1)
    if not first_row or first_row[0] != "티켓 URL 또는 이슈 키":
        ws_input.insert_row(["티켓 URL 또는 이슈 키", "상태", "처리 시각"], index=1)
        ws_input.format("A1:C1", {
            "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER",
        })
        print(f"  '{INPUT_SHEET_NAME}' 헤더 추가 완료")

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
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in pending:
        raw = item["raw_value"]
        row_idx = item["row_idx"]

        print(f"\n처리 중: {raw} (행 {row_idx})")

        # 티켓 키 추출
        try:
            issue_key = extract_issue_key(raw)
        except ValueError as e:
            print(f"  [건너뜀] {e}")
            ws_input.update_cell(row_idx, 2, "오류: 유효하지 않은 티켓")
            ws_input.update_cell(row_idx, 3, timestamp)
            continue

        # Jira 조회
        try:
            issue = fetch_issue(jira, issue_key)
        except Exception as e:
            print(f"  [건너뜀] Jira 조회 실패: {e}")
            ws_input.update_cell(row_idx, 2, "오류: Jira 조회 실패")
            ws_input.update_cell(row_idx, 3, timestamp)
            continue

        print(f"  제목: {issue['summary']} | 상태: {issue['status']}")

        # TC 생성
        print(f"  요구사항 추론 중...")
        augmented_spec = augment_ticket_spec(groq_client, issue, context)
        print(f"  TC 생성 중...")
        tc_list = generate_test_cases(groq_client, issue, augmented_spec, context)
        tc_list = filter_tc_list(tc_list)
        print(f"  생성된 TC: {len(tc_list)}개")
        for tc in tc_list:
            print(f"    [{tc.get('tc_id')}] [{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트항목')}")

        # 티켓별 시트에 저장
        create_ticket_sheet(sh, issue, tc_list, timestamp)

        # 입력 시트 상태 업데이트
        mark_row_done(ws_input, row_idx, timestamp)
        print(f"  상태 업데이트: 완료")

        processed.append({"key": issue["key"], "summary": issue["summary"], "tc_count": len(tc_list)})

    # 슬랙 알림
    if processed:
        notify_slack(processed, sheet_id)

    print(f"\n=== 완료: {len(processed)}개 티켓 처리 / {sum(p['tc_count'] for p in processed)}개 TC 생성 ===")


if __name__ == "__main__":
    main()
