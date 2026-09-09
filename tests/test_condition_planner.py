from condition_planner import extract_numeric_range, plan_conditions
from schemas import make_requirement


def test_extract_numeric_range_supports_all_target_patterns():
    assert extract_numeric_range("비밀번호는 8자 이상 20자 이하만 허용한다") == (8, 20)
    assert extract_numeric_range("닉네임은 2~10자 이내로 입력한다") == (2, 10)
    assert extract_numeric_range("최소 8자, 최대 20자로 제한한다") == (8, 20)
    assert extract_numeric_range("최소 길이 8, 최대 길이 20") == (8, 20)


def test_extract_numeric_range_returns_none_for_free_text():
    assert extract_numeric_range("기존 비밀번호 확인이 필요하다") is None
    assert extract_numeric_range("변경 완료 시 알림 메일을 발송한다") is None


def test_plan_conditions_splits_length_requirement_into_four_boundaries():
    req = make_requirement("REQ-1", "MKQA-1", "비밀번호는 8자 이상 20자 이하만 허용한다")

    conditions = plan_conditions(req)

    assert len(conditions) == 4
    by_type = {c["boundary"]["type"]: c for c in conditions}
    assert by_type["BELOW_MIN"]["boundary"]["value"] == 7
    assert by_type["MIN"]["boundary"]["value"] == 8
    assert by_type["MAX"]["boundary"]["value"] == 20
    assert by_type["ABOVE_MAX"]["boundary"]["value"] == 21
    # condition_id가 전부 고유해야 함
    assert len({c["condition_id"] for c in conditions}) == 4
    assert all(c["requirement_id"] == "REQ-1" for c in conditions)
    assert all(c["status"] == "MISSING_TC" for c in conditions)
    assert all(c["planning_status"] == "STRUCTURED" for c in conditions)


def test_plan_conditions_does_not_split_free_text_requirement():
    """규칙에 매칭되지 않는 자유서술형 요구사항은 조건을 임의로 만들지 않는다."""
    req = make_requirement("REQ-2", "MKQA-1", "기존 비밀번호 확인이 필요하다")

    conditions = plan_conditions(req)

    assert len(conditions) == 1
    assert conditions[0]["boundary"] is None
    assert conditions[0]["planning_status"] == "CONDITION_PLANNING_UNRESOLVED"
    assert conditions[0]["status"] == "MISSING_TC"


def test_plan_conditions_never_splits_needs_clarification_even_with_numbers():
    """정책이 미확정이면 문장에 숫자가 있어도 경계값을 추론하지 않는다."""
    req = make_requirement(
        "CLARIFY-1", "MKQA-1", "잠금 해제까지 5분에서 30분 사이로 하면 될까?",
        status="NEEDS_CLARIFICATION", source_type="POLICY_GAP",
    )

    conditions = plan_conditions(req)

    assert len(conditions) == 1
    assert conditions[0]["status"] == "NEEDS_CLARIFICATION"
    assert conditions[0]["planning_status"] == "CONDITION_PLANNING_UNRESOLVED"
    assert conditions[0]["boundary"] is None
