from __future__ import annotations

import json
from pathlib import Path

import fitz

from utils.visual_regression_fixture import (
    build_visual_regression_fixture,
    load_case_specs,
    validate_visual_regression_fixture,
)
from utils.document_objects import DocumentObject
from utils.pdf_reader import extract_pdf
from utils.selective_ocr import build_triage_plan, render_ocr_candidate_images


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "evaluation" / "visual_regression_p0_v1"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_p0_case_contract_covers_pages_and_failure_modes() -> None:
    cases = load_case_specs(FIXTURE / "cases.jsonl")

    assert {row["source_page"] for row in cases} == {71, 89, 93, 414, 441}
    assert len(cases) == 7
    assert {row["failure_mode"] for row in cases} == {
        "multi_panel_chart",
        "captioned_chart_without_sheet_keyword",
        "four_panel_chart",
        "photo_panel_requires_negative_confirmation",
        "flow_diagram_below_text",
        "vector_table_without_embedded_image",
        "vector_table_caption_only",
    }
    assert all(row["expected_triage"] == "ocr_required" for row in cases)
    assert any(row["expected_status"] == "no_data_confirmed" for row in cases)


def test_p0_locked_fixture_files_and_hashes_are_valid() -> None:
    report = validate_visual_regression_fixture(FIXTURE / "manifest.json")

    assert report["valid"] is True, report["errors"]
    assert report["counts"] == {
        "cases": 7,
        "negative": 1,
        "positive": 6,
        "source_pages": 5,
    }


def test_p0_subset_keeps_explicit_original_page_map() -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))

    assert [(row["subset_page"], row["source_page"]) for row in manifest["pages"]] == [
        (1, 71),
        (2, 89),
        (3, 93),
        (4, 414),
        (5, 441),
    ]
    assert all((FIXTURE / row["page_image"]).exists() for row in manifest["pages"])


def test_p0_annotations_are_reviewed_and_keep_render_contract() -> None:
    cases = {row["case_id"]: row for row in load_case_specs(FIXTURE / "cases.jsonl")}
    annotations = _jsonl(FIXTURE / "annotations.jsonl")

    assert {row["case_id"] for row in annotations} == set(cases)
    assert all(row["review_status"] == "reviewed" for row in annotations)
    for row in annotations:
        assert row["expected_render_variants"] == cases[row["case_id"]][
            "expected_render_variants"
        ]


def test_p1_triage_keeps_all_p0_cases_despite_negative_context() -> None:
    cases = load_case_specs(FIXTURE / "cases.jsonl")
    objects: list[DocumentObject] = []
    for sequence, case in enumerate(cases, start=1):
        expected_type = case["expected_object_type"]
        object_type = expected_type if expected_type in {"table", "chart"} else "figure"
        objects.append(DocumentObject(
            object_id=f"{case['case_id']}-object",
            object_type=object_type,
            page_number=case["source_page"],
            sequence=sequence,
            number=case["object_number"],
            caption=case["caption"],
            nearby_text="참고 사례 사진 " + " ".join(case.get("expected_text_anchors", [])),
            bbox=tuple(case["bbox_pdf"]),
            metadata={
                "caption_only": True,
                "missing_native": object_type == "table",
            },
        ))

    decisions = build_triage_plan(objects, backend="vlm", confidence_threshold=0.78)

    assert {row.object_id for row in decisions if row.action == "ocr_required"} == {
        obj.object_id for obj in objects
    }
    assert all(any(reason.startswith("strong:") for reason in row.reasons) for row in decisions)
    assert all(
        "negative_overridden_by_strong_or_structured_signal" in row.reasons
        for row in decisions
    )


def test_p2_render_contract_is_met_on_locked_source_subset() -> None:
    cases = load_case_specs(FIXTURE / "cases.jsonl")
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    subset_by_source = {
        row["source_page"]: row["subset_page"] for row in manifest["pages"]
    }
    document = extract_pdf(FIXTURE / "source_subset.pdf", render_graph_pages=False)
    page_by_number = {page.page_number: page for page in document.pages}
    objects: list[DocumentObject] = []
    expected_by_object: dict[str, set[str]] = {}
    for sequence, case in enumerate(cases, start=1):
        page_number = subset_by_source[case["source_page"]]
        page = page_by_number[page_number]
        needle = case["object_number"].split(",", 1)[0]
        caption_block = next(
            block for block in page.text_blocks if needle in str(block.get("text") or "")
        )
        expected_type = case["expected_object_type"]
        object_type = expected_type if expected_type in {"table", "chart"} else "figure"
        object_id = f"subset-{case['case_id']}"
        objects.append(DocumentObject(
            object_id=object_id,
            object_type=object_type,
            page_number=page_number,
            sequence=sequence,
            number=case["object_number"],
            caption=case["caption"],
            nearby_text=" ".join(case.get("expected_text_anchors", [])),
            bbox=tuple(caption_block["bbox"]),
            metadata={
                "caption_only": object_type != "table",
                "missing_native": object_type == "table",
            },
        ))
        expected_by_object[object_id] = set(case["expected_render_variants"])

    decisions = build_triage_plan(objects, backend="vlm", confidence_threshold=0.78)
    rendered = render_ocr_candidate_images(document, objects, decisions)
    variants_by_evidence: dict[str, set[str]] = {}
    images_by_evidence: dict[str, list[dict]] = {}
    for _, image in rendered:
        for evidence in image["source_evidence_ids"]:
            variants_by_evidence.setdefault(evidence, set()).add(image["render_variant"])
            images_by_evidence.setdefault(evidence, []).append(image)

    for obj, decision, case in zip(objects, decisions, cases):
        assert variants_by_evidence[decision.evidence_id] == expected_by_object[obj.object_id], (
            obj.object_id,
            variants_by_evidence[decision.evidence_id],
            expected_by_object[obj.object_id],
        )
        primary_variant = (
            "composite" if "composite" in expected_by_object[obj.object_id] else "object"
        )
        primary = next(
            image for image in images_by_evidence[decision.evidence_id]
            if image["render_variant"] == primary_variant
        )
        expected_rect = fitz.Rect(case["bbox_pdf"])
        actual_rect = fitz.Rect(primary["bbox"])
        intersection = expected_rect & actual_rect
        overlap = intersection.get_area() / min(
            expected_rect.get_area(), actual_rect.get_area()
        )
        assert overlap >= 0.45, (obj.object_id, overlap, tuple(actual_rect), tuple(expected_rect))


def test_fixture_builder_supports_small_synthetic_source(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 80), "Figure 1 annual value 2030 10 tCO2eq")
    document.save(source)
    document.close()
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "case_id": "synthetic-1",
                "source_page": 1,
                "caption": "Figure 1 annual value",
                "bbox_pdf": [20, 40, 280, 120],
                "expected_object_type": "chart",
                "expected_triage": "ocr_required",
                "expected_status": "extracted",
                "expected_sheet": "regional_conditions",
                "expected_render_variants": ["object", "full_page_context"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_visual_regression_fixture(
        source,
        cases,
        tmp_path / "fixture",
        dataset_id="synthetic-v1",
        dpi=72,
    )

    report = validate_visual_regression_fixture(manifest, source_override=source)
    assert report["valid"] is True
    assert report["counts"]["cases"] == 1
