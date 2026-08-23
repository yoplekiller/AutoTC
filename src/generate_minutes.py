"""
회의 메모 + Jira 티켓 기반 회의록 자동 생성 → Confluence 페이지 게시

--notes-file로 실제 회의 메모(txt/md)를 넘기면 그 메모를 정리만 해서 회의록을 만든다
(토론내용/조치항목/결정사항을 지어내지 않음). --notes-file 없이 안건만 넘기면 회의 전
"사전 아젠다 초안"을 AI가 작성한다 — 이 경우 실제 회의 기록이 아니므로 제목에
"[사전 아젠다 초안]"이 붙는다.

사용법:
  python src/generate_minutes.py "스프린트 1 킥오프" --notes-file notes/sprint1_kickoff.md
  python src/generate_minutes.py "버그 리뷰 회의" --notes-file notes.txt --tickets MKQA-1 MKQA-42
  python src/generate_minutes.py "QA 주간 회의" --dry-run
"""

import sys
import io
import os
import re
import json
import argparse
import base64
from datetime import datetime

# 두 번 감싸면 이전 래퍼가 GC될 때 내부 버퍼까지 닫혀 "I/O operation on closed file" 오류가 남
# (generate_bug_report.py에서 처음 발견된 것과 동일한 패턴).
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import sanitize

JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
CONFLUENCE_BASE = JIRA_URL.rstrip("/") + "/wiki"
CONFLUENCE_SPACE = os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


# ── Jira ─────────────────────────────────────────────────────────────

def fetch_issues(issue_keys: list) -> list:
    if not issue_keys:
        return []
    from jira import JIRA
    jira = JIRA(server=JIRA_URL, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    issues = []
    for key in issue_keys:
        try:
            issue = jira.issue(key)
            issues.append({
                "key": issue.key,
                "summary": issue.fields.summary,
                "issue_type": issue.fields.issuetype.name,
                "status": issue.fields.status.name,
                "url": f"{JIRA_URL}/browse/{issue.key}",
            })
        except Exception as e:
            print(f"  [건너뜀] {key}: {e}")
    return issues


# ── 메모 파일 ────────────────────────────────────────────────────────

def read_notes_file(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"메모 파일을 찾을 수 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f"메모 파일이 비어 있습니다: {path}")
    return content


# ── AI ───────────────────────────────────────────────────────────────

def summarize_minutes_from_notes(groq_client: Groq, agenda: str, notes: str, issues: list) -> dict:
    """실제 회의 메모를 회의록 형식으로 정리한다. 메모에 없는 내용은 지어내지 않는다."""
    issue_list = "\n".join(
        f"- [{i['key']}] ({i['issue_type']}) {i['summary']}"
        for i in issues
    ) if issues else "없음"

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 회의록 정리 담당자입니다. "
                    "제공된 실제 회의 메모를 구조화된 회의록으로 정리합니다. "
                    "메모에 없는 내용은 절대 지어내지 마세요 — 시간, 발표자, 조치 항목, "
                    "결정사항 등 메모에서 확인할 수 없는 값은 빈 문자열이나 빈 배열로 남겨두세요. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. "
                    "TIN, TBD, N/A, ETC 등 영문 약어를 절대 사용하지 마세요. "
                    "모든 내용은 완성된 한국어 문장으로 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음은 실제 회의 중 작성된 메모입니다. 이 메모를 회의록 형식으로 정리해주세요.
추측이나 창작 없이, 메모에 있는 내용만 반영하세요.

회의 안건: {agenda}

회의 메모:
{notes}

관련 Jira 티켓:
{issue_list}

아래 JSON 형식으로만 응답하세요. 마크다운 없이 JSON만 출력하세요.

{{
  "title": "회의록 제목",
  "objective": "메모에서 파악되는 회의 목표 (메모에 명시되지 않았다면 빈 문자열)",
  "topics": [
    {{
      "time": "메모에 시간이 명시된 경우만 채우고, 없으면 빈 문자열",
      "topic": "메모에서 다룬 주제",
      "presenter": "메모에 발표자가 명시된 경우만 채우고, 없으면 빈 문자열",
      "note": "메모 내용을 정리한 논의 내용"
    }}
  ],
  "action_items": ["메모에 명시된 조치 항목만"],
  "decisions": ["메모에 명시된 결정 사항만"]
}}""",
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return sanitize(json.loads(raw))


def generate_minutes_content(groq_client: Groq, agenda: str, issues: list) -> dict:
    """메모 없이 안건만으로 회의 전 '사전 아젠다 초안'을 창작한다.
    실제 회의 기록이 아니므로 호출부에서 반드시 그 사실을 표시할 것 (main()의 [사전 아젠다 초안] 라벨 참고)."""
    issue_list = "\n".join(
        f"- [{i['key']}] ({i['issue_type']}) {i['summary']}"
        for i in issues
    ) if issues else "없음"

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 회의록 작성 전문가입니다. "
                    "회의 안건과 관련 Jira 티켓을 바탕으로 실무 수준의 회의록 초안을 작성합니다. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. "
                    "TIN, TBD, N/A, ETC 등 영문 약어를 절대 사용하지 마세요. 존재하지 않는 약어를 만들어내지 마세요. "
                    "모든 내용은 완성된 한국어 문장으로 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 회의 안건을 바탕으로 회의록 초안을 작성해주세요.

회의 안건: {agenda}

관련 Jira 티켓:
{issue_list}

아래 JSON 형식으로만 응답하세요. 마크다운 없이 JSON만 출력하세요.

{{
  "title": "회의록 제목",
  "objective": "이 회의의 목표 (1~2문장)",
  "topics": [
    {{
      "time": "10분",
      "topic": "토론 주제",
      "presenter": "발표자 미정",
      "note": "주요 논의 내용 또는 메모"
    }}
  ],
  "action_items": ["조치 항목 1", "조치 항목 2"],
  "decisions": ["결정 사항 1", "결정 사항 2"]
}}""",
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return sanitize(json.loads(raw))


# ── Confluence Storage Format 변환 ───────────────────────────────────

def build_confluence_content(plan: dict, issues: list) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    topic_rows = "".join(
        f"""<tr>
          <td><p>{t.get('time', '')}</p></td>
          <td><p>{t.get('topic', '')}</p></td>
          <td><p>{t.get('presenter', '')}</p></td>
          <td><p>{t.get('note', '')}</p></td>
        </tr>"""
        for t in plan.get("topics", [])
    )

    action_items = "".join(
        f"<li><p>{item}</p></li>"
        for item in plan.get("action_items", [])
    )

    decisions = "".join(
        f"<li><p>{d}</p></li>"
        for d in plan.get("decisions", [])
    )

    related_tickets = "".join(
        f'<li><p><a href="{i["url"]}">{i["key"]}</a> — {i["summary"]} ({i["status"]})</p></li>'
        for i in issues
    ) if issues else "<li><p>관련 티켓 없음</p></li>"

    return f"""<h2>📅 날짜</h2>
<p><time datetime="{today}" /></p>

<h2>👥 참석자</h2>
<ul>
  <li><p>(참석자 이름을 입력하세요)</p></li>
</ul>

<h2>🎯 목표</h2>
<p>{plan.get('objective', '')}</p>

<h2>💬 토론 주제</h2>
<table data-table-width="760" data-layout="default">
  <colgroup>
    <col style="width: 100px;" />
    <col style="width: 200px;" />
    <col style="width: 180px;" />
    <col style="width: 280px;" />
  </colgroup>
  <tbody>
    <tr>
      <th data-highlight-colour="#deebff"><p><strong>시간</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>토픽</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>발표자</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>비고</strong></p></th>
    </tr>
    {topic_rows}
  </tbody>
</table>

<h2>✅ 조치 항목</h2>
<ul>{action_items}</ul>

<h2>↩ 결정사항</h2>
<ul>{decisions}</ul>

<h2>🗃 관련 Jira 티켓</h2>
<ul>{related_tickets}</ul>

<p><em>작성일: {today} | 자동 생성 by AutoTC</em></p>"""


# ── Confluence API ────────────────────────────────────────────────────

def get_auth_headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_confluence_page(title: str, content: str, space: str = None) -> dict:
    url = f"{CONFLUENCE_BASE}/rest/api/content"
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space or CONFLUENCE_SPACE},
        "body": {
            "storage": {
                "value": content,
                "representation": "storage",
            }
        },
    }
    res = requests.post(url, headers=get_auth_headers(), json=payload, timeout=30)
    res.raise_for_status()
    data = res.json()
    return {
        "id": data["id"],
        "title": data["title"],
        "url": f"{CONFLUENCE_BASE}{data['_links']['webui']}",
    }


def send_slack_notification(page: dict, agenda: str):
    if not SLACK_WEBHOOK_URL:
        return
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📝 회의록 자동 생성 완료"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*문서 제목*\n<{page['url']}|{page['title']}>"},
                    {"type": "mrkdwn", "text": f"*안건*\n{agenda}"},
                ],
            },
        ]
    }
    requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="회의 메모 기반 회의록 → Confluence 자동 생성")
    parser.add_argument("agenda", help="회의 안건 (예: '스프린트 1 킥오프')")
    parser.add_argument("--notes-file", default=None, help="회의 중 작성한 메모 파일 경로(.txt/.md) — 지정하면 메모를 정리, 없으면 사전 아젠다 초안 생성")
    parser.add_argument("--tickets", nargs="*", default=[], help="관련 Jira 티켓 키 (예: MKQA-1 MKQA-2)")
    parser.add_argument("--space", default=None, help="Confluence 스페이스 키 (기본: QATEST)")
    parser.add_argument("--dry-run", action="store_true", help="Confluence에 올리지 않고 내용만 출력")
    args = parser.parse_args()

    groq_client = Groq(api_key=GROQ_API_KEY)
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"\n=== 회의록 자동 생성 ===")
    print(f"안건: {args.agenda}")

    issues = []
    if args.tickets:
        print(f"\nJira 티켓 조회 중...")
        issues = fetch_issues(args.tickets)

    is_draft = args.notes_file is None
    if is_draft:
        print(f"\n[안내] --notes-file 없이 실행 — 실제 회의 기록이 아닌 사전 아젠다 초안을 생성합니다.")
        print(f"AI 사전 아젠다 초안 작성 중...")
        plan = generate_minutes_content(groq_client, args.agenda, issues)
    else:
        print(f"\n메모 파일 읽는 중: {args.notes_file}")
        notes = read_notes_file(args.notes_file)
        print(f"AI 회의록 정리 중 (메모 기반, 창작 없음)...")
        plan = summarize_minutes_from_notes(groq_client, args.agenda, notes, issues)

    title = f"{plan.get('title', args.agenda)} ({today})"
    if is_draft:
        title += " [사전 아젠다 초안]"

    print(f"\n[생성 결과 미리보기]")
    print(f"  제목   : {title}")
    print(f"  목표   : {plan.get('objective', '')[:60]}...")
    print(f"  토론   : {len(plan.get('topics', []))}개 주제")
    print(f"  조치   : {len(plan.get('action_items', []))}개 항목")
    print(f"  결정   : {len(plan.get('decisions', []))}개 사항")

    if args.dry_run:
        print(f"\n[dry-run] Confluence 업로드를 건너뜁니다.")
        return

    content = build_confluence_content(plan, issues)

    print(f"\nConfluence 페이지 생성 중...")
    page = create_confluence_page(title, content, args.space)

    print(f"\n=== 완료 ===")
    print(f"  제목 : {page['title']}")
    print(f"  URL  : {page['url']}")

    send_slack_notification(page, args.agenda)
    if SLACK_WEBHOOK_URL:
        print(f"  Slack 알림 전송 완료")


if __name__ == "__main__":
    main()
