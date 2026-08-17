"""
Playwright JSON 테스트 실패 결과 -> Groq AI로 버그 리포트 초안 생성 -> (선택) Jira 티켓 생성

실패한 테스트 하나당 버그 리포트 초안(현상/재현 단계/기대 결과/실제 결과/환경) 하나를 만듭니다.
에러 메시지만으로 추론 불가능한 부분은 AI가 지어내지 않고 "(QA 확인 필요)"로 표시합니다.
재시도 끝에 결국 통과한(flaky) 테스트는 애초에 버그 리포트 대상에서 제외됩니다.

사용법:
  # 초안만 생성 (콘솔 출력 + reports/에 저장, Jira에는 손대지 않음)
  python src/generate_bug_report.py playwright-report/results.json

  # 초안을 검토 후 승인하면 Jira Bug 티켓 생성 (같은 테스트의 기존 열린 티켓이 있으면 새로 만들지 않고 코멘트만 추가)
  python src/generate_bug_report.py playwright-report/results.json --create-jira

  # 승인 프롬프트 없이 자동 진행 (자동화/CI용)
  python src/generate_bug_report.py playwright-report/results.json --create-jira --yes

  # 재시도 2회 이상 실패한 것만 대상으로 (재현성 낮은 실패 제외)
  python src/generate_bug_report.py playwright-report/results.json --create-jira --min-retries 2

  # Groq 호출 없이 실패 테스트 목록만 확인
  python src/generate_bug_report.py playwright-report/results.json --dry-run
"""

import sys
import io
import os
import json
import time
import hashlib
import argparse
from datetime import datetime

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils import sanitize
from src.release_report import parse_report  # 모듈 임포트 시 sys.stdout을 utf-8 TextIOWrapper로 교체함
from src.create_ticket import create_jira_ticket, find_open_ticket_by_label, add_comment

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
                model="openai/gpt-oss-120b",
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


# ── 중복 방지 / 리뷰 출력 ────────────────────────────────────────────────

def _dedup_label(test: dict) -> str:
    """같은 테스트(브라우저+제목) 조합에 안정적인 Jira 라벨을 부여합니다.
    AI가 매번 다른 문구로 요약을 쓰더라도(비결정적) 이 라벨로 같은 실패를 추적해 중복 티켓을 막습니다."""
    key = f"{test.get('project', '')}::{test.get('title', '')}"
    return "autotc-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


def _print_draft(draft: dict):
    print(f"  요약: {draft.get('summary', '')}")
    print(f"  우선순위: {draft.get('priority', '-')}")
    for section in draft.get("sections", []):
        content = section.get("content", "")
        if isinstance(content, list):
            content = " / ".join(str(c) for c in content)
        print(f"  [{section.get('heading', '')}] {content}")


# ── Slack 알림 ────────────────────────────────────────────────────────

def notify_slack(drafts: list, jira_results: list | None):
    if not SLACK_WEBHOOK_URL:
        return

    import requests

    lines = [f"*[버그 리포트 초안 생성 완료]* 실패 테스트 {len(drafts)}건"]
    for i, d in enumerate(drafts):
        priority = d.get("priority", "-")
        summary = d.get("summary", "(제목 없음)")
        r = jira_results[i] if jira_results else None
        if r and r.get("duplicate"):
            lines.append(f"  • [{priority}] <{r['url']}|{r['key']}> {summary} (기존 티켓에 재발 코멘트 추가됨)")
        elif r:
            lines.append(f"  • [{priority}] <{r['url']}|{r['key']}> {summary}")
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
    parser.add_argument("--create-jira", action="store_true", help="초안을 Jira Bug 티켓으로 실제 생성(승인 필요)")
    parser.add_argument("--project", default=None, help="Jira 프로젝트 키 (기본: .env의 JIRA_PROJECT_KEY)")
    parser.add_argument("--dry-run", action="store_true", help="Groq 호출 없이 실패 테스트 목록만 확인")
    parser.add_argument(
        "--min-retries", type=int, default=0,
        help="이 값 이상 재시도(retry_count)된 실패만 대상으로 포함 (기본 0=제한 없음)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Jira 티켓 생성/코멘트 전 확인 프롬프트를 건너뛰고 자동 승인(자동화/CI용)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.report_path):
        print(f"[오류] 리포트 파일 없음: {args.report_path}")
        sys.exit(1)

    result = parse_report(args.report_path)
    print(f"\n=== 버그 리포트 초안 생성 ===")
    flaky_note = f" / flaky(재시도 후 통과) {result['flaky']}건" if result.get("flaky") else ""
    print(f"실패 {result['failed']}건 / 통과 {result['passed']}건 / 건너뜀 {result['skipped']}건{flaky_note}")

    if result.get("flaky_tests"):
        print(f"\n[참고] 재시도 끝에 통과해 버그 리포트 대상에서 제외된 테스트 {len(result['flaky_tests'])}건:")
        for t in result["flaky_tests"]:
            print(f"  - [{t['project']}] {t['title']} (재시도 {t['retry_count']}회 중 통과)")

    failed_tests = result["failed_tests"]
    if args.min_retries > 0:
        before = len(failed_tests)
        failed_tests = [t for t in failed_tests if t.get("retry_count", 1) >= args.min_retries]
        excluded = before - len(failed_tests)
        if excluded:
            print(f"\n[참고] --min-retries {args.min_retries} 미달로 제외된 실패 {excluded}건 (재현성 미확인)")

    if not failed_tests:
        print("생성할 버그 리포트가 없습니다.")
        return

    if args.dry_run:
        print(f"\n[dry-run] 실패 테스트 목록:")
        for t in failed_tests:
            print(f"  - [{t['project']}] {t['title']} (재시도 {t.get('retry_count', 1)}회): {t['error']}")
        return

    groq_client = Groq(api_key=GROQ_API_KEY)
    drafts = []
    for t in failed_tests:
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
        print(f"\nJira 티켓 처리 중 (검토 후 승인 필요, --yes로 자동 승인)...")
        for item in drafts:
            draft = item["draft"]
            test = item["test"]
            label = _dedup_label(test)

            print(f"\n--- {draft.get('summary', '(제목 없음)')} ---")
            _print_draft(draft)

            try:
                existing = find_open_ticket_by_label(label, args.project)
            except Exception as e:
                print(f"  [경고] 중복 확인 실패(계속 진행): {e}")
                existing = None

            if existing:
                print(f"  [중복 감지] 이미 열려있는 티켓 {existing['key']} 발견 — 새 티켓 대신 코멘트만 추가합니다.")
                action_desc = f"{existing['key']}에 재발 코멘트 추가"
            else:
                action_desc = "새 Jira Bug 티켓 생성"

            if not args.yes:
                answer = input(f"  {action_desc}할까요? (y/n): ").strip().lower()
                if answer != "y":
                    print("  건너뜀 (승인 안 됨)")
                    jira_results.append(None)
                    continue

            try:
                if existing:
                    comment = (
                        f"[AutoTC 재발 확인 — {datetime.now().strftime('%Y-%m-%d %H:%M')}]\n"
                        f"{draft.get('summary', '')}\n\n"
                        f"에러: {test.get('error', '')}"
                    )
                    add_comment(existing["key"], comment)
                    entry = {**existing, "summary": draft.get("summary", ""), "duplicate": True}
                    print(f"  코멘트 추가됨: {existing['key']}")
                else:
                    entry = create_jira_ticket(draft, args.project, labels=[label])
                    print(f"  생성됨: {entry['key']} — {entry['summary']}")
                jira_results.append(entry)
            except Exception as e:
                jira_results.append(None)
                print(f"  [오류] Jira 처리 실패: {e}")

    notify_slack([d["draft"] for d in drafts], jira_results)

    print(f"\n=== 완료: 초안 {len(drafts)}건 생성 ===")


if __name__ == "__main__":
    main()
