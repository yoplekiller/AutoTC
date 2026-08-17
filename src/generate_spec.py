"""
기능 기획서(spec) 자동 생성 스크립트
사용법: python src/generate_spec.py --ticket MKQA-37
"""
import sys
import io
import os
import argparse
from dotenv import load_dotenv
from groq import Groq
from jira import JIRA

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXTS_DIR = os.path.join(ROOT_DIR, "contexts")


def fetch_issue(ticket_key: str) -> dict:
    jira = JIRA(
        server=os.getenv("JIRA_URL"),
        basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
    )
    issue = jira.issue(ticket_key)
    return {
        "key": issue.key,
        "summary": issue.fields.summary,
        "issue_type": issue.fields.issuetype.name,
        "description": issue.fields.description or "설명 없음",
    }


def generate_spec(issue: dict) -> str:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    prompt = f"""다음 Jira 티켓을 기반으로 마켓컬리 서비스의 기능 기획서를 작성하세요.
QA 엔지니어가 TC를 30개 이상 뽑을 수 있도록 매우 구체적으로 작성하세요.
버튼 텍스트, 에러 메시지 문구, 정확한 수치를 모두 포함하세요.

[티켓 정보]
티켓 키: {issue['key']}
티켓 유형: {issue['issue_type']}
티켓 제목: {issue['summary']}
티켓 설명: {issue['description']}

아래 항목을 모두 작성하세요:

## 1. 기능 개요 및 목적

## 2. 화면 흐름 (step-by-step)
각 단계마다 화면명, 버튼 텍스트, 입력 필드명을 명시하세요.

## 3. 입력값 유효성 규칙
각 필드별 허용/불가 조건과 에러 메시지 문구를 정확히 작성하세요.
예) 이메일 필드
- 빈값 제출 시: "이메일을 입력해주세요"
- 형식 오류: "올바른 이메일 형식이 아닙니다"
- 중복 이메일: "이미 사용 중인 이메일입니다"

## 4. 핵심 정책 (정확한 수치 포함)
타이머, 유효시간, 최대 횟수, 글자 수 제한 등 수치를 명시하세요.

## 5. 예외/에러 케이스 15개 이상
각 케이스마다 조건, 에러 메시지 문구, 시스템 동작을 명시하세요.

## 6. 비즈니스 규칙
서비스 특화 정책(소셜 로그인 연동, 이용 제한, 연령 제한 등)을 명시하세요.

## 7. 플랫폼별 차이
iOS / Android / Web 에서 다른 동작이 있으면 명시하세요.

## 8. 회귀 체크 포인트
이 기능 수정 시 영향받는 연관 기능 목록을 작성하세요."""

    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 시니어 서비스 기획자입니다. "
                    "QA 엔지니어가 읽고 바로 TC를 작성할 수 있도록 구체적이고 상세한 기획서를 작성합니다. "
                    "추상적인 표현 대신 정확한 문구, 수치, 조건을 사용하세요. "
                    "한국어로 작성하세요."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=4000,
    )
    return response.choices[0].message.content.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True, help="Jira 티켓 키 (예: MKQA-37)")
    args = parser.parse_args()

    print(f"\n=== {args.ticket} 기획서 생성 중 ===\n")

    print("  Jira 티켓 조회 중...")
    issue = fetch_issue(args.ticket)
    print(f"  제목: {issue['summary']}")

    print("  AI 기획서 생성 중...")
    content = generate_spec(issue)

    spec_content = f"""---
ticket: {issue['key']}
feature: {issue['summary']}
service: 마켓컬리
type: 기능 기획서 (포트폴리오용 mock)
generated: 2026-05-29
---

{content}
"""

    filename = f"{args.ticket.lower().replace('-', '_')}_spec.md"
    filepath = os.path.join(CONTEXTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(spec_content)

    print(f"\n저장 완료: contexts/{filename} ({len(content)}자)")
    print("\n[기획서 미리보기]")
    print("-" * 60)
    print(content[:1500] + "\n..." if len(content) > 1500 else content)
    print("-" * 60)
    print(f"\nTC 생성 명령어:")
    print(f"  python src/generate_tc.py {args.ticket} --context {filename.replace('.md', '')}")


if __name__ == "__main__":
    main()
