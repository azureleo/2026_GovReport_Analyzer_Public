from __future__ import annotations

import config
from agents.organizer_agent import OrganizerAgent


def _object(evidence_id: str = "ev-1") -> dict:
    return {
        "object_id": "obj-1",
        "object_type": "chart",
        "page_number": 100,
        "metadata": {"evidence_id": evidence_id, "final_status": "extracted"},
    }


def _observation(sheet: str, fields: dict, *, value=100.0, year=2030) -> dict:
    return {
        "지자체명": "서울특별시",
        "페이지": 100,
        "대상시트": sheet,
        "그래프유형": "표",
        "제목": "검증 표",
        "단위": "천tCO2eq",
        "항목": fields.get("사업명") or fields.get("부문") or "합계",
        "연도": year,
        "값": value,
        "값근거": "표셀",
        "신뢰도": "high",
        "반영여부": "검토",
        "판독필드": {"값근거": "표셀", "연도": year, **fields},
        "근거ID": "ev-1",
        "근거ID목록": ["ev-1"],
    }


def _organize(observation: dict, **base) -> dict:
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [observation],
        "document_objects": [_object()],
        "object_triage": [],
        **base,
    }
    return OrganizerAgent().organize(raw)


def test_forecast_and_reduction_value_roles_are_supported(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    forecast = _organize(_observation("emissions_forecast", {
        "시나리오": "BAU", "부문": "건물",
    }))
    reduction = _organize(_observation("reduction_targets", {
        "값역할": "목표감축량",
        "목표수준": "부문",
        "목표범위": "관리권한",
        "부문": "건물",
        "목표연도": 2030,
    }))

    assert forecast["chart_observations"][0]["병합상태"] == "accept"
    assert forecast["emissions_forecast"][0]["전망값"] == 100.0
    assert reduction["chart_observations"][0]["병합상태"] == "accept"
    assert reduction["reduction_targets"][0]["목표감축량"] == 100.0


def test_qualitative_project_can_merge_without_fake_numeric_value(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    result = _organize(_observation(
        "mitigation_projects",
        {"관리번호": "B-1", "부문": "건물", "사업명": "제로에너지건물 전환"},
        value=None,
        year=None,
    ))

    assert result["chart_observations"][0]["병합상태"] == "accept"
    assert result["mitigation_projects"][0]["사업명"] == "제로에너지건물 전환"


def test_period_range_is_preserved_and_isolated(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    observation = _observation("emissions_forecast", {
        "시나리오": "BAU", "부문": "건물", "기간원문": "2030~2033",
    }, year=None)
    result = _organize(observation)

    assert result["emissions_forecast"] == []
    assert result["chart_observations"][0]["병합상태"] == "needs_review"
    assert "기간값은 단일 연도 아님" in result["chart_observations"][0]["병합차단사유"]


def test_fix_then_merge_only_fills_one_blank_text_value(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    base = {
        "emissions_forecast": [{
            "지자체명": "서울특별시",
            "시나리오": "BAU",
            "부문": "건물",
            "세부부문": "",
            "연도": 2030,
            "전망값": None,
            "단위": "천tCO2eq",
        }],
    }
    result = _organize(_observation("emissions_forecast", {
        "시나리오": "BAU", "부문": "건물",
    }), **base)

    assert result["chart_observations"][0]["병합상태"] == "fix_then_merge"
    assert result["emissions_forecast"][0]["전망값"] == 100.0
