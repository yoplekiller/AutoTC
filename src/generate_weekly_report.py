"""
Jira 완료/생성/진행중 티켓 + (선택) GitHub Actions 실행 이력으로 주간 QA 활동 요약 → Confluence 페이지 게시

사용법:
  python src/generate_weekly_report.py
  python src/generate_weekly_report.py --days 14
  python src/generate_weekly_report.py --repo yoplekiller/KurlyApp --repo yoplekiller/PlaywrightQA
  python src/generate_weekly_report.py --dry-run
"""

import sys
import io
import os
import re
import json
import argparse
import base64
import subprocess
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import sanitize, rate_limit_wait_seconds

JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "MKQA")
CONFLUENCE_BASE = JIRA_URL.rstrip("/") + "/wiki"
CONFLUENCE_SPACE = os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


class DailyTokenLimitError(Exception):
    pass


# ── Jira ─────────────────────────────────────────────────────────────

def fetch_weekly_issues(project_key: str, days: int) -> dict:
    """완료됨 / 신규 생성 / 현재 진행중 티켓을 각각 JQL로 조회합니다."""
    jira = JIRA(server=JIRA_URL, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))

    def _search(jql: str) -> list:
        issues = jira.search_issues(jql, maxResults=100)
        return [
            {
                "key": i.key,
                "summary": i.fields.summary,
                "issue_type": i.fields.issuetype.name,
                "status": i.fields.status.name,
                "url": f"{JIRA_URL}/browse/{i.key}",
            }
            for i in issues
        ]

    completed = _search(
        f'project = {project_key} AND statusCategory = Done '
        f'AND resolutiondate >= -{days}d ORDER BY resolutiondate DESC'
    )
    created = _search(
        f'project = {project_key} AND created >= -{days}d ORDER BY created DESC'
    )
    in_progress = _search(
        f'project = {project_key} AND statusCategory = "In Progress" ORDER BY updated DESC'
    )
    return {"completed": completed, "created": created, "in_progress": in_progress}


# ── GitHub Actions ───────────────────────────────────────────────────

def fetch_ci_summary(repo: str, days: int) -> dict:
    """gh CLI로 최근 워크플로우 실행 이력을 가져와 결과별 개수를 집계합니다."""
    result = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--limit", "200",
         "--json", "conclusion,createdAt,status,name"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(f"  [건너뜀] {repo} CI 조회 실패: {result.stderr.strip()}")
        return {"repo": repo, "total": 0, "success": 0, "failure": 0, "in_progress": 0, "other": 0}

    runs = json.loads(result.stdout)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts = {"repo": repo, "total": 0, "success": 0, "failure": 0, "in_progress": 0, "other": 0}

    for run in runs:
        created_at = datetime.strptime(run["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if created_at < cutoff:
            continue
        counts["total"] += 1
        if run["status"] != "completed":
            counts["in_progress"] += 1
        elif run["conclusion"] == "success":
            counts["success"] += 1
        elif run["conclusion"] == "failure":
            counts["failure"] += 1
        else:
            counts["other"] += 1

    return counts


# ── AI 요약 ──────────────────────────────────────────────────────────

def summarize_week(groq_client: Groq, issues: dict, ci_summaries: list, days: int) -> str:
    def _fmt(items: list) -> str:
        return "\n".join(f"- [{i['key']}] ({i['issue_type']}) {i['summary']}" for i in items) or "없음"

    ci_block = "\n".join(
        f"- {c['repo']}: 총 {c['total']}회 (성공 {c['success']} / 실패 {c['failure']} / 진행중 {c['in_progress']} / 기타 {c['other']})"
        for c in ci_summaries
    ) if ci_summaries else "(CI 정보 없음)"

    prompt = f"""아래는 최근 {days}일간 QA 활동 원자료입니다. 이 데이터에 실제로 있는 내용만 근거로 요약하세요.
데이터에 없는 원인이나 블로커를 추측해서 지어내지 마세요.

[완료된 티켓]
{_fmt(issues['completed'])}

[신규 생성된 티켓]
{_fmt(issues['created'])}

[현재 진행중 티켓]
{_fmt(issues['in_progress'])}

[CI 실행 현황]
{ci_block}

2~4문장으로 이번 주 QA 활동을 요약하세요. 어떤 영역(기능/유형) 작업이 많았는지는 티켓 제목에서 드러나는 만큼만 언급하고,
근거 없는 원인 분석이나 블로커 추측은 하지 마세요. 완성된 한국어 문장으로만 작성하고 JSON이나 마크다운 없이 텍스트만 출력하세요."""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 QA 팀 리드입니다. 주간 활동 데이터를 사실에 근거해서만 요약합니다. "
                            "데이터에 없는 내용을 추측하거나 지어내지 않습니다. 순수한 한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1000,
                reasoning_effort="low",
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과") from e
            if "rate_limit" in e_str or "429" in str(e):
                import time
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"  [Rate Limit] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        raise RuntimeError("summarize_week Rate Limit 재시도 소진")

    return sanitize(response.choices[0].message.content.strip())


# ── Confluence Storage Format 변환 ───────────────────────────────────

def build_confluence_content(issues: dict, ci_summaries: list, summary_text: str, period: str) -> str:
    def _rows(items: list) -> str:
        if not items:
            return '<tr><td colspan="3"><p>없음</p></td></tr>'
        return "".join(
            f"""<tr>
              <td><p><a href="{i['url']}">{i['key']}</a></p></td>
              <td><p>{i['issue_type']}</p></td>
              <td><p>{i['summary']}</p></td>
            </tr>"""
            for i in items
        )

    ci_rows = "".join(
        f"""<tr>
          <td><p>{c['repo']}</p></td>
          <td><p>{c['total']}</p></td>
          <td><p>{c['success']}</p></td>
          <td><p>{c['failure']}</p></td>
        </tr>"""
        for c in ci_summaries
    ) if ci_summaries else '<tr><td colspan="4"><p>CI 정보 없음</p></td></tr>'

    def _table(title: str, items: list) -> str:
        return f"""<h3>{title} ({len(items)}건)</h3>
<table data-table-width="760">
  <tbody>
    <tr>
      <th data-highlight-colour="#deebff"><p><strong>키</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>유형</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>제목</strong></p></th>
    </tr>
    {_rows(items)}
  </tbody>
</table>"""

    return f"""<h2>📋 요약</h2>
<p>{summary_text}</p>

<h2>📊 통계</h2>
<p>완료 {len(issues['completed'])}건 · 신규 생성 {len(issues['created'])}건 · 진행중 {len(issues['in_progress'])}건</p>

<h2>✅ 완료된 티켓</h2>
{_table('완료', issues['completed'])}

<h2>🆕 신규 생성 티켓</h2>
{_table('신규', issues['created'])}

<h2>🔄 진행중 티켓</h2>
{_table('진행중', issues['in_progress'])}

<h2>🤖 CI 실행 현황</h2>
<table data-table-width="760">
  <tbody>
    <tr>
      <th data-highlight-colour="#deebff"><p><strong>저장소</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>총 실행</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>성공</strong></p></th>
      <th data-highlight-colour="#deebff"><p><strong>실패</strong></p></th>
    </tr>
    {ci_rows}
  </tbody>
</table>

<p><em>대상 기간: {period} | 자동 생성 by AutoTC</em></p>"""


# ── Confluence / Slack ────────────────────────────────────────────────

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
        "body": {"storage": {"value": content, "representation": "storage"}},
    }
    res = requests.post(url, headers=get_auth_headers(), json=payload, timeout=30)
    res.raise_for_status()
    data = res.json()
    return {
        "id": data["id"],
        "title": data["title"],
        "url": f"{CONFLUENCE_BASE}{data['_links']['webui']}",
    }


def send_slack_notification(page: dict, issues: dict, ci_summaries: list):
    if not SLACK_WEBHOOK_URL:
        return
    ci_text = "\n".join(f"{c['repo']}: {c['success']}/{c['total']} 성공" for c in ci_summaries) or "없음"
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "📊 QA 주간 리포트"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*완료*\n{len(issues['completed'])}건"},
                    {"type": "mrkdwn", "text": f"*신규*\n{len(issues['created'])}건"},
                    {"type": "mrkdwn", "text": f"*진행중*\n{len(issues['in_progress'])}건"},
                    {"type": "mrkdwn", "text": f"*CI*\n{ci_text}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{page['url']}|{page['title']}>"}},
        ]
    }
    requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="주간 QA 활동 요약 → Confluence 자동 생성")
    parser.add_argument("--days", type=int, default=7, help="집계 기간(일), 기본 7일")
    parser.add_argument("--project", default=JIRA_PROJECT_KEY, help="Jira 프로젝트 키 (기본: .env의 JIRA_PROJECT_KEY)")
    parser.add_argument("--repo", action="append", default=[], help="CI 이력 집계할 GitHub 저장소 (owner/repo), 여러 번 지정 가능")
    parser.add_argument("--space", default=None, help="Confluence 스페이스 키 (기본: QATEST)")
    parser.add_argument("--dry-run", action="store_true", help="Confluence에 올리지 않고 내용만 출력")
    args = parser.parse_args()

    today = datetime.now()
    period_start = (today - timedelta(days=args.days)).strftime("%Y-%m-%d")
    period = f"{period_start} ~ {today.strftime('%Y-%m-%d')}"

    print(f"\n=== QA 주간 리포트 생성 ({period}) ===")

    print(f"\nJira 티켓 조회 중... (프로젝트: {args.project})")
    issues = fetch_weekly_issues(args.project, args.days)
    print(f"  완료 {len(issues['completed'])}건 / 신규 {len(issues['created'])}건 / 진행중 {len(issues['in_progress'])}건")

    ci_summaries = []
    for repo in args.repo:
        print(f"\nCI 이력 조회 중... ({repo})")
        summary = fetch_ci_summary(repo, args.days)
        ci_summaries.append(summary)
        print(f"  총 {summary['total']}회 (성공 {summary['success']} / 실패 {summary['failure']})")

    print(f"\nAI 요약 작성 중...")
    groq_client = Groq(api_key=GROQ_API_KEY)
    try:
        summary_text = summarize_week(groq_client, issues, ci_summaries, args.days)
    except DailyTokenLimitError as e:
        print(f"  [일일 한도 초과] {e} — AI 요약 없이 통계만으로 리포트를 생성합니다.")
        summary_text = "(AI 요약 생성 실패 — Groq 일일 토큰 한도 초과. 아래 통계와 티켓 목록을 참고하세요.)"
    print(f"\n[요약]\n{summary_text}")

    if args.dry_run:
        print(f"\n[dry-run] Confluence 업로드를 건너뜁니다.")
        return

    content = build_confluence_content(issues, ci_summaries, summary_text, period)
    title = f"QA 주간 리포트 ({period})"

    print(f"\nConfluence 페이지 생성 중...")
    page = create_confluence_page(title, content, args.space)

    print(f"\n=== 완료 ===")
    print(f"  제목 : {page['title']}")
    print(f"  URL  : {page['url']}")

    send_slack_notification(page, issues, ci_summaries)
    if SLACK_WEBHOOK_URL:
        print(f"  Slack 알림 전송 완료")


if __name__ == "__main__":
    main()
