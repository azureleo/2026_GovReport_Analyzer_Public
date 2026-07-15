from __future__ import annotations

import json

import config
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


def _labeled_observation(target_sheet: str, fields: dict) -> dict:
    return {
        "지자체명": "가상시",
        "페이지": 10,
        "대상시트": target_sheet,
        "그래프유형": "표",
        "제목": "온실가스 표",
        "단위": "천톤CO2eq",
        "항목": "건물",
        "연도": 2030,
        "값": 100.0,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
    }


def test_forecast와_reduction_targets_라벨_후보는_G3에서_차단하고_16시트_관찰값은_유지한다(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observations = [
        _labeled_observation(
            "emissions_forecast",
            {"시나리오": "BAU", "부문": "건물", "연도": 2030},
        ),
        _labeled_observation(
            "reduction_targets",
            {
                "값역할": "목표배출량",
                "목표수준": "부문",
                "목표범위": "지역전체",
                "부문": "건물",
                "목표연도": 2030,
            },
        ),
    ]

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": observations,
    })

    assert cleaned["emissions_forecast"] == []
    assert cleaned["reduction_targets"] == []
    assert cleaned["chart_observations"] == observations
    blocked_sheet_keys = {
        issue.get("대상시트키")
        for issue in cleaned["validation_report"]
        if issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 대상 시트 미지원" in issue.get("문제내용", "")
    }
    assert blocked_sheet_keys == {"emissions_forecast", "reduction_targets"}
