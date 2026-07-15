from __future__ import annotations

import pytest

import config
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent


def _chart_analysis(target_sheet: str, fields: dict) -> dict:
    title = {
        "forecast": "온실가스 배출 전망",
        "target": "온실가스 감축목표",
    }.get(target_sheet, "온실가스 표")
    return {
        "type": "chart_table",
        "target_sheet": target_sheet,
        "chart_type": "표",
        "title": title,
        "unit": "천톤CO2eq",
        "page_number": 10,
        "municipality": "가상시",
        "confidence": "high",
        "table": [{
            "연도": 2030,
            "항목": "건물",
            "값": 100.0,
            "단위": "천톤CO2eq",
            "fields": fields,
        }],
    }


def _배출현황_표분석(target_sheet: str, sector: str) -> dict:
    sector_field = "부문" if target_sheet == "emissions_regional" else "관리부문"
    return {
        "type": "chart_table",
        "target_sheet": target_sheet,
        "chart_type": "표",
        "title": "온실가스 배출 현황",
        "unit": "천톤CO2eq",
        "page_number": 10,
        "municipality": "가상시",
        "confidence": "high",
        "table": [{
            "연도": 2018,
            "항목": sector,
            "값": 100.0,
            "단위": "천톤CO2eq",
            "fields": {sector_field: sector},
        }],
    }


def test_forecast_chart_rows는_자동_반영하고_반영_관찰값으로_남긴다() -> None:
    analysis = _chart_analysis(
        "forecast",
        {"시나리오": "BAU", "부문": "건물", "연도": 2030},
    )

    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    assert len(merged["emissions_forecast"]) == 1
    assert merged["emissions_forecast"][0]["전망값"] == 100.0
    assert merged["chart_observations"][0]["대상시트"] == "emissions_forecast"
    assert merged["chart_observations"][0]["반영여부"] == "반영"


def test_target_chart_rows는_자동_반영하지_않고_검토_관찰값으로_남긴다() -> None:
    analysis = _chart_analysis(
        "target",
        {
            "값역할": "목표배출량",
            "목표수준": "부문",
            "목표범위": "지역전체",
            "부문": "건물",
            "목표연도": 2030,
        },
    )

    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    assert merged["reduction_targets"] == []
    assert merged["chart_observations"][0]["대상시트"] == "reduction_targets"
    assert merged["chart_observations"][0]["반영여부"] == "검토"


def test_regional_conditions_chart_rows는_기존대로_자동_반영한다() -> None:
    analysis = _chart_analysis(
        "regional_conditions",
        {"지표범주": "인문사회", "지표명": "인구", "연도": 2030},
    )

    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    assert len(merged["regional_conditions"]) == 1
    assert merged["regional_conditions"][0]["값"] == 100.0
    assert merged["chart_observations"][0]["반영여부"] == "반영"


@pytest.mark.parametrize("target_sheet", ["emissions_regional", "emissions_management"])
def test_V2_시트03_04_가스종_부문은_자동반영하지_않고_관찰값으로_남긴다(
    target_sheet: str,
) -> None:
    analysis = _배출현황_표분석(target_sheet, "총 메탄 배출량")

    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    assert merged["emissions_regional"] == []
    assert merged["chart_observations"][0]["대상시트"] == target_sheet
    assert merged["chart_observations"][0]["반영여부"] == "검토"
    assert "G3 부문에 가스종(스키마 불일치)" in merged["chart_observations"][0]["근거"]


def test_V2_시트03_에너지_부문은_기존대로_자동반영한다() -> None:
    analysis = _배출현황_표분석("emissions_regional", "에너지")

    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    assert merged["emissions_regional"][0]["부문"] == "에너지"
    assert merged["chart_observations"][0]["반영여부"] == "반영"


def test_flag_on에서도_자동_반영된_forecast_관찰값은_중복_병합하지_않는다(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    analysis = _chart_analysis(
        "forecast",
        {"시나리오": "BAU", "부문": "건물", "연도": 2030},
    )
    merged = ImageAgent()._merge_image_results({}, [analysis], "가상시")

    cleaned = OrganizerAgent().organize(merged)

    assert len(merged["emissions_forecast"]) == 1
    assert merged["chart_observations"][0]["반영여부"] == "반영"
    assert len(cleaned["emissions_forecast"]) == 1
    assert cleaned["emissions_forecast"][0]["데이터상태"] == "visual_only"
