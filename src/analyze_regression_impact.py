"""
git diff 기반 회귀 영향 분석 — 코드 변경사항이 어떤 기존 TC에 영향을 주는지 AI로 추천

사용법:
  # 현재 저장소의 커밋 안 된 변경사항(staged+unstaged) 기준
  python src/analyze_regression_impact.py C:/path/to/repo reports/tc_MKQA-1_20260802_212409.json

  # 두 브랜치/커밋 사이의 diff 기준
  python src/analyze_regression_impact.py C:/path/to/repo reports/tc_MKQA-1_20260802_212409.json --base main --head feature/x

  # Groq 호출 없이 diff/TC 로딩만 확인 (한도 초과 시에도 확인 가능)
  python src/analyze_regression_impact.py C:/path/to/repo reports/tc_MKQA-1_20260802_212409.json --dry-run
"""

import sys
import os
import re
import json
import argparse
import subprocess
from datetime import datetime

from groq import Groq
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils import sanitize

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DIFF_CHAR_LIMIT = 6000


class DailyTokenLimitError(Exception):
    pass


# ── git diff ─────────────────────────────────────────────────────────

def get_git_diff(repo_path: str, base: str | None, head: str | None) -> tuple[str, list[str]]:
    """저장소의 diff와 변경된 파일 목록을 반환합니다.

    base/head가 없으면 마지막 커밋 대비 현재 작업트리(staged+unstaged) 기준.
    """
    if base and head:
        diff_range = f"{base}...{head}"
        args = ["git", "diff", diff_range]
        stat_args = ["git", "diff", "--stat", diff_range]
    else:
        args = ["git", "diff", "HEAD"]
        stat_args = ["git", "diff", "--stat", "HEAD"]

    diff = subprocess.run(args, cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if diff.returncode != 0:
        raise RuntimeError(f"git diff 실패: {diff.stderr.strip()}")

    stat = subprocess.run(stat_args, cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    changed_files = [
        line.split("|")[0].strip()
        for line in stat.stdout.splitlines()
        if "|" in line
    ]
    return diff.stdout, changed_files


# ── TC 풀 로딩 ───────────────────────────────────────────────────────

def load_tc_pool(tc_json_path: str) -> list:
    """generate_tc.py가 만든 JSON(티켓별 결과 리스트)에서 TC를 전부 펼쳐서 반환합니다.

    각 TC에 소속 티켓 키를 붙여 반환합니다.
    """
    with open(tc_json_path, encoding="utf-8") as f:
        data = json.load(f)

    pool = []
    for ticket in data:
        for tc in ticket.get("test_cases", []):
            pool.append({**tc, "_ticket_key": ticket.get("key", "")})
    return pool


# ── AI 분석 ──────────────────────────────────────────────────────────

def analyze_impact(groq_client: Groq, diff: str, changed_files: list, tc_pool: list) -> dict:
    truncated = diff[:DIFF_CHAR_LIMIT]
    is_truncated = len(diff) > DIFF_CHAR_LIMIT

    tc_summary = "\n".join(
        f"- {tc.get('tc_id', '')} | {tc.get('대분류', '')}/{tc.get('소분류', '')} | "
        f"{tc.get('테스트유형', '')} | {tc.get('테스트시나리오', '')}"
        for tc in tc_pool
    )

    prompt = f"""다음 코드 변경사항(git diff)을 보고, 아래 기존 TC 목록 중 이 변경으로 영향받을 가능성이 있는 TC를 골라주세요.
또한 변경된 부분 중 어떤 기존 TC로도 커버되지 않는 것으로 보이는 부분이 있다면 알려주세요.

[변경된 파일 목록]
{chr(10).join(changed_files) if changed_files else "(파일 목록 없음)"}

[git diff{" — 내용이 길어 일부만 표시됨" if is_truncated else ""}]
{truncated}

[기존 TC 목록]
{tc_summary if tc_summary else "(TC 없음)"}

아래 JSON 형식으로만 응답하세요. 마크다운 없이.
{{
  "impacted": [
    {{"tc_id": "TC_001", "reason": "이 TC가 왜 영향받는지 1문장", "확신도": "High/Medium/Low"}}
  ],
  "coverage_gaps": ["diff가 건드리는데 대응하는 TC가 안 보이는 부분에 대한 설명"]
}}

확실히 무관해 보이는 TC는 포함하지 마세요. 영향 있는 TC가 하나도 없으면 "impacted"를 빈 배열로, 공백이 없으면 "coverage_gaps"를 빈 배열로 응답하세요."""

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
                            "코드 변경사항을 보고 회귀 테스트가 필요한 범위를 정확히 짚어냅니다. "
                            "근거 없이 TC를 과도하게 많이 고르지 마세요. "
                            "JSON만 출력하세요. 한국어로만 작성하세요."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
                raise DailyTokenLimitError("Groq 일일 토큰 한도 초과 — 내일 다시 시도하세요") from e
            if "rate_limit" in e_str or "429" in str(e):
                import time
                wait = 65 * (attempt + 1)
                print(f"  [Rate Limit] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if response is None:
        raise RuntimeError("analyze_impact Rate Limit 재시도 소진")

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return sanitize(json.loads(raw))


# ── 메인 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="git diff 기반 회귀 영향 분석")
    parser.add_argument("repo_path", help="git 저장소 경로")
    parser.add_argument("tc_json_path", help="generate_tc.py가 만든 TC JSON 파일 경로")
    parser.add_argument("--base", default=None, help="비교 기준 브랜치/커밋 (예: main)")
    parser.add_argument("--head", default=None, help="비교 대상 브랜치/커밋 (예: feature/x)")
    parser.add_argument("--dry-run", action="store_true", help="Groq 호출 없이 diff/TC 로딩만 확인")
    args = parser.parse_args()

    print(f"\n=== 회귀 영향 분석 ===")
    print(f"저장소: {args.repo_path}")

    diff, changed_files = get_git_diff(args.repo_path, args.base, args.head)
    if not diff.strip():
        print("변경사항이 없습니다 (git diff 결과 없음).")
        return
    print(f"변경된 파일: {len(changed_files)}개")
    for f in changed_files:
        print(f"  - {f}")

    tc_pool = load_tc_pool(args.tc_json_path)
    print(f"TC 풀: {len(tc_pool)}개 ({args.tc_json_path})")

    if args.dry_run:
        print(f"\n[dry-run] Groq 호출을 건너뜁니다. diff 길이: {len(diff)}자")
        return

    print(f"\nAI 영향 분석 중...")
    groq_client = Groq(api_key=GROQ_API_KEY)
    try:
        result = analyze_impact(groq_client, diff, changed_files, tc_pool)
    except DailyTokenLimitError as e:
        print(f"[일일 한도 초과] {e}")
        sys.exit(1)

    tc_by_id = {tc.get("tc_id"): tc for tc in tc_pool}

    print(f"\n=== 영향받는 TC ({len(result.get('impacted', []))}개) ===")
    for item in result.get("impacted", []):
        tc = tc_by_id.get(item.get("tc_id"), {})
        print(
            f"  [{item.get('tc_id')}] [{item.get('확신도', '-')}] "
            f"[우선순위 {tc.get('우선순위', '-')}/위험도 {tc.get('위험도', '-')}] "
            f"{tc.get('테스트시나리오', '(TC 정보 없음)')}"
        )
        print(f"      사유: {item.get('reason', '')}")

    gaps = result.get("coverage_gaps", [])
    print(f"\n=== 커버리지 공백 ({len(gaps)}개) ===")
    for gap in gaps:
        print(f"  - {gap}")

    os.makedirs("reports", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/regression_impact_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "changed_files": changed_files,
                "tc_pool_source": args.tc_json_path,
                "impacted": result.get("impacted", []),
                "coverage_gaps": gaps,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n저장 완료: {out_path}")


if __name__ == "__main__":
    main()
