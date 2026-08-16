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
from difflib import SequenceMatcher

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv


class DailyTokenLimitError(Exception):
    pass

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

def extract_issue_key(input_str: str) -> str:
    url_match = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", input_str)
    if url_match:
        return url_match.group(1)
    key_match = re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", input_str.strip())
    if key_match:
        return input_str.strip()
    raise ValueError(f"유효하지 않은 티켓: {input_str}")


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
                model="llama-3.3-70b-versatile",
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
                max_tokens=2000,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
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


# AI 응답에 가끔 섞이는 한자/일본어 가나 오타 교정 (한국어 텍스트에 정상적으로 등장할 수 없는 패턴)
_TEXT_FIXES = [
    (re.compile(r"예外처리"), "예외처리"),
    (re.compile(r"\s*不存在"), " 존재하지 않음"),
    (re.compile(r"나타남"), "노출됨"),
]
_FOREIGN_SCRIPT_PATTERN = re.compile(r"[一-鿿㐀-䶿぀-ヿ]+")

# 한글에 공백 없이 붙은 영단어(예: "사진을registered") — AI가 문장을 끝맺지 못하고 영어로 누락한 패턴
_DANGLING_LATIN_PATTERN = re.compile(r"([가-힣])([a-zA-Z]{2,})\b")
_TRAILING_PARTICLE_PATTERN = re.compile(r"(을|를|이|가|은|는|에|와|과|의|로|으로|도)$")
_KOREAN_ENDING_PATTERN = re.compile(r"(음|함|됨|임|완료)$")

# 테스트 단계가 "~한다"체로 끝나는 경우 기존 "~함"체와 통일 (유형별 개별 호출로 인한 문체 혼재 보정)
_DECLARATIVE_ENDING_PATTERN = re.compile(r"(한다|된다|않는다|간다|온다|본다)\.?\s*$")
_DECLARATIVE_ENDING_MAP = {
    "한다": "함", "된다": "됨", "않는다": "않음",
    "간다": "감", "온다": "옴", "본다": "봄",
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
    """문자열에 섞인 한자/일본어 가나/영단어 오타·금지 표현을 교정하고, 매핑이 없는 외래 문자는 제거하며, "~한다"체를 "~함"체로 통일합니다."""
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


def normalize_tc_id(tc: dict, seq: int) -> dict:
    """TC 텍스트를 정제하고, tc_id 형식을 "TC_{3자리}"로 강제 고정하며, 기대결과 앞에 "1. "을 강제합니다."""
    for field, value in tc.items():
        tc[field] = _sanitize_text(value)
    tc["tc_id"] = f"TC_{seq:03d}"
    expected = tc.get("기대결과")
    if isinstance(expected, str) and expected and not re.match(r"^\s*1\.", expected):
        tc["기대결과"] = f"1. {expected}"
    return tc


def filter_tc_list(tc_list: list) -> list:
    """필수 필드(테스트시나리오, 기대결과)가 없는 TC를 제거합니다."""
    return [tc for tc in tc_list if tc.get("테스트시나리오") and tc.get("기대결과")]


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


def _call_tc_api(groq_client: Groq, issue: dict, augmented_spec: str, context: str,
                 test_type: str, count: int, start_idx: int) -> list:
    """특정 테스트 유형의 TC를 생성합니다."""
    type_guide = {
        "기능":     "핵심 사용자 여정(로그인, 주문, 결제, 저장, 제출 등) 중심의 정상 흐름 검증",
        "예외처리": "서비스 장애/데이터 손실/권한 문제로 이어질 수 있는 고위험 비정상 흐름 중심",
        "경계값":   (
            "입력 필드의 데이터 타입에 맞는 경계 조건 중 핵심 비즈니스 영향이 큰 항목만 작성 — "
            "텍스트/코드형(쿠폰코드, 닉네임 등): 글자 수 상한/하한, 공백, 특수문자, 대소문자, 다국어; "
            "숫자형(수량, 금액 등): 최솟값/최댓값/0/음수/소수점; "
            "날짜·시간형: 과거/미래/형식 오류/만료 시점; "
            "선택형(체크박스, 드롭다운 등): 미선택/중복 선택/전체 선택. "
            "필드의 실제 데이터 타입과 맞지 않는 케이스(예: 코드 문자열에 최솟값/최댓값)는 작성하지 말 것"
        ),
        "회귀":     "이 기능 변경으로 영향받는 핵심 연관 기능(E2E 주요 경로) 중심 회귀 검증",
        "보안":     "인증 우회, 권한 상승, 토큰 탈취, SQL Injection 등 보안 취약점",
        "UI/UX":    "핵심 화면의 버튼 활성화, 에러 문구, 전환/로딩 등 사용성에 직접 영향 주는 UI 동작",
        "네트워크": "느린 네트워크, 연결 끊김, 타임아웃 상황에서의 동작",
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
- 중요도 높은 시나리오를 우선 작성 (핵심 사용자 여정, 매출/주문/결제/가입/데이터 저장 영향 우선)
- 사소한 엣지케이스(단순 형식 변형, 실제 영향이 낮은 반복 변형)는 제외
- 사전조건/테스트단계/기대결과: 번호 매겨서 구체적으로 작성 ("1. ..." 형식, 항목이 여러 개면 "2.", "3."으로 이어서)
- 기대결과 각 항목: "~됨" 또는 "~함" 으로 끝낼 것

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
    "기대결과": "1. ...됨"
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
                            "지정된 테스트 유형에 맞는 TC를 작성합니다. 목표 개수는 상한이 아니라 참고치이며, 실제로 검증 가능한 조건보다 많이 만들지 않습니다. "
                            "중요도 높은 핵심 시나리오를 우선하고, 사소한 엣지케이스는 최소화하세요. "
                            "요구사항에 명시되어 있거나 합리적으로 검증 가능한 동작만 TC로 작성하고, 근거 없는 기능이나 화면을 지어내지 마세요. "
                            "테스트 단계는 UI 기준으로 원자적이고 명확하게 작성하세요. "
                            "테스트 수행자가 기능을 구현하거나 설정을 새로 만들거나 코드를 수정해야만 수행 가능한 절차는 TC로 작성하지 마세요. "
                            "기대결과는 Pass/Fail을 객관적으로 판단할 수 있는 사실 기반으로 작성하고, '~됨' 또는 '~함'으로 끝내세요. "
                            "'편리하다', '안정적이다', '원활하다'처럼 측정 기준이 없는 주관적 표현은 기대결과에 쓰지 마세요. "
                            "TC는 구현 방법이 아니라, 구현 완료된 결과물이 요구사항을 만족하는지 검증하는 관점으로 작성하세요. "
                            "환경/설정/빌드/CI 관련 요구사항과 기술 Task는 구현 절차가 아니라 빌드 성공 여부·설정값 적용·실행 결과를 확인하는 절차로 쓰세요. "
                            "요구사항에 정의되지 않은 오류 메시지·오류 화면·오류 로그·경고 팝업·fallback·자동 복구 동작을 상상해서 채우지 마세요. "
                            "정상 요구사항을 단순히 반대로 뒤집어 없는 오류 동작을 지어내는 Negative TC를 만들지 마세요. "
                            "한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=3000,
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
        print(f"    [경고] {test_type} JSON 파싱 실패 (응답: {raw[:200]}...)")
        return []


def analyze_spec_for_plan(groq_client: Groq, issue: dict, augmented_spec: str, context: str = "") -> list:
    """기획서/스펙에서 실제로 검증 가능한 조건의 개수를 분석해 테스트 유형별 TC 수를 결정합니다."""
    import time
    context_block = f"\n\n[기획서/스펙]\n{context}" if context else ""

    prompt = f"""다음 티켓과 기획서를 분석해서, 테스트 유형별로 실제 요구사항에서 검증 가능한 조건이 몇 개인지 세어 TC 개수를 결정하세요.

[티켓]
유형: {issue['issue_type']} | 제목: {issue['summary']}

[추론된 요구사항]
{augmented_spec}{context_block}

[판단 기준 — 정해진 최소 개수는 없습니다]
- 기능/예외처리/경계값/회귀/보안/UI/UX 각 유형에 대해, 요구사항에 실제로 근거가 있거나 요구사항으로부터 합리적으로 검증 가능한 조건의 수만큼만 배정할 것
- 핵심 사용자 여정(가입/주문/결제/저장/제출)과 직접 연관된 기능/회귀를 우선하고, 사소한 엣지케이스는 최소화할 것
- 화면/입력 필드가 많아도 경계값은 핵심 필드 위주로만 선별할 것
- 요구사항에 근거가 부족한 유형은 억지로 채우지 말고 0으로 둘 것 — 개수를 채우기 위한 중복 케이스, 추정 기능, 임의의 예외/보안/UX 케이스는 절대 만들지 말 것
- 개발환경/빌드/설정(예: 프로젝트 생성, 빌드 성공 여부, 환경변수·설정값 지정)에 관한 요구사항도 정상적으로 TC 개수에 포함할 것 — 다만 이런 TC는 설정 파일을 만드는 절차가 아니라 "설정값이 적용됐는지, 명령 실행이 성공하는지"를 확인하는 검증 관점으로 작성될 것이므로, 배정할 유형은 UI 조작이 필요 없는 기능/회귀 위주로 판단할 것
- 기능/예외처리/회귀 등 테스트 유형이 다르다는 이유만으로 동일한 검증 목적의 TC를 중복 배정하지 말 것 — 유형이 다르더라도 실질적으로 같은 검증 목적이면 하나의 유형에만 배정할 것

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{"기능": 숫자, "예외처리": 숫자, "경계값": 숫자, "회귀": 숫자, "보안": 숫자, "UI/UX": 숫자}}

해당 유형에 검증 가능한 조건이 없으면 0으로 설정하세요."""

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
                            "기획서에서 실제로 검증 가능한 조건의 수를 세어 테스트 유형별 TC 수를 판단합니다. "
                            "정해진 최소/목표 개수는 없습니다 — 중요도 높은 핵심 흐름 검증(기능/회귀)을 우선하고, "
                            "요구사항에 근거가 부족한 유형은 억지로 채우지 말고 0으로 두세요. 개수를 채우기 위해 부풀리지 마세요. "
                            "환경/빌드/설정 요구사항도 검증 관점 TC로 정상 포함하세요(설정값·실행 결과 확인, UI 절차 아님). "
                            "동일한 검증 목적의 TC를 테스트 유형만 바꿔 중복 배정하지 마세요. "
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

    # Groq 호출 자체가 실패했을 때만 쓰는 최후의 degrade 값 — 정상 경로의 목표치가 아니다.
    fallback = [("기능", 5), ("예외처리", 4), ("경계값", 3), ("회귀", 4)]

    if response is None:
        print("  [오류] 플랜 분석 Rate Limit 재시도 소진 — 기본값 사용")
        return fallback

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        plan_dict = json.loads(raw)
        plan = [(t, int(n)) for t, n in plan_dict.items() if int(n) > 0]
        return plan if plan else fallback
    except (json.JSONDecodeError, ValueError):
        print("  [경고] 플랜 분석 실패 — 기본값 사용")
        return fallback


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
        print(f"    [{test_type}] {len(batch)}개 완료")
        all_tcs.extend(batch)
        idx += len(batch)

    return all_tcs


# ── 구글 시트 결과 저장 (append) ──────────────────────────────────────

def create_ticket_sheet(sh, issue: dict, tc_list: list, generated_at: str):
    """티켓 키 이름으로 시트를 생성(또는 초기화)하고 TC를 기입."""
    import gspread

    sheet_title = issue["summary"][:100]
    headers = [
        "TC ID", "대분류", "소분류", "테스트 유형", "우선순위",
        "테스트 시나리오(목적)", "사전 조건", "테스트 단계", "기대 결과",
        "실제 결과", "테스트 상태", "비고 / 버그 링크",
    ]
    priority_colors = {
        "High":   {"red": 1.0,  "green": 0.8,  "blue": 0.8},
        "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.8},
        "Low":    {"red": 0.85, "green": 0.92, "blue": 0.85},
    }
    col_widths = [100, 110, 120, 110, 90, 280, 200, 320, 260, 220, 100, 160]

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
    ws.merge_cells("A1:L1")

    # 행 2: 헤더
    ws.update([headers], "A2")
    ws.format("A2:L2", {
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
        ])

    if rows_to_add:
        ws.update(rows_to_add, "A3")
        end_row = 3 + len(rows_to_add)

        # 데이터 셀 정렬: 세로=가운데, 가로=왼쪽
        ws.format(f"A3:L{end_row - 1}", {
            "verticalAlignment": "MIDDLE",
            "horizontalAlignment": "LEFT",
            "wrapStrategy": "WRAP",
        })

        # 우선순위 색상 (E열만) — 셀 하나당 ws.format() 호출 1번씩 하면 TC가 많을 때
        # (기획서 URL 직접 입력처럼 40~50개 이상 나오는 경우) 분당 쓰기 요청 할당량(429)을
        # 초과한다. generate_tc.py의 save_to_sheets에서 실제로 재현/수정한 것과 같은 버그라
        # 여기도 동일하게 batch_format으로 모아서 한 번에 보낸다.
        color_requests = []
        for i, tc in enumerate(tc_list):
            color = priority_colors.get(tc.get("우선순위", ""))
            if color:
                color_requests.append({"range": f"E{3 + i}", "format": {"backgroundColor": color}})
        if color_requests:
            ws.batch_format(color_requests)

        # 기존 드롭다운 초기화 후 테스트 상태(K열) 드롭다운 재설정
        sh.batch_update({"requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 2,
                        "endRowIndex": end_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": 12,
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
                model="llama-3.3-70b-versatile",
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
                max_tokens=1500,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
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


def build_confirmed_spec(spec: str, qa_rows: list) -> str:
    """원본 기획서 + QA Review에서 사람이 확정한 답변을 하나의 텍스트로 합친다.
    사람의 답변은 원본 요구사항을 보완하는 확정 정책으로 취급되어, 기존
    augment_ticket_spec/generate_test_cases 파이프라인에 issue["description"]로 그대로 흘러간다
    (TC 생성 프롬프트 자체는 손대지 않는다)."""
    answered = [q for q in qa_rows if q["answer"]]
    if not answered:
        return spec
    answers_block = "\n".join(f"- {q['question']}\n  → 확정: {q['answer']}" for q in answered)
    return f"{spec}\n\n[QA 검토에서 확정된 추가 정책]\n{answers_block}"


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
    tc_list = [normalize_tc_id(tc, i) for i, tc in enumerate(tc_list, start=1)]
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
