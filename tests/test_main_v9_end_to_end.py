"""External model boundary mocked; PDF/objects/organizer/Excel execute for real."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

import fitz
import openpyxl
import pytest

import config
from agents.image_agent import ImageAgent
from agents.supervisor import Supervisor
from utils.pdf_reader import extract_pdf
from utils.document_objects import build_document_objects, consolidate_document_objects
from utils.pdf_reader import PDFContent, PageContent
from utils.integration_audit import object_inventory, source_validation


def make_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((40, 40), "Carbon emissions forecast BAU", fontsize=12)
    # Real table geometry; the third data cell was absent in native parsing.
    for y in (100, 130, 160):
        page.draw_line((40, y), (460, y))
    for x in (40, 180, 320, 460):
        page.draw_line((x, 100), (x, 160))
    for x, value in ((50, "Sector"), (190, "Year"), (330, "Value")):
        page.insert_text((x, 120), value)
    page.insert_text((50, 150), "building")
    page.insert_text((190, 150), "2030")
    doc.save(path)
    doc.close()


def external_response(prompt, **kwargs):
    if "지자체명만" in prompt or "municipality_name" in prompt:
        return json.dumps({"municipality_name": "테스트시", "지자체명": "테스트시"}, ensure_ascii=False)
    keys = re.findall(r'\{\s*"([a-z_]+)"\s*:\s*\[', prompt)
    if not keys:
        return json.dumps({"assessment": "fixture diagnostic", "quality_level": "보통"})
    key = keys[-1]
    rows = []
    if key == "document_meta":
        rows = [{"지자체명": "테스트시", "계획명": "기본계획", "계획시작연도": 2024, "계획종료연도": 2033}]
    elif key == "emissions_forecast" and "987.6" in prompt:
        rows = [{"지자체명": "테스트시", "전망범위": "지역전체", "시나리오": "BAU", "부문": "건물",
                 "세부부문": "가정", "연도": 2030, "전망값": 987.6, "단위": "천톤CO2eq", "출처페이지": 1}]
    return json.dumps({key: rows}, ensure_ascii=False)


def settings(monkeypatch, root, *, ocr=True, resume=False):
    for key, value in {"FULL_DOCUMENT_SCAN": True, "SELECTIVE_OCR_ENABLED": ocr,
                       "OCR_BACKEND": "directory", "OCR_RESULTS_DIR": str(root / "ocr"),
                       "OCR_NATIVE_CONFIDENCE_THRESHOLD": 1.01, "RUN_STATE_ENABLED": resume,
                       "RUN_STATE_DIR": str(root / "state"), "EXTRACTION_RESUME": resume,
                       "EXTRACTION_RETRY_FAILED_ONLY": False, "SEMANTIC_ROUTING_ENABLED": False,
                       "EXTRACTION_OBJECT_BATCHING_ENABLED": False, "MAX_BATCH_CHARS": None,
                       "MAX_BATCH_TABLE_CELLS": None}.items():
        monkeypatch.setattr(config, key, value)


def prepare_ocr(root):
    source = root / "source.pdf"
    make_pdf(source)
    document = extract_pdf(source, render_graph_pages=False)
    objects = consolidate_document_objects(build_document_objects(document.pages))
    table = next(obj for obj in objects if obj.object_type == "table")
    row = table.to_dict()
    row["object_id"] = "fixture-ocr-table"
    row["text"] = "배출량 전망 BAU 지역전체 987.6 천톤CO2eq"
    row["caption"] = "배출량 전망 BAU 지역전체"
    row["rows"] = [list(cells) for cells in table.rows]
    row["rows"][-1][-1] = "987.6"
    row["metadata"].update(engine="unlimited_ocr", target_sheets=["emissions_forecast"],
                            source_object_ids=[table.object_id], document_sha256=__import__('hashlib').sha256(source.read_bytes()).hexdigest())
    directory = root / "ocr"
    directory.mkdir()
    (directory / "project_document_objects.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return source


def run(source, destination):
    with patch("utils.llm_client.call_text", side_effect=external_response):
        supervisor = Supervisor()
        result = supervisor.run(source, destination, include_images=False, max_pipeline_retries=1)
    return result, supervisor


def read_rows(path, sheet="05_배출전망"):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    values = list(wb[sheet].values)
    wb.close()
    return [dict(zip(values[0], row)) for row in values[1:]]


def test_ocr_value_reaches_actual_excel_and_audit_with_run_state_off(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    result, supervisor = run(source, tmp_path / "result.xlsx")
    rows = read_rows(result)
    assert len(rows) == 1 and rows[0]["전망값"] == 987.6
    assert rows[0]["전망범위"] == "지역전체"
    assert rows[0]["근거ID"] and rows[0]["객체ID"] and rows[0]["출처페이지"]
    assert rows[0]["추출엔진"] == "unlimited_ocr" and rows[0]["OCR보완여부"] == "Y"
    assert "fixture-ocr-table" in rows[0]["출처계보"]
    audit = json.loads(result.with_name(result.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["ledgers"]["ocr_import_audit"]["extraction_success_objects"] == 1
    assert audit["ledgers"]["ocr_import_audit"]["body_reflected_objects"] == 1
    assert supervisor.last_outcome["audit_saved"]
    assert not (tmp_path / "state").exists()


def test_default_path_does_not_import_ocr(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path, ocr=False)
    result, _ = run(source, tmp_path / "disabled.xlsx")
    assert read_rows(result) == []


def test_validation_reason_and_target_reach_actual_excel(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path, ocr=False)
    def response(prompt, **kwargs):
        raw = json.loads(external_response(prompt, **kwargs))
        if "emissions_regional" in raw:
            raw["emissions_regional"] = [{"부문": "건물", "배출유형": "미지원 축", "연도": 2018,
                                          "배출량": 12, "단위": "tCO2eq", "출처페이지": 1}]
        return json.dumps(raw, ensure_ascii=False)
    with patch("utils.llm_client.call_text", side_effect=response):
        result = Supervisor().run(source, tmp_path / "axis_review.xlsx", include_images=False, max_pipeline_retries=1)
    reviews = read_rows(result, "17_검증리포트")
    axis = next(row for row in reviews if row["항목"] == "축 필드 enum 밖 값 제거")
    assert axis["대상시트"] == "emissions_regional"
    assert "미지원 축" in axis["사유"] and axis["감사파일"]


def test_main_cli_flags_execute_actual_offline_ocr_pipeline(tmp_path):
    source = prepare_ocr(tmp_path)
    destination = tmp_path / "main_cli.xlsx"
    # Install fixtures only after CLI parsing, at its late Supervisor import.
    # Thus config is freshly loaded from the CLI options, not mocked in advance.
    wrapper = '''
import builtins, sys
original_import = builtins.__import__
installed = False
def fixed_response(*args, **kwargs):
    from scripts.offline_connection_demo import fixed_external_response
    return fixed_external_response(*args, **kwargs)
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    global installed
    module = original_import(name, globals, locals, fromlist, level)
    if name == "agents.supervisor" and not installed:
        installed = True
        import utils.llm_client as boundary
        boundary.call_text = fixed_response
        def forbidden_vision(*args, **kwargs):
            raise AssertionError("No external Vision call is allowed")
        boundary.call_vision = forbidden_vision
    return module
builtins.__import__ = guarded_import
sys.argv = ["main.py", *sys.argv[1:]]
import main
exit_code = main.main()
import config
assert config.SELECTIVE_OCR_ENABLED
assert config.OCR_BACKEND == "directory"
assert config.MAX_BATCH_CHARS == 10000
assert config.MAX_BATCH_TABLE_CELLS == 100
assert config.SEMANTIC_ROUTING_ENABLED and not config.SEMANTIC_AUTO_RECLASSIFY
raise SystemExit(exit_code)
'''
    environment = dict(os.environ, PYTHON_DOTENV_DISABLED="1", OCR_NATIVE_CONFIDENCE_THRESHOLD="1.01")
    for key in ("RUN_STATE_ENABLED", "EXTRACTION_RESUME", "EXTRACTION_RETRY_FAILED_ONLY",
                "SEMANTIC_CONTRACT_GUARDS_ENABLED", "EXTRACTION_OBJECT_BATCHING_ENABLED"):
        environment[key] = "0"
    result = subprocess.run([sys.executable, "-c", wrapper, str(source), "--output", str(destination),
        "--no-images", "--full-scan", "--retries", "1", "--selective-ocr", "--ocr-results-dir", str(tmp_path / "ocr"),
        "--batch-max-chars", "10000", "--batch-max-table-cells", "100", "--semantic-routing", "audit"],
        cwd=Path(__file__).resolve().parents[1], env=environment, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert result.returncode == 0, result.stdout
    assert read_rows(destination)[0]["전망값"] == 987.6


def test_reexecution_and_resume_do_not_duplicate_ocr_rows(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path, resume=True)
    first, _ = run(source, tmp_path / "first.xlsx")
    second, _ = run(source, tmp_path / "second.xlsx")
    assert read_rows(first) == read_rows(second)
    assert len(read_rows(second)) == 1


def test_audit_save_failure_does_not_invalidate_excel(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path, ocr=False)
    with patch("agents.supervisor.persist_audit", side_effect=PermissionError("fixture denied")):
        result, supervisor = run(source, tmp_path / "audit_failure.xlsx")
    assert result.exists() and supervisor.last_outcome["excel_saved"]
    assert not supervisor.last_outcome["audit_saved"] and supervisor.last_outcome["warnings"]


def test_no_eligible_candidates_no_injection(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    with patch("utils.selective_ocr.build_triage_plan", return_value=[]):
        result, _ = run(source, tmp_path / "no_candidates.xlsx")
    assert read_rows(result) == []
    audit = json.loads(result.with_name(result.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["ledgers"]["ocr_import_audit"]["loaded_objects"] == 0


def test_quarantine_original_survives_with_run_state_off(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path, ocr=False)
    def response(prompt, **kwargs):
        raw = json.loads(external_response(prompt, **kwargs))
        if "emissions_regional" in raw:
            raw["emissions_regional"] = [{"부문": "건물", "배출유형": "에너지", "연도": 2030,
                                          "배출량": None, "출처페이지": 1, "근거ID": "fixture-evidence"}]
        return json.dumps(raw, ensure_ascii=False)
    with patch("utils.llm_client.call_text", side_effect=response):
        result = Supervisor().run(source, tmp_path / "quarantine.xlsx", include_images=False, max_pipeline_retries=1)
    audit = json.loads(result.with_name(result.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    held = audit["ledgers"]["_semantic_contract_quarantine"]
    assert held and "fixture-evidence" in json.dumps(held, ensure_ascii=False)


def test_object_inventory_counts_unique_rows_not_aliases(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    objects = build_document_objects(extract_pdf(source, render_graph_pages=False).pages)
    obj = objects[0]
    obj.metadata["evidence_id"] = "same-fact-evidence"
    data = {"emissions_forecast": [{"객체ID": obj.object_id, "근거ID": "same-fact-evidence"}]}
    rows = object_inventory([obj], data, {})
    assert rows[0]["본문반영행수"] == 1


def test_source_diagnostic_requires_valid_pages_and_numeric_signs():
    document = PDFContent(1, [PageContent(1, "-10", [], [])], "-10")
    records = source_validation({"emissions_forecast": [
        {"출처페이지": 99}, {"출처페이지": 1, "전망값": 10}]}, document)
    assert records[0]["status"] == "missing_source_page"
    assert records[1]["status"] == "numeric_review"


def test_vision_scope_merge_and_late_timeout_preserve_success(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_TRIAGE_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "IMAGE_CHART_TABLE_EXTRACTION", True)
    pages = [PageContent(n, "", [], [{"base64": str(n), "width": 300, "height": 200}]) for n in (1, 2)]
    response = json.dumps({"type": "chart_table", "target_sheet": "emissions_forecast", "chart_type": "표",
        "title": "관리권한 BAU", "confidence": "high", "unit": "천톤CO2eq",
        "table": [{"항목": "건물", "연도": 2030, "종류": "전망", "값": 77,
                   "fields": {"전망범위": "관리권한", "시나리오": "BAU"}}]}, ensure_ascii=False)
    with patch("utils.llm_client.call_vision", side_effect=[response, TimeoutError("fixture timeout")]):
        raw = ImageAgent().extract(pages, {}, "테스트시")
    assert raw["emissions_forecast"][0]["전망범위"] == "관리권한"
    assert raw["emissions_forecast"][0]["전망값"] == 77
    assert raw["emissions_forecast"][0]["출처페이지"] == 1
    assert raw["emissions_regional"] == []
    assert raw["vision_execution"]["completed_batches"] == 1
    assert len(raw["vision_execution"]["failed_batches"]) == 1


def test_ocr_reextraction_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    def invalid_response(prompt, **kwargs):
        if "987.6" in prompt:
            return '{"emissions_forecast": "invalid rows"}'
        return external_response(prompt, **kwargs)
    with patch("utils.llm_client.call_text", side_effect=invalid_response):
        result = Supervisor().run(source, tmp_path / "ocr_failed.xlsx", include_images=False, max_pipeline_retries=1)
    audit = json.loads(result.with_name(result.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    summary = audit["ledgers"]["ocr_import_audit"]
    assert summary["loaded_objects"] == 1 and summary["reading_success_objects"] == 1
    assert summary["extraction_success_objects"] == summary["body_reflected_objects"] == 0
    assert audit["ledgers"]["ocr_reextraction_audit"][0]["status"] == "reextraction_failed"
    assert read_rows(result) == []


def test_partial_vision_batch_keeps_success_and_reports_missing_input(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_TRIAGE_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "IMAGE_CHART_TABLE_EXTRACTION", True)
    pages = [PageContent(n, "", [], [{"base64": str(n), "width": 300, "height": 200}]) for n in (1, 2)]
    response = {"analyses": [{"image_index": 1, "type": "chart_table", "target_sheet": "emissions_forecast",
        "chart_type": "표", "title": "BAU 전망", "confidence": "high", "unit": "천톤CO2eq",
        "table": [{"항목": "건물", "연도": 2030, "종류": "전망", "값": 77,
                   "fields": {"전망범위": "관리권한", "시나리오": "BAU"}}]}]}
    with patch("utils.llm_client.call_vision_batch", return_value=json.dumps(response, ensure_ascii=False)):
        data = ImageAgent().extract(pages, {}, "테스트시")
    assert data["emissions_forecast"][0]["전망값"] == 77
    execution = data["vision_execution"]
    assert execution["completed_batches"] == 0 and execution["partial_batches"] == 1
    assert execution["failed_batches"][0]["diagnostics"][0]["code"] == "missing_image_result"


@pytest.mark.parametrize("mixed_table", [False, True])
def test_confirmed_index_exclusion_keeps_mixed_data_page(monkeypatch, mixed_table):
    monkeypatch.setattr(config, "IMAGE_TRIAGE_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "IMAGE_CHART_TABLE_EXTRACTION", True)
    text = "그림 목차\n" + "\n".join(f"그림 {n} 배출량 ... {n + 10}" for n in range(1, 6))
    page = PageContent(1, text, ["<table><tr><td>2030</td><td>77</td></tr></table>"] if mixed_table else [],
                       [{"base64": "fixture", "width": 500, "height": 600, "caption": "Page 1 full render"}])
    with patch("utils.llm_client.call_vision", return_value='{"type":"해당없음"}') as external:
        data = ImageAgent().extract([page], {}, "테스트시")
    assert external.call_count == int(mixed_table)
    assert data["vision_execution"]["index_pages_excluded"] == ([] if mixed_table else [1])
