import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv

# Slack Bolt 프레임워크 임포트
from slack_bolt import App
from slack_sdk import WebClient

# 기존에 구현한 TC 생성 엔진 함수 임포트
# (경로가 src 패키지 내부에 있으므로 상대 경로 및 모듈 임포트 가용성 확보)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from src.generate_tc import (
        extract_issue_key,
        fetch_issue,
        augment_ticket_spec,
        generate_test_cases,
        filter_tc_list,
        save_excel,
        save_to_sheets,
    )
except ImportError:
    # 모듈 구조에 따른 예외 처리 백업
    from generate_tc import (
        extract_issue_key,
        fetch_issue,
        augment_ticket_spec,
        generate_test_cases,
        filter_tc_list,
        save_excel,
        save_to_sheets,
    )

from jira import JIRA   
from groq import Groq

load_dotenv()

# Slack 및 AI 클라이언트 초기화 (토큰 미설정 시 graceful 처리)
try:
    bolt_app = App(
        token=os.getenv("SLACK_BOT_TOKEN"),
        signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
    )
except Exception as e:
    print(f"[경고] Slack Bolt 초기화 실패 (SLACK_BOT_TOKEN 미설정?): {e}")
    bolt_app = None

app = bolt_app  # __main__ 블록 호환용

JIRA_URL = os.getenv("JIRA_URL", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")  # 기본으로 업데이트할 스프레드시트 ID


# ── 보조 유틸리티 ───────────────────────────────────────────────────

def parse_slack_command_text(text: str) -> tuple:
    """
    슬랙 명령어 텍스트에서 이슈 키와 컨텍스트 옵션을 파싱합니다.
    예: "PROJ-123 --context kream" -> ("PROJ-123", "kream")
    """
    text = text.strip()
    context_match = re.search(r"--context\s+(\S+)", text)
    context_name = context_match.group(1) if context_match else ""
    
    # 옵션 부분을 제외한 순수 이슈 키/URL 추출
    clean_text = re.sub(r"--context\s+\S+", "", text).strip()
    return clean_text, context_name


def analyze_spec_ambiguity(groq_client: Groq, issue: dict, context: str = "") -> str:
    """
    Groq Llama 3.3 모델을 사용하여 Jira 티켓의 기획 요구사항을 분석하고, 
    Shift-Left 관점의 기획 모호성 검출 보고서를 작성합니다.
    """
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
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )
    return response.choices[0].message.content.strip()


# ── 슬랙 커맨드 핸들러: /tc (테스트케이스 생성) ───────────────────────────

# 1. 즉각적인 3초 이내 응답 (Slack 제한 조건 회피)
def ack_tc_command(ack, command):
    text = command.get("text", "").strip()
    if not text:
        ack(
            "⚠️ 사용법이 올바르지 않습니다.\n"
            "사용법: `/tc [Jira티켓번호 또는 URL] --context [서비스명(선택)]`\n"
            "예시: `/tc MKQA-123 --context kream`"
        )
        return
    
    ack(f"🔍 *{text}* 티켓을 분석하고 표준 10대 원칙에 기반한 테스트 케이스를 생성 중입니다. 잠시만 기다려주세요... 🚀")


# 2. 비동기 지연 실행 (Heavy Task 처리 후 최종 응답)
def execute_lazy_tc_generation(command, respond, client):
    raw_text = command.get("text", "")
    channel_id = command.get("channel_id")
    user_id = command.get("user_id")
    
    input_target, context_name = parse_slack_command_text(raw_text)
    
    try:
        # Jira 및 Groq 클라이언트 바인딩
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
        )
        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        
        # 1. 이슈 추출 및 조회
        issue_key = extract_issue_key(input_target)
        issue = fetch_issue(jira, issue_key)
        
        # 2. AI 요구사항 및 예외 추론
        # contexts/{context_name}.md가 있다면 반영
        context_content = ""
        if context_name:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            context_path = os.path.join(root, "contexts", f"{context_name.lower()}.md")
            if os.path.exists(context_path):
                with open(context_path, encoding="utf-8") as f:
                    context_content = f.read().strip()

        augmented_spec = augment_ticket_spec(groq_client, issue, context_content)
        
        # 3. 10대 표준 TC 생성 및 필터링
        tc_list = generate_test_cases(groq_client, issue, augmented_spec, context_content)
        tc_list = filter_tc_list(tc_list)
        
        if not tc_list:
            respond("⚠️ 분석 결과 유효한 테스트 케이스를 생성하지 못했습니다. 기획서 내용을 확인해 주세요.")
            return

        # 4. 파일 저장 및 스프레드시트 기록
        results = [{
            "key": issue["key"],
            "summary": issue["summary"],
            "status": issue["status"],
            "test_cases": tc_list,
        }]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs("reports", exist_ok=True)
        
        # 로컬 엑셀 저장
        xlsx_path = f"reports/tc_{issue_key}_{timestamp}.xlsx"
        save_excel(results, xlsx_path)
        
        # 구글 스프레드시트 동기화 (GOOGLE_SHEET_ID 환경변수가 설정되어 있을 때 작동)
        sheet_url = ""
        if GOOGLE_SHEET_ID:
            try:
                sheet_url = save_to_sheets(results, GOOGLE_SHEET_ID)
            except Exception as sheet_err:
                print(f"[경고] 구글 시트 동기화 실패: {sheet_err}")

        # 5. 완성도 높은 Slack UI (Block Kit)으로 결과 브로드캐스트
        total_tc_count = len(tc_list)
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🤖 AutoTC 테스트 케이스 설계 완료",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"👤 <@{user_id}>님이 요청하신 티켓의 QA 설계가 완료되었습니다.\n*대상 티켓:* <{JIRA_URL}/browse/{issue_key}|{issue_key}> | {issue['summary']}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*생성된 TC 개수:*\n{total_tc_count} 개"},
                    {"type": "mrkdwn", "text": f"*컨텍스트 프로필:*\n{context_name.upper() if context_name else '기본값'}"},
                    {"type": "mrkdwn", "text": f"*우선순위 분포:*\nHigh ({len([t for t in tc_list if t.get('우선순위')=='High'])}개) / Medium ({len([t for t in tc_list if t.get('우선순위')=='Medium'])}개)"}
                ]
            }
        ]

        # 구글 시트 업로드 성공 시 이동 버튼 블록 추가
        if sheet_url:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "📊 구글 스프레드시트 열기",
                            "emoji": True
                        },
                        "value": "open_sheet",
                        "url": sheet_url,
                        "action_id": "button-action",
                        "style": "primary"
                    }
                ]
            })
        else:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"ℹ️ 구글 시트 연동이 누락되어 로컬 파일로 저장되었습니다.\n`경로: {xlsx_path}`"
                }
            })

        # 최종 응답 전송
        respond(blocks=blocks, replace_original=True)
        
    except Exception as err:
        print(f"Error executing slack command /tc: {err}")
        respond(f"❌ *오류 발생:* 테스트 케이스 생성 중 에러가 발생했습니다.\n`사유: {str(err)}`")


# ── 슬랙 커맨드 핸들러: /review (기획 모호성 분석) ───────────────────────

# 1. 즉각적인 3초 이내 응답 (Slack 제한 조건 회피)
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


# 2. 비동기 지연 실행 (Heavy Task 처리 후 최종 응답)
def execute_lazy_review_analysis(command, respond, client):
    raw_text = command.get("text", "")
    channel_id = command.get("channel_id")
    user_id = command.get("user_id")
    
    input_target, context_name = parse_slack_command_text(raw_text)
    
    try:
        # Jira 및 Groq 클라이언트 바인딩
        jira = JIRA(
            server=JIRA_URL,
            basic_auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
        )
        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        
        # 1. 이슈 추출 및 조회
        issue_key = extract_issue_key(input_target)
        issue = fetch_issue(jira, issue_key)
        
        # 2. 서비스 컨텍스트 로드
        context_content = ""
        if context_name:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            context_path = os.path.join(root, "contexts", f"{context_name.lower()}.md")
            if os.path.exists(context_path):
                with open(context_path, encoding="utf-8") as f:
                    context_content = f.read().strip()

        # 3. AI 기반 기획 검증 보고서 생성
        review_result = analyze_spec_ambiguity(groq_client, issue, context_content)
        
        # 4. Jira 티켓에 댓글로 기획 검토 보고서 자동 등록 (기획자/개발자 협업 공유용)
        try:
            jira_comment = f"🤖 *[AutoTC - Shift-Left 기획 요구사항 검증 및 리스크 분석 보고서]*\n\n{review_result}"
            jira.add_comment(issue_key, jira_comment)
            jira_status_msg = "✅ Jira 티켓에 댓글로 보고서 자동 연동 완료"
        except Exception as jira_err:
            print(f"[경고] Jira 댓글 업로드 실패: {jira_err}")
            jira_status_msg = "⚠️ Jira 댓글 등록 실패 (권한 또는 계정 토큰 검토 필요)"

        # 5. 슬랙 응답 UI (Block Kit) 빌드
        # 슬랙 마크다운 섹션 필드의 글자 수 한계(3000자) 대응 슬라이싱 예외 처리 추가
        clean_report = review_result
        if len(clean_report) > 2800:
            clean_report = clean_report[:2800] + "\n\n...(전체 분석 보고서가 너무 길어 슬랙 요약판으로 조절되었습니다. 전체 상세 내용은 아래 Jira 링크 댓글을 참고하세요!)"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🧠 Shift-Left 기획 분석 및 리스크 도출 완료",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"👤 <@{user_id}>님이 요청하신 기획서 정밀 스캔이 완료되었습니다.\n*대상 티켓:* <{JIRA_URL}/browse/{issue_key}|{issue_key}> | {issue['summary']}\n*Jira 연동:* `{jira_status_msg}`"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": clean_report
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🎫 Jira 티켓 보고서 보기",
                            "emoji": True
                        },
                        "value": "open_jira_ticket",
                        "url": f"{JIRA_URL}/browse/{issue_key}",
                        "action_id": "button-action-jira",
                        "style": "primary"
                    }
                ]
            }
        ]

        # 최종 응답 전송
        respond(blocks=blocks, replace_original=True)
        
    except Exception as err:
        print(f"Error executing slack command /review: {err}")
        respond(f"❌ *오류 발생:* 기획 분석 스캔을 처리하지 못했습니다.\n`사유: {str(err)}`")


# 슬랙 앱 커맨드 등록 (ack + lazy 분리 → Slack 3초 타임아웃 회피)
if bolt_app:
    bolt_app.command("/tc")(ack=ack_tc_command, lazy=[execute_lazy_tc_generation])
    bolt_app.command("/review")(ack=ack_review_command, lazy=[execute_lazy_review_analysis])
    bolt_app.command("/spec-review")(ack=ack_review_command, lazy=[execute_lazy_review_analysis])


# ── 실행 진입점 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # 포트 3000번으로 슬랙 커맨드 리스너 소켓 가동
    port = int(os.getenv("PORT", 3000))
    print(f"⚡️ AutoTC 슬랙 에직트 서버 가동 중 (Port: {port})")
    app.start(port=port)