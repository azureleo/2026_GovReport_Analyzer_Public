from __future__ import annotations

import json
import re

import pytest

import config
from agents import hybrid_review_agent as hybrid_module
from agents.hybrid_review_agent import HybridReviewAgent, _backend_available
from utils import llm_client
from utils.pdf_reader import PageContent


SHEET_KEY = "emissions_management"
SHEET_NAME = config.SHEET_KEY_TO_NAME[SHEET_KEY]


def _pages(count: int) -> list[PageContent]:
    return [
        PageContent(
            page_number=idx,
            text=f"서울특별시 수송 부문 배출량 {idx}톤 자체 계획",
            tables=[f"표 {idx}-1 수송 배출량"],
            images=[],
        )
        for idx in range(1, count + 1)
    ]


def _candidate_payload(value: int, *, candidate_type: str = "누락후보") -> dict:
    return {
        "대상시트": SHEET_NAME,
        "후보유형": candidate_type,
        "신뢰도": "high",
        "근거페이지": f"p{value}",
        "candidate_row": {
            "지자체명": "서울특별시",
            "관리부문": "수송",
            "세부부문": f"승용차{value}",
            "직간접구분": "직접배출",
            "연도": 2030,
            "배출량": value,
            "단위": "톤CO2eq",
        },
        "base_similar_row": None,
        "reason": f"p{value} 표에서 확인",
        "merge_recommendation": "검토후병합",
    }


def _configure_hybrid(monkeypatch: pytest.MonkeyPatch, *, auto_merge: bool = False, adjudication: bool = True) -> None:
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_REVIEW_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setattr(config, "HYBRID_REVIEW_SHEETS", [SHEET_KEY])
    monkeypatch.setattr(config, "HYBRID_REVIEW_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 10)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_ENABLED", adjudication)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MODEL", "gemini-2.5-pro")
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_CONTEXT_CHARS", 2000)
    monkeypatch.setattr(config, "HYBRID_AUTO_MERGE_ENABLED", auto_merge)
    monkeypatch.setattr(config, "HYBRID_AUTO_MERGE_MIN_CONFIDENCE", "high")
    monkeypatch.setattr(config, "HYBRID_PROGRESS_LOG_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(hybrid_module, "_route_pages_by_sheet", lambda pages: {SHEET_KEY: pages})


def _page_number_from_review_prompt(prompt: str) -> int:
    match = re.search(r"검수 페이지:\s*p(\d+)", prompt)
    assert match is not None
    return int(match.group(1))


def _prepared_candidate_ids(prompt: str) -> list[str]:
    payload_text = prompt.split("[판정 후보 목록]", maxsplit=1)[1].split("아래 JSON", maxsplit=1)[0]
    payload = json.loads(payload_text)
    return [str(item["candidate_id"]) for item in payload]


def _review_response(value: int) -> str:
    return json.dumps({"hybrid_review_candidates": [_candidate_payload(value)]}, ensure_ascii=False)


def _adjudication_response(candidate_ids: list[str], decisions: list[dict] | None = None) -> str:
    rows = decisions or [
        {
            "candidate_id": candidate_id,
            "decision": "reject",
            "confidence": "high",
            "evidence_page": "p1",
            "evidence_text": "원문 근거",
            "normalized_row": {},
            "reason": "테스트 판정",
            "risk_flags": [],
        }
        for candidate_id in candidate_ids
    ]
    return json.dumps({"adjudications": rows}, ensure_ascii=False)


def test_sheetwise_review_deduplicates_and_normalises_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 두 페이지에서 같은 후보가 반복 반환되는 시트 단위 검수 설정
    _configure_hybrid(monkeypatch, adjudication=False)
    pages = _pages(2)

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        return _review_response(1)

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: 시트 단위 후보 탐색을 실행하면
    final_data, candidates, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=pages,
        final_data={"municipality_name": "서울특별시", SHEET_KEY: []},
        extraction_prompts={SHEET_KEY: ""},
    )

    # Then: 동일 후보는 한 번만 남고 후보 행은 Excel 후보 스키마로 정규화된다.
    assert final_data[SHEET_KEY] == []
    assert merge_log == []
    assert len(candidates) == 1
    assert candidates[0]["대상시트"] == SHEET_NAME
    assert candidates[0]["후보유형"] == "누락후보"
    assert json.loads(candidates[0]["후보행JSON"])["세부부문"] == "승용차1"


def test_sheetwise_adjudication_uses_configured_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 후보 5건과 묶음 크기 2인 시트 단위 판정 설정
    _configure_hybrid(monkeypatch)
    pages = _pages(5)
    adjudication_calls = []

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        if "[판정 후보 목록]" in prompt:
            candidate_ids = _prepared_candidate_ids(prompt)
            adjudication_calls.append(candidate_ids)
            return _adjudication_response(candidate_ids)
        return _review_response(_page_number_from_review_prompt(prompt))

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: 시트 단위 검수와 판정을 실행하면
    _, candidates, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=pages,
        final_data={"municipality_name": "서울특별시", SHEET_KEY: []},
        extraction_prompts={SHEET_KEY: ""},
    )

    # Then: 후보 5건은 2, 2, 1 세 번의 묶음 호출로 판정된다.
    assert len(candidates) == 5
    assert len(merge_log) == 5
    assert [len(call) for call in adjudication_calls] == [2, 2, 1]


def test_batch_adjudication_missing_candidate_ids_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 모델이 candidate_id 없이 일부 후보만 반환하는 잘못된 묶음 판정 응답
    _configure_hybrid(monkeypatch, auto_merge=True)
    pages = _pages(2)

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        if "[판정 후보 목록]" in prompt:
            return json.dumps({
                "adjudications": [
                    {
                        "decision": "accept",
                        "confidence": "high",
                        "evidence_page": "p1",
                        "evidence_text": "원문 근거",
                        "normalized_row": _candidate_payload(1)["candidate_row"],
                        "reason": "id 누락",
                        "risk_flags": [],
                    }
                ]
            }, ensure_ascii=False)
        return _review_response(_page_number_from_review_prompt(prompt))

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: 묶음 판정 결과를 적용하면
    final_data, _, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=pages,
        final_data={"municipality_name": "서울특별시", SHEET_KEY: []},
        extraction_prompts={SHEET_KEY: ""},
    )

    # Then: 위치 기반 오배정 없이 두 후보 모두 판정누락으로 보류된다.
    assert final_data[SHEET_KEY] == []
    assert [row["판정"] for row in merge_log] == ["needs_human", "needs_human"]
    assert all("판정누락" in row["위험플래그"] for row in merge_log)
    assert all(row["최종반영여부"] == "보류" for row in merge_log)


def test_auto_merge_only_accepts_high_confidence_unblocked_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: accept/high, accept/medium, reject/high 판정이 섞인 후보 3건
    _configure_hybrid(monkeypatch, auto_merge=True)
    pages = _pages(3)

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        if "[판정 후보 목록]" in prompt:
            candidate_ids = _prepared_candidate_ids(prompt)
            decisions = []
            for idx, candidate_id in enumerate(candidate_ids, start=1):
                decision = "accept" if idx < 3 else "reject"
                confidence = "high" if idx != 2 else "medium"
                decisions.append({
                    "candidate_id": candidate_id,
                    "decision": decision,
                    "confidence": confidence,
                    "evidence_page": f"p{idx}",
                    "evidence_text": "서울특별시 자체 배출량 근거",
                    "normalized_row": _candidate_payload(idx)["candidate_row"],
                    "reason": "테스트 판정",
                    "risk_flags": [],
                })
            return _adjudication_response(candidate_ids, decisions)
        return _review_response(_page_number_from_review_prompt(prompt))

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: 자동 병합을 활성화한 시트 단위 흐름을 실행하면
    final_data, _, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=pages,
        final_data={"municipality_name": "서울특별시", SHEET_KEY: []},
        extraction_prompts={SHEET_KEY: ""},
    )

    # Then: 안전 조건을 모두 통과한 accept/high 후보만 본 시트에 반영된다.
    assert len(final_data[SHEET_KEY]) == 1
    assert final_data[SHEET_KEY][0]["세부부문"] == "승용차1"
    assert [row["최종반영여부"] for row in merge_log] == ["반영", "보류", "보류"]


def test_sheetwise_review_propagates_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 후보 탐색 호출이 quota 예외를 발생시키는 보조 검수 설정
    _configure_hybrid(monkeypatch)

    def raise_quota(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        raise llm_client.LLMQuotaExceededError("quota")

    monkeypatch.setattr(llm_client, "call_text", raise_quota)

    # When / Then: quota는 상위 Supervisor가 처리할 수 있도록 전파된다.
    with pytest.raises(llm_client.LLMQuotaExceededError):
        HybridReviewAgent().review_and_adjudicate_by_sheet(
            pages=_pages(1),
            final_data={"municipality_name": "서울특별시", SHEET_KEY: []},
            extraction_prompts={SHEET_KEY: ""},
        )


def test_backend_available_supports_api_keys_and_local_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: API 키와 로컬 CLI 존재 여부가 환경별로 달라질 수 있다.
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(config, "CODEX_COMMAND", "codex")
    monkeypatch.setattr(config, "CLAUDE_COMMAND", "claude")
    monkeypatch.setattr(llm_client, "_command_exists", lambda command: command == "codex")

    # When / Then: API 백엔드는 키로, 로컬 백엔드는 CLI 존재로 판단된다.
    assert _backend_available("gemini") == (False, "GEMINI_API_KEY 미설정")
    assert _backend_available("openai") == (True, "")
    assert _backend_available("codex") == (True, "")
    assert _backend_available("claude")[0] is False
    assert _backend_available("auto") == (True, "")
