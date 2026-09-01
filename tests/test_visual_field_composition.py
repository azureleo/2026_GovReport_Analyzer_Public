from __future__ import annotations

import json

import config
from agents import image_agent
from agents.image_agent import ImageAgent
from agents.organizer_agent import (
    OrganizerAgent,
    _compose_visual_row_fields,
)


def _compose(sheet: str, observation: dict, fields: dict | None = None):
    return _compose_visual_row_fields(sheet, observation, fields or {}, "가상시")


def test_caption_axis_legend_compose_regional_row_fields() -> None:
    fields, audit = _compose("regional_conditions", {
        "제목": "연료별 자동차 등록 대수",
        "캡션": "그림 2-4 연료별 자동차 등록 대수",
        "항목": "휘발유",
        "연도": None,
        "단위": "",
        "X축": {"title": "연도", "labels": ["2022"]},
        "Y축": {"title": "등록 대수", "unit": "천대"},
        "범례목록": ["휘발유", "경유"],
    })

    assert fields["지표범주"] == "경제산업"
    assert fields["지표명"] == "자동차 등록 대수"
    assert fields["연도"] == 2022
    assert fields["단위"] == "천대"
    assert fields["범례항목"] == "휘발유"
    assert fields["derivation_type"] == "inferred"
    assert audit["conflicts"] == {}


def test_forecast_caption_axis_legend_complete_required_keys() -> None:
    fields, audit = _compose("emissions_forecast", {
        "제목": "BAU 부문별 온실가스 배출전망",
        "캡션": "표 4-2 BAU 부문별 온실가스 배출전망",
        "항목": "건물",
        "X축": {"labels": ["2030"]},
        "Y축": {"unit": "천톤CO2eq"},
        "범례목록": ["건물"],
    })

    assert fields["시나리오"] == "BAU"
    assert fields["부문"] == "건물"
    assert fields["연도"] == 2030
    assert fields["단위"] == "천톤CO2eq"
    assert audit["status"] == "composed"


def test_reduction_caption_axis_legend_composes_role_scope_and_level() -> None:
    fields, audit = _compose("reduction_targets", {
        "제목": "관리권한 부문별 2030년 목표감축량",
        "항목": "건물",
        "X축": {"labels": ["2030"]},
        "Y축": {"title": "목표감축량", "unit": "천톤CO2eq"},
        "범례목록": ["건물"],
    })

    assert fields["값역할"] == "목표감축량"
    assert fields["목표범위"] == "관리권한"
    assert fields["목표수준"] == "부문"
    assert fields["부문"] == "건물"
    assert fields["목표연도"] == 2030
    assert fields["단위"] == "천톤CO2eq"
    assert audit["conflicts"] == {}


def test_multiple_years_and_emission_types_are_not_invented() -> None:
    fields, audit = _compose("emissions_regional", {
        "제목": "직접배출 및 간접배출 현황",
        "항목": "건물",
        "X축": {"labels": ["2020", "2021"]},
    })

    assert "연도" not in fields
    assert "배출유형" not in fields
    assert set(audit["conflicts"]["배출유형"]) == {"직접배출", "간접배출"}


def test_generic_emission_caption_does_not_assume_direct_emission() -> None:
    fields, audit = _compose("emissions_regional", {
        "제목": "온실가스 배출량",
        "항목": "건물",
        "X축": {"labels": ["2021"]},
    })

    assert "배출유형" not in fields
    assert fields["부문"] == "건물"
    assert audit["conflicts"] == {}


def test_existing_explicit_fields_are_never_overwritten() -> None:
    fields, audit = _compose(
        "emissions_forecast",
        {
            "제목": "BAU 전망",
            "항목": "건물",
            "X축": {"labels": ["2030"]},
        },
        {"시나리오": "정책반영", "부문": "수송", "연도": 2040},
    )

    assert fields["시나리오"] == "정책반영"
    assert fields["부문"] == "수송"
    assert fields["연도"] == 2040
    assert not {"시나리오", "부문", "연도"} & set(audit["filled"])


def test_exact_evidence_only_composes_and_merges_complete_row(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_FIELD_COMPOSITION_ENABLED", True, raising=False)
    observation = {
        "지자체명": "가상시",
        "페이지": 10,
        "대상시트": "emissions_forecast",
        "그래프유형": "막대",
        "제목": "BAU 부문별 배출전망",
        "캡션": "그림 4-1 BAU 부문별 배출전망",
        "항목": "건물",
        "X축": {"labels": ["2030"]},
        "Y축": {"unit": "천톤CO2eq"},
        "범례목록": ["건물"],
        "값": 100.0,
        "신뢰도": "high",
        "반영여부": "검토",
        "판독필드": {"값근거": "명시라벨"},
        "근거": json.dumps({"값근거": "명시라벨"}, ensure_ascii=False),
        "근거ID": "ev-10",
        "근거ID목록": ["ev-10"],
    }
    raw = {
        "municipality_name": "가상시",
        "chart_observations": [observation],
        "document_objects": [{
            "object_id": "obj-10",
            "object_type": "chart",
            "page_number": 10,
            "metadata": {"evidence_id": "ev-10", "final_status": "extracted"},
        }],
        "object_triage": [],
    }

    cleaned = OrganizerAgent().organize(raw)

    assert len(cleaned["emissions_forecast"]) == 1
    row = cleaned["emissions_forecast"][0]
    assert (row["시나리오"], row["부문"], row["연도"]) == ("BAU", "건물", 2030)
    candidate = cleaned["chart_observations"][0]
    assert candidate["근거매칭상태"] == "exact"
    assert candidate["필드조합상태"] == "complete"
    assert set(candidate["필드조합목록"]) >= {"시나리오", "부문", "연도", "단위"}
    inventory = cleaned["visual_inventory"][0]
    assert inventory["필드조합상태"] == "complete"
    assert "시나리오" in inventory["필드조합목록"]


def test_vision_contract_keeps_caption_axis_legend_separate(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_call(_image, prompt, **_kwargs):
        captured["prompt"] = prompt
        return {}, False

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", fake_call)
    ImageAgent()._chart_to_table({"base64": "image"}, 1, "가상시")

    for token in ("caption", "x_axis", "y_axis", "legend", "범례항목", "축항목"):
        assert token in image_agent.CHART_TABLE_SYSTEM
        assert token in captured["prompt"]


def test_image_observation_propagates_visual_signals() -> None:
    analysis = {
        "municipality": "가상시",
        "page_number": 1,
        "chart_type": "막대",
        "title": "배출전망",
        "caption": "그림 1-1 배출전망",
        "x_axis": {"labels": ["2030"]},
        "y_axis": {"unit": "천톤CO2eq"},
        "legend": ["건물"],
        "unit": "천톤CO2eq",
        "confidence": "high",
    }
    item = {"항목": "건물", "연도": 2030, "값": 100, "fields": {"값근거": "명시라벨"}}
    output: dict = {}

    ImageAgent()._append_chart_observation(
        output, analysis, item, "emissions_forecast", False
    )

    observation = output["chart_observations"][0]
    assert observation["캡션"] == "그림 1-1 배출전망"
    assert observation["X축"]["labels"] == ["2030"]
    assert observation["Y축"]["unit"] == "천톤CO2eq"
    assert observation["범례목록"] == ["건물"]
