import os
import re
import sys
from dotenv import load_dotenv

from slack_bolt import App

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from src.generate_tc import extract_issue_key, fetch_issue
except ImportError:
    from generate_tc import extract_issue_key, fetch_issue

from jira import JIRA
from groq import Groq

load_dotenv()

_slack_token = os.getenv("SLACK_BOT_TOKEN")
_slack_secret = os.getenv("SLACK_SIGNING_SECRET")

if _slack_token and _slack_secret:
    bolt_app = App(token=_slack_token, signing_secret=_slack_secret)
else:
    bolt_app = None

JIRA_URL = os.getenv("JIRA_URL", "")


# ── 보조 유틸리티 ───────────────────────────────────────────────────

def parse_slack_command_text(text: str) -> tuple:
    text = text.strip()
    context_match = re.search(r"--context\s+(\S+)", text)
    context_name = context_match.group(1) if context_match else ""
    clean_text = re.sub(r"--context\s+\S+", "", text).strip()
    return clean_text, context_name


def analyze_spec_ambiguity(groq_client: Groq, issue: dict, context: str = "") -> str:
    context_section = f"\n\n[서비스 컨텍스트]\n{context}" if context else ""

    prompt = f"""아래 Jira 티켓의 요약과 설명을 검토하고, 기획 결함을 조기에 방지하기 위한 '요구사항 모호성 분석 및 리스크 보고서'를 작성해주세요.

티켓 유형: {issue['issue_type']}
티켓 제목: {issue['summary']}
티켓 설명: {issue['description']}{context_section}

다음 항목들을 마크다운 양식으로 아주 구체적이고 현실적인 시나리오를 들어 기술해 주세요:

### 🎯 1. 기획의 모호성 검출 (Ambiguous Specs)
- 기획서 본문에서 구체적인 스펙이 모호하게 작성되어 개발 중 오해가 생길 수 있는 지점 지적
- "간헐적", "적절한 처리", "빠르게"와 같은 비수량적 표현의 구체화 요구

### ⚠️ 2. 누락된 예외 처리 정책 (Missing Edge Cases)
- 유저 세션 끊김, 네트워크 통신 실패, 잘못된 입력값, 중복 요청(더블 클릭), 권한 미비 등 실무 개발/QA 관점에서 반드시 정의해야 하지만 기획서에 누락된 방어 시나리오 제시 (최소 3개)

### 💥 3. 시스템 영향도 및 사이드 이펙트 리스크 (Side Effect Analysis)
- 이번 기능 수정으로 인해 예상치 못하게 충돌이 나거나 오동작할 수 있는 인접 기능 또는 DB 데이터 연동 리스크 도출

### 💡 4. QA 추천 제안사항 (Shift-Left Action Items)
- 품질을 조기에 확보하기 위해 기획자/개발자에게 지금 바로 제안해야 할 세부 완료 기준(Acceptance Criteria) 또는 설계 보완 아이디어

설명 없이 위 항목들만 명확한 마크다운 문서로 출력하세요."""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 글로벌 IT 기업의 리드 QA 엔지니어이자 제품 분석가입니다. "
                    "개발 전 기획 요구사항을 분석하여 기획 모호함, 예외 누락, 사이드 이펙트 리스크를 미리 발굴해 내는 "
                    "Shift-Left Testing 전문가입니다. "
                    "반드시 존칭어와 순수한 한국어만을 사용하여 논리적이고 전문성 있게 작성하세요."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content.strip()


# ── 슬랙 커맨드 핸들러: /review, /spec-review (기획 모호성 분석) ──────────

def ack_review_command(ack, command):
    text = command.get("text", "").strip()
    if not text:
        ack(
            "⚠️ 사용법이 올바르지 않습니다.\n"
            "사용법: `/review [Jira티켓번호 또는 URL] --context [서비스명(선택)]`\n"
            "예시: `/review MKQA-123 --context kream`"
        )
        return
    ack(f"🧠 *{text}* 기획 요구사항의 모호성과 예외 처리 정책 누락 여부를 스캔 중입니다... (Shift-Left Gate) 🚀")


def execute_lazy_review_analysis(command, respond, client):
    raw_text = command.get("text", "")
    user_id = command.get("user_id")

    input_target, context_name = parse_slack_command_text(raw_text)

    try:
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
        )
        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        issue_key = extract_issue_key(input_target)
        issue = fetch_issue(jira, issue_key)

        context_content = ""
        if context_name:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            context_path = os.path.join(root, "contexts", f"{context_name.lower()}.md")
            if os.path.exists(context_path):
                with open(context_path, encoding="utf-8") as f:
                    context_content = f.read().strip()

        review_result = analyze_spec_ambiguity(groq_client, issue, context_content)

        try:
            jira_comment = f"🤖 *[AutoTC - Shift-Left 기획 요구사항 검증 및 리스크 분석 보고서]*\n\n{review_result}"
            jira.add_comment(issue_key, jira_comment)
            jira_status_msg = "✅ Jira 티켓에 댓글로 보고서 자동 연동 완료"
        except Exception as jira_err:
            print(f"[경고] Jira 댓글 업로드 실패: {jira_err}")
            jira_status_msg = "⚠️ Jira 댓글 등록 실패 (권한 또는 계정 토큰 검토 필요)"

        clean_report = review_result
        if len(clean_report) > 2800:
            clean_report = clean_report[:2800] + "\n\n...(전체 상세 내용은 아래 Jira 링크 댓글을 참고하세요!)"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧠 Shift-Left 기획 분석 및 리스크 도출 완료", "emoji": True}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"👤 <@{user_id}>님이 요청하신 기획서 정밀 스캔이 완료되었습니다.\n"
                        f"*대상 티켓:* <{JIRA_URL}/browse/{issue_key}|{issue_key}> | {issue['summary']}\n"
                        f"*Jira 연동:* `{jira_status_msg}`"
                    )
                }
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": clean_report}
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🎫 Jira 티켓 보고서 보기", "emoji": True},
                        "value": "open_jira_ticket",
                        "url": f"{JIRA_URL}/browse/{issue_key}",
                        "action_id": "button-action-jira",
                        "style": "primary"
                    }
                ]
            }
        ]

        respond(blocks=blocks, replace_original=True)

    except Exception as err:
        print(f"Error executing slack command /review: {err}")
        respond(f"❌ *오류 발생:* 기획 분석 스캔을 처리하지 못했습니다.\n`사유: {str(err)}`")


# ── 커맨드 등록 ────────────────────────────────────────────────────

if bolt_app:
    bolt_app.command("/review")(
        ack=ack_review_command,
        lazy=[execute_lazy_review_analysis]
    )
    bolt_app.command("/spec-review")(
        ack=ack_review_command,
        lazy=[execute_lazy_review_analysis]
    )
