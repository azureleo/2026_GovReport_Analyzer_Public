from __future__ import annotations

from agents.image_agent import ImageAgent


def test_summary_지역여건은_재추론하고_명시적_비전전략은_유지한다() -> None:
    agent = ImageAgent()

    assert agent._infer_target_sheet({
        "target_sheet": "summary",
        "title": "장래인구추계",
    }) == "regional_conditions"
    assert agent._infer_target_sheet({
        "target_sheet": "vision_strategy",
        "title": "장래인구추계",
    }) == "vision_strategy"


def test_summary_재추론은_관찰값_근거_끝에_판정근거를_남긴다() -> None:
    agent = ImageAgent()
    results = agent._merge_image_results(
        {},
        [{
            "type": "chart_table",
            "target_sheet": "summary",
            "title": "장래인구추계",
            "summary": "인구 변화 표",
            "chart_type": "표",
            "confidence": "high",
            "table": [{"연도": 2030, "값": 100, "fields": {"지표명": "인구"}}],
        }],
        "가상시",
    )

    observation = results["chart_observations"][0]
    assert observation["대상시트"] == "regional_conditions"
    assert observation["근거"].endswith("; 대상시트 재추론(summary)")


def test_병합_연도범위는_대상시트별로_적용한다() -> None:
    agent = ImageAgent()
    analysis = {"confidence": "high", "chart_type": "표"}
    item = {"연도": 2005, "값": 100}

    _, regional_confidence, regional_reasons = agent._chart_merge_decision(
        analysis, item, "regional_conditions"
    )
    _, targets_confidence, targets_reasons = agent._chart_merge_decision(
        analysis, item, "reduction_targets"
    )

    assert "연도 범위 외" not in regional_reasons
    assert regional_confidence == "high"
    assert "연도 범위 외" in targets_reasons
    assert targets_confidence == "low"
