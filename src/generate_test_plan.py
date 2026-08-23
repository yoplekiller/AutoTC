"""
Jira 티켓 기반 테스트 계획서 자동 생성 → Confluence 페이지 게시

사용법:
  python src/generate_test_plan.py MKQA-1 MKQA-2 MKQA-3
  python src/generate_test_plan.py MKQA-1 MKQA-4 MKQA-5 --title "Sprint 1 테스트 계획서"
  python src/generate_test_plan.py MKQA-1 --dry-run
"""

import sys
import os
import re
import json
import argparse
import base64
from datetime import datetime

import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import sanitize

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
CONFLUENCE_BASE = JIRA_URL.rstrip("/") + "/wiki"
CONFLUENCE_SPACE = os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


# ── Jira ─────────────────────────────────────────────────────────────

def fetch_issues(issue_keys: list) -> list:
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
                "priority": getattr(issue.fields.priority, "name", "Medium") if issue.fields.priority else "Medium",
                "description": issue.fields.description or "설명 없음",
                "url": f"{JIRA_URL}/browse/{issue.key}",
            })
            print(f"  [{key}] {issue.fields.summary}")
        except Exception as e:
            print(f"  [건너뜀] {key}: {e}")
    return issues


# ── AI ───────────────────────────────────────────────────────────────

def generate_plan_content(groq_client: Groq, issues: list) -> dict:
    issue_list = "\n".join(
        f"- [{i['key']}] ({i['issue_type']}) {i['summary']}\n  설명: {i['description'][:200]}"
        for i in issues
    )

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 경력 5년 이상의 QA 엔지니어입니다. "
                    "Jira 티켓 목록을 분석해 실무 수준의 테스트 계획서를 작성합니다. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. 한국어, 숫자, 영문 외 다른 언어(중국어, 일본어, 러시아어 등)는 절대 사용하지 마세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 Jira 티켓 목록을 기반으로 테스트 계획서를 작성해주세요.

[티켓 목록]
{issue_list}

아래 JSON 형식으로만 응답하세요. 마크다운 없이 JSON만 출력하세요.

{{
  "title": "테스트 계획서 제목",
  "test_objective": "테스트 목적 (2~3문장)",
  "in_scope": ["테스트 범위 항목1", "항목2", "항목3"],
  "out_of_scope": ["제외 범위 항목1", "항목2"],
  "test_strategy": {{
    "functional": "기능 테스트 전략 설명",
    "regression": "회귀 테스트 전략 설명",
    "boundary": "경계값 테스트 전략 설명"
  }},
  "test_environment": ["환경 항목1 (예: OS, 브라우저)", "항목2"],
  "entry_criteria": ["진입 기준1", "진입 기준2"],
  "exit_criteria": ["종료 기준1", "종료 기준2"],
  "risks": [
    {{"risk": "리스크 내용", "mitigation": "대응 방안"}}
  ]
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

    def ul(items):
        return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"

    ticket_rows = "".join(
        f"""<tr>
          <td><a href="{i['url']}">{i['key']}</a></td>
          <td>{i['issue_type']}</td>
          <td>{i['summary']}</td>
          <td>{i['status']}</td>
          <td>{i['priority']}</td>
        </tr>"""
        for i in issues
    )

    risk_rows = "".join(
        f"<tr><td>{r['risk']}</td><td>{r['mitigation']}</td></tr>"
        for r in plan.get("risks", [])
    )

    strategy = plan.get("test_strategy", {})

    return f"""<h2>1. 테스트 목적</h2>
<p>{plan['test_objective']}</p>

<h2>2. 테스트 대상 티켓</h2>
<table>
  <thead>
    <tr>
      <th>티켓 키</th><th>유형</th><th>제목</th><th>상태</th><th>우선순위</th>
    </tr>
  </thead>
  <tbody>{ticket_rows}</tbody>
</table>

<h2>3. 테스트 범위</h2>
<h3>In Scope</h3>
{ul(plan.get('in_scope', []))}
<h3>Out of Scope</h3>
{ul(plan.get('out_of_scope', []))}

<h2>4. 테스트 전략</h2>
<ul>
  <li><strong>기능 테스트:</strong> {strategy.get('functional', '')}</li>
  <li><strong>회귀 테스트:</strong> {strategy.get('regression', '')}</li>
  <li><strong>경계값 테스트:</strong> {strategy.get('boundary', '')}</li>
</ul>

<h2>5. 테스트 환경</h2>
{ul(plan.get('test_environment', []))}

<h2>6. 진입 기준 / 종료 기준</h2>
<h3>진입 기준 (Entry Criteria)</h3>
{ul(plan.get('entry_criteria', []))}
<h3>종료 기준 (Exit Criteria)</h3>
{ul(plan.get('exit_criteria', []))}

<h2>7. 리스크 및 대응 방안</h2>
<table>
  <thead><tr><th>리스크</th><th>대응 방안</th></tr></thead>
  <tbody>{risk_rows}</tbody>
</table>

<p><em>작성일: {today} | 자동 생성 by AutoTC</em></p>"""


# ── Confluence API ────────────────────────────────────────────────────

def get_auth_headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def create_confluence_page(title: str, content: str) -> dict:
    url = f"{CONFLUENCE_BASE}/rest/api/content"
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": CONFLUENCE_SPACE},
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


def send_slack_notification(page: dict, issues: list):
    if not SLACK_WEBHOOK_URL:
        return
    ticket_list = ", ".join(i["key"] for i in issues)
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📋 테스트 계획서 자동 생성 완료"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*문서 제목*\n<{page['url']}|{page['title']}>"},
                    {"type": "mrkdwn", "text": f"*대상 티켓*\n{ticket_list}"},
                ],
            },
        ]
    }
    requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jira 티켓 기반 테스트 계획서 → Confluence 자동 생성")
    parser.add_argument("tickets", nargs="+", help="Jira 티켓 키 (예: MKQA-1 MKQA-2 MKQA-3)")
    parser.add_argument("--title", default=None, help="문서 제목 (미입력 시 AI 자동 생성)")
    parser.add_argument("--dry-run", action="store_true", help="Confluence에 올리지 않고 내용만 출력")
    args = parser.parse_args()

    groq_client = Groq(api_key=GROQ_API_KEY)

    print(f"\n=== 테스트 계획서 자동 생성 ===")
    print(f"대상 티켓: {', '.join(args.tickets)}")
    print(f"\nJira 티켓 조회 중...")
    issues = fetch_issues(args.tickets)

    if not issues:
        print("[오류] 유효한 티켓이 없습니다.")
        sys.exit(1)

    print(f"\nAI 계획서 작성 중...")
    plan = generate_plan_content(groq_client, issues)

    title = args.title or plan.get("title", f"테스트 계획서 ({datetime.now().strftime('%Y-%m-%d')})")
    content = build_confluence_content(plan, issues)

    print(f"\n[생성 결과 미리보기]")
    print(f"  제목     : {title}")
    print(f"  테스트 목적: {plan['test_objective'][:80]}...")
    print(f"  In Scope : {len(plan.get('in_scope', []))}개 항목")
    print(f"  리스크   : {len(plan.get('risks', []))}개")

    if args.dry_run:
        print(f"\n[dry-run] Confluence 업로드를 건너뜁니다.")
        print("\n--- Storage Format 미리보기 ---")
        print(content[:500] + "...")
        return

    print(f"\nConfluence 페이지 생성 중...")
    page = create_confluence_page(title, content)

    print(f"\n=== 완료 ===")
    print(f"  제목 : {page['title']}")
    print(f"  URL  : {page['url']}")

    send_slack_notification(page, issues)
    if SLACK_WEBHOOK_URL:
        print(f"  Slack 알림 전송 완료")


if __name__ == "__main__":
    main()
