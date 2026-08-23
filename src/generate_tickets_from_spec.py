"""
기획서(스펙 문서) → 에픽 1개 + 하위 티켓(스토리/작업/버그) 여러 개 자동 생성

실무에서는 기획서가 먼저 나오고, 거기서 팀이 에픽/스토리/작업으로 쪼개서(breakdown)
Jira에 등록한다. create_ticket.py(짧은 설명 → 티켓 1개)와는 반대로, 이 스크립트는
긴 기획서 문서를 입력받아 한 번에 에픽+여러 하위 티켓으로 쪼갠다.
로컬 파일 / Confluence 페이지 URL / Confluence 페이지 제목, 셋 중 하나를 입력으로 쓸 수 있다.

사용법:
  python src/generate_tickets_from_spec.py --spec-file spec.md
  python src/generate_tickets_from_spec.py --confluence-url https://xxx.atlassian.net/wiki/spaces/.../pages/123
  python src/generate_tickets_from_spec.py --confluence-title "월급까지 - Phase 1 기획서"
  python src/generate_tickets_from_spec.py --spec-file spec.md --dry-run
  python src/generate_tickets_from_spec.py --spec-file spec.md --yes
"""

import argparse
import json
import os
import re
import sys
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.watch_sheet import fetch_confluence_page  # 임포트 시 자체적으로 sys.stdout을 utf-8로 감쌈

# watch_sheet 임포트가 이미 stdout을 utf-8로 감쌌으므로, 남아있다면(=아직 utf-8 아니면) 여기서 한 번만 감쌈.
# 두 번 감싸면 이전 래퍼가 GC될 때 내부 버퍼까지 닫혀 "I/O operation on closed file" 오류가 남.
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from dotenv import load_dotenv
from groq import Groq

from utils import sanitize
from src.create_ticket import build_description, jira_client

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "MKQA")

# MKQA 프로젝트(next-gen/팀 관리형)의 실제 이슈 타입 이름. Jira createmeta로 직접 확인함(2026-08-09) —
# create_ticket.py가 AI에게 뽑게 하는 영문 "Bug/Story/Task"와 실제 이름이 다르므로 추측하지 않고 그대로 사용.
EPIC_ISSUE_TYPE = "에픽"
CHILD_ISSUE_TYPES = ("버그", "스토리", "작업")


def read_spec_file(path: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(f"기획서 파일을 찾을 수 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f"기획서 파일이 비어 있습니다: {path}")
    return content


def read_spec_from_confluence(url: str) -> str:
    """watch_sheet.py의 fetch_confluence_page를 재사용한다 (같은 Atlassian 계정 인증 공유)."""
    content = fetch_confluence_page(url)
    if not content:
        raise ValueError(
            f"Confluence 페이지를 가져오지 못했습니다: {url}\n"
            "URL 형식(.../pages/숫자ID/...)과 JIRA_EMAIL/JIRA_API_TOKEN 권한을 확인해주세요."
        )
    return content


def find_confluence_url_by_title(title: str, space: str = None) -> str:
    """Confluence 스페이스에서 제목이 정확히 일치하는 페이지를 찾아 URL을 반환한다. 못 찾으면 빈 문자열."""
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    base = JIRA_URL.rstrip("/")
    space_key = space or os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")

    escaped_title = title.replace('"', '\\"')
    cql = f'space="{space_key}" AND title="{escaped_title}"'

    resp = requests.get(
        f"{base}/wiki/rest/api/content/search",
        params={"cql": cql},
        auth=(email, token),
        timeout=15,
    )
    if resp.status_code != 200:
        return ""

    results = resp.json().get("results", [])
    if not results:
        return ""

    page_id = results[0]["id"]
    return f"{base}/wiki/spaces/{space_key}/pages/{page_id}"


def read_spec_from_confluence_title(title: str, space: str = None) -> str:
    url = find_confluence_url_by_title(title, space)
    if not url:
        space_key = space or os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")
        raise ValueError(
            f'"{space_key}" 스페이스에서 제목이 "{title}"인 페이지를 찾지 못했습니다. '
            "제목이 정확히 일치해야 합니다(부분 일치 안 됨)."
        )
    return read_spec_from_confluence(url)


def generate_breakdown(groq_client: Groq, spec: str) -> dict:
    """기획서를 에픽 1개 + 하위 티켓 여러 개로 분해한다. 기획서에 없는 내용은 지어내지 않는다."""
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 Jira 이슈 분해(breakdown) 전문가입니다. "
                    "기획서를 분석해 하나의 에픽과 그 아래에 들어갈 여러 개의 하위 티켓으로 나눕니다. "
                    f"하위 티켓의 issue_type은 반드시 {CHILD_ISSUE_TYPES} 중 하나여야 합니다(영어 사용 금지). "
                    "기획서에 없는 내용은 절대 지어내지 말고, 기획서에 실제로 있는 범위 안에서만 분해하세요. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. 한국어, 숫자, 영문 외 다른 언어는 절대 사용하지 마세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 기획서를 에픽 1개와 하위 티켓 여러 개로 분해해주세요.

기획서:
{spec}

아래 JSON 형식으로만 응답하세요. 마크다운 기호(```) 없이 순수한 JSON만 출력하세요.

{{
  "epic": {{
    "summary": "이 기획서 전체를 아우르는 에픽 제목",
    "sections": [
      {{"heading": "목표", "content": "이 기획서의 전체 목표"}},
      {{"heading": "범위", "content": ["범위1", "범위2"]}}
    ]
  }},
  "tickets": [
    {{
      "summary": "하위 티켓 제목",
      "issue_type": "스토리 또는 작업 또는 버그 중 하나",
      "priority": "High|Medium|Low",
      "sections": [
        {{"heading": "설명", "content": "이 티켓에서 해야 할 일"}}
      ],
      "acceptance_criteria": ["완료 기준 1", "완료 기준 2"]
    }}
  ]
}}""",
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return sanitize(json.loads(raw))


def create_epic(epic_data: dict, project_key: str) -> dict:
    jira = jira_client()
    fields = {
        "project": {"key": project_key},
        "summary": epic_data.get("summary", "제목 없음"),
        "issuetype": {"name": EPIC_ISSUE_TYPE},
        "description": build_description(epic_data),
    }
    new_issue = jira.create_issue(fields=fields)
    return {"key": new_issue.key, "url": f"{JIRA_URL}/browse/{new_issue.key}"}


def create_child_ticket(ticket_data: dict, epic_key: str, project_key: str) -> dict:
    jira = jira_client()
    issue_type = ticket_data.get("issue_type", "작업")
    if issue_type not in CHILD_ISSUE_TYPES:
        # AI가 지침을 어기고 다른 값(예: 영문)을 냈을 때의 방어 — 생성 자체가 실패하지 않도록 기본값으로 대체
        issue_type = "작업"

    fields = {
        "project": {"key": project_key},
        "summary": ticket_data.get("summary", "제목 없음"),
        "issuetype": {"name": issue_type},
        "priority": {"name": ticket_data.get("priority", "Medium")},
        "description": build_description(ticket_data),
        "parent": {"key": epic_key},
    }
    new_issue = jira.create_issue(fields=fields)
    return {
        "key": new_issue.key,
        "url": f"{JIRA_URL}/browse/{new_issue.key}",
        "issue_type": issue_type,
        "summary": ticket_data.get("summary", "제목 없음"),
    }


def main():
    parser = argparse.ArgumentParser(description="기획서 → 에픽 + 하위 티켓 자동 생성")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--spec-file", help="기획서 파일 경로(.txt/.md)")
    source_group.add_argument("--confluence-url", help="기획서가 있는 Confluence 페이지 URL")
    source_group.add_argument("--confluence-title", help="기획서가 있는 Confluence 페이지 제목 (정확히 일치해야 함)")
    parser.add_argument("--space", default=None, help="Confluence 스페이스 키 (--confluence-title과 함께 사용, 기본: QATEST)")
    parser.add_argument("--project", default=None, help="Jira 프로젝트 키 (기본: 환경변수 JIRA_PROJECT_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="Jira에 생성하지 않고 결과만 출력")
    parser.add_argument("-y", "--yes", action="store_true", help="승인 프롬프트 건너뛰기")
    args = parser.parse_args()

    project_key = args.project or JIRA_PROJECT_KEY
    groq = Groq(api_key=GROQ_API_KEY)

    print("\n=== 기획서 기반 티켓 생성 ===")
    if args.spec_file:
        print(f"기획서 파일: {args.spec_file}")
        spec = read_spec_file(args.spec_file)
    elif args.confluence_url:
        print(f"Confluence 페이지: {args.confluence_url}")
        spec = read_spec_from_confluence(args.confluence_url)
    else:
        print(f"Confluence 페이지 제목: {args.confluence_title}")
        spec = read_spec_from_confluence_title(args.confluence_title, args.space)

    print("\nAI 분해 중...")
    breakdown = generate_breakdown(groq, spec)

    epic = breakdown.get("epic", {})
    tickets = breakdown.get("tickets", [])

    print("\n[분해 결과 미리보기]")
    print(f"  에픽: {epic.get('summary', '(제목 없음)')}")
    print(f"  하위 티켓: {len(tickets)}개")
    for i, t in enumerate(tickets, 1):
        print(f"    {i}. [{t.get('issue_type', '?')}] {t.get('summary', '(제목 없음)')}")

    if args.dry_run:
        print("\n[dry-run] Jira 생성을 건너뜁니다.")
        return

    if not args.yes:
        answer = input(
            f"\n에픽 1개 + 티켓 {len(tickets)}개를 Jira({project_key})에 실제로 생성할까요? (y/n): "
        ).strip().lower()
        if answer != "y":
            print("취소했습니다.")
            return

    print("\n에픽 생성 중...")
    epic_result = create_epic(epic, project_key)
    print(f"  {epic_result['key']} — {epic_result['url']}")

    print("\n하위 티켓 생성 중...")
    created = []
    for t in tickets:
        result = create_child_ticket(t, epic_result["key"], project_key)
        created.append(result)
        print(f"  {result['key']} [{result['issue_type']}] — {result['summary']}")

    print("\n=== 완료 ===")
    print(f"  에픽: {epic_result['key']}")
    print(f"  하위 티켓: {len(created)}개")


if __name__ == "__main__":
    main()
