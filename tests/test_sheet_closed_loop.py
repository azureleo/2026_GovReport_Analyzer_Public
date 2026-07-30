from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

import config
from agents import supervisor as supervisor_module
from agents.extractor_agent import ExtractorAgent
from utils import llm_client
from utils.pdf_reader import PageContent


SHEET_A = "emissions_management"
SHEET_B = "reduction_targets"
SHEET_C = "mitigation_projects"


def _page(page_number: int, text: str = "서울특별시 온실가스 배출량 감축목표") -> PageContent:
    return PageContent(page_number=page_number, text=text, tables=[], images=[])


def test_sheet_closed_loop_default_is_disabled() -> None:
    # Given / When: 별도 환경 오버라이드 없이 config를 읽으면
    # Then: 실험 경로인 시트별 폐루프는 기본 비활성이다.
    assert config.SHEET_CLOSED_LOOP_ENABLED is False


def test_route_pages_full_scan_public_wrapper_keeps_all_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 전체 문서 스캔 모드와 두 페이지 입력
    monkeypatch.setattr(config, "FULL_DOCUMENT_SCAN", True)
    pages = [_page(1), _page(2)]
    agent = ExtractorAgent()

    # When: 공개 라우팅 래퍼를 호출하면
    routed = agent.route_pages(pages)

    # Then: 모든 추출 시트가 모든 페이지를 후보로 공유하고 라우팅 원장도 갱신된다.
    assert routed[SHEET_A] == pages
    assert all(agent.routed_page_nums[key] == {1, 2} for key in config.EXTRACTION_SHEETS)


def test_extract_sheet_pages_runs_batches_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 한 시트의 네 배치를 두 워커로 실행하도록 설정한다.
    monkeypatch.setattr(config, "PARALLEL_PROCESSING_ENABLED", True)
    monkeypatch.setattr(config, "TEXT_WORKERS", 2)
    pages = [_page(page_number) for page_number in range(1, 5)]
    agent = ExtractorAgent()
    active = 0
    max_active = 0
    lock = threading.Lock()
    first_pair_barrier = threading.Barrier(2)

    def fake_extract(
        sheet_key: str,
        batch_text: str,
        municipality: str,
        guideline_prompt: str = "",
        **kwargs,
    ) -> list[dict]:
        nonlocal active, max_active
        page_number = int(batch_text.split("페이지 ", maxsplit=1)[1].split(" ", maxsplit=1)[0])
        with lock:
            active += 1
            max_active = max(max_active, active)
        if page_number in {1, 2}:
            try:
                first_pair_barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass
        time.sleep(0.02)
        with lock:
            active -= 1
        agent._record_batch(sheet_key, [page_number], "ok", 1)
        return [{"지자체명": municipality, "연도": page_number, "관리부문": "건물", "배출량": page_number}]

    monkeypatch.setattr(agent, "_extract_sheet", fake_extract)

    # When: 시트 단위 공개 추출 메서드를 실행하면
    rows = agent.extract_sheet_pages(SHEET_A, pages, "서울특별시", {}, batch_size=1)

    # Then: 시트 내 배치가 병렬로 겹쳐 실행되고 결과 순서·원장 형식을 유지한다.
    assert max_active >= 2
    assert [row["연도"] for row in rows] == [1, 2, 3, 4]
    assert [record.status for record in agent.ledger] == ["ok", "ok", "ok", "ok"]


def test_hybrid_sheetwise_max_candidates_override_limits_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 한 시트 후보 2건이 나오지만 호출별 override 상한이 1인 보조검수
    from agents import hybrid_review_agent as hybrid_module
    from agents.hybrid_review_agent import HybridReviewAgent

    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_PROVIDER", "gemini")
    monkeypatch.setattr(config, "HYBRID_REVIEW_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 2)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_BATCH_SIZE", 4)
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0)
    monkeypatch.setattr(config, "HYBRID_AUTO_MERGE_ENABLED", False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(hybrid_module, "_route_pages_by_sheet", lambda pages: {SHEET_A: pages})
    adjudicated_ids: list[str] = []

    def fake_call_text(prompt: str, system: str = "", max_retries: int = 1, stage: str | None = None) -> str:
        if "[판정 후보 목록]" in prompt:
            import json

            payload = json.loads(prompt.split("[판정 후보 목록]", maxsplit=1)[1].split("아래 JSON", maxsplit=1)[0])
            adjudicated_ids.extend(str(item["candidate_id"]) for item in payload)
            return json.dumps({
                "adjudications": [
                    {
                        "candidate_id": item["candidate_id"],
                        "decision": "reject",
                        "confidence": "high",
                        "evidence_page": "p1",
                        "evidence_text": "근거",
                        "normalized_row": {},
                        "reason": "테스트",
                        "risk_flags": [],
                    }
                    for item in payload
                ]
            }, ensure_ascii=False)
        import json

        page_number = prompt.split("검수 페이지: p", maxsplit=1)[1].split("\n", maxsplit=1)[0]
        return json.dumps({
            "hybrid_review_candidates": [
                {
                    "대상시트": config.SHEET_KEY_TO_NAME[SHEET_A],
                    "후보유형": "누락후보",
                    "신뢰도": "high",
                    "근거페이지": f"p{page_number}",
                    "candidate_row": {"지자체명": "서울특별시", "관리부문": "건물", "세부부문": page_number, "연도": 2030, "배출량": 1},
                    "base_similar_row": None,
                    "reason": "테스트 후보",
                    "merge_recommendation": "검토후병합",
                }
            ]
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "call_text", fake_call_text)

    # When: override 상한 1로 시트 단위 검수·판정을 실행하면
    _, candidates, merge_log = HybridReviewAgent().review_and_adjudicate_by_sheet(
        pages=[_page(1), _page(2)],
        final_data={"municipality_name": "서울특별시", SHEET_A: []},
        extraction_prompts={SHEET_A: ""},
        target_sheets=[SHEET_A],
        routed_pages={SHEET_A: [_page(1), _page(2)]},
        max_candidates_override=1,
    )

    # Then: 후보는 2건 남기되 판정은 1건에서 멈춘다.
    assert len(candidates) == 2
    assert len(merge_log) == 1
    assert adjudicated_ids == [f"{SHEET_A}-1"]


@dataclass
class _FakeExtractor:
    events: list[str]
    routes: dict[str, list[PageContent]]
    ledger: list = field(default_factory=list)
    routed_page_nums: dict[str, set[int]] = field(default_factory=dict)

    def _extract_municipality_name(self, full_text: str) -> str:
        return "서울특별시"

    def route_pages(self, pages: list[PageContent]) -> dict[str, list[PageContent]]:
        self.routed_page_nums = {key: {page.page_number for page in value} for key, value in self.routes.items()}
        return self.routes

    def extract_sheet_pages(
        self,
        sheet_key: str,
        sheet_pages: list[PageContent],
        municipality: str,
        extraction_prompts: dict[str, str],
        batch_size: int = config.BATCH_SIZE,
    ) -> list[dict]:
        self.events.append(f"{sheet_key}:extract")
        return [{"지자체명": municipality, "시트": sheet_key, "출처페이지": [page.page_number for page in sheet_pages]}]


@dataclass
class _FakeOrganizer:
    events: list[str]

    def organize_sheet(self, sheet_key: str, rows: list[dict], municipality: str) -> list[dict]:
        self.events.append(f"{sheet_key}:clean")
        return [dict(row, 지자체명=municipality) for row in rows]


class _CappedHybrid:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._last_review_candidates: list[dict] = []
        self._last_merge_log: list[dict] = []

    def review_and_adjudicate_by_sheet(
        self,
        *,
        pages: list[PageContent],
        final_data: dict,
        extraction_prompts: dict[str, str],
        progress=None,
        target_sheets: list[str] | None = None,
        routed_pages: dict[str, list[PageContent]] | None = None,
        max_candidates_override: int | None = None,
    ) -> tuple[dict, list[dict], list[dict]]:
        sheet_key = (target_sheets or [""])[0]
        self.events.append(f"{sheet_key}:review:{max_candidates_override}")
        count = 2 if max_candidates_override is None else min(2, max_candidates_override)
        self._last_review_candidates = [{"대상시트": sheet_key, "idx": idx} for idx in range(1, 3)]
        self._last_merge_log = [
            {"대상시트": sheet_key, "최종반영여부": "반영" if idx == 1 else "보류"}
            for idx in range(1, count + 1)
        ]
        if count:
            final_data.setdefault(sheet_key, []).append({"지자체명": "서울특별시", "병합": sheet_key})
        return final_data, self._last_review_candidates, self._last_merge_log


class _QuotaHybrid(_CappedHybrid):
    def review_and_adjudicate_by_sheet(self, **kwargs):
        sheet_key = (kwargs.get("target_sheets") or [""])[0]
        if sheet_key == SHEET_B:
            self.events.append(f"{sheet_key}:review:quota")
            self._last_review_candidates = [{"대상시트": sheet_key, "idx": "partial"}]
            self._last_merge_log = [{"대상시트": sheet_key, "최종반영여부": "보류"}]
            raise llm_client.LLMQuotaExceededError("quota")
        return super().review_and_adjudicate_by_sheet(**kwargs)


def test_supervisor_closed_loop_interleaves_sheet_steps_and_preserves_global_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 두 시트와 전역 후보판정 상한 3건인 폐루프 실행
    events: list[str] = []
    routes = {SHEET_A: [_page(1)], SHEET_B: [_page(2)]}
    fake_extractor = _FakeExtractor(events, routes)
    fake_organizer = _FakeOrganizer(events)
    fake_hybrid = _CappedHybrid(events)
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_SHEETS", [SHEET_A, SHEET_B])
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 3)
    monkeypatch.setattr(supervisor_module, "ExtractorAgent", lambda **kwargs: fake_extractor)
    monkeypatch.setattr(supervisor_module, "OrganizerAgent", lambda: fake_organizer)
    monkeypatch.setattr(supervisor_module, "HybridReviewAgent", lambda: fake_hybrid)

    # When: 시트별 폐루프 텍스트 경로를 실행하면
    raw_data, extractor, candidates, merge_log = supervisor_module.Supervisor()._run_sheet_closed_loop(
        pages=[_page(1), _page(2)],
        full_text="서울특별시 탄소중립 기본계획",
        extraction_prompts={},
    )

    # Then: 각 시트는 추출→정제→검수→재정제 순서로 닫히고 판정 잔여 상한이 다음 시트로 전달된다.
    assert extractor is fake_extractor
    assert events == [
        f"{SHEET_A}:extract",
        f"{SHEET_A}:clean",
        f"{SHEET_A}:review:3",
        f"{SHEET_A}:clean",
        f"{SHEET_B}:extract",
        f"{SHEET_B}:clean",
        f"{SHEET_B}:review:1",
        f"{SHEET_B}:clean",
    ]
    assert len(candidates) == 4
    assert len(merge_log) == 3


def test_supervisor_closed_loop_quota_keeps_partial_logs_and_continues_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: 두 번째 시트 검수에서 quota가 발생하는 세 시트 폐루프
    events: list[str] = []
    routes = {SHEET_A: [_page(1)], SHEET_B: [_page(2)], SHEET_C: [_page(3)]}
    fake_extractor = _FakeExtractor(events, routes)
    fake_organizer = _FakeOrganizer(events)
    fake_hybrid = _QuotaHybrid(events)
    monkeypatch.setattr(config, "HYBRID_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "HYBRID_REVIEW_SHEETS", [SHEET_A, SHEET_B, SHEET_C])
    monkeypatch.setattr(config, "HYBRID_ADJUDICATION_MAX_CANDIDATES", 0)
    monkeypatch.setattr(supervisor_module, "ExtractorAgent", lambda **kwargs: fake_extractor)
    monkeypatch.setattr(supervisor_module, "OrganizerAgent", lambda: fake_organizer)
    monkeypatch.setattr(supervisor_module, "HybridReviewAgent", lambda: fake_hybrid)

    # When: 폐루프가 quota를 만나면
    raw_data, _, candidates, merge_log = supervisor_module.Supervisor()._run_sheet_closed_loop(
        pages=[_page(1), _page(2), _page(3)],
        full_text="서울특별시 탄소중립 기본계획",
        extraction_prompts={},
    )

    # Then: 완료·부분 검수 로그는 보존하고 남은 시트는 검수를 건너뛰되 추출·정제는 계속한다.
    assert f"{SHEET_C}:extract" in events
    assert f"{SHEET_C}:clean" in events
    assert not any(event.startswith(f"{SHEET_C}:review") for event in events)
    assert [row["대상시트"] for row in candidates] == [SHEET_A, SHEET_A, SHEET_B]
    assert [row["대상시트"] for row in merge_log] == [SHEET_A, SHEET_A, SHEET_B]
    assert SHEET_C in raw_data
