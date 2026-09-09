"""
Requirement -> TestCondition -> TestCase 추적성 계산 (AutoTC 2.0, 2026-09-09).

augment_ticket_spec()이 만드는 "REQ-N." 텍스트를 구조화된 Requirement로 파싱하고,
condition_planner(v1)로 TestCondition을 만든 뒤 TC와 연결해 Coverage/GAP을 계산한다.

범위 밖(의도적으로 하지 않는 것):
- LLM 기반 조건 설계(condition_planner v1은 규칙 기반 숫자 범위만 다룬다)
- 생성 파이프라인의 실패를 요구사항 단위로 정밀 추적하는 것(현재 생성은 REQ 단위가 아니라
  테스트유형 단위로 진행되므로 GENERATION_FAILED는 호출자가 명시적으로 넘겨준 경우에만 반영한다)
"""

import re

from schemas import make_requirement
from condition_planner import plan_conditions

_REQ_ITEM_PATTERN = re.compile(
    # 다음 "REQ-N." 또는 다음 상위 섹션 번호("4. 보안...", "5. 확인이 필요한 질문" 등) 직전까지를
    # 이 요구사항의 본문으로 본다 — 해당 섹션에 REQ가 하나도 없어도(예: "4. 보안·권한: 해당 없음")
    # 앞선 REQ-N의 본문이 그 뒤 섹션 전체를 삼켜버리지 않게 하기 위함.
    r"REQ-(\d+)\.\s*(.+?)(?=\n\s*REQ-\d+\.|\n\s*\d+\.\s|\Z)", re.DOTALL
)
_CLARIFICATION_HEADER_PATTERN = re.compile(
    r"확인이\s*필요한\s*질문[^\n]*\n"
)
_LEADING_MARKER_PATTERN = re.compile(r"^\s*(?:[-*・]|\d+[.)])\s*")


def parse_requirements(augmented_spec: str, source_id: str) -> list:
    """augmented_spec 텍스트에서 REQ-N 항목과 "확인이 필요한 질문" 섹션을 Requirement로 파싱한다.

    REQ-N 항목은 status="OK"(원문에 번호가 매겨진 명시적 요구사항), 확인이 필요한 질문 섹션은
    status="NEEDS_CLARIFICATION"(아직 정책이 확정되지 않음)으로 분리한다.
    """
    requirements = []

    for match in _REQ_ITEM_PATTERN.finditer(augmented_spec or ""):
        num, text = match.group(1), match.group(2).strip()
        if not text:
            continue
        requirements.append(
            make_requirement(
                requirement_id=f"REQ-{num}",
                source_id=source_id,
                original_text=text,
                status="OK",
                source_type="EXPLICIT_REQUIREMENT",
            )
        )

    header_match = _CLARIFICATION_HEADER_PATTERN.search(augmented_spec or "")
    if header_match:
        block = augmented_spec[header_match.end():].strip()
        if block and block != "없음":
            idx = 0
            for line in block.split("\n"):
                line = _LEADING_MARKER_PATTERN.sub("", line).strip()
                if not line or line == "없음":
                    continue
                idx += 1
                requirements.append(
                    make_requirement(
                        requirement_id=f"CLARIFY-{idx}",
                        source_id=source_id,
                        original_text=line,
                        status="NEEDS_CLARIFICATION",
                        source_type="POLICY_GAP",
                    )
                )

    return requirements


def build_conditions(requirements: list) -> list:
    """요구사항마다 condition_planner.plan_conditions()로 TestCondition을 만든다.

    명시적 숫자 범위(예: "8자 이상 20자 이하")가 파싱되면 요구사항 1개가 BELOW_MIN/MIN/MAX/
    ABOVE_MAX 조건 4개로 분할된다. 그 외(자유서술형, 미확정 정책)는 조건 1개로 남는다 —
    규칙에 안 걸리는 문장에서 경계값을 임의로 추론하지 않는다(condition_planner.py 참고).
    """
    conditions = []
    for req in requirements:
        conditions.extend(plan_conditions(req))
    return conditions


def _boundary_value_mentioned(condition: dict, text_blob: str) -> bool:
    """조건의 경계값(정수)이 TC 텍스트에 리터럴로 등장하는지 확인한다 (숫자 매칭만, 의미 추론 없음)."""
    boundary = condition.get("boundary")
    if not boundary:
        return False
    value = str(boundary["value"])
    return re.search(rf"(?<!\d){re.escape(value)}(?!\d)", text_blob) is not None


def _resolve_condition_id(tc: dict, valid_refs: list, conditions_by_req: dict) -> str | None:
    """TC가 정확히 어느 조건에 해당하는지 결정한다.

    - 요구사항이 조건 1개(자유서술형/미확정)로만 매핑돼 있으면 참조만으로 충분히 결정되므로
      기존과 동일하게 자동 연결한다(기존 단순 requirement 흐름 유지).
    - 요구사항이 경계값 조건 여러 개로 분할돼 있으면 requirement_refs만으로는 어느 경계값인지
      알 수 없다 — TC 텍스트에 그 경계값이 리터럴 숫자로 등장할 때만 연결하고, 등장하는 값이
      0개거나 2개 이상(모호)이면 임의로 추론하지 않고 연결하지 않는다.
    """
    text_blob = " ".join(
        str(tc.get(f, "")) for f in ("테스트시나리오", "사전조건", "테스트단계", "기대결과")
    )
    for req_id in valid_refs:
        candidates = conditions_by_req.get(req_id, [])
        if len(candidates) == 1:
            return candidates[0]["condition_id"]
        matches = [c for c in candidates if _boundary_value_mentioned(c, text_blob)]
        if len(matches) == 1:
            return matches[0]["condition_id"]
    return None


def link_test_cases(
    conditions: list,
    requirements: list,
    tc_list: list,
    generation_failed_requirement_ids: set | None = None,
) -> tuple:
    """TC의 requirement_refs를 검증하고 condition_id를 부여한 뒤, 조건별 Coverage 상태를 갱신한다.

    conditions/tc_list를 제자리에서 갱신하고 (조건_id, TC 리스트, invalid_references)를 반환한다.
    dedupe_tc_list처럼 물리 삭제는 하지 않는다 — 여기서도 TC를 지우지 않는다.
    """
    generation_failed_requirement_ids = generation_failed_requirement_ids or set()
    valid_req_ids = {r["requirement_id"] for r in requirements}
    conditions_by_req: dict = {}
    for c in conditions:
        conditions_by_req.setdefault(c["requirement_id"], []).append(c)
    invalid_references = []

    for tc in tc_list:
        refs = tc.get("requirement_refs") or []
        valid_refs = [r for r in refs if r in valid_req_ids]
        invalid_refs = [r for r in refs if r not in valid_req_ids]

        for ref in invalid_refs:
            invalid_references.append({"tc_id": tc.get("tc_id"), "requirement_id": ref})

        tc["condition_id"] = _resolve_condition_id(tc, valid_refs, conditions_by_req)
        tc["invalid_requirement_refs"] = invalid_refs

    for condition in conditions:
        if condition["status"] == "NEEDS_CLARIFICATION":
            continue  # 정책 미확정 — TC 유무와 무관하게 유지

        covering = [
            tc for tc in tc_list
            if tc.get("condition_id") == condition["condition_id"]
            and tc.get("quality_status") != "REJECT"
        ]
        if covering:
            condition["status"] = "COVERED"
        elif condition["requirement_id"] in generation_failed_requirement_ids:
            condition["status"] = "GENERATION_FAILED"
        else:
            condition["status"] = "MISSING_TC"

    return conditions, tc_list, invalid_references


def compute_coverage_report(conditions: list, invalid_references: list | None = None) -> dict:
    """조건별 상태 집계. Requirement 하나에 TC 하나 연결됐다고 완전 커버는 아니라는 원칙은
    P1의 정밀 조건 분할(경계/상태 세분화)이 붙어야 의미가 생긴다 — 여기서는 최소 집계만 한다.
    """
    from schemas import CONDITION_STATUSES

    by_status = {status: 0 for status in CONDITION_STATUSES}
    for condition in conditions:
        by_status[condition["status"]] += 1

    return {
        "total_conditions": len(conditions),
        "by_status": by_status,
        "invalid_references": invalid_references or [],
    }


def is_generation_complete(tc_list: list) -> bool:
    """TC가 0건이면(생성 실패/한도 초과 등) 조용히 성공으로 넘어가지 않도록 명시적으로 False를 반환한다."""
    return len(tc_list) > 0
