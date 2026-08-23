from __future__ import annotations

from agents.image_agent import _physical_observation_duplicate
from utils.document_objects import DocumentObject
from utils.evidence_merge import build_evidence_catalog, match_evidence
from utils.object_routing import deduplicate_evidence_objects


def _chart(
    object_id: str,
    *,
    caption: str = "그림 4-1 배출량 추이",
    metadata: dict | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> DocumentObject:
    return DocumentObject(
        object_id=object_id,
        object_type="chart",
        page_number=71,
        sequence=1,
        caption=caption,
        number="그림 4-1",
        bbox=bbox,
        metadata={"engine": "pymupdf", **(metadata or {})},
    )


def test_same_physical_id_consolidates_aliases_and_keeps_provenance() -> None:
    native = _chart(
        "p71_chart_1",
        metadata={"physical_object_id": "physical:p71-chart-main"},
    )
    vlm = _chart(
        "vlm-p71-chart-1",
        metadata={
            "engine": "vlm",
            "physical_object_id": "physical:p71-chart-main",
            "source_object_ids": ["p71_chart_1"],
        },
    )
    vlm.rows = [["연도", "배출량"], ["2030", "100"]]

    merged, removed = deduplicate_evidence_objects([native, vlm])

    assert removed == 1
    assert len(merged) == 1
    assert merged[0].rows == vlm.rows
    assert merged[0].metadata["physical_object_id"] == "physical:p71-chart-main"
    assert set(merged[0].metadata["alias_object_ids"]) == {
        "p71_chart_1", "vlm-p71-chart-1",
    } - {merged[0].object_id}
    assert merged[0].metadata["physical_merge_status"] == "alias_merged"
    assert "physical_object_id" in merged[0].metadata["physical_merge_methods"]


def test_different_panels_are_not_merged_even_with_same_evidence_and_caption() -> None:
    left = _chart(
        "p89_panel_1",
        metadata={
            "evidence_id": "ev-p89-multipanel",
            "canonical_object_id": "p89_figure_parent",
            "render_group_id": "physical:p89-figure",
            "panel_index": 1,
            "panel_count": 2,
        },
        bbox=(10, 100, 280, 320),
    )
    right = _chart(
        "p89_panel_2",
        metadata={
            "evidence_id": "ev-p89-multipanel",
            "canonical_object_id": "p89_figure_parent",
            "render_group_id": "physical:p89-figure",
            "panel_index": 2,
            "panel_count": 2,
        },
        bbox=(300, 100, 570, 320),
    )

    merged, removed = deduplicate_evidence_objects([left, right])

    assert removed == 0
    assert len(merged) == 2


def test_table_consolidation_selects_richest_table_without_appending_rows() -> None:
    short = DocumentObject(
        object_id="p93_table_native",
        object_type="table",
        page_number=93,
        sequence=1,
        number="표 4-2",
        caption="표 4-2 연도별 목표",
        rows=[["연도", "목표"], ["2030", "100"]],
        metadata={"engine": "pymupdf", "evidence_id": "ev-p93-table"},
    )
    rich = DocumentObject(
        object_id="p93_table_ocr",
        object_type="table",
        page_number=93,
        sequence=2,
        number="표 4-2",
        caption="표 4-2 연도별 목표",
        rows=[["연도", "목표"], ["2030", "100"], ["2050", "0"]],
        metadata={"engine": "ocr", "evidence_id": "ev-p93-table"},
    )

    merged, removed = deduplicate_evidence_objects([short, rich])

    assert removed == 1
    assert len(merged) == 1
    assert merged[0].rows == rich.rows
    assert len(merged[0].rows) == 3


def test_evidence_catalog_uses_physical_id_to_collapse_ledger_rows() -> None:
    rows = [
        {
            "object_id": "p414_chart_1",
            "object_type": "chart",
            "page_number": 414,
            "metadata": {
                "evidence_id": "ev-p414-chart",
                "physical_object_id": "physical:p414-chart",
                "final_status": "extracted",
            },
        },
        {
            "object_id": "vlm-p414-chart-1",
            "object_type": "chart",
            "page_number": 414,
            "metadata": {
                "evidence_id": "ev-p414-chart",
                "physical_object_id": "physical:p414-chart",
                "final_status": "extracted",
            },
        },
    ]

    result = match_evidence("ev-p414-chart", build_evidence_catalog(rows))

    assert result.status == "exact"
    assert set(result.object_ids) == {"p414_chart_1", "vlm-p414-chart-1"}


def test_same_physical_id_on_different_pages_is_not_collapsed() -> None:
    rows = [
        {
            "object_id": "p10_chart_1",
            "object_type": "chart",
            "page_number": 10,
            "metadata": {
                "evidence_id": "ev-repeated",
                "physical_object_id": "physical:repeated-caption",
                "final_status": "extracted",
            },
        },
        {
            "object_id": "p20_chart_1",
            "object_type": "chart",
            "page_number": 20,
            "metadata": {
                "evidence_id": "ev-repeated",
                "physical_object_id": "physical:repeated-caption",
                "final_status": "extracted",
            },
        },
    ]

    result = match_evidence("ev-repeated", build_evidence_catalog(rows))

    assert result.status == "multiple_objects"


def test_visual_observation_dedup_only_applies_across_exact_render_variants() -> None:
    base = {
        "물리객체ID": "physical:p441-chart",
        "렌더변형": "object",
        "렌더변형목록": ["object"],
        "대상시트": "regional_conditions",
        "제목": "연도별 인구",
        "항목": "인구",
        "연도": 2030,
        "값": 100,
        "단위": "명",
        "판독필드": {"지표명": "인구"},
    }
    same_value_other_render = {
        **base,
        "렌더변형": "full_page_context",
        "렌더변형목록": ["full_page_context"],
    }
    different_row = {
        **same_value_other_render,
        "연도": 2040,
        "값": 90,
    }
    same_render = dict(base)

    assert _physical_observation_duplicate([base], same_value_other_render) is base
    assert _physical_observation_duplicate([base], different_row) is None
    assert _physical_observation_duplicate([base], same_render) is None
