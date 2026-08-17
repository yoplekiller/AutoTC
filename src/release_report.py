"""
Playwright JSON 테스트 결과 -> Groq AI 분석 -> Slack 릴리즈 리포트 전송

사용법:
  python src/release_report.py playwright-report/results.json

Playwright 측 설정 (playwright.config.ts):
  reporter: [['json', { outputFile: 'playwright-report/results.json' }], ...]
"""

import sys
import io
import os
import json
import re
import time
import argparse
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from groq import Groq
from dotenv import load_dotenv


class DailyTokenLimitError(Exception):
    pass


load_dotenv()

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
PLAYWRIGHT_REPORT_URL = os.getenv("PLAYWRIGHT_REPORT_URL", "")

FAILED_STATUSES = {"failed", "timedOut", "interrupted"}
RECOMMENDATION_ICON = {"Go": "✅", "Caution": "⚠️", "No-Go": "\U0001f6d1"}


# ── Playwright JSON 파싱 ─────────────────────────────────────────────

def _walk_specs(suite: dict):
    for spec in suite.get("specs", []):
        yield spec
    for child in suite.get("suites", []):
        yield from _walk_specs(child)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _first_line(message: str) -> str:
    """실패 메시지에서 원인 파악에 필요한 핵심 줄만 추출합니다.
    Playwright 에러 첫 줄은 매처 이름뿐인 경우가 많아(예: "expect(locator).toBeVisible() failed"),
    실제 원인이 담긴 다음 줄들(Locator/strict mode violation 등)까지 몇 개 더 모아서 반환합니다."""
    clean = _ANSI_ESCAPE_RE.sub("", message)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    return " / ".join(lines[:4])[:400]


def parse_report(report_path: str) -> dict:
    """Playwright --reporter=json 출력 파일을 파싱합니다.

    Playwright는 테스트별로 자체 판정(`test.status`: expected/unexpected/flaky/skipped)을
    함께 기록합니다. retries가 설정된 환경(예: CI)에서 재시도 끝에 결국 통과한 테스트는
    "flaky"로 표시되는데, 이를 실패로 잘못 집계하지 않도록 이 필드를 우선 사용합니다.
    구버전 리포트처럼 이 필드가 없으면 마지막 시도 결과로 폴백합니다.
    """
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    stats = report.get("stats", {})
    failed_tests = []
    flaky_tests = []
    passed = failed = skipped = flaky = 0

    for suite in report.get("suites", []):
        for spec in _walk_specs(suite):
            for test in spec.get("tests", []):
                results = test.get("results", [])
                last = results[-1] if results else {}
                retry_count = len(results)
                pw_status = test.get("status") or last.get("status", "skipped")

                if pw_status in ("expected", "passed"):
                    passed += 1
                elif pw_status == "skipped":
                    skipped += 1
                elif pw_status == "flaky":
                    # 재시도 끝에 결국 통과 - 버그 리포트 대상은 아니지만 불안정성 신호로 별도 기록
                    passed += 1
                    flaky += 1
                    failed_attempt = next(
                        (r for r in results if r.get("status") in FAILED_STATUSES), last
                    )
                    error = (failed_attempt.get("error") or {}).get("message", "")
                    flaky_tests.append({
                        "title": spec.get("title", "(제목 없음)"),
                        "project": test.get("projectName", "unknown"),
                        "retry_count": retry_count,
                        "error": _first_line(error),
                    })
                else:
                    # unexpected: 재시도까지 모두 실패
                    failed += 1
                    error = (last.get("error") or {}).get("message", "")
                    failed_tests.append({
                        "title": spec.get("title", "(제목 없음)"),
                        "project": test.get("projectName", "unknown"),
                        "status": last.get("status", pw_status),
                        "retry_count": retry_count,
                        "error": _first_line(error),
                    })

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "flaky": flaky,
        "flaky_tests": flaky_tests,
        "total": passed + failed + skipped,
        "duration_sec": round(stats.get("duration", 0) / 1000),
        "failed_tests": failed_tests,
    }


# ── Groq 분석 ────────────────────────────────────────────────────────

def analyze_with_groq(groq_client: Groq, result: dict) -> dict:
    """실패 패턴을 분석해 릴리즈 권고(Go/Caution/No-Go)와 한 줄 요약을 받습니다."""
    if not result["failed_tests"]:
        return {"recommendation": "Go", "summary": "실패한 테스트 없음 - 릴리즈 가능"}

    failures_text = "\n".join(
        f"- [{t['project']}] {t['title']}: {t['error']}"
        for t in result["failed_tests"][:20]
    )

    response = None
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model="openai/gpt-oss-120b",
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "당신은 시니어 QA 리드입니다. Playwright 테스트 실패 목록을 보고 "
                            "릴리즈 가능 여부를 판단합니다. 반드시 순수한 한국어로만 작성하세요."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"""테스트 결과: 통과 {result['passed']} / 실패 {result['failed']} / 건너뜀 {result['skipped']}

실패 목록:
{failures_text}

아래 JSON 형식으로만 답하세요. 다른 텍스트는 출력하지 마세요.
{{"recommendation": "Go 또는 Caution 또는 No-Go", "summary": "한 줄 요약(실패 원인 패턴 위주, 60자 이내)"}}""",
                    },
                ],
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                wait = 65 * (attempt + 1)
                print(f"  [Rate Limit] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        raise RuntimeError("analyze_with_groq Rate Limit 재시도 소진")

    try:
        parsed = json.loads(response.choices[0].message.content)
        recommendation = parsed.get("recommendation", "Caution")
        if recommendation not in RECOMMENDATION_ICON:
            recommendation = "Caution"
        return {"recommendation": recommendation, "summary": parsed.get("summary", "")}
    except json.JSONDecodeError:
        return {"recommendation": "Caution", "summary": "AI 분석 응답 파싱 실패 — 직접 확인 필요"}


# ── Slack 전송 ───────────────────────────────────────────────────────

def build_slack_blocks(result: dict, analysis: dict) -> list:
    icon = RECOMMENDATION_ICON.get(analysis["recommendation"], "⚠️")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon} 릴리즈 판단: {analysis['recommendation']}", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*\U0001f4ca 결과:* ✅ {result['passed']} / ❌ {result['failed']} / "
                    f"⏭️ {result['skipped']} *(총 {result['total']})*\n"
                    f"*⏱️ 소요 시간:* {result['duration_sec']}초\n"
                    f"*\U0001f4ac AI 요약:* {analysis['summary']}"
                ),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"\U0001f552 {now}"}]},
    ]

    if result["failed_tests"]:
        blocks.append({"type": "divider"})
        lines = "\n".join(
            f"• [{t['project']}] {t['title']} — {t['error'][:100]}"
            for t in result["failed_tests"][:5]
        )
        hidden = len(result["failed_tests"]) - 5
        hidden_text = f"\n\n📎 추가 실패 {hidden}건은 리포트에서 확인하세요." if hidden > 0 else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*❌ 실패 테스트*\n{lines}{hidden_text}"},
        })

    if PLAYWRIGHT_REPORT_URL:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "\U0001f4ca Playwright Report", "emoji": True},
                "url": PLAYWRIGHT_REPORT_URL,
                "style": "primary",
            }],
        })

    return blocks


def send_to_slack(result: dict, analysis: dict):
    if not WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL이 설정되지 않아 전송을 건너뜀")
        return

    payload = {
        "text": f"릴리즈 판단: {analysis['recommendation']} (실패 {result['failed']}건)",
        "blocks": build_slack_blocks(result, analysis),
    }

    response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    if response.status_code == 200:
        print("[Slack] 리포트 전송 완료")
    else:
        print(f"[ERROR] Slack 전송 실패: {response.status_code} {response.text}")


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Playwright JSON 리포트 -> Groq 분석 -> Slack 전송")
    parser.add_argument("report_path", help="Playwright JSON 리포트 경로 (예: playwright-report/results.json)")
    args = parser.parse_args()

    if not os.path.exists(args.report_path):
        print(f"[ERROR] 리포트 파일 없음: {args.report_path}")
        sys.exit(1)

    result = parse_report(args.report_path)
    print(f"[결과] 통과 {result['passed']} / 실패 {result['failed']} / 건너뜀 {result['skipped']}")

    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    try:
        analysis = analyze_with_groq(groq_client, result)
    except DailyTokenLimitError as e:
        print(f"[ERROR] {e}")
        analysis = {"recommendation": "Caution", "summary": "AI 분석 불가(토큰 한도 초과) — 결과 직접 확인 필요"}

    print(f"[AI 판단] {analysis['recommendation']} — {analysis['summary']}")
    send_to_slack(result, analysis)


if __name__ == "__main__":
    main()
