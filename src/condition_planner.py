"""
condition_planner v1 — 명시적으로 파싱 가능한 숫자 범위 제약을 경계값 TestCondition으로 분할한다
(AutoTC 2.0 P1 착수분, 2026-09-09).

deterministic(규칙 기반)만 다룬다. LLM 기반 조건 설계는 이번 단계 범위 밖 — 규칙에 안 걸리면
임의로 추론하지 않고 요구사항 전체를 조건 1개로 남긴다(planning_status="CONDITION_PLANNING_UNRESOLVED").

지원 패턴:
  - "N 이상 M 이하" / "N자 이상 M자 이하"
  - "N~M" (물결/틸드)
  - "최소 N자, 최대 M자" / "최소 길이 N, 최대 길이 M"
"""

import re

from schemas import make_test_condition

_UNIT = r"[가-힣]{0,2}"  # "자"/"개"/"회"/"일" 등 짧은 단위 접미사

_RANGE_PATTERNS = [
    # "8자 이상 20자 이하" / "8 이상 20 이하"
    re.compile(rf"(\d+)\s*{_UNIT}\s*이상\s*(\d+)\s*{_UNIT}\s*이하"),
    # "8~20자" / "8~20" (물결/유사기호만 — 하이픈은 날짜·티켓번호 등과 혼동되므로 제외)
    re.compile(rf"(\d+)\s*[~∼]\s*(\d+)\s*{_UNIT}"),
    # "최소 8자, 최대 20자" / "최소 길이 8, 최대 길이 20"
    re.compile(rf"최소\s*(?:길이\s*)?(\d+)\s*{_UNIT}[^최]{{0,20}}?최대\s*(?:길이\s*)?(\d+)"),
]

BOUNDARY_TYPES = ("BELOW_MIN", "MIN", "MAX", "ABOVE_MAX")


def extract_numeric_range(text: str) -> tuple | None:
    """텍스트에서 명시적 min/max 숫자 범위를 찾는다. 못 찾으면 None (자유서술은 추론하지 않음)."""
    for pattern in _RANGE_PATTERNS:
        m = pattern.search(text or "")
        if not m:
            continue
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            continue  # 범위가 아니라 단일 값 — 경계 4분할 대상이 아님
        return lo, hi
    return None


def plan_conditions(requirement: dict) -> list:
    """요구사항 1개에 대해 TestCondition 목록을 만든다.

    - 정책이 미확정(NEEDS_CLARIFICATION)이면 숫자 범위가 보여도 절대 분할하지 않는다 —
      아직 확정되지 않은 문장에서 경계값을 뽑아내는 것 자체가 근거 없는 추론이기 때문.
    - 명시적 숫자 범위가 파싱되면 BELOW_MIN/MIN/MAX/ABOVE_MAX 4개 조건으로 분할한다.
    - 그 외(자유서술형)는 기존과 동일하게 요구사항 전체를 조건 1개로 남긴다.
    """
    req_id = requirement["requirement_id"]
    text = requirement["original_text"]

    if requirement["status"] == "NEEDS_CLARIFICATION":
        return [
            make_test_condition(
                condition_id=f"COND-{req_id}",
                requirement_id=req_id,
                purpose=text,
                status="NEEDS_CLARIFICATION",
                planning_status="CONDITION_PLANNING_UNRESOLVED",
            )
        ]

    rng = extract_numeric_range(text)
    if rng is None:
        return [
            make_test_condition(
                condition_id=f"COND-{req_id}",
                requirement_id=req_id,
                purpose=text,
                status="MISSING_TC",
                planning_status="CONDITION_PLANNING_UNRESOLVED",
            )
        ]

    lo, hi = rng
    boundary_values = {"BELOW_MIN": lo - 1, "MIN": lo, "MAX": hi, "ABOVE_MAX": hi + 1}
    return [
        make_test_condition(
            condition_id=f"COND-{req_id}-{boundary_type}",
            requirement_id=req_id,
            purpose=f"{text} — {boundary_type}({boundary_values[boundary_type]})",
            status="MISSING_TC",
            planning_status="STRUCTURED",
            boundary={"type": boundary_type, "value": boundary_values[boundary_type]},
        )
        for boundary_type in BOUNDARY_TYPES
    ]
