import hashlib
import json
from pathlib import Path

import pytest

import config
from utils.document_objects import DocumentObject
from utils.ocr_loader_validation import normalized_bbox_to_pdf
from utils.ocr_pipeline import select_supplement_objects
from utils.pdf_reader import PageContent, PDFContent
from utils.selective_ocr import (
    DirectoryOCRBackend, TriageDecision, evidence_id, merge_document_objects, parse_ocr_text,
)


def native(object_id="native-1", bbox=(10, 10, 90, 90), rows=None):
    return DocumentObject(object_id, "table", 1, 1,
        rows=rows if rows is not None else [["연도", "값"], ["2030", ""]],
        bbox=bbox, metadata={"engine": "pymupdf", "native_confidence": 0.1})


def candidate(obj):
    return TriageDecision(obj.object_id, evidence_id(obj), obj.object_type, obj.page_number,
                          obj.bbox, 0.1, "ocr_required")


def document(source=""):
    page = PageContent(1, "", [], [], width=100, height=100)
    return PDFContent(1, [page], "", source_path=str(source))


def record(obj, **metadata):
    value = obj.to_dict()
    value["object_id"] = "ocr-" + obj.object_id
    value["metadata"] = {"engine": "unlimited_ocr", **metadata}
    return value


def write_records(tmp_path, rows):
    path = tmp_path / "project_document_objects.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return DirectoryOCRBackend(tmp_path)


def test_no_candidates_does_not_read_directory(tmp_path, monkeypatch):
    backend = DirectoryOCRBackend(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("no-candidate path must not enumerate OCR files")
    monkeypatch.setattr(Path, "rglob", forbidden)
    assert backend.load(document(), []) == []
    assert backend.diagnostics[0]["code"] == "no_candidates_no_load"


def test_jsonl_corruption_and_missing_result_are_observable(tmp_path):
    (tmp_path / "project_document_objects.jsonl").write_text("{bad\n[]\n", encoding="utf-8")
    backend = DirectoryOCRBackend(tmp_path)
    assert backend.load(document(), [candidate(native())]) == []
    codes = {d["code"] for d in backend.diagnostics}
    assert {"invalid_jsonl", "invalid_object_record", "candidate_result_missing"} <= codes


def test_missing_directory_returns_diagnostic(tmp_path):
    backend = DirectoryOCRBackend(tmp_path / "missing")
    assert backend.load(document(), [candidate(native())]) == []
    assert backend.diagnostics[0]["code"] == "results_directory_missing"


def test_same_page_different_table_is_held(tmp_path):
    intended = native(bbox=(0, 0, 40, 40))
    unrelated = native("other", bbox=(60, 60, 100, 100))
    backend = write_records(tmp_path, [record(unrelated)])
    assert backend.load(document(), [candidate(intended)]) == []
    assert backend.review_objects[0]["metadata"]["ocr_application_status"].startswith("held_")


def test_hybrid_native_engine_is_preserved_and_not_reimported(tmp_path):
    obj = native()
    backend = write_records(tmp_path, [record(obj, engine="pymupdf")])
    assert backend.load(document(), [candidate(obj)]) == []
    assert any(d["code"] == "hybrid_native_object_preserved_skip" for d in backend.diagnostics)


def test_explicit_selection_preserves_original_engine_and_lineage(tmp_path):
    obj = native(bbox=None)
    backend = write_records(tmp_path, [record(obj, engine="local_ocr_variant",
        source_object_ids=[obj.object_id], evidence_id=evidence_id(obj))])
    result = backend.load(document(), [candidate(obj)])
    assert len(result) == 1
    assert result[0].metadata["engine"] == "local_ocr_variant"
    assert result[0].metadata["evidence_id"] == evidence_id(obj)
    assert {obj.object_id, "ocr-"+obj.object_id} <= set(result[0].metadata["source_object_ids"])


def test_unlocated_legacy_object_is_review_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OCR_ALLOW_LEGACY_UNLOCATED", False, raising=False)
    obj = native(bbox=None)
    backend = write_records(tmp_path, [record(obj)])
    assert backend.load(document(), [candidate(obj)]) == []


def test_document_hash_mismatch_is_rejected(tmp_path):
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"same-name different contents")
    obj = native()
    backend = write_records(tmp_path, [record(obj, pdf_sha256=hashlib.sha256(b"wrong").hexdigest())])
    assert backend.load(document(pdf), [candidate(obj)]) == []
    assert any(d["code"] == "document_hash_mismatch" for d in backend.diagnostics)


@pytest.mark.parametrize("bbox", [[0, 0, float("nan"), 10], [0, 0, 1000, 10], [40, 40, 10, 10]])
def test_invalid_bbox_is_reported(tmp_path, bbox):
    obj = native()
    row = record(obj)
    row["bbox"] = bbox
    backend = write_records(tmp_path, [row])
    assert backend.load(document(), [candidate(obj)]) == []
    assert any(d["code"] == "invalid_object_or_coordinates" for d in backend.diagnostics)


def test_999_coordinate_contract_and_page_rotation(tmp_path):
    page = PageContent(1, "", [], [], width=100, height=200,
        metadata={"rotation": 90, "unrotated_width": 200, "unrotated_height": 100})
    box = [199.8, 49.95, 599.4, 149.85]
    assert normalized_bbox_to_pdf(box, page) == pytest.approx((10, 40, 30, 80))
    obj = native(bbox=(10, 40, 30, 80))
    row = record(obj, coordinate_space="normalized_999")
    row["bbox"] = box
    backend = write_records(tmp_path, [row])
    loaded = backend.load(PDFContent(1, [page], ""), [candidate(obj)])
    assert loaded[0].bbox == pytest.approx(obj.bbox)


def test_duplicate_payload_is_idempotent_and_conflicting_payloads_are_held(tmp_path):
    obj = native()
    row = record(obj, source_object_ids=[obj.object_id])
    backend = write_records(tmp_path, [row, row])
    assert len(backend.load(document(), [candidate(obj)])) == 1
    other = json.loads(json.dumps(row))
    other["rows"][1][1] = "999"
    backend = write_records(tmp_path, [row, other])
    assert backend.load(document(), [candidate(obj)]) == []
    assert any(d["code"] == "conflicting_ocr_payloads" for d in backend.diagnostics)


def test_one_missing_cell_is_filled_and_is_a_real_supplement():
    obj = native()
    imported = native("ocr-1", rows=[["연도", "값"], ["2030", "100"]])
    imported.metadata.update(engine="unlimited_ocr", ocr_source_file="saved.jsonl",
        source_object_ids=[obj.object_id], source_evidence_id=evidence_id(obj),
        evidence_id=evidence_id(obj), ocr_application_status="selected")
    merged = merge_document_objects([obj], [imported])
    assert obj.rows[1][1] == ""
    assert merged[0].rows[1][1] == "100"
    assert merged[0].metadata["ocr_status"] == "filled_native_cells"
    selected = select_supplement_objects([obj], merged)
    assert len(selected) == 1
    fingerprint = selected[0].metadata["ocr_extraction_fingerprint"]
    assert select_supplement_objects([obj], merged, processed_fingerprints={fingerprint}) == []


def test_known_native_value_conflict_is_not_overwritten():
    obj = native(rows=[["연도", "값"], ["2030", "100"]])
    imported = native("ocr-1", rows=[["연도", "값"], ["2030", "101"]])
    imported.metadata.update(engine="unlimited_ocr", ocr_source_file="saved.jsonl",
        source_object_ids=[obj.object_id], source_evidence_id=evidence_id(obj),
        evidence_id=evidence_id(obj), ocr_application_status="selected")
    merged = merge_document_objects([obj], [imported])
    assert merged[0].rows[1][1] == "100"
    assert merged[0].metadata["ocr_conflicts"][0]["conflicts"][0]["native"] == "100"
    assert select_supplement_objects([obj], merged) == []


def test_explicit_evidence_never_overrides_disjoint_geometry():
    obj = native(bbox=(0, 0, 40, 40))
    imported = native("ocr-1", bbox=(60, 60, 100, 100),
        rows=[["연도", "값"], ["2030", "100"]])
    imported.metadata.update(engine="unlimited_ocr", ocr_source_file="saved.jsonl",
        source_object_ids=[obj.object_id], source_evidence_id=evidence_id(obj),
        evidence_id=evidence_id(obj), ocr_application_status="selected")
    merged = merge_document_objects([obj], [imported])
    assert merged[0].rows == obj.rows
    assert imported.metadata["ocr_application_status"] == "held_location_mismatch"
    assert select_supplement_objects([obj], merged) == []


def test_main_parser_supports_ref_tags_and_merged_header_rows():
    text = '<|ref|><table><tr><th rowspan="2">부문</th><th colspan="2">배출량</th></tr>' \
           '<tr><th>2020</th><th>2030</th></tr><tr><td>산업</td><td>0</td><td>20</td></tr></table>' \
           '<|/ref|><|det|>[0, 0, 999, 999]<|/det|>'
    obj = parse_ocr_text(text, page_number=1, page_width=100, page_height=200, backend="unlimited_ocr")[0]
    assert obj.rows == [["부문", "배출량", "배출량"], ["부문", "2020", "2030"], ["산업", "0", "20"]]
    assert obj.metadata["header_row_count"] == 2
    assert obj.bbox == (0, 0, 100, 200)
