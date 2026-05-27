"""
회의 안건 + Jira 티켓 기반 회의록 자동 생성 → Confluence 페이지 게시

사용법:
  python src/generate_minutes.py "스프린트 1 킥오프"
  python src/generate_minutes.py "버그 리뷰 회의" --tickets MKQA-1 MKQA-42
  python src/generate_minutes.py "QA 주간 회의" --dry-run
"""

import sys
import os
import re
import json
import argparse
import base64
from datetime import datetime

import requests
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils import sanitize

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


# ── AI ───────────────────────────────────────────────────────────────

def generate_minutes_content(groq_client: Groq, agenda: str, issues: list) -> dict:
    issue_list = "\n".join(
        f"- [{i['key']}] ({i['issue_type']}) {i['summary']}"
        for i in issues
    ) if issues else "없음"

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 회의록 작성 전문가입니다. "
                    "회의 안건과 관련 Jira 티켓을 바탕으로 실무 수준의 회의록 초안을 작성합니다. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. 한국어, 숫자, 영문 외 다른 언어(중국어, 일본어, 러시아어 등)는 절대 사용하지 마세요."
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
    parser = argparse.ArgumentParser(description="회의 안건 기반 회의록 → Confluence 자동 생성")
    parser.add_argument("agenda", help="회의 안건 (예: '스프린트 1 킥오프')")
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

    print(f"\nAI 회의록 작성 중...")
    plan = generate_minutes_content(groq_client, args.agenda, issues)

    title = f"{plan.get('title', args.agenda)} ({today})"

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
