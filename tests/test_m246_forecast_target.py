from __future__ import annotations

import json

import pytest

import config
from agents.image_agent import CHART_TABLE_SYSTEM, ImageAgent
from agents.organizer_agent import OrganizerAgent


@pytest.mark.parametrize(
    ("target_sheet", "expected"),
    [
        ("forecast", "emissions_forecast"),
        ("target", "reduction_targets"),
    ],
)
def test_전망과_감축목표_legacy_target_sheet를_신규_시트로_매핑한다(
    target_sheet: str,
    expected: str,
) -> None:
    assert ImageAgent()._infer_target_sheet({"target_sheet": target_sheet}) == expected


def test_차트표_프롬프트에_전망과_감축목표_분류_규칙을_명시한다() -> None:
    assert (
        "regional_conditions, emissions_regional, emissions_management, emissions_forecast, "
        "reduction_targets, vision_strategy, mitigation_projects, financial_plan, "
        "foundation_measures, other 중 하나"
        in CHART_TABLE_SYSTEM
    )
    assert "배출·흡수 전망" in CHART_TABLE_SYSTEM
    assert "기후 시나리오(SSP·RCP 등의 기온·강수 전망), 영향·취약성·리스크 자료는 foundation" in CHART_TABLE_SYSTEM
    assert "감축목표 차트" in CHART_TABLE_SYSTEM
    assert "배출전망(forecast)" in CHART_TABLE_SYSTEM
    assert "차트·캡션의 시나리오 표기 그대로(BAU|목표|전망 등), 없으면 생략" in CHART_TABLE_SYSTEM
    assert "감축목표(target)" in CHART_TABLE_SYSTEM
    assert "LEAP" not in CHART_TABLE_SYSTEM


def test_차트표_프롬프트의_estimated_true_only_관례를_보존한다() -> None:
    assert (
        '7. 막대/선의 값이 축 눈금만으로 추정된 값이면 fields에 {"estimated": true}를 넣고 '
        "confidence는 medium 이하로 두세요."
        in CHART_TABLE_SYSTEM
    )


def _배출전망_관찰값(scenario: str | None) -> dict:
    fields = {"부문": "건물", "연도": 2030}
    if scenario is not None:
        fields["시나리오"] = scenario
    return {
        "지자체명": "가상시",
        "페이지": 10,
        "대상시트": "emissions_forecast",
        "그래프유형": "표",
        "제목": "온실가스 배출 전망",
        "단위": "천톤CO2eq",
        "항목": "건물",
        "연도": 2030,
        "값": 100.0,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
    }


def test_시나리오_공란_배출전망은_G3_키누락으로_차단한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [_배출전망_관찰값(None)],
    })

    assert cleaned["emissions_forecast"] == []
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and issue.get("대상시트키") == "emissions_forecast"
        and "G3 1차 키 누락(시나리오)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [(None, "needs_review"), ("BAU", "duplicate")],
)
def test_배출전망은_시나리오가_있을_때만_기존행과_교차검증한다(
    monkeypatch,
    scenario: str | None,
    expected_status: str,
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    text_row = {
        "지자체명": "가상시",
        "시나리오": "BAU",
        "부문": "건물",
        "연도": 2030,
        "전망값": 100.0,
        "단위": "천톤CO2eq",
    }

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "emissions_forecast": [text_row],
        "chart_observations": [_배출전망_관찰값(scenario)],
    })

    assert len(cleaned["emissions_forecast"]) == 1
    assert cleaned["emissions_forecast"][0]["데이터상태"] != "visual_only"
    assert cleaned["chart_observations"][0]["병합상태"] == expected_status
    if scenario is None:
        assert "G3 1차 키 누락(시나리오)" in cleaned["chart_observations"][0]["병합차단사유"]
    else:
        assert any(
            issue.get("영역") == "시각병합"
            and issue.get("항목") == "생략"
            and "교차일치" in issue.get("문제내용", "")
            for issue in cleaned["validation_report"]
        )


def test_시나리오_공란과_연도_누락은_G3_키누락으로_차단한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _배출전망_관찰값(None)
    observation.pop("연도")
    observation["근거"] = json.dumps({"부문": "건물"}, ensure_ascii=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    assert cleaned["emissions_forecast"] == []
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 1차 키 누락(시나리오, 연도)" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )
