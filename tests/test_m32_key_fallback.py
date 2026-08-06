"""M3-2 08 폴백과 09 엄격 키 계약 테스트."""
from __future__ import annotations

from scripts.golden_score_contract import 행, 키
from scripts.golden_score_matching import 매칭하기


def _행(번호: int, **값) -> 행:
    return 행(번호=번호, 값=값)


def test_관리번호가_있는_08_09_엄격키는_기존_계약을_유지한다() -> None:
    assert 키(
        _행(1, 관리번호="R-01", 부문="건물", 사업명="효율 개선"),
        "08_감축사업목록",
        False,
    ) == "관리번호=R1"
    assert 키(
        _행(2, 관리번호="R-01", 사업명="효율 개선", 연도="2030"),
        "09_연차별이행계획",
        False,
    ) == "관리번호=R1|연도=2030"


def test_관리번호가_빈_08_엄격키는_사업정보로_폴백한다() -> None:
    assert 키(
        _행(1, 관리번호="", 부문="건물", 사업명="효율 개선"),
        "08_감축사업목록",
        False,
    ) == "부문=건물|사업명=효율개선"


def test_관리번호가_빈_09_엄격키는_없고_완화티어가_담당한다() -> None:
    golden_rows = [_행(1, 관리번호="", 사업명="효율 개선", 연도=2030)]
    output_rows = [_행(1, 관리번호=None, 사업명="효율개선", 연도="2030")]

    assert 키(golden_rows[0], "09_연차별이행계획", False) is None
    assert 키(output_rows[0], "09_연차별이행계획", False) is None

    matches, remaining_golden, remaining_output, *_ = 매칭하기(
        golden_rows, output_rows, "09_연차별이행계획"
    )
    assert [match.방식 for match in matches] == ["완화"]
    assert remaining_golden == set()
    assert remaining_output == set()


def test_08_09_관리번호_보유행과_빈행은_계약별_티어로_매칭한다() -> None:
    cases = {
        "08_감축사업목록": (
            [
                _행(1, 관리번호="R-01", 부문="건물", 사업명="효율 개선"),
                _행(2, 관리번호="", 부문="수송", 사업명="대중교통 확대"),
            ],
            [
                _행(1, 관리번호="R1", 부문="건물", 사업명="효율개선"),
                _행(2, 관리번호=None, 부문="수송", 사업명="대중교통확대"),
            ],
            ["엄격", "엄격"],
        ),
        "09_연차별이행계획": (
            [
                _행(1, 관리번호="R-01", 사업명="효율 개선", 연도=2030),
                _행(2, 관리번호="", 사업명="대중교통 확대", 연도=2031),
            ],
            [
                _행(1, 관리번호="R1", 사업명="효율개선", 연도="2030"),
                _행(2, 관리번호=None, 사업명="대중교통확대", 연도="2031"),
            ],
            ["엄격", "완화"],
        ),
    }

    for sheet_name, (golden_rows, output_rows, expected_tiers) in cases.items():
        matches, remaining_golden, remaining_output, *_ = 매칭하기(
            golden_rows, output_rows, sheet_name
        )
        assert [match.방식 for match in matches] == expected_tiers
        assert remaining_golden == set()
        assert remaining_output == set()


def test_08_09_이차키와_12_엄격폴백은_변하지_않는다() -> None:
    assert 키(
        _행(1, 관리번호="R-01", 사업명="효율 개선"),
        "08_감축사업목록",
        True,
    ) == "사업명=효율개선"
    assert 키(
        _행(2, 관리번호="R-01", 사업명="효율 개선", 연도=2030),
        "09_연차별이행계획",
        True,
    ) == "사업명=효율개선|연도=2030"
    assert 키(
        _행(3, 과제ID="", 대응기반영역="기반", 과제명="역량 강화"),
        "12_대응기반강화",
        False,
    ) == "대응기반영역=기반|과제명=역량강화"
