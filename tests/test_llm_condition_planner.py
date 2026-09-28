import json
from types import SimpleNamespace

from llm_condition_planner import (
    assign_test_cases_with_llm,
    plan_conditions_with_llm,
    refine_conditions_with_llm,
)
from schemas import make_requirement
from traceability import build_conditions, compute_coverage_report, link_test_cases


class FakeGroq:
    """chat.completions.create가 미리 넣어둔 응답을 순서대로 돌려주는 가짜 Groq 클라이언트."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _requirements():
    return [
        make_requirement("REQ-1", "MKQA-1", "비밀번호는 8자 이상 20자 이하만 허용한다"),
        make_requirement("REQ-2", "MKQA-1", "비밀번호 변경 시 기존 비밀번호를 확인한다"),
        make_requirement(
            "CLARIFY-1", "MKQA-1", "변경 후 다른 기기 세션도 로그아웃할까?",
            status="NEEDS_CLARIFICATION", source_type="POLICY_GAP",
        ),
    ]


_REQ2_SPLIT = {
    "conditions": {
        "REQ-2": [
            {"purpose": "기존 비밀번호 일치 시 변경 진행", "oracle": "새 비밀번호 입력 화면 노출"},
            {"purpose": "기존 비밀번호 불일치 시 거부", "oracle": "'비밀번호가 일치하지 않습니다' 문구"},
        ]
    }
}


def test_refine_splits_only_unresolved_confirmed_requirements():
    reqs = _requirements()
    groq = FakeGroq(_REQ2_SPLIT)

    conditions = refine_conditions_with_llm(groq, reqs, build_conditions(reqs))

    by_req = {}
    for c in conditions:
        by_req.setdefault(c["requirement_id"], []).append(c)
    # 규칙 기반 경계값(REQ-1)과 정책 미확정(CLARIFY-1)은 그대로
    assert [c["planning_status"] for c in by_req["REQ-1"]] == ["STRUCTURED"] * 4
    assert len(by_req["CLARIFY-1"]) == 1 and by_req["CLARIFY-1"][0]["status"] == "NEEDS_CLARIFICATION"
    # 자유서술형 REQ-2만 LLM이 분할
    assert [c["condition_id"] for c in by_req["REQ-2"]] == ["COND-REQ-2-L1", "COND-REQ-2-L2"]
    assert all(c["planning_status"] == "LLM_INFERRED" for c in by_req["REQ-2"])
    assert by_req["REQ-2"][1]["oracle"] == "'비밀번호가 일치하지 않습니다' 문구"
    # LLM에는 대상 요구사항만 보낸다 — 경계값/미확정 문장은 프롬프트에 안 들어감
    assert "REQ-2" in groq.prompts[0]
    assert "REQ-1:" not in groq.prompts[0] and "CLARIFY-1" not in groq.prompts[0]


def test_refine_skips_llm_call_when_nothing_to_refine():
    reqs = [make_requirement("REQ-1", "MKQA-1", "비밀번호는 8자 이상 20자 이하만 허용한다")]
    groq = FakeGroq()  # 호출되면 pop에서 IndexError

    conditions = refine_conditions_with_llm(groq, reqs, build_conditions(reqs))

    assert len(conditions) == 4
    assert groq.prompts == []


def test_refine_falls_back_to_rule_based_on_llm_failure_or_bad_output():
    reqs = _requirements()
    baseline = build_conditions(reqs)

    for response in (RuntimeError("boom"), "not json", {"conditions": {"REQ-2": []}}, {"conditions": "x"}):
        conditions = refine_conditions_with_llm(FakeGroq(response), reqs, build_conditions(reqs))
        assert [c["condition_id"] for c in conditions] == [c["condition_id"] for c in baseline]


def test_refine_ignores_unknown_requirement_ids_and_empty_purposes():
    reqs = _requirements()
    groq = FakeGroq({
        "conditions": {
            "REQ-2": [{"purpose": ""}, {"purpose": "불일치 시 거부"}],
            "REQ-99": [{"purpose": "지어낸 요구사항"}],
            "CLARIFY-1": [{"purpose": "미확정 정책을 조건으로 만듦"}],
        }
    })

    conditions = refine_conditions_with_llm(groq, reqs, build_conditions(reqs))

    ids = [c["condition_id"] for c in conditions]
    assert "COND-REQ-2-L1" in ids and "COND-REQ-2-L2" not in ids
    assert not any("REQ-99" in i for i in ids)
    assert "COND-CLARIFY-1" in ids  # 미확정 정책은 분할되지 않고 그대로


def _tc(tc_id, refs, scenario):
    return {"tc_id": tc_id, "requirement_refs": refs, "테스트시나리오": scenario, "기대결과": "-"}


def test_assignments_are_validated_against_tc_requirement_refs():
    reqs = _requirements()
    conditions = refine_conditions_with_llm(FakeGroq(_REQ2_SPLIT), reqs, build_conditions(reqs))
    tcs = [
        _tc("TC_001", ["REQ-2"], "기존 비밀번호 일치"),
        _tc("TC_002", ["REQ-2"], "기존 비밀번호 불일치"),
        _tc("TC_003", ["REQ-1"], "8자 입력"),
    ]
    groq = FakeGroq({"assignments": {
        "TC_001": "COND-REQ-2-L1",
        "TC_002": "COND-REQ-2-L9",   # 존재하지 않는 조건
        "TC_003": "COND-REQ-2-L2",   # REQ-2를 참조하지도 않은 TC (대상 아님)
        "TC_404": "COND-REQ-2-L2",   # 존재하지 않는 TC
    }})

    assignments = assign_test_cases_with_llm(groq, conditions, tcs)

    assert assignments == {"TC_001": "COND-REQ-2-L1"}
    # 매핑 프롬프트에는 분할된 요구사항을 참조한 TC만 들어간다
    assert "TC_003" not in groq.prompts[0]


def test_end_to_end_coverage_exposes_uncovered_llm_condition():
    """자유서술형 요구사항에 TC가 1개만 있어도 예전엔 COVERED였지만, 분할 후엔 빈 조건이 GAP으로 드러난다."""
    reqs = _requirements()
    tcs = [_tc("TC_001", ["REQ-2"], "기존 비밀번호 일치 시 변경")]
    groq = FakeGroq(_REQ2_SPLIT, {"assignments": {"TC_001": "COND-REQ-2-L1"}})

    conditions, assignments = plan_conditions_with_llm(groq, reqs, build_conditions(reqs), tcs)
    conditions, tcs, invalid = link_test_cases(conditions, reqs, tcs, condition_assignments=assignments)
    report = compute_coverage_report(conditions, invalid)

    status = {c["condition_id"]: c["status"] for c in conditions}
    assert status["COND-REQ-2-L1"] == "COVERED"
    assert status["COND-REQ-2-L2"] == "MISSING_TC"
    assert tcs[0]["condition_id"] == "COND-REQ-2-L1"
    assert report["by_status"]["COVERED"] == 1


def test_link_rejects_assignment_to_condition_of_unreferenced_requirement():
    reqs = _requirements()
    conditions = refine_conditions_with_llm(FakeGroq(_REQ2_SPLIT), reqs, build_conditions(reqs))
    tcs = [_tc("TC_001", ["REQ-1"], "8자 입력")]

    _, tcs, _ = link_test_cases(
        conditions, reqs, tcs, condition_assignments={"TC_001": "COND-REQ-2-L1"}
    )

    # REQ-2 조건 매핑은 거부되고, 기존 규칙(경계값 8 리터럴 매칭)으로 판정된다
    assert tcs[0]["condition_id"] == "COND-REQ-1-MIN"
