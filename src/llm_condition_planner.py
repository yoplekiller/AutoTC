"""
LLM 기반 조건 설계 — condition_planner v1(규칙 기반)이 못 쪼갠 자유서술형 요구사항을 LLM으로
검증 조건 여러 개로 분할하고, 생성된 TC를 그 조건에 매핑한다 (AutoTC 2.0, 2026-09-28).

v1과의 역할 분담:
  - STRUCTURED(숫자 범위 경계값): v1 결과를 그대로 둔다 — 규칙으로 확정된 걸 LLM이 덮어쓰지 않음
  - NEEDS_CLARIFICATION(정책 미확정): 절대 분할하지 않는다 — 미확정 문장에서 조건을 뽑는 건 근거 없는 추론
  - CONDITION_PLANNING_UNRESOLVED + 요구사항 확정: 이 모듈의 대상. 결과는 planning_status="LLM_INFERRED"

실패 원칙: LLM 호출 실패/응답 형식 오류/검증 탈락은 전부 v1 결과로 조용히 폴백한다.
조건 설계는 TC 생성의 부가 단계이므로 여기서 예외를 올려 티켓 처리 전체를 깨뜨리지 않는다.

비용: 티켓당 최대 2회 호출(조건 설계 1회 + TC 매핑 1회), 대상 요구사항이 없으면 0회.
"""

import json
import re
import time

from groq import Groq

from schemas import make_test_condition
from utils import rate_limit_wait_seconds

MODEL = "openai/gpt-oss-120b"
MAX_CONDITIONS_PER_REQUIREMENT = 6


def _chat_json(groq_client: Groq, system: str, prompt: str, max_tokens: int) -> dict | None:
    """JSON 응답 1회 호출. 분당 Rate Limit만 재시도하고, 그 외 실패는 None(=폴백 신호)으로 돌려준다."""
    for attempt in range(3):
        try:
            response = groq_client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                reasoning_effort="low",
                response_format={"type": "json_object"},
            )
            break
        except Exception as e:
            e_str = str(e).lower()
            if ("rate_limit" in e_str or "429" in str(e)) and "per day" not in e_str and "tpd" not in e_str:
                wait = rate_limit_wait_seconds(e, attempt)
                print(f"    [Rate Limit/분당] {wait}초 대기 후 재시도...")
                time.sleep(wait)
                continue
            print(f"    [경고] LLM 조건 설계 호출 실패 — 규칙 기반 결과로 폴백: {e}")
            return None
    else:
        print("    [경고] LLM 조건 설계 Rate Limit 재시도 소진 — 규칙 기반 결과로 폴백")
        return None

    raw = (response.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"    [경고] LLM 조건 설계 응답 파싱 실패 (응답: {raw[:100]}) — 규칙 기반 결과로 폴백")
        return None
    return data if isinstance(data, dict) else None


def _optional_text(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def refine_conditions_with_llm(groq_client: Groq, requirements: list, conditions: list) -> list:
    """UNRESOLVED 조건(자유서술형 + 확정 요구사항)만 LLM으로 여러 검증 조건으로 분할한다.

    요구사항별로 독립 판정한다 — LLM이 일부 요구사항만 제대로 답해도 그 요구사항만 교체하고
    나머지는 v1 조건 1개를 유지한다. TC를 보여주지 않는다(기존 TC에 맞춰 조건을 역으로 만들면
    GAP 탐지 의미가 사라지므로 조건은 요구사항 원문만 보고 설계한다).
    """
    ok_req_ids = {r["requirement_id"] for r in requirements if r["status"] == "OK"}
    targets = [
        c for c in conditions
        if c["planning_status"] == "CONDITION_PLANNING_UNRESOLVED" and c["requirement_id"] in ok_req_ids
    ]
    if not targets:
        return conditions

    req_text = {r["requirement_id"]: r["original_text"] for r in requirements}
    req_block = "\n".join(f"- {c['requirement_id']}: {req_text[c['requirement_id']]}" for c in targets)

    prompt = f"""아래 요구사항 각각을 서로 독립적으로 검증 가능한 테스트 조건으로 분할하세요.

[요구사항]
{req_block}

[규칙]
- 요구사항 문장에 근거가 있는 조건만 만드세요. 문장에 없는 기능/정책/수치를 지어내지 마세요.
- 한 조건 = 하나의 관찰 가능한 결과. 같은 검증 목적을 표현만 바꿔 중복으로 만들지 마세요.
- 정상 흐름과, 문장에서 직접 도출되는 실패/거부 흐름을 구분하세요(예: "기존 비밀번호 확인" → 일치 시 진행 / 불일치 시 거부).
- 더 쪼갤 근거가 없으면 조건 1개만 반환해도 됩니다. 요구사항당 최대 {MAX_CONDITIONS_PER_REQUIREMENT}개.
- purpose는 한 문장, oracle은 확인할 관찰 대상. 요구사항에 명시되지 않은 화면 문구/메시지 내용/부가 동작을 지어내지 마세요 — 문구가 명시 안 됐으면 "오류 메시지 노출", "알림 메일 수신"처럼 관찰 대상만 쓰세요.

아래 JSON 형식으로만 응답하세요.
{{"conditions": {{"REQ-번호": [{{"purpose": "...", "input_partition": "...", "initial_state": "...", "event": "...", "oracle": "..."}}]}}}}
input_partition/initial_state/event는 해당 없으면 null."""

    data = _chat_json(
        groq_client,
        system=(
            "당신은 경력 10년차 시니어 QA 엔지니어입니다. 요구사항을 테스트 조건(Test Condition)으로 분할합니다. "
            "요구사항 원문에 근거 없는 조건은 절대 만들지 않습니다. JSON만 출력하세요."
        ),
        prompt=prompt,
        max_tokens=2500,
    )
    planned = (data or {}).get("conditions")
    if not isinstance(planned, dict):
        return conditions

    replacements = {}
    for target in targets:
        req_id = target["requirement_id"]
        items = planned.get(req_id)
        if not isinstance(items, list):
            continue
        new_conditions = []
        for item in items[:MAX_CONDITIONS_PER_REQUIREMENT]:
            if not isinstance(item, dict) or not _optional_text(item.get("purpose")):
                continue
            new_conditions.append(
                make_test_condition(
                    condition_id=f"COND-{req_id}-L{len(new_conditions) + 1}",
                    requirement_id=req_id,
                    purpose=str(item["purpose"]),
                    status="MISSING_TC",
                    planning_status="LLM_INFERRED",
                    input_partition=_optional_text(item.get("input_partition")),
                    initial_state=_optional_text(item.get("initial_state")),
                    event=_optional_text(item.get("event")),
                    oracle=_optional_text(item.get("oracle")),
                )
            )
        if new_conditions:
            replacements[target["condition_id"]] = new_conditions

    refined = []
    for c in conditions:
        refined.extend(replacements.get(c["condition_id"], [c]))
    return refined


def assign_test_cases_with_llm(groq_client: Groq, conditions: list, tc_list: list) -> dict:
    """LLM_INFERRED 조건이 2개 이상인 요구사항을 참조하는 TC를 조건에 매핑한다 → {tc_id: condition_id}.

    여기서 돌려준 매핑은 link_test_cases가 "TC가 참조한 요구사항 소속 조건인지" 다시 검증한 뒤에만
    쓴다. 이 함수에서도 존재하지 않는 조건 id / TC가 참조하지 않은 요구사항의 조건은 미리 버린다.
    """
    llm_by_req: dict = {}
    for c in conditions:
        if c["planning_status"] == "LLM_INFERRED":
            llm_by_req.setdefault(c["requirement_id"], []).append(c)
    multi_reqs = {req_id for req_id, conds in llm_by_req.items() if len(conds) > 1}
    if not multi_reqs:
        return {}

    target_tcs = [
        tc for tc in tc_list
        if tc.get("tc_id") and multi_reqs.intersection(tc.get("requirement_refs") or [])
    ]
    if not target_tcs:
        return {}

    cond_block = "\n".join(
        f"- {c['condition_id']} ({c['requirement_id']}): {c['purpose']}"
        + (f" / 확인: {c['oracle']}" if c.get("oracle") else "")
        for req_id in sorted(multi_reqs) for c in llm_by_req[req_id]
    )
    tc_block = "\n".join(
        f"- {tc['tc_id']} (참조: {', '.join(tc.get('requirement_refs') or [])}): "
        f"{tc.get('테스트시나리오', '')} / 기대결과: {tc.get('기대결과', '')}"
        for tc in target_tcs
    )
    prompt = f"""각 TC가 아래 테스트 조건 중 정확히 어느 것을 검증하는지 매핑하세요.

[테스트 조건]
{cond_block}

[TC]
{tc_block}

[규칙]
- TC가 참조한 요구사항에 속한 조건 중에서만 고르세요.
- 기대결과가 조건의 확인 내용과 실제로 일치할 때만 매핑하세요. 애매하면 null.

아래 JSON 형식으로만 응답하세요.
{{"assignments": {{"TC_001": "COND-REQ-1-L1", "TC_002": null}}}}"""

    data = _chat_json(
        groq_client,
        system="당신은 QA 리드입니다. TC와 테스트 조건의 추적성을 매핑합니다. 근거가 약하면 null로 둡니다. JSON만 출력하세요.",
        prompt=prompt,
        max_tokens=1500,
    )
    raw_assignments = (data or {}).get("assignments")
    if not isinstance(raw_assignments, dict):
        return {}

    req_of_condition = {c["condition_id"]: c["requirement_id"] for c in conditions}
    refs_of_tc = {tc["tc_id"]: set(tc.get("requirement_refs") or []) for tc in target_tcs}
    assignments = {}
    for tc_id, condition_id in raw_assignments.items():
        if tc_id not in refs_of_tc or condition_id not in req_of_condition:
            continue
        if req_of_condition[condition_id] in refs_of_tc[tc_id]:
            assignments[tc_id] = condition_id
    return assignments


def plan_conditions_with_llm(groq_client: Groq, requirements: list, conditions: list, tc_list: list) -> tuple:
    """진입점 3곳(generate_tc/watch_sheet/generate_tc_from_spec)에서 쓰는 한 줄짜리 묶음.

    → (정제된 conditions, link_test_cases에 넘길 condition_assignments)
    """
    refined = refine_conditions_with_llm(groq_client, requirements, conditions)
    inferred = sum(1 for c in refined if c["planning_status"] == "LLM_INFERRED")
    if inferred:
        print(f"  [LLM 조건 설계] 자유서술형 요구사항 → 조건 {inferred}개로 분할")
    assignments = assign_test_cases_with_llm(groq_client, refined, tc_list)
    if assignments:
        print(f"  [LLM 조건 설계] TC {len(assignments)}개를 분할된 조건에 매핑")
    return refined, assignments
