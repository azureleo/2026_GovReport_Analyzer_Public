from __future__ import annotations

import json
from pathlib import Path

import fitz
from openpyxl import Workbook

from utils.fixed_visual_sample import (
    FixedSampleDataset,
    FixedSampleItem,
    build_fixed_sample_dataset,
    evaluate_fixed_sample_merge_ab,
    evaluate_fixed_sample_results,
    prepare_merge_annotations,
    validate_fixed_sample_dataset,
)


def _source_pdf(path: Path, pages: int = 9) -> None:
    document = fitz.open()
    for page_number in range(1, pages + 1):
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 40), f"Page {page_number} chart title {page_number}")
        page.draw_rect(fitz.Rect(40, 80, 260, 300))
    document.save(path)
    document.close()


def _inventory(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "시각요소"
    sheet.append([
        "요소ID", "페이지", "요소유형", "제목", "데이터포함", "기대추출", "관련시트", "비고",
    ])
    rows = [
        ["E001", 1, "그래프", "차트 1", "Y", "Y", "05_배출전망", ""],
        ["E002", 2, "이미지표", "표 2", "Y", "Y", "02_지역여건", ""],
        ["E003", 3, "그래프", "차트 3", "Y", "Y", "06_감축목표", ""],
        ["E004", 4, "이미지", "도식 4", "N", "Y", "07_비전전략", ""],
        ["E005", 5, "그래프", "차트 5", "Y", "Y", "05_배출전망", ""],
        ["E006", 6, "장식", "장식 6", "N", "N", "", ""],
        ["E007", 7, "지도·사진", "사진 7", "N", "N", "", ""],
        ["E008", 8, "그래프", "차트 8", "Y", "Y", "03_배출현황_지역", ""],
        ["E009", 9, "장식", "장식 9", "N", "N", "", ""],
    ]
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def _audit(path: Path) -> None:
    statuses = {
        "E001": "vision_유실",
        "E002": "기록됨",
        "E003": "기록됨",
        "E004": "기록됨",
        "E005": "vision_유실",
        "E008": "기록됨",
    }
    path.write_text(json.dumps({
        "details": [
            {"요소ID": sample_id, "상태": status}
            for sample_id, status in statuses.items()
        ],
    }, ensure_ascii=False), encoding="utf-8")


def test_build_is_deterministic_and_includes_failures_and_controls(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    inventory = tmp_path / "inventory.xlsx"
    audit = tmp_path / "audit.json"
    _source_pdf(source)
    _inventory(inventory)
    _audit(audit)

    first = build_fixed_sample_dataset(
        source,
        inventory,
        tmp_path / "sample-a",
        dataset_id="test-visual-v1",
        audit_path=audit,
        positive_controls=2,
        negative_controls=2,
        dpi=72,
    )
    second = build_fixed_sample_dataset(
        source,
        inventory,
        tmp_path / "sample-b",
        dataset_id="test-visual-v1-copy",
        audit_path=audit,
        positive_controls=2,
        negative_controls=2,
        dpi=72,
    )

    assert first.fingerprint == second.fingerprint
    assert [item.sample_id for item in first.items] == [item.sample_id for item in second.items]
    assert {item.sample_id for item in first.items if item.role == "failure"} == {"E001", "E005"}
    assert sum(item.role == "positive_control" for item in first.items) == 2
    assert sum(item.role == "negative_control" for item in first.items) == 2
    assert first.payload["counts"]["objects"] == 6
    assert first.payload["counts"]["unique_pages"] == 6
    assert all((first.manifest_path.parent / item.image_path).exists() for item in first.items)
    assert validate_fixed_sample_dataset(first.manifest_path).valid is True
    annotation = json.loads(
        (first.manifest_path.parent / "annotations_template.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert annotation["expected_sheet"]
    assert annotation["expected_merge"] in {"pending", "reject"}
    assert annotation["expected_visual_rows"] == []
    assert annotation["expected_merged_rows"] == []
    assert annotation["fixture_evidence"] == "single"
    assert annotation["reviewer"] == ""
    assert annotation["review_basis"] == ""
    assert annotation["reviewed_at"] is None


def test_validation_detects_source_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    inventory = tmp_path / "inventory.xlsx"
    _source_pdf(source)
    _inventory(inventory)
    dataset = build_fixed_sample_dataset(
        source,
        inventory,
        tmp_path / "sample",
        dataset_id="drift-test",
        positive_controls=2,
        negative_controls=1,
        dpi=72,
    )

    source.write_bytes(source.read_bytes() + b"drift")
    report = validate_fixed_sample_dataset(dataset.manifest_path)

    assert report.valid is False
    assert any("SHA256" in error for error in report.errors)


def test_evaluation_separates_coverage_recall_specificity_and_numeric_values(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    inventory = tmp_path / "inventory.xlsx"
    _source_pdf(source)
    _inventory(inventory)
    dataset = build_fixed_sample_dataset(
        source,
        inventory,
        tmp_path / "sample",
        dataset_id="metric-test",
        positive_controls=2,
        negative_controls=1,
        dpi=72,
    )
    positive_ids = [item.sample_id for item in dataset.items if item.expected]
    negative_id = next(item.sample_id for item in dataset.items if not item.expected)
    results = [
        {
            "sample_id": positive_ids[0],
            "status": "extracted",
            "table": [{"연도": 2030, "값": 10.5}],
        },
        {
            "sample_id": positive_ids[1],
            "status": "no_data",
            "table": [],
        },
        {
            "sample_id": negative_id,
            "status": "not_relevant",
            "table": [],
        },
    ]

    annotations = [{
        "sample_id": positive_ids[0],
        "review_status": "confirmed",
        "expected_rows": [{"연도": 2030, "값": 10.5}],
    }]
    report = evaluate_fixed_sample_results(dataset, results, annotations)

    assert report["metrics"]["response_coverage"] == 1.0
    assert report["metrics"]["expected_recall"] == 0.5
    assert report["metrics"]["negative_specificity"] == 1.0
    assert report["metrics"]["classification_accuracy"] == 0.6667
    assert report["metrics"]["reviewed_classification_accuracy"] is None
    assert report["metrics"]["numeric_value_coverage"] == 0.5
    assert report["metrics"]["structured_cell_accuracy"] == 1.0
    assert report["metrics"]["numeric_value_recall"] == 1.0
    assert report["metrics"]["numeric_value_precision"] == 1.0
    assert report["metrics"]["value_accuracy"] == 1.0
    assert report["counts"]["value_cells_matched"] == 2


def _merge_dataset(tmp_path: Path) -> FixedSampleDataset:
    item = FixedSampleItem(
        sample_id="E001",
        page=188,
        element_type="이미지표",
        title="관리권한 배출량",
        data_included=True,
        expected=True,
        related_sheet="04_배출현황_관리권한",
        note="",
        role="positive_control",
        expected_status="extracted",
        source_status="기록됨",
        image_path="pages/E001.png",
        image_sha256="unused",
        page_text_excerpt="관리권한 온실가스 배출량",
    )
    return FixedSampleDataset(
        manifest_path=tmp_path / "manifest.json",
        payload={"dataset_id": "merge-ab-test", "dataset_fingerprint": "fixed"},
        items=(item,),
    )


def _merge_result(value: float | None = 100.0) -> dict:
    return {
        "sample_id": "E001",
        "status": "extracted",
        "response_received": True,
        "object_type": "이미지표",
        "scope": "municipality",
        "target_sheet": "emissions_management",
        "title": "관리권한 배출량",
        "unit": "천톤CO2eq",
        "confidence": "high",
        "table": [{
            "항목": "건물",
            "연도": 2030,
            "값": value,
            "단위": "천톤CO2eq",
            "fields": {
                "관리부문": "건물",
                "세부부문": "공공",
                "직간접구분": "직접",
                "연도": 2030,
            },
        }],
    }


def test_prepare_merge_annotations_preserves_value_contract(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    rows = prepare_merge_annotations(dataset, [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_rows": [{"연도": 2030, "배출량": 100.0}],
        "notes": "사람 확정",
    }])

    assert rows == [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_status": "extracted",
        "expected_title": "관리권한 배출량",
        "expected_unit": None,
        "expected_sheet": "04_배출현황_관리권한",
        "expected_merge": "pending",
        "expected_visual_rows": [{"연도": 2030, "배출량": 100.0}],
        "expected_merged_rows": [{"연도": 2030, "배출량": 100.0}],
        "expected_rows": [{"연도": 2030, "배출량": 100.0}],
        "fixture_evidence": "single",
        "fixture_baseline_rows": [],
        "reviewer": "",
        "review_basis": "",
        "reviewed_at": None,
        "notes": "사람 확정",
    }]


def test_value_scoring_flattens_fields_and_does_not_reuse_actual_rows(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    results = [{
        "sample_id": "E001",
        "status": "extracted",
        "table": [
            {"항목": "강수강도", "값": 17.1, "단위": "mm/일", "fields": {"기간": "21~30년"}},
            {"항목": "강수강도", "값": 15.6, "단위": "mm/일", "fields": {"기간": "31~40년"}},
        ],
    }]
    annotations = [{
        "sample_id": "E001",
        "review_status": "reviewed",
        "expected_visual_rows": [
            {"항목": "강수강도", "기간": "21~30년", "값": 17.1},
            {"항목": "강수강도", "기간": "31~40년", "값": 15.6},
            {"항목": "강수강도", "기간": "41~50년", "값": 14.1},
        ],
    }]

    report = evaluate_fixed_sample_results(dataset, results, annotations)

    assert report["counts"]["value_cells_expected"] == 9
    assert report["counts"]["value_cells_matched"] == 6
    assert report["metrics"]["structured_cell_accuracy"] == 0.6667
    assert report["metrics"]["numeric_value_recall"] == 0.6667
    assert report["metrics"]["numeric_value_precision"] == 1.0
    assert report["metrics"]["value_accuracy"] == 0.6667


def test_reviewed_status_accuracy_uses_annotation_contract(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    results = [{
        "sample_id": "E001",
        "status": "no_data",
        "table": [],
    }]
    annotations = [{
        "sample_id": "E001",
        "review_status": "reviewed",
        "expected_status": "no_data",
    }]

    report = evaluate_fixed_sample_results(dataset, results, annotations)

    # 매니페스트의 임시 정답(extracted)은 보존하고, 원본 대조 정답은 별도 표시한다.
    assert report["metrics"]["classification_accuracy"] == 0.0
    assert report["metrics"]["reviewed_classification_accuracy"] == 1.0
    assert report["counts"]["reviewed_status_objects"] == 1
    assert report["counts"]["reviewed_status_matches"] == 1


def test_merge_ab_exact_evidence_keeps_valid_visual_row(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    annotations = [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_sheet": "04_배출현황_관리권한",
        "expected_merge": "accept",
        "expected_rows": [{
            "관리부문": "건물",
            "세부부문": "공공",
            "직간접구분": "직접",
            "연도": 2030,
            "배출량": 100.0,
        }],
        "fixture_evidence": "single",
    }]

    report = evaluate_fixed_sample_merge_ab(dataset, [_merge_result()], annotations)
    evidence = report["metrics"]["evidence"]

    assert evidence["decision_accuracy"] == 1.0
    assert evidence["target_sheet_accuracy"] == 1.0
    assert evidence["auto_merge_precision"] == 1.0
    assert evidence["auto_merge_recall"] == 1.0
    assert evidence["exact_evidence_match_rate"] == 1.0
    assert len(report["details"]["evidence"][0]["merged_rows"]) == 1


def test_merge_ab_isolates_multiple_evidence_instead_of_legacy_merge(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    annotations = [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_sheet": "04_배출현황_관리권한",
        "expected_merge": "needs_review",
        "expected_rows": [],
        "fixture_evidence": "multiple_ids",
    }]

    report = evaluate_fixed_sample_merge_ab(dataset, [_merge_result()], annotations)
    legacy = report["metrics"]["legacy"]
    evidence = report["metrics"]["evidence"]

    assert legacy["false_merge_rows"] == 1
    assert evidence["false_merge_rows"] == 0
    assert evidence["decision_accuracy"] == 1.0
    assert evidence["multiple_evidence_isolation_rate"] == 1.0
    assert report["comparison"]["false_merge_reduction"] == 1
    assert report["details"]["evidence"][0]["merged_rows"] == []


def test_merge_ab_isolates_all_null_and_text_conflict(tmp_path: Path) -> None:
    dataset = _merge_dataset(tmp_path)
    null_annotations = [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_merge": "needs_review",
        "fixture_evidence": "single",
    }]
    null_report = evaluate_fixed_sample_merge_ab(
        dataset,
        [_merge_result(value=None)],
        null_annotations,
    )
    assert null_report["metrics"]["evidence"]["null_isolation_rate"] == 1.0

    conflict_annotations = [{
        "sample_id": "E001",
        "review_status": "confirmed",
        "expected_merge": "needs_review",
        "fixture_evidence": "single",
        "fixture_baseline_rows": [{
            "_sheet": "emissions_management",
            "지자체명": "서울특별시",
            "인벤토리출처": "본문",
            "관리부문": "건물",
            "세부부문": "공공",
            "직간접구분": "직접",
            "연도": 2030,
            "배출량": 120.0,
            "단위": "천톤CO2eq",
        }],
    }]
    conflict_report = evaluate_fixed_sample_merge_ab(
        dataset,
        [_merge_result(value=100.0)],
        conflict_annotations,
    )
    evidence_detail = conflict_report["details"]["evidence"][0]

    assert conflict_report["metrics"]["evidence"]["text_conflict_isolation_rate"] == 1.0
    assert evidence_detail["decision"] == "needs_review"
    assert evidence_detail["merged_rows"] == []
