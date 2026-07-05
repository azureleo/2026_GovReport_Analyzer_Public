from __future__ import annotations

import json

import pytest

import config
from agents.hybrid_review_agent import HybridReviewAgent, select_targeted_review_rows
from utils import llm_client
from utils.pdf_reader import PageContent


def _page(number: int, text: str) -> PageContent:
    return PageContent(page_number=number, text=text, tables=[], images=[])


@pytest.fixture(autouse=True)
def _hybrid_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_TARGETED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_REVIEW_MODEL", "gemini-test")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "HYBRID_REVIEW_TARGET_MAX_ROWS", 20)
    monkeypatch.setattr(config, "STAGE_PROVIDERS", {"review": ""})
    monkeypatch.setattr(config, "STAGE_MODELS", {"review": ""})


def test_select_targeted_review_rows_from_warning_and_conflicting_status() -> None:
    # Given: organizer가 실제 생성하는 구조적 대상 필드와 conflicting 행이 함께 있다.
    final_data = {
        "municipality_name": "서울특별시",
        "reduction_targets": [
            {"목표수준": "총괄", "목표연도": 2030, "출처페이지": "p10"},
        ],
        "emissions_management": [
            {"관리부문": "건물", "연도": 2030, "배출량": 10, "데이터상태": "conflicting", "출처페이지": "p20"},
        ],
        "validation_report": [
            {
                "심각도": "경고",
                "영역": "감축목표",
                "항목": "감축률 불일치(합계 2030)",
                "문제내용": "보고값과 산식 계산값 불일치",
                "대상시트키": "reduction_targets",
                "대상행번호": 1,
            }
        ],
    }

    # When: 타깃 검수 대상을 고르면
    targets = select_targeted_review_rows(final_data)

    # Then: 경고 행과 conflicting 행이 모두 포함된다.
    assert [(target["sheet_key"], target["row_index"], target["pages"]) for target in targets] == [
        ("reduction_targets", 1, [10]),
        ("emissions_management", 1, [20]),
    ]


def test_select_targeted_review_rows_from_real_organizer_report() -> None:
    # Given: organizer가 감축률 불일치와 중복 값충돌을 실제 검증리포트/데이터상태로 만든다.
    from agents.organizer_agent import OrganizerAgent

    raw = {
        "municipality_name": "서울특별시",
        "reduction_targets": [
            {
                "목표수준": "총괄",
                "목표범위": "GIR",
                "부문": "합계",
                "기준배출량": 100,
                "목표배출량": 60,
                "감축률": 10,
                "목표연도": 2030,
                "출처페이지": "p10",
            }
        ],
        "emissions_management": [
            {"관리부문": "건물", "세부부문": "공공", "직간접구분": "직접", "연도": 2030, "배출량": 10, "출처페이지": "p20"},
            {"관리부문": "건물", "세부부문": "공공", "직간접구분": "직접", "연도": 2030, "배출량": 20, "출처페이지": "p21"},
        ],
    }
    final_data = OrganizerAgent().organize(raw)

    # When: 타깃 검수 대상을 고르면
    targets = select_targeted_review_rows(final_data)

    # Then: 실제 리포트의 감축률 행과 값충돌 행이 최종 정제본 행번호로 선정된다.
    selected = {(target["sheet_key"], target["row_index"]) for target in targets}
    assert ("reduction_targets", 1) in selected
    assert ("emissions_management", 1) in selected


def test_failed_ledger_pages_are_optional_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 원장 실패 페이지와 같은 출처페이지를 가진 행이 있다.
    final_data = {
        "municipality_name": "서울특별시",
        "emissions_management": [
            {"관리부문": "건물", "연도": 2030, "배출량": 10, "출처페이지": "p20"},
            {"관리부문": "수송", "연도": 2030, "배출량": 30, "출처페이지": "p30"},
        ],
        "validation_report": [
            {
                "심각도": "경고",
                "영역": "04_배출현황_관리권한",
                "항목": "원장 파싱실패",
                "문제내용": "p20 배치 실패: JSONDecodeError",
            }
        ],
    }

    # When/Then: 플래그가 꺼져 있으면 추가하지 않고, 켜면 실패 페이지 행만 추가한다.
    monkeypatch.setattr(config, "HYBRID_REVIEW_INCLUDE_FAILED_PAGES", False)
    assert select_targeted_review_rows(final_data) == []
    monkeypatch.setattr(config, "HYBRID_REVIEW_INCLUDE_FAILED_PAGES", True)
    targets = select_targeted_review_rows(final_data)
    assert [(target["sheet_key"], target["row_index"]) for target in targets] == [("emissions_management", 1)]


def test_targeted_review_uses_review_stage_and_does_not_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: conflicting 행 1건과 stage를 기록하는 fake LLM
    stages: list[str | None] = []
    final_data = {
        "municipality_name": "서울특별시",
        "emissions_management": [
            {"관리부문": "건물", "연도": 2030, "배출량": 10, "데이터상태": "conflicting", "출처페이지": "p2"},
        ],
    }

    def fake_call_text_json(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None):
        stages.append(stage)
        return {
            "hybrid_review_candidates": [
                {
                    "candidate_type": "판단불가",
                    "confidence": "medium",
                    "candidate_row": {"관리부문": "건물", "연도": 2030, "배출량": 10},
                    "base_similar_row": {"관리부문": "건물", "연도": 2030, "배출량": 10},
                    "reason": "원문 재확인 필요",
                    "merge_recommendation": "자동병합금지",
                }
            ]
        }, True

    monkeypatch.setattr(llm_client, "call_text_json", fake_call_text_json)

    # When: 타깃 검수를 실행하면
    candidates = HybridReviewAgent().review_targeted(
        pages=[_page(1, "앞 페이지"), _page(2, "건물 2030년 배출량 10"), _page(3, "뒤 페이지")],
        final_data=final_data,
        extraction_prompts={},
    )

    # Then: review stage로 호출하고 후보 시트만 채우며 원본 행은 병합/수정하지 않는다.
    assert stages == ["review"]
    assert len(candidates) == 1
    assert candidates[0]["검수상태"] == "타깃검수"
    assert final_data["emissions_management"] == [
        {"관리부문": "건물", "연도": 2030, "배출량": 10, "데이터상태": "conflicting", "출처페이지": "p2"}
    ]
    assert "hybrid_merge_log" not in final_data


def test_stage_review_provider_controls_temporary_backend_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 하이브리드 provider와 review stage provider가 서로 다르다.
    from agents import hybrid_review_agent as hybrid_module

    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(config, "HYBRID_REVIEW_PROVIDER", "claude")
    monkeypatch.setattr(config, "HYBRID_REVIEW_MODEL", "claude-special")
    monkeypatch.setattr(config, "LOCAL_AGENT_MODEL", "codex-default")
    monkeypatch.setattr(config, "STAGE_PROVIDERS", {"review": "codex"})
    monkeypatch.setattr(config, "STAGE_MODELS", {"review": ""})
    monkeypatch.setattr(hybrid_module, "_backend_available", lambda provider: (True, ""))
    final_data = {
        "municipality_name": "서울특별시",
        "emissions_management": [
            {"관리부문": "건물", "연도": 2030, "배출량": 10, "데이터상태": "conflicting", "출처페이지": "p2"},
        ],
    }

    def fake_call_text_json(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None):
        observed.append((config.LLM_PROVIDER, config.LOCAL_AGENT_MODEL))
        return {"hybrid_review_candidates": []}, True

    monkeypatch.setattr(llm_client, "call_text_json", fake_call_text_json)

    # When: 타깃 검수를 실행하면
    HybridReviewAgent().review_targeted(
        pages=[_page(2, "건물 2030년 배출량 10")],
        final_data=final_data,
        extraction_prompts={},
    )

    # Then: 실제 임시 백엔드와 모델은 STAGE_PROVIDER_REVIEW 기준으로 일관된다.
    assert observed == [("codex", "codex-default")]
