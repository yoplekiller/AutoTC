"""
기획서(Confluence 페이지 / 로컬 파일) 기반 매뉴얼 TC 자동 생성

generate_tc.py는 Jira 티켓 1건을 입력받아 TC를 만들지만, 기획서는 아직 티켓으로
쪼개지기 전이라 Jira 키가 없는 경우가 많다. 이 스크립트는 기획서 원문을 직접 입력받아
generate_tc.py의 파이프라인(요구사항 추론 → 유형별 TC 플랜 → TC 생성 → 필터/중복제거 →
엑셀 저장)을 그대로 재사용한다. Jira 티켓 조회 없이도 동작하므로 티켓화 전 단계에서
바로 TC 초안을 뽑아볼 수 있다.

기획서는 전체 범위를 다루는 문서로 보고, 항상 "에픽" 수준의 최소 TC 기준
(기능 8개 이상, 예외처리 7개 이상 등, generate_tc.py의 analyze_spec_for_plan 참고)을 적용한다.

사용법:
  python src/generate_tc_from_spec.py --spec-file spec.md
  python src/generate_tc_from_spec.py --confluence-url https://xxx.atlassian.net/wiki/spaces/.../pages/123
  python src/generate_tc_from_spec.py --confluence-title "월급까지 - Phase 1 기획서"
  python src/generate_tc_from_spec.py --spec-file spec.md --context kream
  python src/generate_tc_from_spec.py --spec-file spec.md --key SPEC-PAYDAY-P1

  # 로컬 저장과 별개로, 티켓 기반 flow가 쓰는 것과 같은 구글 스프레드시트(SPREADSHEET_ID)에
  # 결과 탭을 하나 추가로 만들고 싶을 때(옵트인, 기본은 로컬 저장만)
  python src/generate_tc_from_spec.py --spec-file spec.md --sheet
"""

import argparse
import json
import os
import re
import sys
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# watch_sheet 임포트가 stdout을 utf-8로 감싸므로, generate_tc의 감싸기는 이미 utf-8이면 건너뛴다
# (두 번 감싸면 이전 TextIOWrapper가 GC될 때 내부 버퍼까지 닫혀 "I/O operation on closed file" 오류가 남 —
#  generate_bug_report.py 개발 중 실제로 겪었던 문제와 같은 종류).
from src.watch_sheet import fetch_confluence_page  # noqa: F401  (임포트만으로 stdout이 utf-8로 감싸짐)

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from datetime import datetime

from dotenv import load_dotenv
from groq import Groq

from src.generate_tc import (
    DailyTokenLimitError,
    augment_ticket_spec,
    dedupe_tc_list,
    filter_tc_list,
    generate_test_cases,
    load_context,
    save_excel,
    save_to_sheets,
)
from src.generate_tickets_from_spec import (
    find_confluence_url_by_title,
    read_spec_file,
    read_spec_from_confluence,
)

load_dotenv()


def slugify(text: str) -> str:
    """결과 파일명/식별용 키를 만든다. 기획서 제목이나 파일명처럼 사람이 알아볼 수 있는 문자열을 넣는다."""
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "_", text).strip("_")
    return (slug[:40] or "SPEC").upper()


def build_pseudo_issue(spec: str, key: str) -> dict:
    """기획서 원문을 generate_tc.py의 파이프라인이 기대하는 issue 딕셔너리 형태로 감싼다.

    issue_type을 "에픽"으로 고정하는 이유: 기획서는 티켓 하나보다 범위가 넓은 문서이므로,
    analyze_spec_for_plan()의 유형별 최소 TC 기준 중 가장 넓은 Epic 기준(기능 8개 이상 등)을
    적용받도록 한다.
    """
    first_line = next((line.strip("# ").strip() for line in spec.splitlines() if line.strip()), "")
    return {
        "key": key,
        "summary": first_line[:200] or "기획서",
        "status": "기획",
        "description": spec,
        "issue_type": "에픽",
    }


def process_spec(groq_client: Groq, spec: str, key: str, context: str = "") -> dict:
    """기획서 한 건을 처리해 generate_tc.py의 결과 아이템과 동일한 형태로 반환한다."""
    issue = build_pseudo_issue(spec, key)
    print(f"  제목: {issue['summary']}")
    print("  요구사항 추론 중...")
    augmented_spec = augment_ticket_spec(groq_client, issue, context)
    print(f"  --- 요구사항 분석 ---\n{augmented_spec}\n  ---")
    print("  TC 생성 중...")
    tc_list = generate_test_cases(groq_client, issue, augmented_spec, context)
    tc_list = filter_tc_list(tc_list)
    tc_list = dedupe_tc_list(tc_list)
    print(f"  생성된 TC: {len(tc_list)}개")
    for tc in tc_list:
        print(
            f"    [{tc.get('tc_id')}] [{tc.get('대분류', '-')}] "
            f"[{tc.get('테스트유형', '-')}] [{tc.get('우선순위', '-')}] {tc.get('테스트시나리오', '')}"
        )

    return {
        "key": issue["key"],
        "summary": issue["summary"],
        "status": issue["status"],
        "augmented_spec": augmented_spec,
        "test_cases": tc_list,
    }


def main():
    parser = argparse.ArgumentParser(description="기획서(Confluence/로컬 파일) → 매뉴얼 TC 자동 생성")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--spec-file", help="기획서 파일 경로(.md/.txt)")
    source_group.add_argument("--confluence-url", help="기획서가 있는 Confluence 페이지 URL")
    source_group.add_argument("--confluence-title", help="기획서가 있는 Confluence 페이지 제목 (정확히 일치해야 함)")
    parser.add_argument("--space", default=None, help="Confluence 스페이스 키 (--confluence-title과 함께 사용, 기본: QATEST)")
    parser.add_argument("--context", default="", help="서비스 컨텍스트 이름 (예: kream, kurly)")
    parser.add_argument("--key", default=None, help="결과 식별용 키 (미지정 시 기획서 제목/파일명에서 자동 생성)")
    parser.add_argument(
        "--sheet",
        action="store_true",
        help="로컬 저장과 별개로, SPREADSHEET_ID 환경변수의 구글 시트에도 결과를 새 탭으로 저장 (기본: 안 함)",
    )
    args = parser.parse_args()

    context = load_context(args.context)
    if context:
        print(f"  컨텍스트 로드됨: contexts/{args.context}.md")

    print("\n=== 기획서 기반 TC 자동 생성 ===")

    confluence_url = None
    if args.spec_file:
        print(f"기획서 파일: {args.spec_file}")
        spec = read_spec_file(args.spec_file)
        label_source = os.path.splitext(os.path.basename(args.spec_file))[0]
    elif args.confluence_url:
        print(f"Confluence 페이지: {args.confluence_url}")
        confluence_url = args.confluence_url
        spec = read_spec_from_confluence(args.confluence_url)
        label_source = args.confluence_url
    else:
        print(f"Confluence 페이지 제목: {args.confluence_title}")
        confluence_url = find_confluence_url_by_title(args.confluence_title, args.space)
        if not confluence_url:
            space_key = args.space or os.getenv("CONFLUENCE_SPACE_KEY", "QATEST")
            print(f'[오류] "{space_key}" 스페이스에서 제목이 "{args.confluence_title}"인 페이지를 찾지 못했습니다.')
            sys.exit(1)
        spec = read_spec_from_confluence(confluence_url)
        label_source = args.confluence_title

    key = args.key or slugify(label_source)
    print(f"결과 식별 키: {key}")

    groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    try:
        result = process_spec(groq_client, spec, key, context)
    except DailyTokenLimitError as e:
        print(f"\n[일일 한도 초과] {e}")
        sys.exit(1)

    # url을 명시적으로 넣어야 save_excel/save_to_sheets가 Jira 링크 대신 이 링크를 쓴다.
    # 로컬 파일 입력이라 링크가 없으면 빈 문자열을 넣어서, 존재하지도 않는 Jira 티켓 링크가
    # 걸리지 않도록 한다(save_excel은 "url" 키가 아예 없을 때만 Jira 링크로 조립함).
    result["url"] = confluence_url or ""

    if not result["test_cases"]:
        print("\n[오류] 생성된 TC가 없습니다.")
        sys.exit(1)

    print("\n=== 결과 저장 중... ===")
    os.makedirs("reports", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = key.replace("/", "_")

    json_path = f"reports/tc_{label}_{timestamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([result], f, ensure_ascii=False, indent=2)
    print(f"  JSON 저장 완료: {json_path}")

    xlsx_path = f"reports/tc_{label}_{timestamp}.xlsx"
    save_excel([result], xlsx_path)
    print(f"  엑셀 저장 완료: {xlsx_path}")

    if args.sheet:
        sheet_id = os.getenv("SPREADSHEET_ID", "")
        if not sheet_id:
            print("  [경고] --sheet를 줬지만 SPREADSHEET_ID 환경변수가 없어 구글 시트 저장을 건너뜁니다.")
        else:
            print("  구글 시트에 저장 중...")
            save_to_sheets([result], sheet_id)
            print("  구글 시트 저장 완료")

    print(f"\n=== 완료: {len(result['test_cases'])}개 TC 생성 ===")


if __name__ == "__main__":
    main()
