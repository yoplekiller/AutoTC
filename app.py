"""
AutoTC Slack 슬래시 커맨드 서버

지원 커맨드:
  /testplan MKQA-1 MKQA-2 MKQA-3       → Confluence 테스트 계획서 자동 생성
  /ticket 결제 시 쿠폰 적용이 안 됨    → Jira 티켓 자동 생성

실행:
  python app.py
"""

import os
import threading
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")

app = Flask(__name__)


def reply(response_url: str, text: str, is_error: bool = False):
    """Slack response_url로 처리 결과를 전송합니다."""
    requests.post(response_url, json={
        "response_type": "in_channel",
        "text": text if not is_error else f":x: {text}",
    }, timeout=10)


# ── /testplan ─────────────────────────────────────────────────────────

def run_testplan(ticket_keys: list, title: str, response_url: str):
    try:
        from src.generate_test_plan import fetch_issues, generate_plan_content, build_confluence_content, create_confluence_page
        from groq import Groq

        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        issues = fetch_issues(ticket_keys)
        if not issues:
            reply(response_url, "유효한 티켓을 찾을 수 없습니다.", is_error=True)
            return

        plan = generate_plan_content(groq_client, issues)
        doc_title = title or plan.get("title", f"테스트 계획서")
        content = build_confluence_content(plan, issues)
        page = create_confluence_page(doc_title, content)

        ticket_list = ", ".join(f"`{i['key']}`" for i in issues)
        reply(response_url,
            f":white_check_mark: *테스트 계획서 생성 완료*\n"
            f">*제목:* {page['title']}\n"
            f">*티켓:* {ticket_list}\n"
            f">*URL:* {page['url']}"
        )
    except Exception as e:
        reply(response_url, f"오류 발생: {e}", is_error=True)


@app.route("/slack/testplan", methods=["POST"])
def slack_testplan():
    text = request.form.get("text", "").strip()
    response_url = request.form.get("response_url")

    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": "사용법: `/testplan MKQA-1 MKQA-2 MKQA-3` 또는 `/testplan MKQA-1 --title Sprint1 테스트 계획서`"
        })

    # --title 파싱
    title = ""
    if "--title" in text:
        parts = text.split("--title", 1)
        ticket_part = parts[0].strip()
        title = parts[1].strip()
    else:
        ticket_part = text

    ticket_keys = ticket_part.upper().split()

    threading.Thread(
        target=run_testplan,
        args=(ticket_keys, title, response_url),
        daemon=True,
    ).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":hourglass: `{', '.join(ticket_keys)}` 기반으로 테스트 계획서 생성 중..."
    })


# ── /ticket ───────────────────────────────────────────────────────────

def run_ticket(description: str, issue_type: str, response_url: str):
    try:
        from src.create_ticket import generate_ticket_content, create_jira_ticket
        from groq import Groq

        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        ticket_data = generate_ticket_content(groq_client, description, issue_type or None)
        result = create_jira_ticket(ticket_data, os.getenv("JIRA_PROJECT_KEY", "MKQA"))

        type_emoji = {"Bug": ":bug:", "Story": ":book:", "Task": ":white_check_mark:"}.get(result["issue_type"], ":pushpin:")
        priority_emoji = {"High": ":red_circle:", "Medium": ":yellow_circle:", "Low": ":green_circle:"}.get(result["priority"], "")

        reply(response_url,
            f"{type_emoji} *Jira 티켓 생성 완료*\n"
            f">*티켓:* <{result['url']}|{result['key']}>\n"
            f">*제목:* {result['summary']}\n"
            f">*우선순위:* {priority_emoji} {result['priority']}"
        )
    except Exception as e:
        reply(response_url, f"오류 발생: {e}", is_error=True)


@app.route("/slack/ticket", methods=["POST"])
def slack_ticket():
    text = request.form.get("text", "").strip()
    response_url = request.form.get("response_url")

    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": "사용법: `/ticket 결제 시 쿠폰 적용이 안 됨` 또는 `/ticket 내용 --type Bug`"
        })

    # --type 파싱
    issue_type = ""
    if "--type" in text:
        parts = text.split("--type", 1)
        description = parts[0].strip()
        issue_type = parts[1].strip().capitalize()
    else:
        description = text

    threading.Thread(
        target=run_ticket,
        args=(description, issue_type, response_url),
        daemon=True,
    ).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":hourglass: *\"{description}\"* 티켓 생성 중..."
    })


# ── /minutes ─────────────────────────────────────────────────────────

def run_minutes(agenda: str, ticket_keys: list, response_url: str):
    try:
        from src.generate_minutes import fetch_issues, generate_minutes_content, build_confluence_content, create_confluence_page
        from groq import Groq

        groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        issues = fetch_issues(ticket_keys) if ticket_keys else []
        plan = generate_minutes_content(groq_client, agenda, issues)
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        title = f"{plan.get('title', agenda)} ({today})"
        content = build_confluence_content(plan, issues)
        page = create_confluence_page(title, content)

        reply(response_url,
            f":memo: *회의록 생성 완료*\n"
            f">*제목:* <{page['url']}|{page['title']}>\n"
            f">*안건:* {agenda}"
        )
    except Exception as e:
        reply(response_url, f"오류 발생: {e}", is_error=True)


@app.route("/slack/minutes", methods=["POST"])
def slack_minutes():
    text = request.form.get("text", "").strip()
    response_url = request.form.get("response_url")

    if not text:
        return jsonify({
            "response_type": "ephemeral",
            "text": "사용법: `/minutes 스프린트 1 킥오프` 또는 `/minutes QA 주간 회의 --tickets MKQA-1 MKQA-2`"
        })

    # --tickets 파싱
    ticket_keys = []
    if "--tickets" in text:
        parts = text.split("--tickets", 1)
        agenda = parts[0].strip()
        ticket_keys = parts[1].strip().upper().split()
    else:
        agenda = text

    threading.Thread(
        target=run_minutes,
        args=(agenda, ticket_keys, response_url),
        daemon=True,
    ).start()

    return jsonify({
        "response_type": "in_channel",
        "text": f":hourglass: *\"{agenda}\"* 회의록 작성 중..."
    })


# ── 서버 실행 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))

    # Railway 배포 환경 (PORT 환경변수 자동 주입됨)
    if os.getenv("RAILWAY_ENVIRONMENT"):
        app.run(host="0.0.0.0", port=port)

    # 로컬 실행 — ngrok으로 공개 URL 생성
    else:
        try:
            from pyngrok import ngrok
            public_url = ngrok.connect(port).public_url
            print(f"\n{'='*50}")
            print(f"  AutoTC Slack 서버 시작 (로컬)")
            print(f"  Public URL: {public_url}")
            print(f"")
            print(f"  Slack App에 아래 URL 등록:")
            print(f"  /testplan → {public_url}/slack/testplan")
            print(f"  /ticket   → {public_url}/slack/ticket")
            print(f"  /minutes  → {public_url}/slack/minutes")
            print(f"{'='*50}\n")
        except ImportError:
            print(f"  서버 시작: http://localhost:{port}")
            print(f"  (ngrok 미설치 — 외부 접근 불가)")
        app.run(port=port)
