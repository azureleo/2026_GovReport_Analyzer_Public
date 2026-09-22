from pathlib import Path

import fitz

import config
from agents.image_agent import (
    ImageAgent,
    _apply_reference_context_policy,
    _coverage_reduce_images,
)
from utils.document_objects import DocumentObject, build_document_objects
from utils.pdf_reader import PDFContent, PageContent
from utils.selective_ocr import (
    DirectoryOCRBackend,
    TriageDecision,
    apply_triage_metadata,
    build_triage_plan,
    evidence_id,
    merge_document_objects,
    parse_ocr_text,
    reconstruct_candidate_regions,
    reconcile_triage_decisions,
    render_ocr_candidate_images,
    strong_data_signals,
    vision_analyses_to_objects,
)


def _page() -> PageContent:
    table = "<table><tr><th>연도</th><th>배출량</th></tr><tr><td>2021</td><td>100</td></tr></table>"
    return PageContent(
        page_number=3,
        text="표 2-1 연도별 배출량\n그림 2-2 온실가스 배출량 추이",
        tables=[table],
        images=[{
            "base64": "image",
            "width": 400,
            "height": 300,
            "caption": "",
            "bbox": [100, 300, 500, 600],
            "source_kind": "embedded",
        }],
        text_blocks=[{
            "block_id": 1,
            "text": "표 2-1 연도별 배출량\n그림 2-2 온실가스 배출량 추이",
            "bbox": [50, 50, 400, 90],
            "line_count": 2,
            "char_count": 30,
        }],
        table_records=[{
            "html": table,
            "bbox": [50, 100, 500, 260],
            "strategy": "lines_strict",
            "row_count": 2,
            "column_count": 2,
        }],
        width=595,
        height=842,
        metadata={"drawing_count": 80},
    )


def test_document_objects_preserve_bbox_caption_number_and_type() -> None:
    objects = build_document_objects([_page()])

    table = next(obj for obj in objects if obj.object_type == "table")
    chart = next(obj for obj in objects if obj.object_type == "chart")
    text = next(obj for obj in objects if obj.object_type == "text")

    assert table.number == "표 2-1"
    assert table.bbox == (50.0, 100.0, 500.0, 260.0)
    assert table.metadata["extraction_strategy"] == "lines_strict"
    assert chart.number == "그림 2-2"
    assert text.bbox == (50.0, 50.0, 400.0, 90.0)


def test_triage_skips_complete_native_table_and_selects_chart() -> None:
    objects = build_document_objects([_page()])
    decisions = build_triage_plan(objects, backend="vlm", confidence_threshold=0.78)
    by_id = {row.object_id: row for row in decisions}

    table = next(obj for obj in objects if obj.object_type == "table")
    chart = next(obj for obj in objects if obj.object_type == "chart")

    assert by_id[table.object_id].action == "native_keep"
    assert by_id[table.object_id].final_status == "extracted"
    assert by_id[chart.object_id].action == "ocr_required"
    assert by_id[chart.object_id].final_status == "needs_review"
    assert by_id[chart.object_id].evidence_id.startswith("ev-p3-visual-")


def test_triage_serializes_physical_object_role_and_provenance() -> None:
    proxy = DocumentObject(
        object_id="p9_image_1",
        object_type="image",
        page_number=9,
        sequence=1,
        number="그림 2-1",
        caption="그림 2-1 감축 경로",
        metadata={
            "engine": "pymupdf",
            "render_proxy": True,
            "source_object_ids": ["p9_chart_1"],
        },
    )

    decision = build_triage_plan([proxy], backend="vlm", confidence_threshold=0.78)[0]
    serialized = decision.to_dict()

    assert serialized["render_proxy"] is True
    assert serialized["caption"] == "그림 2-1 감축 경로"
    assert serialized["number"] == "그림 2-1"
    assert serialized["canonical_object_id"] == "p9_image_1"
    assert serialized["physical_object_id"]
    assert set(serialized["source_object_ids"]) == {"p9_image_1", "p9_chart_1"}


def test_caption_without_native_table_becomes_ocr_candidate() -> None:
    page = PageContent(
        page_number=4,
        text="표 4-1 부문별 감축량",
        tables=[],
        images=[],
        text_blocks=[{
            "block_id": 1,
            "text": "표 4-1 부문별 감축량",
            "bbox": [60, 100, 300, 120],
        }],
        width=595,
        height=842,
    )
    objects = build_document_objects([page])
    missing = next(obj for obj in objects if obj.object_type == "table")
    decision = next(
        row for row in build_triage_plan(objects, backend="vlm", confidence_threshold=0.78)
        if row.object_id == missing.object_id
    )

    assert missing.metadata["missing_native"] is True
    assert decision.action == "ocr_required"


def test_strong_caption_signal_overrides_negative_reference_hint() -> None:
    image = DocumentObject(
        object_id="p89_image_1",
        object_type="image",
        page_number=89,
        sequence=1,
        number="그림 2-38",
        caption="그림 2-38 1991~2022년 서울시 연 최고기온 변화",
        nearby_text="참고 사례 사진 외부 관측자료",
    )

    decision = build_triage_plan([image], backend="vlm", confidence_threshold=0.78)[0]

    assert decision.action == "ocr_required"
    assert "strong:numbered_caption" in decision.reasons
    assert "strong:year_or_period" in decision.reasons
    assert "negative_overridden_by_strong_or_structured_signal" in decision.reasons


def test_decorative_numbered_photo_without_data_signal_is_still_skipped() -> None:
    image = DocumentObject(
        object_id="p10_image_1",
        object_type="image",
        page_number=10,
        sequence=1,
        number="그림 1-1",
        caption="그림 1-1 위원회 사진",
        nearby_text="행사 사진 참고 사례",
    )

    decision = build_triage_plan([image], backend="vlm", confidence_threshold=0.78)[0]

    assert strong_data_signals(image) == []
    assert decision.action == "skip_non_data"
    assert "no_strong_data_signal" in decision.reasons
    assert "decorative_or_reference" in decision.reasons


def test_visual_structure_signal_keeps_weak_caption_for_review() -> None:
    image = DocumentObject(
        object_id="p20_image_1",
        object_type="image",
        page_number=20,
        sequence=1,
        number="그림 3-2",
        caption="그림 3-2 분석 결과",
        nearby_text="참고 사례",
        metadata={"axis_detected": True, "drawing_count": "unknown"},
    )

    decision = build_triage_plan([image], backend="vlm", confidence_threshold=0.78)[0]

    assert decision.action == "ocr_required"
    assert "strong:visual_features:axis_detected" in decision.reasons


def test_caption_proxy_renders_panels_composite_and_page_context(tmp_path: Path) -> None:
    source = tmp_path / "multipanel.pdf"
    pdf = fitz.open()
    pdf_page = pdf.new_page(width=600, height=800)
    pdf_page.draw_rect((90, 200, 280, 380), color=(0, 0, 0))
    pdf_page.draw_rect((300, 200, 510, 380), color=(0, 0, 0))
    pdf_page.insert_text((180, 405), "Figure 2-1 panel statistics")
    pdf.save(source)
    pdf.close()

    page = PageContent(
        page_number=1,
        text="Figure 2-1 panel statistics",
        tables=[],
        images=[
            {"bbox": [90, 200, 280, 380], "source_kind": "embedded"},
            {"bbox": [300, 200, 510, 380], "source_kind": "embedded"},
        ],
        text_blocks=[{
            "text": "Figure 2-1 panel statistics",
            "bbox": [180, 390, 420, 412],
        }],
        width=600,
        height=800,
    )
    obj = DocumentObject(
        object_id="p1_chart_1",
        object_type="chart",
        page_number=1,
        sequence=1,
        number="Figure 2-1",
        caption="Figure 2-1 panel statistics",
        bbox=(180, 390, 420, 412),
        metadata={"caption_only": True},
    )
    image_alias = DocumentObject(
        object_id="p1_image_1",
        object_type="image",
        page_number=1,
        sequence=1,
        number="Figure 2-1",
        caption="Figure 2-1 panel statistics",
        bbox=(90, 200, 280, 380),
        metadata={"axis_detected": True},
    )
    decisions = build_triage_plan(
        [obj, image_alias], backend="vlm", confidence_threshold=0.78
    )
    decision = decisions[0]
    document = PDFContent(1, [page], page.text, source_path=str(source))

    rendered = render_ocr_candidate_images(document, [obj, image_alias], decisions)
    variants = [image["render_variant"] for _, image in rendered]

    assert variants.count("panel") == 2
    assert variants.count("composite") == 1
    assert variants.count("full_page_context") == 1
    assert all(image["source_evidence_ids"] == [decision.evidence_id] for _, image in rendered)
    assert all(
        set(image["source_object_ids"]) == {"p1_chart_1", "p1_image_1"}
        for _, image in rendered
    )
    assert all(image["physical_reconstruction"] is True for _, image in rendered)
    attached = ImageAgent._attach_source_metadata({}, rendered[0][1])
    assert attached["render_variant"] == "panel"
    assert attached["panel_count"] == 2


def test_identical_candidate_regions_reuse_rendered_png(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "render-cache.pdf"
    pdf = fitz.open()
    pdf.new_page(width=600, height=800)
    pdf.save(source)
    pdf.close()

    page = PageContent(
        page_number=1,
        text="",
        tables=[],
        images=[],
        width=600,
        height=800,
    )
    first = DocumentObject(
        object_id="p1_chart_1",
        object_type="chart",
        page_number=1,
        sequence=1,
        number="그림 1-1",
        caption="그림 1-1 배출량",
        bbox=(80, 120, 520, 420),
    )
    second = DocumentObject(
        object_id="p1_chart_2",
        object_type="chart",
        page_number=1,
        sequence=2,
        number="그림 1-2",
        caption="그림 1-2 감축량",
        bbox=(80, 120, 520, 420),
    )
    decisions = [
        TriageDecision(
            object_id=obj.object_id,
            evidence_id=f"ev-{obj.object_id}",
            object_type="chart",
            page_number=1,
            bbox=obj.bbox,
            native_confidence=0.0,
            action="ocr_required",
            reasons=["test"],
        )
        for obj in (first, second)
    ]
    document = PDFContent(1, [page], "", source_path=str(source))
    stats = {}
    monkeypatch.setattr(config, "OCR_RENDER_CACHE_ENABLED", True)

    rendered = render_ocr_candidate_images(
        document,
        [first, second],
        decisions,
        stats=stats,
    )

    assert len(rendered) == 2
    assert rendered[0][1]["base64"] == rendered[1][1]["base64"]
    assert stats["render_requests"] == 2
    assert stats["render_unique"] == 1
    assert stats["render_reused"] == 1


def test_caption_proxy_reconstructs_vector_table_without_full_page_crop(tmp_path: Path) -> None:
    source = tmp_path / "vector-table.pdf"
    pdf = fitz.open()
    pdf_page = pdf.new_page(width=600, height=800)
    for x in (80, 250, 420, 520):
        pdf_page.draw_line((x, 170), (x, 330), color=(0, 0, 0))
    for y in (170, 220, 270, 330):
        pdf_page.draw_line((80, y), (520, y), color=(0, 0, 0))
    pdf_page.insert_text((180, 145), "Table 3-1 climate indicators")
    pdf_page.insert_text((100, 205), "temperature 39.6 C")
    pdf.save(source)
    pdf.close()

    page = PageContent(
        page_number=1,
        text="Table 3-1 climate indicators\ntemperature 39.6 C",
        tables=[],
        images=[],
        text_blocks=[
            {"text": "Table 3-1 climate indicators", "bbox": [180, 130, 430, 150]},
            {"text": "temperature 39.6 C", "bbox": [100, 190, 280, 210]},
        ],
        width=600,
        height=800,
    )
    obj = DocumentObject(
        object_id="p1_table_1",
        object_type="table",
        page_number=1,
        sequence=1,
        number="Table 3-1",
        caption="Table 3-1 climate indicators",
        bbox=(180, 130, 430, 150),
        metadata={"missing_native": True},
    )
    reopened = fitz.open(source)
    try:
        regions = reconstruct_candidate_regions(reopened[0], page, obj, [obj])
    finally:
        reopened.close()

    object_region = next(region for region in regions if region.variant == "object")
    assert object_region.method == "vector_text_envelope"
    assert object_region.bbox[1] <= 130
    assert object_region.bbox[3] >= 330
    assert object_region.bbox != (0.0, 0.0, 600.0, 800.0)


def test_coverage_reducer_preserves_p2_variants_over_legacy_page_render() -> None:
    page = PageContent(1, "", [], [])
    raw = [
        (page, {"base64": "legacy", "caption": "Page 1 full render", "source_kind": "page_render"}),
        (page, {"base64": "panel", "render_variant": "panel", "source_kind": "ocr_candidate"}),
        (page, {"base64": "composite", "render_variant": "composite", "source_kind": "ocr_candidate"}),
        (page, {"base64": "context", "render_variant": "full_page_context", "source_kind": "ocr_candidate"}),
    ]

    reduced = _coverage_reduce_images(raw)

    assert {image["render_variant"] for _, image in reduced} == {
        "panel", "composite", "full_page_context",
    }


def test_low_confidence_native_table_is_replaced_by_richer_ocr_object() -> None:
    native = DocumentObject(
        object_id="p7_table_1",
        object_type="table",
        page_number=7,
        sequence=1,
        rows=[["연도", "값"]],
        number="표 7-1",
        metadata={"missing_native": True},
    )
    decision = build_triage_plan([native], backend="unlimited_ocr", confidence_threshold=0.78)[0]
    apply_triage_metadata([native], [decision])
    ocr = DocumentObject(
        object_id="ocr-p7-table-1",
        object_type="table",
        page_number=7,
        sequence=1,
        rows=[["연도", "값"], ["2021", "100"], ["2022", "90"]],
        number="표 7-1",
        metadata={"engine": "unlimited_ocr", "source_evidence_id": evidence_id(native)},
    )

    merged = merge_document_objects([native], [ocr])
    result = next(obj for obj in merged if obj.metadata.get("evidence_id") == evidence_id(native))

    assert len(result.rows) == 3
    assert result.metadata["ocr_status"] == "replaced_low_confidence_native"
    assert result.metadata["ocr_backend"] == "unlimited_ocr"


def test_native_placeholder_is_collapsed_and_kept_as_canonical_alias() -> None:
    native = DocumentObject(
        object_id="p108_table_2",
        object_type="table",
        page_number=108,
        sequence=2,
        rows=[["부문", "값"], ["건물", "100"]],
        number="표 3-8",
        caption="표 3-8 부문별 배출량",
    )
    placeholder = DocumentObject(
        object_id="p108_table_3",
        object_type="table",
        page_number=108,
        sequence=3,
        number="표 3-8",
        caption="표 3-8 부문별 배출량",
        metadata={"missing_native": True},
    )

    merged = merge_document_objects([native, placeholder], [])

    assert len(merged) == 1
    assert merged[0].object_id == "p108_table_2"
    assert "p108_table_3" in merged[0].metadata["alias_object_ids"]
    assert merged[0].metadata["had_missing_native_proxy"] is True


def test_native_objects_with_colliding_evidence_are_not_discarded() -> None:
    left = DocumentObject(
        object_id="p20_chart_1",
        object_type="chart",
        page_number=20,
        sequence=1,
        caption="전력 소비 추이",
        bbox=(20, 100, 250, 300),
        metadata={"evidence_id": "ev-collision"},
    )
    right = DocumentObject(
        object_id="p20_chart_2",
        object_type="chart",
        page_number=20,
        sequence=2,
        caption="자동차 등록 추이",
        bbox=(300, 100, 550, 300),
        metadata={"evidence_id": "ev-collision"},
    )

    merged = merge_document_objects([left, right], [])

    assert [obj.object_id for obj in merged] == ["p20_chart_1", "p20_chart_2"]


def test_reconcile_triage_links_stale_proxy_to_canonical_object() -> None:
    chart = DocumentObject(
        object_id="p140_chart_1",
        object_type="chart",
        page_number=140,
        sequence=1,
        number="그림 4-2",
        caption="그림 4-2 온실가스 감축 경로",
        rows=[["연도", "값"], ["2030", "100"]],
    )
    proxy = DocumentObject(
        object_id="p140_image_1",
        object_type="image",
        page_number=140,
        sequence=1,
        number="그림 4-2",
        caption="그림 4-2 온실가스 감축 경로",
        metadata={"render_proxy": True},
    )
    decisions = build_triage_plan([chart, proxy], backend="vlm", confidence_threshold=0.78)
    merged = merge_document_objects([chart, proxy], [])

    reconcile_triage_decisions(merged, decisions)

    assert len(merged) == 1
    assert {row.canonical_object_id for row in decisions} == {merged[0].object_id}
    assert all(row.physical_object_id == merged[0].metadata["physical_object_id"] for row in decisions)


def test_triage_terminal_state_is_reconnected_by_evidence_id() -> None:
    native = DocumentObject(
        object_id="p7_chart_1",
        object_type="chart",
        page_number=7,
        sequence=1,
        number="그림 7-1",
    )
    decision = build_triage_plan([native], backend="vlm", confidence_threshold=0.78)[0]
    decision.final_status = "extracted"
    decision.attempt_count = 2
    decision.terminal_reason = "분할 재시도 성공"
    replacement = DocumentObject(
        object_id="vlm-p7-chart-1",
        object_type="chart",
        page_number=7,
        sequence=1,
        rows=[["연도", "값"], ["2030", "100"]],
        metadata={"source_evidence_id": decision.evidence_id},
    )

    apply_triage_metadata([replacement], [decision])

    assert replacement.metadata["final_status"] == "extracted"
    assert replacement.metadata["attempt_count"] == 2
    assert replacement.metadata["terminal_reason"] == "분할 재시도 성공"


def test_markdown_ocr_output_is_converted_to_document_objects() -> None:
    objects = parse_ocr_text(
        "| 연도 | 배출량 |\n| --- | --- |\n| 2021 | 100 |",
        page_number=8,
        page_width=595,
        page_height=842,
        backend="unlimited_ocr",
    )

    table = next(obj for obj in objects if obj.object_type == "table")
    assert table.rows[1] == ["2021", "100"]
    assert table.metadata["ocr_backend"] == "unlimited_ocr"


def test_directory_backend_only_loads_selected_pages(tmp_path: Path) -> None:
    (tmp_path / "page_2.md").write_text(
        "| 항목 | 값 |\n| --- | --- |\n| 전력 | 10 |",
        encoding="utf-8",
    )
    (tmp_path / "page_9.md").write_text("무관 페이지", encoding="utf-8")
    document = PDFContent(
        total_pages=9,
        pages=[PageContent(2, "표 2-1", [], [], width=595, height=842)],
        full_text="표 2-1",
    )
    candidates = [TriageDecision(
        object_id="p2_table_1",
        evidence_id="ev-p2-table-test",
        object_type="table",
        page_number=2,
        bbox=None,
        native_confidence=0.1,
        action="ocr_required",
        backend="unlimited_ocr",
    )]

    objects = DirectoryOCRBackend(tmp_path).load(document, candidates)

    assert objects
    assert {obj.page_number for obj in objects} == {2}


def test_duplicate_render_keeps_all_evidence_ids() -> None:
    page = PageContent(1, "", [], [])
    left = {
        "base64": "same",
        "width": 500,
        "height": 700,
        "caption": "candidate",
        "source_evidence_ids": ["ev-a"],
        "negative_revalidation_signals": ["strong:chart_structure"],
    }
    right = {
        **left,
        "source_evidence_ids": ["ev-b"],
        "negative_revalidation_signals": ["strong:quantified_unit"],
    }

    reduced = _coverage_reduce_images([(page, left), (page, right)])

    assert len(reduced) == 1
    assert reduced[0][1]["source_evidence_ids"] == ["ev-a", "ev-b"]
    assert reduced[0][1]["negative_revalidation_signals"] == [
        "strong:chart_structure",
        "strong:quantified_unit",
    ]


def test_reference_context_is_analyzed_then_blocked_and_propagated(monkeypatch) -> None:
    monkeypatch.setattr(config, "IMAGE_REFERENCE_ANALYZE_THEN_BLOCK", True, raising=False)
    monkeypatch.setattr(config, "IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT", True, raising=False)
    page = PageContent(
        7,
        "OECD 해외사례 온실가스 배출량 추이",
        [],
        [],
    )
    image = {
        "ocr_required": True,
        "source_evidence_ids": ["ev-reference"],
        "source_object_ids": ["obj-reference"],
    }
    triage_item = {"passed": True, "score": 8, "reasons": []}

    flagged, filtered = _apply_reference_context_policy(
        page, image, triage_item, "서울특별시"
    )

    assert flagged is True
    assert filtered is False
    assert triage_item["passed"] is True
    assert image["reference_merge_policy"] == "block_auto_merge"

    analysis = ImageAgent._attach_source_metadata({
        "type": "chart_table",
        "chart_type": "표",
        "title": "배출량 추이",
        "confidence": "high",
        "page_number": 7,
        "table": [{"연도": 2030, "값": 100, "fields": {"부문": "에너지"}}],
    }, image)
    can_merge, _, reasons = ImageAgent()._chart_merge_decision(
        analysis,
        analysis["table"][0],
        "emissions_regional",
    )
    objects = vision_analyses_to_objects([analysis])

    assert analysis["reference_context"] is True
    assert can_merge is False
    assert "참고자료/해외사례" in reasons
    assert objects[0].metadata["reference_context"] is True
    assert objects[0].metadata["reference_merge_policy"] == "block_auto_merge"


def test_legacy_reference_prefilter_never_drops_ocr_required(monkeypatch) -> None:
    monkeypatch.setattr(config, "IMAGE_REFERENCE_ANALYZE_THEN_BLOCK", False, raising=False)
    monkeypatch.setattr(config, "IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT", True, raising=False)
    page = PageContent(7, "OECD 해외사례 배출량", [], [])

    ordinary = {}
    ordinary_item = {"passed": True, "score": 8, "reasons": []}
    _, ordinary_filtered = _apply_reference_context_policy(
        page, ordinary, ordinary_item, "서울특별시"
    )
    required = {"ocr_required": True}
    required_item = {"passed": True, "score": 8, "reasons": []}
    _, required_filtered = _apply_reference_context_policy(
        page, required, required_item, "서울특별시"
    )

    assert ordinary_filtered is True
    assert ordinary_item["passed"] is False
    assert required_filtered is False
    assert required_item["passed"] is True


def test_image_agent_can_use_native_only_backend_without_vision() -> None:
    saved = (config.SELECTIVE_OCR_ENABLED, config.OCR_BACKEND, config.OCR_RESULTS_DIR)
    config.SELECTIVE_OCR_ENABLED = True
    config.OCR_BACKEND = "none"
    config.OCR_RESULTS_DIR = ""
    page = _page()
    document = PDFContent(total_pages=1, pages=[page], full_text=page.text)
    try:
        result = ImageAgent().extract(
            [page],
            {"municipality_name": "서울특별시"},
            "서울특별시",
            document=document,
        )
    finally:
        config.SELECTIVE_OCR_ENABLED, config.OCR_BACKEND, config.OCR_RESULTS_DIR = saved

    assert result["object_triage"]
    assert result["document_objects"]
    assert result["ocr_document_objects"] == []
    assert all(
        row["final_status"] in {"extracted", "not_relevant", "no_data", "needs_review"}
        for row in result["object_triage"]
    )
    assert any(
        row["action"] == "ocr_required" and row["final_status"] == "needs_review"
        for row in result["object_triage"]
    )
