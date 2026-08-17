import sys
import os
import re
import json
import argparse
import requests
from jira import JIRA
from groq import Groq
from dotenv import load_dotenv

# 상위 폴더의 src.utils 모듈을 가져오기 위한 경로 설정 및 예외 처리
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.utils import sanitize
except ImportError:
    # 혹시 utils 구조가 없거나 임포트에 실패할 경우를 위한 백업 보정 함수
    def sanitize(data):
        return data

load_dotenv()

# 환경 변수 로드
JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "MKQA")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")


def generate_ticket_content(groq_client: Groq, description: str, issue_type: str = None) -> dict:
    """Groq Llama 3.3 모델을 사용하여 1차적으로 요구사항 분석 및 구조화된 JSON 데이터 추출"""
    type_instruction = (
        f"이슈 유형은 반드시 '{issue_type}'으로 설정하세요."
        if issue_type
        else "설명을 분석해 가장 적합한 이슈 유형(Bug/Story/Task)을 자동 선택하세요."
    )

    prompt = f"""다음 설명을 바탕으로 Jira 티켓을 작성해주세요.

설명: {description}

{type_instruction}

아래 JSON 형식으로만 응답하세요. 마크다운 기호(```) 없이 순수한 JSON만 출력하세요.

Bug일 경우:
{{
  "summary": "간결하고 명확한 제목",
  "issue_type": "Bug",
  "priority": "High|Medium|Low",
  "sections": [
    {{"heading": "현상", "content": "어떤 문제가 발생하는지 설명"}},
    {{"heading": "재현 단계", "content": ["1. 단계1", "2. 단계2", "3. 단계3"]}},
    {{"heading": "기대 결과", "content": "정상 동작 시 예상 결과"}},
    {{"heading": "실제 결과", "content": "현재 실제로 발생하는 결과"}}
  ],
  "acceptance_criteria": ["검수 기준 1", "검수 기준 2"]
}}

Story일 경우:
{{
  "summary": "간결하고 명확한 제목",
  "issue_type": "Story",
  "priority": "High|Medium|Low",
  "sections": [
    {{"heading": "사용자 스토리", "content": "~로서 ~하고 싶다. ~하기 위해."}},
    {{"heading": "배경 및 목적", "content": "왜 이 기능이 필요한지 설명"}}
  ],
  "acceptance_criteria": ["AC 1", "AC 2", "AC 3"]
}}

Task일 경우:
{{
  "summary": "간결하고 명확한 제목",
  "issue_type": "Task",
  "priority": "High|Medium|Low",
  "sections": [
    {{"heading": "작업 내용", "content": "무엇을 해야 하는지 설명"}},
    {{"heading": "작업 범위", "content": ["범위1", "범위2"]}}
  ],
  "acceptance_criteria": ["완료 기준 1", "완료 기준 2"]
}}"""

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 Jira 티켓 작성 전문가입니다. "
                    "짧은 설명을 받아 실무 수준의 Jira 티켓을 작성합니다. "
                    "반드시 JSON 형식으로만 응답하세요. "
                    "반드시 순수한 한국어로만 작성하세요. 한국어, 숫자, 영문 외 다른 언어는 절대 사용하지 마세요."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    raw = response.choices[0].message.content.strip()
    # 마크다운 코드 블록 제거용 정규식 가다듬기
    raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {
            "summary": description[:100],
            "issue_type": issue_type or "Task",
            "priority": "Medium",
            "sections": [{"heading": "설명", "content": description}],
            "acceptance_criteria": [],
        }

    return sanitize(data)


def build_description(data: dict) -> str:
    """sections와 acceptance_criteria를 Jira 텍스트로 변환합니다."""
    lines = []

    for section in data.get("sections", []):
        heading = section.get("heading", "")
        content = section.get("content", "")
        if heading:
            lines.append(f"*{heading}*")
        if isinstance(content, list):
            lines.extend(str(item) for item in content)
        else:
            lines.append(str(content))
        lines.append("")

    criteria = data.get("acceptance_criteria", [])
    if criteria:
        lines.append("*완료 기준 (AC)*")
        for ac in criteria:
            lines.append(f"* {ac}")

    return "\n".join(lines).strip()


def jira_client() -> JIRA:
    return JIRA(server=JIRA_URL, basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN))


def create_jira_ticket(ticket_data: dict, project_key: str = None, labels: list = None) -> dict:
    """Jira REST API로 실제 티켓을 생성하고 결과를 반환합니다."""
    project_key = project_key or JIRA_PROJECT_KEY

    jira = jira_client()

    issue_type = ticket_data.get("issue_type", "Task")
    priority   = ticket_data.get("priority", "Medium")
    summary    = ticket_data.get("summary", "제목 없음")
    description = build_description(ticket_data)

    fields = {
        "project":     {"key": project_key},
        "summary":     summary,
        "issuetype":   {"name": issue_type},
        "priority":    {"name": priority},
        "description": description,
    }
    if labels:
        fields["labels"] = labels

    new_issue = jira.create_issue(fields=fields)

    return {
        "key":        new_issue.key,
        "url":        f"{JIRA_URL}/browse/{new_issue.key}",
        "summary":    summary,
        "issue_type": issue_type,
        "priority":   priority,
    }


def find_open_ticket_by_label(label: str, project_key: str = None) -> dict:
    """같은 label(재현 식별자)을 가진, Done이 아닌 티켓이 이미 있으면 반환합니다(중복 생성 방지용). 없으면 None."""
    project_key = project_key or JIRA_PROJECT_KEY
    jira = jira_client()

    jql = f'project = "{project_key}" AND labels = "{label}" AND statusCategory != Done ORDER BY created DESC'
    issues = jira.search_issues(jql, maxResults=1)
    if not issues:
        return None

    issue = issues[0]
    return {"key": issue.key, "url": f"{JIRA_URL}/browse/{issue.key}"}


def add_comment(issue_key: str, comment: str):
    """기존 티켓에 코멘트를 추가합니다(중복 대신 재발 기록용)."""
    jira = jira_client()
    jira.add_comment(issue_key, comment)