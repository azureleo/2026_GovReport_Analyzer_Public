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
