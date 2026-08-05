"""
Playwright JSON 테스트 실패 결과 -> Groq AI로 버그 리포트 초안 생성 -> (선택) Jira 티켓 생성

실패한 테스트 하나당 버그 리포트 초안(현상/재현 단계/기대 결과/실제 결과/환경) 하나를 만듭니다.
에러 메시지만으로 추론 불가능한 부분은 AI가 지어내지 않고 "(QA 확인 필요)"로 표시합니다.

사용법:
  # 초안만 생성 (콘솔 출력 + reports/에 저장)
  python src/generate_bug_report.py playwright-report/results.json

  # 초안을 Jira Bug 티켓으로 실제 생성까지
  python src/generate_bug_report.py playwright-report/results.json --create-jira

  # Groq 호출 없이 실패 테스트 목록만 확인
  python src/generate_bug_report.py playwright-report/results.json --dry-run
"""

import sys
import io
import os
import json
import time
import argparse
from datetime import datetime

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils import sanitize
from src.release_report import parse_report  # 모듈 임포트 시 sys.stdout을 utf-8 TextIOWrapper로 교체함
from src.create_ticket import create_jira_ticket

# release_report 임포트가 이미 sys.stdout을 감쌌으므로, 남아있다면(=아직 utf-8 아니면) 여기서 한 번만 감쌈.
# 두 번 감싸면 이전 래퍼가 GC될 때 내부 버퍼까지 닫혀 "I/O operation on closed file" 오류가 남.
if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


class DailyTokenLimitError(Exception):
    pass


# ── AI 초안 생성 ─────────────────────────────────────────────────────

def draft_bug_report(groq_client: Groq, failed_test: dict) -> dict:
    """실패한 테스트 1건 -> Jira Bug 티켓용 구조화 초안."""
    prompt = f"""다음은 실패한 Playwright 테스트 1건입니다. 이 정보만 근거로 버그 리포트 초안을 작성하세요.
에러 메시지에서 명확히 추론되지 않는 재현 단계/환경 정보는 지어내지 말고 "(QA 확인 필요)"로 표시하세요.

테스트 제목: {failed_test['title']}
실행 환경(project): {failed_test['project']}
에러 메시지: {failed_test['error']}

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{
  "summary": "간결한 버그 제목 (테스트 제목+증상 기반)",
  "priority": "High/Medium/Low",
  "sections": [
    {{"heading": "현상", "content": "에러 메시지 기반으로 무엇이 실패했는지 설명"}},
    {{"heading": "재현 단계", "content": ["1. ...", "2. ...", "3. (QA 확인 필요)"]}},
    {{"heading": "기대 결과", "content": "테스트 제목에서 유추 가능한 정상 동작"}},
    {{"heading": "실제 결과", "content": "에러 메시지 요약"}},
    {{"heading": "환경", "content": "실행 환경(project) 값 그대로 기재"}}
  ]
}}"""

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 경력 10년차 시니어 QA 엔지니어입니다. "
                            "테스트 실패 정보만 근거로 버그 리포트 초안을 작성합니다. "
                            "근거 없는 원인/재현 절차를 지어내지 않고 불확실하면 명시적으로 표시합니다. "
                            "JSON만 출력하세요. 한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1000,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
                print(f"    [Rate Limit] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        raise RuntimeError("draft_bug_report Rate Limit 재시도 소진")

    raw = response.choices[0].message.content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    data["issue_type"] = "Bug"
    return sanitize(data)


# ── Slack 알림 ────────────────────────────────────────────────────────

def notify_slack(drafts: list, jira_results: list | None):
    if not SLACK_WEBHOOK_URL:
        return

    import requests

    lines = [f"*[버그 리포트 초안 생성 완료]* 실패 테스트 {len(drafts)}건"]
    for i, d in enumerate(drafts):
        priority = d.get("priority", "-")
        summary = d.get("summary", "(제목 없음)")
        if jira_results and jira_results[i]:
            lines.append(f"  • [{priority}] <{jira_results[i]['url']}|{jira_results[i]['key']}> {summary}")
        else:
            lines.append(f"  • [{priority}] {summary} (검수 후 파일링 필요)")
    lines.append("\n⚠️ AI가 에러 메시지만으로 작성한 초안입니다. QA 검수·보완 후 사용해주세요.")

    payload = {"text": "\n".join(lines)}
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200:
            print("  Slack 알림 발송 완료")
        else:
            print(f"  [경고] Slack 알림 실패: {resp.status_code}")
    except Exception as e:
        print(f"  [경고] Slack 알림 오류: {e}")


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Playwright 실패 테스트 -> 버그 리포트 초안 자동 생성")
    parser.add_argument("report_path", help="Playwright JSON 리포트 경로")
    parser.add_argument("--create-jira", action="store_true", help="초안을 Jira Bug 티켓으로 실제 생성")
    parser.add_argument("--project", default=None, help="Jira 프로젝트 키 (기본: .env의 JIRA_PROJECT_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="Groq 호출 없이 실패 테스트 목록만 확인")
    args = parser.parse_args()

    if not os.path.exists(args.report_path):
        print(f"[오류] 리포트 파일 없음: {args.report_path}")
        sys.exit(1)

    result = parse_report(args.report_path)
    print(f"\n=== 버그 리포트 초안 생성 ===")
    print(f"실패 {result['failed']}건 / 통과 {result['passed']}건 / 건너뜀 {result['skipped']}건")

    if not result["failed_tests"]:
        print("실패한 테스트가 없어 생성할 버그 리포트가 없습니다.")
        return

    if args.dry_run:
        print(f"\n[dry-run] 실패 테스트 목록:")
        for t in result["failed_tests"]:
            print(f"  - [{t['project']}] {t['title']}: {t['error']}")
        return

    groq_client = Groq(api_key=GROQ_API_KEY)
    drafts = []
    for t in result["failed_tests"]:
        print(f"\n초안 작성 중: [{t['project']}] {t['title']}")
        try:
            draft = draft_bug_report(groq_client, t)
        except DailyTokenLimitError as e:
            print(f"  [일일 한도 초과] {e}")
            print(f"  지금까지 생성된 {len(drafts)}개 초안으로 저장합니다.")
            break
        drafts.append({"test": t, "draft": draft})
        print(f"  [{draft.get('priority', '-')}] {draft.get('summary', '')}")

    if not drafts:
        print("\n생성된 초안이 없습니다.")
        return

    os.makedirs("reports", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/bug_reports_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(drafts, f, ensure_ascii=False, indent=2)
    print(f"\n저장 완료: {out_path}")

    jira_results = None
    if args.create_jira:
        jira_results = []
        print(f"\nJira 티켓 생성 중...")
        for item in drafts:
            try:
                created = create_jira_ticket(item["draft"], args.project)
                jira_results.append(created)
                print(f"  생성됨: {created['key']} — {created['summary']}")
            except Exception as e:
                jira_results.append(None)
                print(f"  [오류] Jira 티켓 생성 실패: {e}")

    notify_slack([d["draft"] for d in drafts], jira_results)

    print(f"\n=== 완료: 초안 {len(drafts)}건 생성 ===")


if __name__ == "__main__":
    main()
