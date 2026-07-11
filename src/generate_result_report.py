"""
QA 결과보고서 자동 생성 → Confluence 페이지 게시

1뎁스 구성: 테스트요약 / 테스트 수행일정 / 검증범위 및 특이사항 / 검증대상 / 테스트케이스 진행결과 / 결함현황
(테스트요약은 2뎁스로 과제명~결함현황 14개 필드 요약 표를 포함)

사용법:
  python src/generate_result_report.py MKQA-1 MKQA-2 --title "결제 모듈 결과보고서" \
      --round 1차 --author 임재민 --qa 임재민 --deploy-date 2026-07-20 \
      --start-date 2026-07-14 --end-date 2026-07-18 \
      --env "Chromium / Windows 11" --playwright-report playwright-report/results.json

  python src/generate_result_report.py MKQA-1 --dry-run
"""

import sys
import os
import argparse
from datetime import datetime

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.generate_test_plan import fetch_issues, create_confluence_page
from src.release_report import parse_report

load_dotenv()

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


# ── 2뎁스: 테스트요약 표 ─────────────────────────────────────────────

def build_summary_table(meta: dict, issues: list, result: dict | None) -> str:
    scope_summary = f"{len(issues)}건 (하단 '검증대상' 참고)" if issues else "-"
    target_summary = scope_summary
    defect_summary = f"실패 {result['failed']}건 / 통과 {result['passed']}건" if result else "-"
    schedule = f"{meta['start_date']} ~ {meta['end_date']}"

    rows = [
        ("과제명", meta["title"]),
        ("차수정보", meta["round_info"]),
        ("작성자", meta["author"]),
        ("QA담당자", meta["qa_owner"]),
        ("배포일정", meta["deploy_date"]),
        ("수행일정", schedule),
        ("검증범위", scope_summary),
        ("체크리스트", meta["checklist"]),
        ("기획문서", meta["spec_doc"]),
        ("Figma", meta["figma"]),
        ("특이사항", meta["note"] or "특이사항 없음"),
        ("검증대상", target_summary),
        ("검증환경", meta["env"]),
        ("결함현황", defect_summary),
    ]
    row_html = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    return f"<table><tbody>{row_html}</tbody></table>"


# ── 1뎁스 섹션들 ──────────────────────────────────────────────────────

def build_schedule_section(meta: dict) -> str:
    return f"""<table>
  <thead><tr><th>시작일</th><th>종료일</th></tr></thead>
  <tbody><tr><td>{meta['start_date']}</td><td>{meta['end_date']}</td></tr></tbody>
</table>"""


def build_scope_section(issues: list, note: str) -> str:
    scope_items = "".join(f"<li>[{i['key']}] {i['summary']}</li>" for i in issues) or "<li>대상 티켓 없음</li>"
    return f"""<h3>검증 범위</h3>
<ul>{scope_items}</ul>
<h3>특이사항</h3>
<p>{note or '특이사항 없음'}</p>"""


def build_target_table(issues: list) -> str:
    rows = "".join(
        f"""<tr>
          <td><a href="{i['url']}">{i['key']}</a></td>
          <td>{i['issue_type']}</td>
          <td>{i['summary']}</td>
          <td>{i['status']}</td>
          <td>{i['priority']}</td>
        </tr>"""
        for i in issues
    )
    return f"""<table>
  <thead>
    <tr><th>티켓 키</th><th>유형</th><th>제목</th><th>상태</th><th>우선순위</th></tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""


def build_tc_result_section(result: dict | None) -> str:
    if result is None:
        return "<p>테스트 결과 데이터 없음 (--playwright-report 인자로 JSON 경로를 지정하세요)</p>"

    pass_rate = round(result["passed"] / result["total"] * 100, 1) if result["total"] else 0
    return f"""<table>
  <thead><tr><th>통과</th><th>실패</th><th>건너뜀</th><th>총계</th><th>통과율</th><th>소요 시간</th></tr></thead>
  <tbody>
    <tr>
      <td>{result['passed']}</td>
      <td>{result['failed']}</td>
      <td>{result['skipped']}</td>
      <td>{result['total']}</td>
      <td>{pass_rate}%</td>
      <td>{result['duration_sec']}초</td>
    </tr>
  </tbody>
</table>"""


def build_defect_section(result: dict | None) -> str:
    if result is None:
        return "<p>테스트 결과 데이터 없음 (--playwright-report 인자로 JSON 경로를 지정하세요)</p>"
    if not result["failed_tests"]:
        return "<p>결함 없음 — 실패한 테스트가 없습니다.</p>"

    rows = "".join(
        f"<tr><td>{t['title']}</td><td>{t['project']}</td><td>{t['status']}</td><td>{t['error']}</td></tr>"
        for t in result["failed_tests"]
    )
    return f"""<table>
  <thead><tr><th>테스트명</th><th>프로젝트</th><th>상태</th><th>에러 메시지</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""


# ── Confluence Storage Format 조합 ───────────────────────────────────

def build_confluence_content(meta: dict, issues: list, result: dict | None) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    return f"""<h2>1. 테스트요약</h2>
{build_summary_table(meta, issues, result)}

<h2>2. 테스트 수행일정</h2>
{build_schedule_section(meta)}

<h2>3. 검증범위 및 특이사항</h2>
{build_scope_section(issues, meta['note'])}

<h2>4. 검증대상</h2>
{build_target_table(issues)}

<h2>5. 테스트케이스 진행결과</h2>
{build_tc_result_section(result)}

<h2>6. 결함현황</h2>
{build_defect_section(result)}

<p><em>작성일: {today} | 자동 생성 by AutoTC</em></p>"""


# ── Slack 알림 ────────────────────────────────────────────────────────

def send_slack_notification(page: dict, meta: dict):
    if not SLACK_WEBHOOK_URL:
        return
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📄 QA 결과보고서 자동 생성 완료"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*문서 제목*\n<{page['url']}|{page['title']}>"},
                    {"type": "mrkdwn", "text": f"*차수*\n{meta['round_info']}"},
                ],
            },
        ]
    }
    requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Jira 티켓 + Playwright 결과 기반 QA 결과보고서 → Confluence 자동 생성")
    parser.add_argument("tickets", nargs="+", help="Jira 티켓 키 (예: MKQA-1 MKQA-2)")
    parser.add_argument("--title", required=True, help="과제명")
    parser.add_argument("--round", dest="round_info", default="1차", help="차수정보 (기본값: 1차)")
    parser.add_argument("--author", required=True, help="작성자")
    parser.add_argument("--qa", dest="qa_owner", default=None, help="QA담당자 (미입력 시 작성자와 동일)")
    parser.add_argument("--deploy-date", default="-", help="배포일정")
    parser.add_argument("--start-date", default=datetime.now().strftime("%Y-%m-%d"), help="수행일정 시작일")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y-%m-%d"), help="수행일정 종료일")
    parser.add_argument("--checklist", default="-", help="체크리스트 링크 또는 상태")
    parser.add_argument("--spec-doc", default="-", help="기획문서 링크")
    parser.add_argument("--figma", default="-", help="Figma 링크")
    parser.add_argument("--note", default="", help="특이사항")
    parser.add_argument("--env", default="-", help="검증환경 (예: Chromium / Windows 11)")
    parser.add_argument("--playwright-report", default=None, help="Playwright JSON 결과 파일 경로")
    parser.add_argument("--dry-run", action="store_true", help="Confluence에 올리지 않고 내용만 출력")
    args = parser.parse_args()

    meta = {
        "title": args.title,
        "round_info": args.round_info,
        "author": args.author,
        "qa_owner": args.qa_owner or args.author,
        "deploy_date": args.deploy_date,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "checklist": args.checklist,
        "spec_doc": args.spec_doc,
        "figma": args.figma,
        "note": args.note,
        "env": args.env,
    }

    print(f"\n=== QA 결과보고서 자동 생성 ===")
    print(f"대상 티켓: {', '.join(args.tickets)}")
    print(f"\nJira 티켓 조회 중...")
    issues = fetch_issues(args.tickets)

    if not issues:
        print("[오류] 유효한 티켓이 없습니다.")
        sys.exit(1)

    result = None
    if args.playwright_report:
        if not os.path.exists(args.playwright_report):
            print(f"[경고] Playwright 리포트 파일 없음: {args.playwright_report} (테스트 결과 섹션은 비어있게 생성됩니다)")
        else:
            result = parse_report(args.playwright_report)
            print(f"[테스트 결과] 통과 {result['passed']} / 실패 {result['failed']} / 건너뜀 {result['skipped']}")

    content = build_confluence_content(meta, issues, result)
    page_title = f"{meta['title']} 결과보고서 ({meta['round_info']}, {datetime.now().strftime('%Y-%m-%d')})"

    if args.dry_run:
        print(f"\n[dry-run] Confluence 업로드를 건너뜁니다.")
        print(f"  제목 : {page_title}")
        print("\n--- Storage Format 미리보기 ---")
        print(content[:800] + "...")
        return

    print(f"\nConfluence 페이지 생성 중...")
    page = create_confluence_page(page_title, content)

    print(f"\n=== 완료 ===")
    print(f"  제목 : {page['title']}")
    print(f"  URL  : {page['url']}")

    send_slack_notification(page, meta)
    if SLACK_WEBHOOK_URL:
        print(f"  Slack 알림 전송 완료")


if __name__ == "__main__":
    main()
