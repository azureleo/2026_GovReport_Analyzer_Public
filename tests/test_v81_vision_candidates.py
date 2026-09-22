"""v8-1 S3 — vision 후보·배치·타임아웃 조정: 목차 캡션 프록시 제외, 문맥 렌더 조건화, 단계별 타임아웃."""

from __future__ import annotations

from unittest.mock import patch

import fitz

import config
from agents.image_agent import _candidate_metrics, _vision_batch_size
from utils import llm_client
from utils.document_objects import build_document_objects
from utils.pdf_reader import PageContent
from utils.selective_ocr import (
    CandidateRenderRegion,
    _context_render_allowed,
    build_triage_plan,
    index_caption_pages,
)


def _caption_list_page(page_number: int, count: int, *, index_heading: bool) -> PageContent:
    lines = ["표 목차"] if index_heading else ["제3장 온실가스 배출 현황"]
    blocks = []
    for i in range(1, count + 1):
        caption = f"표 3-{i} 온실가스 배출 현황 {i}"
        lines.append(f"{caption} ······ {10 + i}" if index_heading else caption)
        blocks.append({"block_id": i, "text": caption, "bbox": [60, 80 + i * 30, 400, 100 + i * 30]})
    return PageContent(
        page_number=page_number, text="\n".join(lines), tables=[], images=[],
        text_blocks=blocks, width=595, height=842,
    )


def test_index_page_caption_proxies_are_skipped_even_with_strong_signals() -> None:
    page = _caption_list_page(2, 8, index_heading=True)
    objects = build_document_objects([page])
    plan = build_triage_plan(
        objects, backend="vlm", confidence_threshold=0.78,
        page_texts={page.page_number: page.text},
    )
    proxies = [row for row in plan if row.object_type == "table"]
    assert proxies and all(row.action == "skip_index_caption" for row in proxies)
    assert all(row.final_status == "not_relevant" for row in proxies)
    assert all("index_page_caption_reference" in row.reasons for row in proxies)


def test_many_caption_proxies_without_native_objects_count_as_list_page() -> None:
    page = _caption_list_page(3, 6, index_heading=False)
    objects = build_document_objects([page])
    assert index_caption_pages(objects, None) == {3}
    plan = build_triage_plan(objects, backend="vlm", confidence_threshold=0.78)
    assert all(row.action == "skip_index_caption" for row in plan if row.object_type == "table")


def test_single_caption_proxy_on_body_page_is_still_ocr_required() -> None:
    page = _caption_list_page(5, 1, index_heading=False)
    objects = build_document_objects([page])
    plan = build_triage_plan(
        objects, backend="vlm", confidence_threshold=0.78,
        page_texts={page.page_number: page.text},
    )
    proxy = next(row for row in plan if row.object_type == "table")
    assert proxy.action == "ocr_required"
    assert index_caption_pages(objects, {5: page.text}) == set()


def test_full_page_context_only_for_panels_or_small_objects() -> None:
    bounds = fitz.Rect(0, 0, 600, 800)
    big = [CandidateRenderRegion("object", (20, 20, 580, 600), "native_bbox")]
    small = [CandidateRenderRegion("object", (20, 20, 120, 100), "native_bbox")]
    panels = [
        CandidateRenderRegion("panel", (20, 20, 300, 300), "adjacent_image_cluster", panel_index=1, panel_count=2),
        CandidateRenderRegion("panel", (320, 20, 580, 300), "adjacent_image_cluster", panel_index=2, panel_count=2),
    ]
    assert _context_render_allowed(big, bounds) is False
    assert _context_render_allowed(small, bounds) is True
    assert _context_render_allowed(panels, bounds) is True


def test_vision_timeout_and_batch_defaults_depend_on_stage_and_backend() -> None:
    with patch.object(config, "LOCAL_AGENT_TIMEOUT", 300), patch.object(config, "LOCAL_AGENT_VISION_TIMEOUT", 600):
        assert llm_client.local_agent_timeout("extraction") == 300
        assert llm_client.local_agent_timeout("vision") == 600
        assert llm_client.local_agent_timeout(None) == 300
    with patch.object(config, "LLM_PROVIDER", "codex"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _vision_batch_size() == config.IMAGE_ANALYSIS_BATCH_SIZE_LOCAL_AGENT
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _vision_batch_size() == config.IMAGE_ANALYSIS_BATCH_SIZE
    with patch.object(config, "LLM_PROVIDER", "gemini"), \
            patch.object(config, "STAGE_PROVIDERS", {"vision": "codex"}):
        assert _vision_batch_size() == config.IMAGE_ANALYSIS_BATCH_SIZE_LOCAL_AGENT
    assert config.VISION_RECOVERY_MAX_OBJECT_ATTEMPTS == 3


def test_candidate_metrics_count_variants_and_batches() -> None:
    page = PageContent(page_number=1, text="", tables=[], images=[])
    candidates = [
        (page, {"render_variant": "object"}),
        (page, {"render_variant": "object"}),
        (page, {"render_variant": "full_page_context"}),
        (page, {"source_kind": "page_render"}),
    ]
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "IMAGE_ANALYSIS_BATCH_SIZE", 3):
        metrics = _candidate_metrics(candidates)
    assert metrics["vision_candidates_total"] == 4
    assert metrics["vision_batches_total"] == 2
    assert metrics["vision_candidates_variant.object"] == 2
    assert metrics["vision_candidates_variant.full_page_context"] == 1
    assert metrics["vision_candidates_variant.page_render"] == 1
