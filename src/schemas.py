"""
Requirement -> TestCondition -> TestCase 추적성 데이터 구조 (AutoTC 2.0 P0 vertical slice, 2026-09-09).

기존 코드베이스가 전부 plain dict 기반이라(tc_core.py의 issue/tc dict 등) 여기도 dataclass 대신
dict 팩토리 함수로 통일한다 — json.dump에 바로 넣을 수 있고 나머지 코드와 스타일이 맞는다.

이번 단계에서 SourceSnapshot/Requirement/TestCondition의 최소 구조만 정의한다.
Clarification/GenerationRun/ExecutionResult 같은 나머지 객체는 P1 이후 범위.
"""

# 조건(TestCondition)에 TC가 실제로 연결됐는지를 나타내는 상태.
# 물리 삭제나 조용한 통과 대신, 어떤 조건이 비었는지/모호한지/근거가 틀렸는지를 명시적으로 남기기 위함.
CONDITION_STATUSES = {
    "COVERED",              # 유효한 TC(REJECT 아님)가 1개 이상 연결됨
    "MISSING_TC",           # 요구사항은 확정됐는데 연결된 TC가 없음
    "NEEDS_CLARIFICATION",  # 근거가 되는 요구사항 자체가 미확정 정책/질문 상태
    "INVALID_REFERENCE",    # TC가 존재하지 않는 requirement_id를 참조함
    "GENERATION_FAILED",    # 생성 단계에서 실패가 감지됐는데 아직 TC로 채워지지 않음
}

REQUIREMENT_STATUSES = {"OK", "NEEDS_CLARIFICATION"}

# condition_planner v1이 이 조건을 어떻게 만들었는지 — coverage 상태(CONDITION_STATUSES)와는
# 별개 축이다. STRUCTURED/UNRESOLVED가 바뀌어도 COVERED/MISSING_TC 판정 로직(TC 연결 여부)은
# 그대로라, 자유서술형 요구사항의 기존 커버리지 동작이 이 필드 추가로 깨지지 않는다.
CONDITION_PLANNING_STATUSES = {
    "STRUCTURED",                    # 명시적 제약(숫자 범위 등)을 파싱해 경계 조건으로 분할함
    "CONDITION_PLANNING_UNRESOLVED",  # 규칙에 매칭되지 않아 요구사항 전체를 조건 1개로만 남김(임의 추론 안 함)
    "LLM_INFERRED",                  # 규칙에 안 걸린 자유서술형을 LLM이 분할함(llm_condition_planner) — 원문 명시가 아니라 AI 추론임을 구분
}

# 근거 출처 구분 — Audit 5번 섹션 지적사항(모든 TC가 source_type="requirement"로 덮여
# 원문 명시/AI 추론/과거 결함이 뒤섞이는 문제)을 Requirement 레벨에서부터 구분해둔다.
SOURCE_TYPES = {
    "EXPLICIT_REQUIREMENT",  # augment_ticket_spec가 "REQ-N."으로 번호 매긴 항목
    "POLICY_GAP",            # "확인이 필요한 질문" 섹션 — 아직 정책이 확정되지 않음
}


def make_source_snapshot(source_id: str, raw_text: str, version: str, collected_at: str) -> dict:
    return {
        "source_id": source_id,
        "raw_text": raw_text,
        "version": version,
        "collected_at": collected_at,
    }


def make_requirement(
    requirement_id: str,
    source_id: str,
    original_text: str,
    status: str = "OK",
    source_type: str = "EXPLICIT_REQUIREMENT",
    constraints: list | None = None,
) -> dict:
    if status not in REQUIREMENT_STATUSES:
        raise ValueError(f"invalid requirement status: {status}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"invalid source_type: {source_type}")
    return {
        "requirement_id": requirement_id,
        "source_id": source_id,
        "original_text": original_text.strip(),
        "status": status,
        "source_type": source_type,
        "constraints": constraints or [],
    }


def make_test_condition(
    condition_id: str,
    requirement_id: str,
    purpose: str,
    status: str = "MISSING_TC",
    planning_status: str = "STRUCTURED",
    input_partition: str | None = None,
    boundary: dict | None = None,
    initial_state: str | None = None,
    event: str | None = None,
    oracle: str | None = None,
) -> dict:
    if status not in CONDITION_STATUSES:
        raise ValueError(f"invalid condition status: {status}")
    if planning_status not in CONDITION_PLANNING_STATUSES:
        raise ValueError(f"invalid planning_status: {planning_status}")
    return {
        "condition_id": condition_id,
        "requirement_id": requirement_id,
        "purpose": purpose.strip(),
        "status": status,
        "planning_status": planning_status,
        "input_partition": input_partition,
        # 구조화된 데이터로 저장 — {"type": "MIN"/"MAX"/"BELOW_MIN"/"ABOVE_MAX", "value": int}.
        # 문자열 설명에만 박아두면 코드가 다시 파싱해야 하므로 dict로 유지한다.
        "boundary": boundary,
        "initial_state": initial_state,
        "event": event,
        "oracle": oracle,
    }
