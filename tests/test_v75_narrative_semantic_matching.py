"""v7-5 서술형 시트 의미완화 매칭 회귀 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.golden_score_contract import 행
from scripts import golden_score_matching
from tests.test_score_against_golden import _점수


def _골든행(**values) -> dict:
    return {
        **values,
        "골든_출처유형": "본문텍스트",
        "골든_출처페이지": "10",
    }


@pytest.mark.parametrize(
    ("sheet_name", "values", "expected"),
    [
        (
            "01_계획개요",
            {"항목명": "계획 목적", "항목값": "탄소중립 전환", "내용": "제외 토큰"},
            {"계획", "목적", "탄소중립", "전환"},
        ),
        (
            "02_지역여건",
            {"지표명": "자동차 등록", "지표세부범주": "수송 교통"},
            {"자동차", "등록", "수송", "교통"},
        ),
        (
            "07_비전전략",
            {
                "전략명": "장기 비전",
                "비전문구": "탄소중립 서울",
                "설명": "도시 전환",
                "세부전략": "제외 토큰",
            },
            {"장기", "비전", "탄소중립", "서울", "도시", "전환"},
        ),
        (
            "13_이행관리환류",
            {"거버넌스기구": "탄소중립 위원회", "역할": "정책 심의", "절차단계": "제외"},
            {"탄소중립", "위원회", "정책", "심의"},
        ),
    ],
)
def test_시트별_계약_필드만_의미완화_토큰으로_사용한다(
    sheet_name: str, values: dict, expected: set[str]
) -> None:
    assert golden_score_matching._지표토큰(행(2, values), sheet_name) == expected


def test_자카드가_정확히_06이면_의미완화_후보로_허용한다() -> None:
    golden = 행(2, {"개요유형": "목적", "항목명": "가 나 다"})
    output = 행(2, {"개요유형": "목적", "항목명": "가 나 다 라 마"})

    assert golden_score_matching._의미완화후보(golden, output, "01_계획개요") == 0.6


def test_01_항목명과_항목값_토큰으로_의미완화_매칭한다(tmp_path: Path) -> None:
    golden = _골든행(개요유형="목적", 항목명="계획 수립 목적", 항목값="탄소중립 계획")
    output = {
        "개요유형": "목적",
        "항목명": "계획 수립 목적 및 배경",
        "항목값": "탄소중립 계획",
    }

    result = _점수(tmp_path, {"01_계획개요": [output]}, {"01_계획개요": [golden]})

    sheet = result["시트별"]["01_계획개요"]
    assert sheet["의미완화매칭수"] == 1
    assert sheet["문자유사매칭수"] == 0
    assert sheet["값일치율"] is None
    assert result["출처유형별_전체_의미완화포함"]["텍스트 유래"]["매칭수"] == 1
    assert result["출처유형별_수치시트_의미완화포함"]["텍스트 유래"]["매칭수"] == 0
    assert result["의미완화매칭쌍"][0]["시트"] == "01_계획개요"
    report = Path(result["리포트"]["md"]).read_text(encoding="utf-8")
    assert "01_계획개요" in report.split("## 의미완화 매칭 쌍", 1)[1]
    assert "계획 수립 목적 및 배경" in report


def test_01_개요유형은_한쪽만_비면_통과하고_양쪽_채움_상이면_배제한다(
    tmp_path: Path,
) -> None:
    golden = _골든행(개요유형="목적", 항목명="계획 수립 목적", 항목값="탄소중립 계획")
    base_output = {
        "항목명": "계획 수립 목적 및 배경",
        "항목값": "탄소중립 계획",
    }
    (tmp_path / "빈값").mkdir()
    blank = _점수(
        tmp_path / "빈값",
        {"01_계획개요": [{**base_output, "개요유형": ""}]},
        {"01_계획개요": [golden]},
    )
    (tmp_path / "상이").mkdir()
    divergent = _점수(
        tmp_path / "상이",
        {"01_계획개요": [{**base_output, "개요유형": "추진경과"}]},
        {"01_계획개요": [golden]},
    )

    assert blank["시트별"]["01_계획개요"]["의미완화매칭수"] == 1
    assert divergent["시트별"]["01_계획개요"]["의미완화매칭수"] == 0


def test_01_개요유형이_양쪽_모두_비면_전제를_충족하지_않는다(tmp_path: Path) -> None:
    golden = _골든행(개요유형="", 항목명="계획 수립 목적", 항목값="탄소중립 계획")
    output = {
        "개요유형": "",
        "항목명": "계획 수립 목적 및 배경",
        "항목값": "탄소중립 계획",
    }

    result = _점수(tmp_path, {"01_계획개요": [output]}, {"01_계획개요": [golden]})

    assert result["시트별"]["01_계획개요"]["의미완화매칭수"] == 0


def test_07_빈_전략명과_비전문구를_포함해_의미완화_매칭한다(tmp_path: Path) -> None:
    golden = _골든행(
        전략수준="비전",
        전략명="장기 비전",
        비전문구="2050 탄소중립 선도도시 서울",
        설명="도시 전환 방향",
    )
    output = {
        "전략수준": "",
        "전략명": "",
        "비전문구": "장기 비전 2050 탄소중립 선도도시 서울",
        "설명": "도시 전환 방향",
    }

    result = _점수(tmp_path, {"07_비전전략": [output]}, {"07_비전전략": [golden]})

    assert result["시트별"]["07_비전전략"]["의미완화매칭수"] == 1
    assert result["의미완화매칭쌍"][0]["시트"] == "07_비전전략"
    assert result["의미완화매칭쌍"][0]["출력"]["전략명"] is None


def test_07_전략수준이_양쪽_채움_상이면_배제한다(tmp_path: Path) -> None:
    golden = _골든행(
        전략수준="비전",
        전략명="장기 비전",
        비전문구="2050 탄소중립 선도도시 서울",
        설명="도시 전환 방향",
    )
    output = {
        "전략수준": "세부전략",
        "전략명": "",
        "비전문구": "장기 비전 2050 탄소중립 선도도시 서울",
        "설명": "도시 전환 방향",
    }

    result = _점수(tmp_path, {"07_비전전략": [output]}, {"07_비전전략": [golden]})

    assert result["시트별"]["07_비전전략"]["의미완화매칭수"] == 0


def test_13_거버넌스기구_접두_발산을_역할_토큰과_함께_매칭한다(tmp_path: Path) -> None:
    golden = _골든행(
        거버넌스기구="2050 서울특별시 탄소중립녹색성장위원회",
        역할="탄소중립 정책 수립 심의",
        절차단계="계획수립",
    )
    output = {
        "거버넌스기구": "서울특별시 탄소중립녹색성장위원회",
        "역할": "탄소중립 정책 수립 심의",
        "절차단계": "이행점검",
    }

    result = _점수(tmp_path, {"13_이행관리환류": [output]}, {"13_이행관리환류": [golden]})

    assert result["시트별"]["13_이행관리환류"]["의미완화매칭수"] == 1
    assert result["의미완화매칭쌍"][0]["시트"] == "13_이행관리환류"


def test_자카드가_06_미만이면_서술형을_매칭하지_않는다(tmp_path: Path) -> None:
    golden = _골든행(개요유형="목적", 항목명="계획 수립 목적", 항목값="탄소중립 전환")
    output = {
        "개요유형": "목적",
        "항목명": "계획 수립 배경",
        "항목값": "기후위기 대응",
    }

    result = _점수(tmp_path, {"01_계획개요": [output]}, {"01_계획개요": [golden]})

    assert result["시트별"]["01_계획개요"]["의미완화매칭수"] == 0


def test_서술형_최고점_후보가_동률이면_매칭하지_않는다(tmp_path: Path) -> None:
    golden = _골든행(
        거버넌스기구="2050 서울특별시 탄소중립녹색성장위원회",
        역할="탄소중립 정책 수립 심의",
        절차단계="계획수립",
    )
    output = {
        "거버넌스기구": "서울특별시 탄소중립녹색성장위원회",
        "역할": "탄소중립 정책 수립 심의",
        "절차단계": "이행점검",
    }

    result = _점수(
        tmp_path,
        {"13_이행관리환류": [output, {**output, "담당부서": "기후환경본부"}]},
        {"13_이행관리환류": [golden]},
    )

    assert result["시트별"]["13_이행관리환류"]["의미완화매칭수"] == 0


@pytest.mark.parametrize("sheet_name", ["01_계획개요", "07_비전전략", "13_이행관리환류"])
def test_문자유사_티어는_서술형_시트에서_호출하지_않는다(
    monkeypatch: pytest.MonkeyPatch, sheet_name: str
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("문자유사 티어는 02_지역여건 전용이어야 한다")

    monkeypatch.setattr(golden_score_matching, "_문자유사매칭하기", fail_if_called)

    golden_score_matching.매칭하기([행(2, {})], [행(2, {})], sheet_name)


def test_매칭하기_기본인자는_명시적_false와_동일하다() -> None:
    golden = 행(
        2,
        {
            "개요유형": "목적",
            "항목명": "계획 수립 목적",
            "항목값": "탄소중립 계획",
        },
    )
    output = 행(
        2,
        {
            "개요유형": "목적",
            "항목명": "계획 수립 목적 및 배경",
            "항목값": "탄소중립 계획",
        },
    )

    default_result = golden_score_matching.매칭하기(
        [golden], [output], "01_계획개요"
    )
    explicit_result = golden_score_matching.매칭하기(
        [golden], [output], "01_계획개요", 보수티어만=False
    )

    assert default_result == explicit_result
    assert len(default_result[3]) == 1


def test_보수티어만이면_의미완화와_문자유사를_호출하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("보수 티어 경로에서 후속 유사도 티어를 호출했다")

    monkeypatch.setattr(golden_score_matching, "_의미완화매칭하기", fail_if_called)
    monkeypatch.setattr(golden_score_matching, "_문자유사매칭하기", fail_if_called)
    golden = 행(
        2,
        {"지표범주": "인구", "지표명": "인구 수", "연도": 2020, "값": 10},
    )
    output = 행(
        2,
        {"지표범주": "인구", "지표명": "인구 규모", "연도": 2020, "값": 10},
    )

    matches, _, _, semantic, character, observations = (
        golden_score_matching.매칭하기(
            [golden], [output], "02_지역여건", 보수티어만=True
        )
    )

    assert matches == []
    assert semantic == []
    assert character == []
    assert observations == []
