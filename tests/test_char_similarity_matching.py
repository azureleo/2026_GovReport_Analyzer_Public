"""02_지역여건 문자유사 4차 매칭 회귀 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.golden_score_contract import CHAR_SIMILARITY_MIN_JACCARD, 행
from scripts.golden_score_matching import _문자유사도
from tests.test_score_against_golden import _점수
from tests.test_semantic_relax_matching import _지역여건행, _출력행


def test_붙여쓰기와_합성명_구조차를_문자유사로_매칭한다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업",
        지표세부범주="수송·교통",
        지표명="자동차 등록 대수(휘발유)",
        연도=2013,
        값=1622,
    )
    output = _출력행(
        지표범주="인문사회",
        지표세부범주="연료별 자동차 등록대수",
        지표명="휘발유 차량",
        연도=2013,
        값=1622,
    )

    assert _문자유사도(행(2, golden), 행(2, output)) >= CHAR_SIMILARITY_MIN_JACCARD
    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})

    sheet = result["시트별"]["02_지역여건"]
    assert sheet["의미완화매칭수"] == 0
    assert sheet["문자유사매칭수"] == 1


@pytest.mark.parametrize(
    "변경값",
    [
        {"값": 1700},
        {"연도": 2014},
        {"단위": "명"},
    ],
    ids=["값 불일치", "연도 불일치", "단위 상충"],
)
def test_문자유사가_높아도_전제를_충족하지_않으면_매칭하지_않는다(
    tmp_path: Path, 변경값: dict
) -> None:
    golden = _지역여건행(
        지표범주="경제산업",
        지표세부범주="수송·교통",
        지표명="자동차 등록 대수(휘발유)",
        연도=2013,
        값=1622,
    )
    output_values = dict(
        지표범주="인문사회",
        지표세부범주="연료별",
        지표명="자동차등록대수휘발유",
        연도=2013,
        값=1622,
    )
    output_values.update(변경값)
    output = _출력행(**output_values)

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})

    assert result["시트별"]["02_지역여건"]["문자유사매칭수"] == 0


def test_같은_골든의_최고_문자유사도_출력이_둘이면_매칭하지_않는다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업",
        지표세부범주="수송·교통",
        지표명="자동차 등록 대수(휘발유)",
        연도=2013,
        값=1622,
    )
    output = _출력행(
        지표범주="인문사회",
        지표세부범주="연료별 자동차 등록대수",
        지표명="휘발유 차량",
        연도=2013,
        값=1622,
    )

    result = _점수(tmp_path, {"02_지역여건": [output, output]}, {"02_지역여건": [golden]})

    assert result["시트별"]["02_지역여건"]["문자유사매칭수"] == 0


def test_의미완화가_소비한_행은_문자유사_후보에서_제외한다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업",
        지표세부범주="수송·교통",
        지표명="자동차 등록 대수(휘발유)",
        연도=2013,
        값=1622,
    )
    output = _출력행(
        지표범주="인문사회",
        지표세부범주="교통",
        지표명="자동차 등록 대수",
        연도=2013,
        값=1622,
        단위="천 대",
    )

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})
    sheet = result["시트별"]["02_지역여건"]

    assert sheet["의미완화매칭수"] == 1
    assert sheet["문자유사매칭수"] == 0


def test_문자유사는_기존_헤드라인을_바꾸지_않고_별도_누적_보고한다(tmp_path: Path) -> None:
    char_golden = _지역여건행(
        지표범주="경제산업",
        지표세부범주="수송·교통",
        지표명="자동차 등록 대수(휘발유)",
        연도=2013,
        값=1622,
    )
    char_output = _출력행(
        지표범주="인문사회",
        지표세부범주="연료별 자동차 등록대수",
        지표명="휘발유 차량",
        연도=2013,
        값=1622,
    )
    observed_golden = _지역여건행(
        지표범주="관찰",
        지표세부범주="",
        지표명="abcdef",
        연도=2020,
        값=99,
    )
    observed_output = _출력행(
        지표범주="관찰후보",
        지표세부범주="",
        지표명="abcxyz",
        연도=2020,
        값=99,
    )

    result = _점수(
        tmp_path,
        {"02_지역여건": [char_output, observed_output]},
        {"02_지역여건": [char_golden, observed_golden]},
    )
    sheet = result["시트별"]["02_지역여건"]

    assert {
        "매칭수": sheet["매칭수"],
        "리콜": sheet["리콜"],
        "정밀도": sheet["정밀도"],
        "값일치율": sheet["값일치율"],
        "완화매칭수": sheet["완화매칭수"],
        "의미완화매칭수": sheet["의미완화매칭수"],
    } == {
        "매칭수": 0,
        "리콜": 0.0,
        "정밀도": 0.0,
        "값일치율": None,
        "완화매칭수": 0,
        "의미완화매칭수": 0,
    }
    assert result["문자유사매칭수"] == 1
    assert result["문자유사매칭쌍"][0]["유사도"].startswith("자카드=")
    assert len(result["문자유사관찰쌍"]) == 1
    assert result["문자유사관찰쌍"][0]["유사도"] == "자카드=0.2500"
    assert result["출처유형별_전체"]["시각 유래"]["매칭수"] == 0
    assert result["출처유형별_전체_의미완화포함"]["시각 유래"]["매칭수"] == 0
    assert result["출처유형별_전체_문자유사포함"]["시각 유래"]["매칭수"] == 1
    assert result["출처유형별_전체_문자유사포함"]["시각 유래"]["값일치율"] is None

    report = Path(result["리포트"]["md"]).read_text(encoding="utf-8")
    assert "문자유사 매칭: 1건(02_지역여건)" in report
    assert "| 문자유사 |" in report
    assert "| 시각 유래(문자유사 포함) | 2 | 1 | 0.5000 | 0 | - |" in report
    assert "## 문자유사 매칭 쌍" in report
    assert "자동차 등록 대수(휘발유)" in report
    assert "## 문자유사 관찰(비매칭)" in report
    assert "abcdef" in report
