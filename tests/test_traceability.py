from traceability import (
    build_conditions,
    compute_coverage_report,
    is_generation_complete,
    link_test_cases,
    parse_requirements,
)

SAMPLE_AUGMENTED_SPEC = """1. 기능 목적
비밀번호 변경 기능

2. 주요 기능 요구사항
REQ-1. 비밀번호는 8자 이상 20자 이하만 허용한다
REQ-2. 비밀번호 변경 시 기존 비밀번호 확인이 필요하다
REQ-3. 변경 완료 시 알림 메일을 발송한다

3. 예외/비정상 케이스
REQ-4. 기존 비밀번호가 틀리면 변경이 거부된다

4. 보안·권한 고려사항
해당 없음

5. 확인이 필요한 질문
- 비밀번호 변경 후 다른 기기 세션은 강제 로그아웃되는가?
- 알림 메일 발송 실패 시 재시도 정책은?
"""


def _tc(tc_id, refs, quality_status="PASS", **fields):
    base = {
        "tc_id": tc_id,
        "테스트시나리오": f"{tc_id} 시나리오",
        "기대결과": f"1. {tc_id} 결과 확인됨",
        "requirement_refs": refs,
        "quality_status": quality_status,
    }
    base.update(fields)
    return base


def _conditions_for_req(conditions, req_id):
    return [c for c in conditions if c["requirement_id"] == req_id]


def _boundary_tc(tc_id, value, scenario="비밀번호 길이 경계값 검증", quality_status="PASS"):
    """REQ-1(8~20자)의 특정 경계값을 검증하는 TC. 시나리오/기대결과는 의도적으로 동일하게 두고
    (dedupe가 예전엔 여기서 물리 삭제했던 케이스), 실제 입력값만 테스트단계에 리터럴로 남긴다.
    """
    return _tc(
        tc_id, ["REQ-1"], quality_status=quality_status,
        테스트시나리오=scenario,
        기대결과="1. 처리 결과가 확인됨",
        테스트단계=f"1. 길이 {value}인 비밀번호로 등록을 시도함",
    )


# ── 요구사항 파싱 ───────────────────────────────────────────────────

def test_parse_requirements_splits_explicit_and_clarification():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")

    req_ids = [r["requirement_id"] for r in requirements]
    assert req_ids == ["REQ-1", "REQ-2", "REQ-3", "REQ-4", "CLARIFY-1", "CLARIFY-2"]

    explicit = [r for r in requirements if r["requirement_id"].startswith("REQ-")]
    assert all(r["status"] == "OK" for r in explicit)
    assert all(r["source_type"] == "EXPLICIT_REQUIREMENT" for r in explicit)
    assert "8자 이상 20자 이하" in explicit[0]["original_text"]
    # REQ-4 본문이 뒤 섹션(보안/확인이 필요한 질문)을 삼키지 않아야 한다
    assert "확인이 필요한 질문" not in explicit[3]["original_text"]
    assert "다른 기기 세션" not in explicit[3]["original_text"]

    clarifications = [r for r in requirements if r["requirement_id"].startswith("CLARIFY-")]
    assert all(r["status"] == "NEEDS_CLARIFICATION" for r in clarifications)
    assert all(r["source_type"] == "POLICY_GAP" for r in clarifications)
    assert "강제 로그아웃" in clarifications[0]["original_text"]


# ── 필수 테스트 1: 8~20 → 7/8/20/21 네 조건 생성 ─────────────────────

def test_length_requirement_generates_four_boundary_conditions():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    req1_conditions = _conditions_for_req(conditions, "REQ-1")
    assert len(req1_conditions) == 4
    values = {c["boundary"]["value"] for c in req1_conditions}
    assert values == {7, 8, 20, 21}
    assert all(c["requirement_id"] == "REQ-1" for c in req1_conditions)
    assert len({c["condition_id"] for c in req1_conditions}) == 4  # 전부 고유 ID


# ── 필수 테스트 2: 20 조건에 TC가 없으면 MISSING_TC ──────────────────

def test_boundary_condition_without_tc_is_missing():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    # 7, 8, 21은 TC로 커버하고 20(MAX)만 비워둔다
    tc_list = [
        _boundary_tc("TC-001", 7),
        _boundary_tc("TC-002", 8),
        _boundary_tc("TC-003", 21),
    ]
    conditions, tc_list, invalid_refs = link_test_cases(conditions, requirements, tc_list)

    by_type = {c["boundary"]["type"]: c for c in _conditions_for_req(conditions, "REQ-1")}
    assert by_type["BELOW_MIN"]["status"] == "COVERED"
    assert by_type["MIN"]["status"] == "COVERED"
    assert by_type["MAX"]["status"] == "MISSING_TC"
    assert by_type["ABOVE_MAX"]["status"] == "COVERED"
    assert invalid_refs == []

    report = compute_coverage_report(conditions, invalid_refs)
    assert report["by_status"]["COVERED"] == 3
    assert report["by_status"]["MISSING_TC"] == 4  # REQ-1의 MAX(20) + REQ-2/3/4


# ── 필수 테스트 3: 7과 21 TC는 문장이 비슷해도 서로 다른 조건에 남는다 ──

def test_similar_boundary_tcs_map_to_different_conditions_and_are_preserved():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    # 시나리오/기대결과 문장은 동일 — 예전 dedupe였으면 하나가 삭제됐을 케이스
    tc_list = [
        _boundary_tc("TC-001", 7, scenario="허용하지 않는 길이의 비밀번호 등록이 차단됨"),
        _boundary_tc("TC-002", 21, scenario="허용하지 않는 길이의 비밀번호 등록이 차단됨"),
    ]
    conditions, tc_list, invalid_refs = link_test_cases(conditions, requirements, tc_list)

    assert len(tc_list) == 2  # 둘 다 보존됨 (물리 삭제 없음)
    assert tc_list[0]["condition_id"] != tc_list[1]["condition_id"]
    by_type = {c["boundary"]["type"]: c["condition_id"] for c in _conditions_for_req(conditions, "REQ-1")}
    assert tc_list[0]["condition_id"] == by_type["BELOW_MIN"]
    assert tc_list[1]["condition_id"] == by_type["ABOVE_MAX"]


# ── 필수 테스트 4: 파싱 불가능한 자유서술 requirement는 임의 조건으로 안 쪼개진다 ──

def test_free_text_requirement_stays_single_unresolved_condition():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    for req_id in ("REQ-2", "REQ-3", "REQ-4"):
        req_conditions = _conditions_for_req(conditions, req_id)
        assert len(req_conditions) == 1
        assert req_conditions[0]["boundary"] is None
        assert req_conditions[0]["planning_status"] == "CONDITION_PLANNING_UNRESOLVED"


# ── 필수 테스트 5: 기존 단순(비-경계값) requirement의 커버리지 흐름은 그대로 ──

def test_simple_requirement_coverage_flow_unchanged():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    tc_list = [_tc("TC-001", ["REQ-2"])]  # REQ-3, REQ-4는 커버 안 함
    conditions, tc_list, invalid_refs = link_test_cases(conditions, requirements, tc_list)

    def _status(req_id):
        return _conditions_for_req(conditions, req_id)[0]["status"]

    assert _status("REQ-2") == "COVERED"
    assert _status("REQ-3") == "MISSING_TC"
    assert _status("REQ-4") == "MISSING_TC"
    assert invalid_refs == []
    assert tc_list[0]["condition_id"] == _conditions_for_req(conditions, "REQ-2")[0]["condition_id"]


def test_needs_clarification_condition_ignores_tc_presence():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    tc_list = []
    conditions, tc_list, _ = link_test_cases(conditions, requirements, tc_list)

    assert _conditions_for_req(conditions, "CLARIFY-1")[0]["status"] == "NEEDS_CLARIFICATION"
    assert _conditions_for_req(conditions, "CLARIFY-2")[0]["status"] == "NEEDS_CLARIFICATION"


def test_invalid_requirement_reference_is_detected():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    tc_list = [_tc("TC-001", ["REQ-999"])]  # 존재하지 않는 요구사항 참조
    conditions, tc_list, invalid_refs = link_test_cases(conditions, requirements, tc_list)

    assert invalid_refs == [{"tc_id": "TC-001", "requirement_id": "REQ-999"}]
    assert tc_list[0]["condition_id"] is None  # 유효한 참조가 없으므로 조건에 연결되지 않음
    # REQ-999를 참조했다고 실제 REQ-2 조건이 잘못 COVERED 되면 안 된다
    assert _conditions_for_req(conditions, "REQ-2")[0]["status"] == "MISSING_TC"


def test_rejected_tc_does_not_count_as_covered():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    tc_list = [_tc("TC-001", ["REQ-2"], quality_status="REJECT")]
    conditions, tc_list, _ = link_test_cases(conditions, requirements, tc_list)

    assert _conditions_for_req(conditions, "REQ-2")[0]["status"] == "MISSING_TC"


def test_ambiguous_boundary_tc_is_not_linked_to_any_condition():
    """TC 텍스트에 경계값이 2개 이상 등장하면(모호함) 임의로 하나를 골라 연결하지 않는다."""
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)

    tc = _tc(
        "TC-001", ["REQ-1"],
        테스트단계="1. 길이 7과 길이 21 두 경우를 모두 입력해봄",
    )
    conditions, tc_list, _ = link_test_cases(conditions, requirements, [tc])

    assert tc_list[0]["condition_id"] is None


def test_coverage_report_counts_by_status():
    requirements = parse_requirements(SAMPLE_AUGMENTED_SPEC, source_id="MKQA-1")
    conditions = build_conditions(requirements)
    tc_list = [_boundary_tc("TC-001", 8)]  # REQ-1의 MIN(8)만 커버
    conditions, tc_list, invalid_refs = link_test_cases(conditions, requirements, tc_list)

    report = compute_coverage_report(conditions, invalid_refs)

    assert report["total_conditions"] == 9  # REQ-1(4) + REQ-2/3/4(1씩) + CLARIFY-1/2(1씩)
    assert report["by_status"]["COVERED"] == 1
    assert report["by_status"]["MISSING_TC"] == 6  # REQ-1의 나머지 3개 + REQ-2/3/4
    assert report["by_status"]["NEEDS_CLARIFICATION"] == 2


def test_zero_test_cases_is_not_treated_as_complete():
    assert is_generation_complete([]) is False
    assert is_generation_complete([_tc("TC-001", ["REQ-2"])]) is True
