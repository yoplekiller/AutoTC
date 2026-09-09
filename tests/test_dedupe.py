from tc_core import dedupe_tc_list


def _tc(tc_id, scenario, expected, **extra):
    return {"tc_id": tc_id, "테스트시나리오": scenario, "기대결과": expected, **extra}


def test_similar_boundary_tcs_are_both_preserved():
    """경계값 7(하한 미만)과 21(상한 초과) TC는 시나리오/기대결과 문장이 같아도 둘 다 남아야 한다."""
    tc_list = [
        _tc(
            "TC_001", "허용하지 않는 길이의 비밀번호 등록이 차단됨", "1. 비밀번호 등록이 거부됨",
            테스트단계="1. 길이 7인 비밀번호로 등록 시도함",
        ),
        _tc(
            "TC_002", "허용하지 않는 길이의 비밀번호 등록이 차단됨", "1. 비밀번호 등록이 거부됨",
            테스트단계="1. 길이 21인 비밀번호로 등록 시도함",
        ),
    ]

    result = dedupe_tc_list(tc_list)

    assert len(result) == 2  # 물리 삭제되지 않음
    assert {tc["tc_id"] for tc in result} == {"TC_001", "TC_002"}
    assert result[0]["dedupe_status"] == "UNIQUE"
    assert result[1]["dedupe_status"] == "POSSIBLE_DUPLICATE"
    assert result[1]["duplicate_of"] == "TC_001"


def test_dissimilar_tcs_are_all_unique():
    tc_list = [
        _tc("TC_001", "로그인 성공", "1. 메인화면 진입됨"),
        _tc("TC_002", "회원가입 성공", "1. 가입완료 메시지 노출됨"),
    ]

    result = dedupe_tc_list(tc_list)

    assert len(result) == 2
    assert all(tc["dedupe_status"] == "UNIQUE" for tc in result)
    assert all(tc["duplicate_of"] is None for tc in result)
