from __future__ import annotations

from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent


def _forecast_chart_analysis() -> dict:
    return {
        "type": "chart_table",
        "target_sheet": "forecast",
        "chart_type": "표",
        "title": "온실가스 배출 전망",
        "unit": "천톤CO2eq",
        "page_number": 10,
        "municipality": "가상시",
        "confidence": "high",
        "table": [{
            "연도": 2030,
            "항목": "건물",
            "값": 100.0,
            "단위": "천톤CO2eq",
            "fields": {"시나리오": "BAU", "부문": "건물", "연도": 2030},
        }],
    }


def test_표형_고신뢰_forecast_chart_rows는_05에_visual_only로_자동_반영한다() -> None:
    merged = ImageAgent()._merge_image_results({}, [_forecast_chart_analysis()], "가상시")

    cleaned = OrganizerAgent().organize(merged)

    assert len(merged["emissions_forecast"]) == 1
    assert merged["chart_observations"][0]["반영여부"] == "반영"
    assert len(cleaned["emissions_forecast"]) == 1
    assert cleaned["emissions_forecast"][0]["데이터상태"] == "visual_only"
