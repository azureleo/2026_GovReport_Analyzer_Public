from __future__ import annotations

import json

import config
from agents import image_agent
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent, _visual_candidate_row


필수_프롬프트_토큰 = (
    "지표범주",
    "지표명",
    "배출유형",
    "부문",
    "세부부문",
    "관리부문",
    "직간접구분",
    "시나리오",
    "값역할",
    "목표수준",
    "목표범위",
    "목표연도",
    "계획구분",
    "사업명",
    "재원구분",
    "추측 금지",
)


def _프롬프트를_수집한다(monkeypatch) -> tuple[str, str]:
    단일_호출 = {}
    배치_호출 = {}

    def 단일_대역(_image, prompt, **_kwargs):
        단일_호출["prompt"] = prompt
        return {}, False

    def 배치_대역(_images, prompt, **_kwargs):
        배치_호출["prompt"] = prompt
        return {}, False

    monkeypatch.setattr(image_agent.llm_client, "call_vision_json", 단일_대역)
    ImageAgent()._chart_to_table({"base64": "이미지"}, 1, "가상시")
    monkeypatch.setattr(image_agent.llm_client, "call_vision_batch_json", 배치_대역)
    페이지 = type("페이지", (), {"page_number": 1, "text": ""})()
    ImageAgent()._chart_to_table_batch([(페이지, {"base64": "가"}), (페이지, {"base64": "나"})], "가상시")
    return 단일_호출["prompt"], 배치_호출["prompt"]


def test_차트표_시스템과_두_프롬프트에_필수_분류_지시가_있다(monkeypatch) -> None:
    단일_프롬프트, 배치_프롬프트 = _프롬프트를_수집한다(monkeypatch)

    for prompt in (image_agent.CHART_TABLE_SYSTEM, 단일_프롬프트, 배치_프롬프트):
        assert all(token in prompt for token in 필수_프롬프트_토큰)


def test_estimated는_축_추정값일_때만_true인_문구를_유지한다() -> None:
    기존_규칙_7 = '7. 막대/선의 값이 축 눈금만으로 추정된 값이면 fields에 {"estimated": true}를 넣고 confidence는 medium 이하로 두세요.'

    assert 기존_규칙_7 in image_agent.CHART_TABLE_SYSTEM


def _감축목표_관찰값(value_role: str, value: float = 100.0) -> dict:
    fields = {
        "값역할": value_role,
        "목표수준": "부문",
        "목표범위": "지역전체",
        "부문": "건물",
        "목표연도": 2030,
    }
    return {
        "지자체명": "가상시",
        "페이지": 10,
        "대상시트": "reduction_targets",
        "그래프유형": "표",
        "제목": "감축목표",
        "단위": "천톤CO2eq",
        "항목": "건물",
        "연도": 2030,
        "값": value,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
    }


def test_목표감축량_역할은_후보_컬럼에_배치하되_라벨_경로는_미지원으로_차단한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _감축목표_관찰값("목표감축량")
    fields = json.loads(observation["근거"])

    candidate = _visual_candidate_row("reduction_targets", observation, fields, "가상시")

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    assert candidate is not None
    assert candidate["목표감축량"] == 100.0
    assert candidate["목표배출량"] is None
    assert cleaned["reduction_targets"] == []
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 대상 시트 미지원" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )


def test_목표배출량_역할도_후보_컬럼에_배치하되_라벨_경로는_미지원으로_차단한다(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_MERGE_LABELED_ENABLED", True, raising=False)
    observation = _감축목표_관찰값("목표배출량")
    fields = json.loads(observation["근거"])

    candidate = _visual_candidate_row("reduction_targets", observation, fields, "가상시")

    cleaned = OrganizerAgent().organize({
        "municipality_name": "가상시",
        "chart_observations": [observation],
    })

    assert candidate is not None
    assert candidate["목표배출량"] == 100.0
    assert candidate.get("목표감축량") is None
    assert cleaned["reduction_targets"] == []
    assert any(
        issue.get("영역") == "시각병합"
        and issue.get("항목") == "차단"
        and "G3 대상 시트 미지원" in issue.get("문제내용", "")
        for issue in cleaned["validation_report"]
    )
