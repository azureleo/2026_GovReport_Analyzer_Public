from __future__ import annotations

from unittest.mock import patch

import config
from agents.extractor_agent import ExtractorAgent
from utils import parallel
from utils.document_objects import build_document_objects
from utils.pdf_reader import PDFContent, PageContent
from utils.pipeline_artifacts import PipelineArtifacts


def _document() -> PDFContent:
    page = PageContent(
        page_number=1,
        text="표 1 온실가스 배출량\n연도  배출량\n2020  100",
        tables=["<table><tr><th>연도</th><th>배출량</th></tr><tr><td>2020</td><td>100</td></tr></table>"],
        images=[],
    )
    return PDFContent(total_pages=1, pages=[page], full_text=page.text)


def test_pipeline_artifacts_build_document_objects_once_and_reuse_prompt_snapshot() -> None:
    document = _document()
    prompts = {"emissions_regional": "지침"}

    with patch(
        "utils.pipeline_artifacts.build_document_objects",
        wraps=build_document_objects,
    ) as builder:
        artifacts = PipelineArtifacts.create(document, prompts)
        first = artifacts.objects_for_pages([1])
        second = artifacts.objects_for_pages([1])
        mutable = artifacts.clone_document_objects()

    assert builder.call_count == 1
    assert first == second
    assert len(first) == len(artifacts.document_objects)
    assert artifacts.extraction_prompts == prompts
    prompts["emissions_regional"] = "변경"
    assert artifacts.extraction_prompts["emissions_regional"] == "지침"
    mutable[0].metadata["triage_action"] = "ocr_required"
    assert "triage_action" not in artifacts.document_objects[0].metadata


def test_text_enqueue_deduplicates_only_exact_contract_page_prompt_and_payload(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_QUEUE_DEDUP_ENABLED", True)
    agent = ExtractorAgent()
    base = {
        "sheet_key": "emissions_regional",
        "page_nums": [10, 11],
        "batch_text": "=== 페이지 10 ===\nA\n=== 페이지 11 ===\nB",
        "guideline_prompt": "동일 지침",
    }
    duplicate = dict(base)
    different_prompt = {**base, "guideline_prompt": "다른 지침"}
    different_payload = {**base, "batch_text": base["batch_text"] + "\n추가"}

    prepared = agent._prepare_tasks(
        [base, duplicate, different_prompt, different_payload],
        "sheet",
    )

    assert len(prepared) == 3
    assert agent.enqueued_tasks == 3
    assert agent.deduplicated_tasks == 1
    assert all(task.get("enqueue_fingerprint") for task in prepared)


def test_parallel_queue_metrics_record_submitted_started_completed() -> None:
    parallel.reset_parallel_stats()

    results = parallel.parallel_map_collect(
        lambda value: value * 2,
        [1, 2, 3],
        workers=2,
        stats_label="test_queue",
    )

    assert results == [(2, None), (4, None), (6, None)]
    stats = parallel.get_parallel_stats()["test_queue"]
    assert stats["submitted"] == 3
    assert stats["started"] == 3
    assert stats["completed"] == 3
    assert stats["queue_wait_seconds"] >= 0
    assert stats["max_queue_wait_seconds"] >= 0
