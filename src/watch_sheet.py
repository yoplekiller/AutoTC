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
import io
import os
import re
import json
import argparse
from datetime import datetime
from difflib import SequenceMatcher

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


def _fix_dangling_latin(line):
    """한글 뒤에 붙은 영단어를 제거하고, 조사로 끝나 미완성된 문장을 보정합니다."""
    if not _DANGLING_LATIN_PATTERN.search(line):
        return line
    line = _DANGLING_LATIN_PATTERN.sub(r"\1", line).rstrip()
    if not _KOREAN_ENDING_PATTERN.search(line):
        line = _TRAILING_PARTICLE_PATTERN.sub("", line).rstrip()
        line += "이 정상적으로 처리됨"
    return line


def _sanitize_text(value):
    """문자열에 섞인 한자/일본어 가나/영단어 오타·금지 표현을 교정하고, 매핑이 없는 외래 문자는 제거합니다."""
    if not isinstance(value, str):
        return value
    for pattern, replacement in _TEXT_FIXES:
        value = pattern.sub(replacement, value)
    if _FOREIGN_SCRIPT_PATTERN.search(value):
        value = _FOREIGN_SCRIPT_PATTERN.sub("", value)
        value = re.sub(r"[ \t]{2,}", " ", value).strip()
    if _DANGLING_LATIN_PATTERN.search(value):
        value = "\n".join(_fix_dangling_latin(line) for line in value.split("\n"))
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
- 사전조건/테스트단계/기대결과: 번호 매겨서 구체적으로 작성 ("1. ..." 형식, 항목이 여러 개면 "2.", "3."으로 이어서)
- 기대결과 각 항목: "~됨" 또는 "~함" 으로 끝낼 것
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
                            "지정된 테스트 유형과 개수를 반드시 지켜서 TC를 작성합니다. "
                            "테스트 단계는 UI 기준으로 원자적이고 명확하게 작성하세요. "
                            "기대결과는 눈으로 판별 가능한 팩트로, '~됨' 또는 '~함'으로 끝내세요. "
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
    """기획서/스펙 복잡도를 분석해 테스트 유형별 적정 TC 수를 결정합니다."""
    import time
    type_map = {
        "Bug": "Bug", "버그": "Bug",
        "Story": "Story", "스토리": "Story",
        "Task": "Task", "작업": "Task",
        "Epic": "Epic", "에픽": "Epic",
    }
    issue_type = type_map.get(issue["issue_type"], "Story")

    minimums = {
        "Bug":   {"기능": 3, "예외처리": 2, "경계값": 2, "회귀": 3},
        "Story": {"기능": 3, "예외처리": 3, "경계값": 2, "회귀": 2, "보안": 1, "UI/UX": 1},
        "Task":  {"기능": 3, "예외처리": 2, "경계값": 2, "회귀": 1},
        "Epic":  {"기능": 4, "예외처리": 3, "경계값": 3, "회귀": 3, "보안": 2},
    }.get(issue_type, {"기능": 3, "예외처리": 2, "경계값": 2, "회귀": 2})

    min_desc = "\n".join([f"  - {t}: 최소 {n}개" for t, n in minimums.items()])
    context_block = f"\n\n[기획서/스펙]\n{context}" if context else ""

    prompt = f"""다음 티켓과 기획서를 분석해서 테스트 유형별로 몇 개의 TC가 필요한지 결정하세요.

[티켓]
유형: {issue['issue_type']} | 제목: {issue['summary']}

[추론된 요구사항]
{augmented_spec}{context_block}

[최솟값 — 반드시 지킬 것]
{min_desc}

판단 기준:
- 화면/입력 필드가 많을수록 경계값/예외처리 증가
- 정책/비즈니스 룰이 복잡할수록 기능/예외처리 증가
- 연관 기능이 많을수록 회귀 증가
- 보안·권한 처리가 있으면 보안 포함

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{"기능": 숫자, "예외처리": 숫자, "경계값": 숫자, "회귀": 숫자, "보안": 숫자, "UI/UX": 숫자}}

보안/UI/UX가 불필요하면 0으로 설정하세요."""

    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "기획서 복잡도를 분석해 테스트 유형별 적정 TC 수를 판단합니다. "
                            "과도하게 적거나 많지 않게, 실무 기준으로 판단하세요. "
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

    if response is None:
        print("  [오류] 플랜 분석 Rate Limit 재시도 소진 — 기본값 사용")
        return [(t, n) for t, n in minimums.items()]

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        plan_dict = json.loads(raw)
        plan = []
        for t, min_n in minimums.items():
            n = max(int(plan_dict.get(t, min_n)), min_n)
            plan.append((t, n))
        for t, n in plan_dict.items():
            if t not in minimums and int(n) > 0:
                plan.append((t, int(n)))
        return plan
    except (json.JSONDecodeError, ValueError):
        print("  [경고] 플랜 분석 실패 — 기본값 사용")
        return [(t, n) for t, n in minimums.items()]


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
    ticket_url = f"{JIRA_URL}/browse/{issue['key']}"
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

        # 우선순위 색상 (E열만)
        for i, tc in enumerate(tc_list):
            color = priority_colors.get(tc.get("우선순위", ""))
            if color:
                ws.format(f"E{3 + i}", {"backgroundColor": color})

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


def mark_row_review_pending(ws_input, row_idx: int, timestamp: str):
    """입력 시트 해당 행의 B열=검수 대기(AI 생성 완료, 사람 확인 필요), C열=처리시각으로 업데이트."""
    ws_input.update_cell(row_idx, 2, "검수 대기 (AI 생성 완료)")
    ws_input.update_cell(row_idx, 3, timestamp)


# ── Slack 알림 ────────────────────────────────────────────────────────

def notify_slack(processed: list, sheet_id: str):
    """처리 완료된 티켓 목록을 Slack으로 알림."""
    if not SLACK_WEBHOOK_URL:
        return

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    lines = [f"*[TC 자동 생성 완료 — 검수 요청]* {len(processed)}개 티켓 처리됨"]
    for item in processed:
        tc_count = item["tc_count"]
        lines.append(f"  • `{item['key']}` {item['summary']} — TC {tc_count}개 (검수 대기)")
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

        # spec 파일 자동 탐색 또는 생성
        spec_context = get_or_generate_spec(groq_client, issue, context)

        # TC 생성
        print(f"  요구사항 추론 중...")
        try:
            augmented_spec = augment_ticket_spec(groq_client, issue, spec_context)
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                print(f"  [일일 한도 초과] Groq 일일 토큰 소진 — 이후 티켓 처리 중단")
                ws_input.update_cell(row_idx, 2, "오류: 일일 토큰 한도 초과")
                ws_input.update_cell(row_idx, 3, timestamp)
                break
            raise
        print(f"  TC 생성 중...")
        tc_list = generate_test_cases(groq_client, issue, augmented_spec, spec_context)
        tc_list = filter_tc_list(tc_list)
        tc_list = dedupe_tc_list(tc_list)
        tc_list = [normalize_tc_id(tc, i) for i, tc in enumerate(tc_list, start=1)]
        print(f"  생성된 TC: {len(tc_list)}개")
        for tc in tc_list:
            print(f"    [{tc.get('tc_id')}] [{tc.get('대분류', '-')}] [{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트시나리오', '')}")

        # 티켓별 시트에 저장
        create_ticket_sheet(sh, issue, tc_list, timestamp)

        # 입력 시트 상태 업데이트 (AI 생성 완료 — 사람 검수 전까지는 확정 아님)
        mark_row_review_pending(ws_input, row_idx, timestamp)
        print(f"  상태 업데이트: 검수 대기")

        processed.append({"key": issue["key"], "summary": issue["summary"], "tc_count": len(tc_list)})

    # 슬랙 알림
    if processed:
        notify_slack(processed, sheet_id)

    print(f"\n=== 완료: {len(processed)}개 티켓 처리 / {sum(p['tc_count'] for p in processed)}개 TC 생성 ===")


if __name__ == "__main__":
    main()
