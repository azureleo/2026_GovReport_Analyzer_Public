from __future__ import annotations

import config
from agents import image_agent
from agents.image_agent import (
    ImageAgent,
    _negative_revalidation_signals,
)
from agents.organizer_agent import OrganizerAgent
from utils.pdf_reader import PageContent
from utils.vision_recovery import has_usable_table_value
from utils.visual_contract import (
    STRUCTURED_VISUAL_TYPES,
    VISUAL_CONTRACT_VERSION,
    VISUAL_TARGET_SHEETS,
    normalize_visual_table_rows,
)


def _strong_image(caption: str = "그림 5-1 2030년 감축목표 40.4%") -> dict:
    return {
        "base64": "image",
        "caption": caption,
        "source_evidence_ids": ["ev-p4"],
        "source_object_ids": ["obj-p4"],
    }


def _structured_result(*, image_index: int | None = None) -> dict:
    result = {
        "type": "structured_visual",
        "target_sheet": "vision_strategy",
        "chart_type": "strategy_map",
        "title": "2050 탄소중립 비전 및 추진전략",
        "unit": "",
        "table": [{
            "연도": None,
            "항목": "건물 부문 전환",
            "종류": "구조",
            "값": None,
            "단위": "",
            "fields": {
                "값근거": "명시라벨",
                "구조역할": "전략",
                "상위항목": "2050 탄소중립",
                "관계": "포함",
                "순서": 1,
            },
        }],
        "summary": "비전 아래 추진전략을 배치한 체계도",
        "confidence": "high",
    }
    if image_index is not None:
        result["image_index"] = image_index
        result["page_number"] = image_index
    return result


def test_p4_contract_expands_visual_types_and_target_sheets() -> None:
    assert VISUAL_CONTRACT_VERSION == 5
    assert {"diagram", "infographic", "flow", "strategy_map"} <= STRUCTURED_VISUAL_TYPES
    assert {
        "regional_conditions",
        "vision_strategy",
        "foundation_measures",
        "other",
    } <= VISUAL_TARGET_SHEETS
    for token in (
        "diagram",
        "infographic",
        "flow",
        "strategy_map",
        "vision_strategy",
        "foundation_measures",
        "target_sheet=other",
        "구조역할",
        "상위항목",
        "관계",
    ):
        assert token in image_agent.CHART_TABLE_SYSTEM


def test_structured_rows_are_usable_without_fabricated_numeric_values() -> None:
    assert has_usable_table_value(_structured_result()["table"]) is True
    assert has_usable_table_value([{
        "연도": None,
        "항목": "읽을 수 없는 차트",
        "값": None,
        "fields": {"값근거": "unknown"},
    }]) is False


def test_structured_dedup_keeps_same_label_under_different_parents() -> None:
    rows = normalize_visual_table_rows([
        {
            "연도": None,
            "항목": "효율 향상",
            "값": None,
            "fields": {"구조역할": "전략", "상위항목": "건물", "관계": "포함"},
        },
        {
            "연도": None,
            "항목": "효율 향상",
            "값": None,
            "fields": {"구조역할": "전략", "상위항목": "수송", "관계": "포함"},
        },
    ], chart_type="strategy_map", title="부문별 전략")

    assert len(rows) == 2


def test_negative_revalidation_signal_requires_more_than_caption_number() -> None:
    assert _negative_revalidation_signals(_strong_image())
    assert _negative_revalidation_signals(_strong_image("그림 5-1 일반 사진")) == []


def test_single_negative_is_revalidated_once_and_recovers_structure(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", True, raising=False)
    calls: list[str] = []

    def fake_call(_image, prompt, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return {"type": "해당없음", "negative_reason": "표가 아님"}, True
        return _structured_result(), True

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", fake_call)

    result = ImageAgent()._chart_to_table(_strong_image(), 5, "가상시")

    assert len(calls) == 2
    assert result is not None
    assert result["type"] == "chart_table"
    assert result["visual_result_type"] == "structured_visual"
    assert result["target_sheet"] == "vision_strategy"
    assert result["negative_revalidation"]["outcome"] == "recovered"
    assert result["source_evidence_ids"] == ["ev-p4"]


def test_negative_without_strong_signal_is_not_revalidated(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", True, raising=False)
    calls = 0

    def fake_call(_image, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return {"type": "해당없음", "negative_reason": "일반 사진"}, True

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", fake_call)

    result = ImageAgent()._chart_to_table(_strong_image("그림 5-1 일반 사진"), 5, "가상시")

    assert calls == 1
    assert result is not None
    assert result["type"] == "해당없음"
    assert "negative_revalidation" not in result


def test_second_negative_is_preserved_as_confirmed_not_relevant(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", True, raising=False)
    calls = 0

    def fake_call(_image, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        return {"type": "해당없음", "negative_reason": "실제 장식 이미지"}, True

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", fake_call)
    agent = ImageAgent()

    result = agent._chart_to_table(_strong_image(), 5, "가상시")
    outcome = agent._object_outcomes.outcome("ev-p4")

    assert calls == 2
    assert result is not None and result["type"] == "해당없음"
    assert result["negative_revalidation"]["outcome"] == "confirmed_negative"
    assert outcome.status == "not_relevant"
    assert outcome.attempt_count == 1
    assert outcome.terminal_reason == "음성 재검증 후 비데이터 확정"


def test_failed_negative_revalidation_becomes_needs_review_without_parent_retry(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", True, raising=False)
    calls = 0

    def fake_call(_image, _prompt, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "해당없음"}, True
        return {}, False

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", fake_call)
    agent = ImageAgent()

    result = agent._chart_to_table(_strong_image(), 5, "가상시", fail_fast=True)
    outcome = agent._object_outcomes.outcome("ev-p4")

    assert calls == 2
    assert result is not None and result["type"] == "해당없음"
    assert result["negative_revalidation"]["outcome"] == "failed"
    assert outcome.status == "needs_review"


def test_batch_path_revalidates_negative_and_preserves_all_object_results(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", True, raising=False)
    pages = [
        PageContent(1, "그림 1-1 2030년 감축목표 40.4%", [], []),
        PageContent(2, "그림 2-1 비전 전략", [], []),
    ]
    batch = [
        (pages[0], _strong_image("그림 1-1 2030년 감축목표 40.4%")),
        (pages[1], {**_strong_image("그림 2-1 비전 전략"), "source_evidence_ids": ["ev-p4-2"]}),
    ]
    monkeypatch.setattr(
        image_agent.llm_client,
        "call_vision_batch_json",
        lambda *_args, **_kwargs: ({
            "analyses": [
                {"image_index": 1, "page_number": 1, "type": "해당없음"},
                _structured_result(image_index=2),
            ],
        }, True),
    )
    monkeypatch.setattr(
        image_agent.llm_client,
        "call_vision_json",
        lambda *_args, **_kwargs: (_structured_result(), True),
    )

    results = ImageAgent()._chart_to_table_batch(batch, "가상시", fail_fast=True)

    assert len(results) == 2
    recovered = next(row for row in results if row["page_number"] == 1)
    assert recovered["negative_revalidation"]["outcome"] == "recovered"
    assert all(row["contract_version"] == 5 for row in results)


def test_structured_visual_is_preserved_but_never_auto_merged(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", False, raising=False)
    analysis = {
        **_structured_result(),
        "type": "chart_table",
        "page_number": 5,
        "municipality": "가상시",
        "negative_revalidation": {
            "attempted": True,
            "outcome": "recovered",
            "signals": ["caption:year_or_period"],
        },
    }

    raw = ImageAgent()._merge_image_results({}, [analysis], "가상시")
    observation = raw["chart_observations"][0]
    cleaned = OrganizerAgent().organize({"municipality_name": "가상시", **raw})

    assert observation["대상시트"] == "vision_strategy"
    assert observation["시각구조유형"] == "strategy_map"
    assert observation["음성재검증상태"] == "recovered"
    assert observation["자동병합정책"] == "block_structured_visual"
    assert observation["반영여부"] == "검토"
    assert cleaned["vision_strategy"] == []
    assert "G4 구조 시각자료 자동 병합 금지" in cleaned["chart_observations"][0]["병합차단사유"]
    assert cleaned["visual_inventory"][0]["음성재검증상태"] == "recovered"


def test_other_target_is_preserved_instead_of_falling_back_to_emissions() -> None:
    agent = ImageAgent()
    analysis = {
        "target_sheet": "other",
        "chart_type": "diagram",
        "confidence": "high",
        "title": "분류되지 않은 조직 관계도",
    }

    assert agent._infer_target_sheet(analysis) == "other"
    can_merge, _, reasons = agent._chart_merge_decision(
        analysis,
        {"연도": 2030, "값": 1, "fields": {"값근거": "명시라벨"}},
        "other",
    )
    assert can_merge is False
    assert "기타 대상 시트 자동 병합 금지" in reasons
