"""소수정 v6.3 결정론 회귀 테스트."""

import json

import config
import pytest
from agents.organizer_agent import OrganizerAgent, normalize_direct_indirect_type


def _감축목표행(**overrides):
    row = {
        "지자체명": "테스트시",
        "목표수준": "총괄",
        "목표범위": "지역전체",
        "부문": "건물",
        "기준연도": 2018,
        "목표연도": 2030,
    }
    row.update(overrides)
    return row


def _감축목표정제(rows):
    return OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "reduction_targets": rows,
    })


def test_S1_감축목표는_기준연도가_다르면_병합하지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준연도=2005, 기준배출량=100),
        _감축목표행(기준연도=2018, 기준배출량=100),
    ])

    assert [row["기준연도"] for row in cleaned["reduction_targets"]] == [2005, 2018]


def test_S1_감축목표의_빈_기준연도는_수치충돌이_없으면_흡수한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준연도=None, 기준배출량=100, 목표배출량=70),
    ])

    assert len(cleaned["reduction_targets"]) == 1
    assert cleaned["reduction_targets"][0]["기준연도"] == 2018
    assert cleaned["reduction_targets"][0]["목표배출량"] == 70.0


def test_S1_감축목표의_빈_기준연도는_수치충돌이면_분리하고_기록한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준연도=None, 기준배출량=120),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert any("중복 키 값 충돌" in issue["항목"] for issue in cleaned["validation_report"])


def test_S1_감축목표의_공통필드가_천배면_선행값을_유지하고_채우지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=49_445),
        _감축목표행(기준배출량=49_445_000, 목표감축량=9_802_000),
    ])

    rows = cleaned["reduction_targets"]
    assert len(rows) == 1
    assert rows[0]["기준배출량"] == 49_445.0
    assert rows[0]["목표감축량"] is None
    assert any("스케일 표기 차 의심(톤↔천톤)" in issue["항목"] for issue in cleaned["validation_report"])


def test_S1_감축목표의_스케일서명이_천배면_교차채움하지_않는다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=49_445),
        _감축목표행(목표감축량=49_445_000),
    ])

    assert len(cleaned["reduction_targets"]) == 2
    assert [row.get("목표감축량") for row in cleaned["reduction_targets"]] == [None, 49_445_000.0]
    assert [row.get("기준배출량") for row in cleaned["reduction_targets"]] == [49_445.0, None]


def test_S1_감축목표의_일반_수치충돌은_기존_충돌경로를_유지한다() -> None:
    cleaned = _감축목표정제([
        _감축목표행(기준배출량=100),
        _감축목표행(기준배출량=120),
    ])

    assert len(cleaned["reduction_targets"]) == 1
    assert cleaned["reduction_targets"][0]["데이터상태"] == "conflicting"
    assert any("중복 키 값 충돌" in issue["항목"] for issue in cleaned["validation_report"])


def _시트03_관찰값(*, item: str | None) -> dict:
    observation = {
        "지자체명": "테스트시",
        "페이지": 10,
        "대상시트": "emissions_regional",
        "제목": "에너지 배출량 현황",
        "단위": "천톤CO2eq",
        "연도": 2020,
        "값": 100,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps({"배출유형": "직접배출", "연도": 2020}, ensure_ascii=False),
    }
    if item is not None:
        observation["항목"] = item
    return observation


def test_S4_시트03은_항목이_없으면_차트제목을_부문으로_쓰지_않는다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "chart_observations": [_시트03_관찰값(item=None)],
    })

    assert cleaned["emissions_regional"] == []
    assert any("G3 1차 키 누락(부문)" in issue["문제내용"] for issue in cleaned["validation_report"])


def test_S4_시트03은_항목이_있으면_기존처럼_부문으로_병합한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "chart_observations": [_시트03_관찰값(item="에너지")],
    })

    assert len(cleaned["emissions_regional"]) == 1
    assert cleaned["emissions_regional"][0]["부문"] == "에너지"


def test_S5_직간접구분의_흡수와_복합배출_표기를_정규화한다() -> None:
    assert normalize_direct_indirect_type("sink") == "흡수"
    assert normalize_direct_indirect_type("absorption") == "흡수"
    assert normalize_direct_indirect_type("DIRECT+INDIRECT") == "직접+간접"
    assert normalize_direct_indirect_type("Direct + Indirect Emissions") == "직접+간접"


@pytest.mark.parametrize("sector", ["총 메탄 배출량", "메탄"])
def test_S6_시트03은_잡음토큰_제거후_가스명과_전체일치하면_차단한다(
    monkeypatch, sector: str
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "chart_observations": [_시트03_관찰값(item=sector)],
    })

    assert cleaned["emissions_regional"] == []
    assert any(
        "G3 부문에 가스종(스키마 불일치)" in issue["문제내용"]
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize("sector", ["메탄가스화시설", "에너지"])
def test_S6_시트03은_잡음제거후_가스명과_다르면_허용한다(monkeypatch, sector: str) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "chart_observations": [_시트03_관찰값(item=sector)],
    })

    assert len(cleaned["emissions_regional"]) == 1


def test_S7_IPCC_단독_대분류코드는_기존처럼_정규화한다() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_regional": [{
            "배출유형": "직접배출", "부문": "1 에너지", "연도": 2020, "배출량": 10
        }],
    })

    row = cleaned["emissions_regional"][0]
    assert row["부문"] == "에너지"
    assert row["세부부문"] == "1 에너지"


def test_S7_연도는_IPCC_부문코드로_오인하지_않는다() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_regional": [{
            "배출유형": "직접배출", "부문": "2018 에너지", "연도": 2020, "배출량": 10
        }],
    })

    row = cleaned["emissions_regional"][0]
    assert row["부문"] == "2018 에너지"
    assert row.get("부문원문") is None
