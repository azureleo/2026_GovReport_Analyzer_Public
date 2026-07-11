"""02_지역여건 의미완화 매칭 회귀 테스트."""
from __future__ import annotations

from pathlib import Path

from tests.test_score_against_golden import _점수


def _지역여건행(
    *,
    지표범주: str,
    지표세부범주: str,
    지표명: str,
    연도: int,
    값: float,
    단위: str = "천대",
    출처유형: str = "그래프",
) -> dict:
    return {
        "지표범주": 지표범주,
        "지표세부범주": 지표세부범주,
        "지표명": 지표명,
        "연도": 연도,
        "값": 값,
        "단위": 단위,
        "골든_출처유형": 출처유형,
        "골든_출처페이지": "10",
    }


def _출력행(**kwargs) -> dict:
    row = _지역여건행(**kwargs)
    row.pop("골든_출처유형")
    row.pop("골든_출처페이지")
    return row


def test_명명과_분류가_다른_자동차_등록_대수를_의미완화로_매칭한다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업", 지표세부범주="수송·교통", 지표명="자동차 등록 대수(휘발유)", 연도=2013, 값=1622
    )
    output = _출력행(
        지표범주="인문사회", 지표세부범주="교통", 지표명="자동차 등록 대수", 연도=2013, 값=1622, 단위="천 대"
    )

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})

    assert result["시트별"]["02_지역여건"]["의미완화매칭수"] == 1
    assert result["의미완화매칭수"] == 1
    assert result["의미완화매칭쌍"][0]["매칭방식"] == "의미완화"
    report = Path(result["리포트"]["md"]).read_text(encoding="utf-8")
    assert "의미완화 매칭: 1건(02_지역여건)" in report
    assert "| 의미완화 |" in report
    assert "## 의미완화 매칭 쌍" in report
    assert "자동차 등록 대수(휘발유)" in report


def test_토큰이_다른_강수량은_연도와_값이_같아도_매칭하지_않는다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="자연환경", 지표세부범주="기후", 지표명="평균 강수량", 연도=2020, 값=100
    )
    output = _출력행(
        지표범주="자연환경", 지표세부범주="전망", 지표명="5일 최다강수량 전망", 연도=2020, 값=100
    )

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})

    assert result["시트별"]["02_지역여건"]["의미완화매칭수"] == 0


def test_값이_다르면_이름과_연도가_유사해도_매칭하지_않는다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업", 지표세부범주="수송·교통", 지표명="자동차 등록 대수(휘발유)", 연도=2013, 값=3116
    )
    output = _출력행(
        지표범주="인문사회", 지표세부범주="교통", 지표명="자동차 등록 대수", 연도=2013, 값=1622
    )

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})

    assert result["시트별"]["02_지역여건"]["의미완화매칭수"] == 0


def test_값으로_유일한_후보만_일대일_매칭하고_최고점_동률은_버린다(tmp_path: Path) -> None:
    goldens = [
        _지역여건행(
            지표범주="경제산업", 지표세부범주="교통", 지표명="자동차 등록 대수(휘발유)", 연도=2013, 값=3116
        ),
        _지역여건행(
            지표범주="경제산업", 지표세부범주="교통", 지표명="자동차 등록 대수(LPG)", 연도=2013, 값=1622
        ),
    ]
    output = _출력행(
        지표범주="인문사회", 지표세부범주="교통", 지표명="자동차 등록 대수", 연도=2013, 값=1622
    )
    (tmp_path / "유일").mkdir()
    unique = _점수(tmp_path / "유일", {"02_지역여건": [output]}, {"02_지역여건": goldens})

    assert unique["시트별"]["02_지역여건"]["의미완화매칭수"] == 1
    assert unique["의미완화매칭쌍"][0]["골든"]["지표명"] == "자동차 등록 대수(LPG)"

    ambiguous_goldens = [goldens[1], dict(goldens[1], 지표명="자동차 등록 대수(휘발유)")]
    (tmp_path / "동률").mkdir()
    ambiguous = _점수(tmp_path / "동률", {"02_지역여건": [output]}, {"02_지역여건": ambiguous_goldens})
    assert ambiguous["시트별"]["02_지역여건"]["의미완화매칭수"] == 0


def test_의미완화는_기존_매칭_값일치율과_미매칭_목록을_바꾸지_않는다(tmp_path: Path) -> None:
    golden = _지역여건행(
        지표범주="경제산업", 지표세부범주="수송·교통", 지표명="자동차 등록 대수(휘발유)", 연도=2013, 값=1622
    )
    output = _출력행(
        지표범주="인문사회", 지표세부범주="교통", 지표명="자동차 등록 대수", 연도=2013, 값=1622
    )

    result = _점수(tmp_path, {"02_지역여건": [output]}, {"02_지역여건": [golden]})
    sheet = result["시트별"]["02_지역여건"]

    assert sheet["매칭수"] == 0
    assert sheet["리콜"] == 0.0
    assert sheet["정밀도"] == 0.0
    assert sheet["값일치율"] is None
    assert len(result["미매칭상세"]["골든"]) == 1
    assert len(result["미매칭상세"]["출력"]) == 1
    assert result["출처유형별_전체"]["시각 유래"]["매칭수"] == 0
    assert result["출처유형별_전체_의미완화포함"]["시각 유래"]["매칭수"] == 1


def test_02가_아닌_시트에는_의미완화를_적용하지_않는다(tmp_path: Path) -> None:
    golden = {
        "배출유형": "직접배출",
        "부문": "건물",
        "세부부문": "가정",
        "연도": 2020,
        "배출량": 10,
        "단위": "톤",
        "골든_출처유형": "그래프",
        "골든_출처페이지": "3",
    }
    output = {
        "배출유형": "간접배출",
        "부문": "산업",
        "세부부문": "공정",
        "연도": 2020,
        "배출량": 10,
        "단위": "톤",
    }

    result = _점수(tmp_path, {"03_배출현황_지역": [output]}, {"03_배출현황_지역": [golden]})

    assert result["시트별"]["03_배출현황_지역"]["의미완화매칭수"] == 0
    assert result["의미완화매칭수"] == 0
