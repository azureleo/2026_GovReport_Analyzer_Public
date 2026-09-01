from __future__ import annotations

import json

import config
from agents.organizer_agent import OrganizerAgent, _infer_indicator_category


def _지역여건_관찰값(title: str) -> dict:
    fields = {"지표명": title, "연도": 2030}
    return {
        "지자체명": "가상시",
        "페이지": 10,
        "대상시트": "regional_conditions",
        "그래프유형": "표",
        "제목": title,
        "단위": "개소",
        "항목": title,
        "연도": 2030,
        "값": 100,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
    }


def test_지역여건_지표범주는_유일한_표준어휘일_때만_추론한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)

    natural = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [_지역여건_관찰값("공원 종류별 공원수")],
    })
    ambiguous = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [_지역여건_관찰값("에너지 사용 사업체 현황")],
    })

    assert natural["regional_conditions"][0]["지표범주"] == "자연환경"
    assert any(
        issue.get("영역") == "시각병합" and issue.get("항목") == "병합"
        for issue in natural["validation_report"]
    )
    assert ambiguous["regional_conditions"] == []
    assert _infer_indicator_category("에너지 사용 사업체 현황") is None
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 1차 키 누락(지표범주)" in issue.get("문제내용", "")
        for issue in ambiguous["validation_report"]
    )


def test_지역여건_자동차등록은_연료_문맥과_충돌해도_경제산업으로_보완한다(
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", True, raising=False)
    observation = _지역여건_관찰값("연료별 자동차 등록 대수")
    observation["항목"] = "휘발유"
    observation["판독필드"] = {
        "지표명": "연료별 자동차 등록 대수",
        "연도": 2030,
    }

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    assert len(cleaned["regional_conditions"]) == 1
    row = cleaned["regional_conditions"][0]
    assert row["지표범주"] == "경제산업"
    assert row["지표명"] == "자동차 등록 대수(휘발유)"
    assert row["derivation_type"] == "inferred"


def test_지역여건_보완_off는_충돌한_범주를_기존처럼_보류한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", False, raising=False)
    observation = _지역여건_관찰값("연료별 자동차 등록 대수")
    observation["항목"] = "휘발유"

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    assert cleaned["regional_conditions"] == []
    assert cleaned["chart_observations"][0]["병합상태"] == "needs_review"


def test_지역여건_단일_명시연도만_복원하고_연도범위는_보류한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", True, raising=False)
    single = _지역여건_관찰값("2022년 공원 면적")
    ranged = _지역여건_관찰값("2016~2017년 주택 보급률")
    for observation in (single, ranged):
        observation["연도"] = None
        observation["판독필드"] = {
            "지표범주": "자연환경" if observation is single else "인문사회",
            "지표명": observation["제목"],
            "기간원문": observation["제목"].split(" ", 1)[0],
        }

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [single, ranged],
    })

    assert [row["연도"] for row in cleaned["regional_conditions"]] == [2022]
    assert cleaned["chart_observations"][1]["병합상태"] == "needs_review"
    assert "G3 1차 키 누락(연도)" in cleaned["chart_observations"][1]["병합차단사유"]
