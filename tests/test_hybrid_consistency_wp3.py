from __future__ import annotations

import json

import pytest

import config
from agents import hybrid_review_agent as hybrid_module
from agents.hybrid_review_agent import HybridReviewAgent, reconcile_reflected_merge_log
from utils import llm_client
from utils.pdf_reader import PageContent


SHEET_KEY = "emissions_management"
SHEET_NAME = config.SHEET_KEY_TO_NAME[SHEET_KEY]


def _page(number: int) -> PageContent:
    return PageContent(page_number=number, text="서울특별시 건물 도로 수송 배출량", tables=[], images=[])


def _configure_sheetwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_REVIEW_MODEL", "gemini-test")
    monkeypatch.setattr(config, "HYBRID_REVIEW_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 1)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MODEL", "gemini-test")
    monkeypatch.setattr(config, "HYBRID_AUTO_MERGE_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(hybrid_module, "_route_pages_by_sheet", lambda pages: {SHEET_KEY: pages})


def test_missing_candidate_duplicate_is_filtered_before_adjudication(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: organizer dedup 키로는 이미 기본본에 존재하는 누락후보가 보조검수에서 반환된다.
    _configure_sheetwise(monkeypatch)
    final_data = {
        "municipality_name": "서울특별시",
        SHEET_KEY: [
            {
                "지자체명": "서울특별시",
                "관리부문": "건물",
                "세부부문": "도로 수송",
                "직간접구분": "직접배출",
                "연도": 2030,
                "배출량": 10,
            }
        ],
    }

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        if "[판정 후보 목록]" in prompt:
            raise AssertionError("중복 누락후보는 판정 단계까지 가면 안 됩니다")
        return json.dumps({
            "hybrid_review_candidates": [
                {
                    "대상시트": SHEET_NAME,
                    "후보유형": "누락후보",
                    "신뢰도": "high",
                    "근거페이지": "p1",
                    "candidate_row": {
                        "지자체명": "서울특별시",
                        "관리부문": "건물",
                        "세부부문": "도로　수송",
                        "직간접구분": "직접배출",
                        "연도": "2030",
                        "배출량": 10,
                    },
                    "base_similar_row": None,
                    "reason": "테스트 중복 후보",
                    "merge_recommendation": "검토후병합",
                }
            ]
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: 시트 단위 검수·판정을 실행하면
    _, candidates, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=[_page(1)],
        final_data=final_data,
        extraction_prompts={SHEET_KEY: ""},
        target_sheets=[SHEET_KEY],
        routed_pages={SHEET_KEY: [_page(1)]},
    )

    # Then: 후보 생성 단계에서 같은 organizer dedup 키로 중복이 제거되어 자기모순 차단이 없다.
    assert candidates == []
    assert merge_log == []


def test_reflected_merge_log_is_downgraded_when_final_sheet_lacks_same_row() -> None:
    # Given: 병합 로그는 반영이라고 기록됐지만 최종 시트에는 같은 key의 다른 값만 남아 있다.
    merge_log = [
        {
            "대상시트": config.SHEET_KEY_TO_NAME["reduction_targets"],
            "최종반영여부": "반영",
            "정규화행JSON": json.dumps({
                "지자체명": "서울특별시",
                "목표수준": "총괄",
                "목표범위": "GIR",
                "부문": "합계",
                "목표연도": 2030,
                "목표배출량": 61,
            }, ensure_ascii=False),
            "병합차단사유": "",
        }
    ]
    final_data = {
        "reduction_targets": [
            {
                "지자체명": "서울특별시",
                "목표수준": "총괄",
                "목표범위": "GIR",
                "부문": "합계",
                "목표연도": 2030,
                "목표배출량": 60,
            }
        ]
    }

    # When: 최종 시트 기준으로 병합 로그를 재대조하면
    reconciled = reconcile_reflected_merge_log(final_data, merge_log)

    # Then: 최종 시트에 같은 정규화 값이 없는 반영 로그는 보류로 내려간다.
    assert reconciled[0]["최종반영여부"] == "보류(병합후중복제거)"
    assert "병합후중복제거" in reconciled[0]["병합차단사유"]


def test_reflected_merge_log_stays_reflected_when_final_sheet_contains_same_row() -> None:
    # Given: 병합 로그의 정규화행JSON이 최종 시트에 그대로 남아 있다.
    normalized_row = {
        "지자체명": "서울특별시",
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접배출",
        "연도": 2030,
        "배출량": 10,
    }
    merge_log = [
        {
            "대상시트": SHEET_NAME,
            "최종반영여부": "반영",
            "정규화행JSON": json.dumps(normalized_row, ensure_ascii=False),
            "병합차단사유": "",
        }
    ]

    # When: 최종 시트 기준으로 병합 로그를 재대조하면
    reconciled = reconcile_reflected_merge_log({SHEET_KEY: [normalized_row]}, merge_log)

    # Then: 실제 존재하는 반영 행은 그대로 유지된다.
    assert reconciled[0]["최종반영여부"] == "반영"
