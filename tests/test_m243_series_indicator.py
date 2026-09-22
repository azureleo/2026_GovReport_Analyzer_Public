from __future__ import annotations

import json

import pytest

import config
from agents.organizer_agent import OrganizerAgent


@pytest.fixture(autouse=True)
def _legacy_series_layout(monkeypatch) -> None:
    # 이 파일의 기존 회귀는 보완 OFF 기준의 열 배치를 고정한다.
    monkeypatch.setattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", False, raising=False)


def _지역여건_관찰값(*, item: str | None, value: float = 100.0) -> dict:
    observation = {
        "지자체명": "서울특별시",
        "페이지": 42,
        "대상시트": "regional_conditions",
        "그래프유형": "막대그래프",
        "제목": "건축물 연차별 현황",
        "단위": "동",
        "연도": 2020,
        "값": value,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(
            {
                "지표범주": "인문사회",
                "지표명": "건축물 연차별 현황",
                "연도": 2020,
            },
            ensure_ascii=False,
        ),
    }
    if item is not None:
        observation["항목"] = item
    return observation


def test_다시리즈_지역여건은_서로_다른_키로_모두_병합한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [
            _지역여건_관찰값(item="10년 미만", value=100.0),
            _지역여건_관찰값(item="10년 이상 20년 미만", value=200.0),
        ],
    }

    cleaned = OrganizerAgent().organize(raw)

    assert [row["지표명"] for row in cleaned["regional_conditions"]] == [
        "10년 미만",
        "10년 이상 20년 미만",
    ]
    visual_issues = [
        row for row in cleaned["validation_report"] if row.get("영역") == "시각병합"
    ]
    assert sum(row.get("항목") == "병합" for row in visual_issues) == 2
    assert not any(
        row.get("심각도") == "경고" or "동일 키 시각 행 존재" in row.get("문제내용", "")
        for row in visual_issues
    )


def test_지역여건_시리즈라벨과_차트명은_가이드라인_컬럼에_배치한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [_지역여건_관찰값(item="10년 미만")],
    })

    row = cleaned["regional_conditions"][0]
    assert row["지표명"] == "10년 미만"
    assert row["지표세부범주"] == "건축물 연차별 현황"


@pytest.mark.parametrize("item", ["건축물 연차별 현황", None])
def test_단일시리즈_지역여건은_기존_차트지표명_조립을_유지한다(monkeypatch, item) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [_지역여건_관찰값(item=item)],
    })

    row = cleaned["regional_conditions"][0]
    assert row["지표명"] == "건축물 연차별 현황"
    assert row["지표세부범주"] == ""


def test_플래그가_꺼지면_지역여건_라벨병합_산출은_불변이다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", False, raising=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "서울특별시",
        "chart_observations": [_지역여건_관찰값(item="10년 미만")],
    })

    assert cleaned["regional_conditions"] == []
    assert not any(
        row.get("영역") == "시각병합" for row in cleaned["validation_report"]
    )


def test_보완_on은_분해형_차트명과_계열을_하나의_지표명으로_조립한다(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", True, raising=False)
    observation = _지역여건_관찰값(item="가정용")
    observation["제목"] = "가상시 용도별 전력 소비량"
    observation["근거"] = json.dumps({
        "지표범주": "에너지",
        "지표명": "가상시 용도별 전력 소비량",
        "연도": 2020,
    }, ensure_ascii=False)

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    row = cleaned["regional_conditions"][0]
    assert row["지표명"] == "전력 소비량(가정용)"
    assert row["지표세부범주"] == "가상시 용도별 전력 소비량"
    assert row["derivation_type"] == "normalized"
