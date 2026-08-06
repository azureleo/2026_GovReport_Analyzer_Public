from pathlib import Path

import config
from agents.image_agent import ImageAgent, _coverage_reduce_images
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
    }
    right = {
        **left,
        "source_evidence_ids": ["ev-b"],
    }

    reduced = _coverage_reduce_images([(page, left), (page, right)])

    assert len(reduced) == 1
    assert reduced[0][1]["source_evidence_ids"] == ["ev-a", "ev-b"]


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
