"""외부 모델만 대체. 실제 PDF → 추출 → Organizer → Excel 재조회 통합 포함."""
from copy import deepcopy
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
from agents.extractor_agent import ExtractorAgent
from agents.supervisor import Supervisor, QUALITY_SYSTEM
from utils.document_objects import DocumentObject, build_document_objects, parse_html_table_structure
from utils.pdf_reader import PageContent, extract_pdf
from utils.source_coverage import (audit_saved_run, compare, itemize, persist, read_excel,
                                   sha256_file, snapshot, source_snapshot)


def table(rows=None, **metadata):
    return DocumentObject("table-1", "table", 1, 1,
        rows=rows or [["사업명", "연도", "예상감축량", "단위"], ["Green roof", "2024", "12.5", "tCO2eq"]],
        bbox=(10, 10, 400, 100), metadata={"evidence_id": "ev-table-1", "header_row_count": 1, **metadata})


def extracted(**overrides):
    return {"사업명": "Green roof", "연도": 2024, "예상감축량": 12.5,
            "단위": "tCO2eq", "출처페이지": 1, **overrides}


def compare_one(obj=None, row=None, **kwargs):
    data = {} if row is False else {"quantitative_reductions": [row or extracted()]}
    journal, summary = compare([obj or table()], "document-hash", data, **kwargs)
    return journal[0], summary


def test_stages_unknown_not_invented_and_zero_is_a_fact():
    obj = table([["사업명", "단위", "2024", "2025", "2026"], ["Green roof", "tCO2eq", "12.5", "0", "-"]])
    journal, summary = compare([obj], "doc", {"quantitative_reductions": [extracted()]})
    facts = [r for r in journal if "identity" in r]
    assert [(r["identity"]["year"], r["status"]) for r in facts] == [(2024, "covered"), (2025, "missing_candidate")]
    assert facts[1]["raw_value"] == "0"
    assert all(r["stages"]["raw"]["status"] == "unknown" for r in facts)
    assert summary["comparable_facts"] == 2 and summary["counts"]["excluded"] == 1


@pytest.mark.parametrize("raw_value,raw_unit,value,out_unit", [
    ("3960000", "㎡", 396, "ha"), ("10", "MW", 10000, "kW"),
    ("70", "천그루", 70000, "그루"), ("2", "억원", 200, "백만원"),
])
def test_explicit_unit_equivalence(raw_value, raw_unit, value, out_unit):
    metric = "예산액" if raw_unit == "억원" else "활동량"
    unit_field = "예산단위" if metric == "예산액" else "활동단위"
    sheet = "financial_plan" if metric == "예산액" else "quantitative_reductions"
    obj = table([["사업명", "연도", metric, unit_field], ["Task", "2024", raw_value, raw_unit]])
    row = {"사업명": "Task", "연도": 2024, metric: value, unit_field: out_unit, "출처페이지": 1}
    journal, _ = compare([obj], "doc", {sheet: [row]})
    assert journal[0]["status"] == "covered"


def test_co2_and_co2eq_not_equivalent():
    result, _ = compare_one(row=extracted(단위="tCO2"))
    assert result["status"] == "unit_review"


def test_conflict_not_hidden_by_a_matching_duplicate():
    journal, _ = compare([table()], "doc", {"quantitative_reductions": [extracted(), extracted(예상감축량=99)]})
    assert journal[0]["status"] == "conflict_review"
    assert journal[0]["stages"]["excel"]["matches"] and journal[0]["stages"]["excel"]["conflicts"]


def test_wrong_department_and_same_code_different_domain_are_not_covered():
    obj = table([["과제ID", "과제명", "대응기반영역", "담당부서"], ["4-1", "Task", "국제협력", "Dept A"]])
    row = {"과제ID": "4-1", "과제명": "Task", "대응기반영역": "적응", "주관부서": "Dept A", "출처페이지": 1}
    journal, _ = compare([obj], "doc", {"foundation_measures": [row]})
    assert journal[0]["status"] == "missing_candidate"
    row["대응기반영역"] = "국제협력"
    row["주관부서"] = "Dept B"
    journal, _ = compare([obj], "doc", {"foundation_measures": [row]})
    assert journal[0]["status"] == "conflict_review"


def test_missing_context_and_funding_are_review_not_simple_missing():
    result, _ = compare_one(obj=table(계획구분="감축"))
    assert result["status"] == "ambiguous_review"
    obj = table([["사업명", "연도", "예산액", "예산단위"], ["Task", "2024", "1", "억원"]])
    journal, _ = compare([obj], "doc", {"financial_plan": [
        {"사업명": "Task", "연도": 2024, "예산액": 1, "예산단위": "억원", "재원구분": "국비", "출처페이지": 1}]})
    assert journal[0]["status"] == "ambiguous_review"


def test_missing_pages_is_not_covered_by_name_only():
    row = extracted()
    row.pop("출처페이지")
    result, _ = compare_one(row=row)
    assert result["status"] == "ambiguous_review"


def test_quarantine_storage_loss_and_cleaning_loss_separated():
    raw = {"quantitative_reductions": [extracted()]}
    held = [{"sheet_key": "quantitative_reductions", "row": extracted(), "reason_codes": ["fixture_hold"]}]
    result, _ = compare_one(row=False, raw=raw, cleaned={}, quarantine=held)
    assert result["status"] == "cleaning_excluded"
    assert result["quarantine"][0]["reason_codes"] == ["fixture_hold"]
    result, _ = compare_one(row=False, raw=raw, cleaned={})
    assert result["reason"] == "present_raw_absent_after_cleaning"
    result, _ = compare_one(row=False, raw=raw, cleaned=raw)
    assert result["status"] == "storage_review"


def test_equivalent_fact_elsewhere_not_reextraction_candidate():
    obj = table([["사업명", "연도", "활동량", "활동단위"], ["Task", "2024", "10", "MW"]])
    journal, _ = compare([obj], "doc", {"annual_implementation": [
        {"사업명": "Task", "연도": 2024, "목표물량": 10000, "목표단위": "kW", "출처페이지": 1}]})
    assert journal[0]["status"] == "covered_elsewhere"


def test_period_and_image_unsupported_not_missing():
    obj = table([["사업명", "연도", "예산액", "예산단위"], ["Task", "2024~2033", "100", "억원"]])
    image = DocumentObject("image-1", "image", 2, 1)
    journal, summary = compare([obj, image], "doc", {})
    assert summary["comparable_facts"] == 0
    assert {r["reason"] for r in journal} == {"period_or_ambiguous_year", "image_or_caption_only"}
    assert not any(r["status"] == "missing_candidate" for r in journal)


def test_reference_duplicate_and_formula_behaviour():
    original = table()
    alias = deepcopy(original)
    alias.object_id = "alias-object"
    facts, _, counts = itemize([original, alias, table(index_reference=True)], "doc")
    assert len(facts) == 1 and counts["duplicate_object"] == counts["excluded_reference"] == 1
    assert "alias-object" in facts[0]["source"]["aliases"]
    result, _ = compare_one(row=extracted(예상감축량="=10+2.5"))
    assert result["status"] == "ambiguous_review"


def test_exposure_distinguishes_route_skips_empty_and_failure():
    routes = [{"sheet_key": "quantitative_reductions", "not_selected_pages": [1]}]
    result, _ = compare_one(row=False, routes=routes)
    assert result["extraction"]["no_record_reason"] == "not_routed"
    records = [{"batch_id": "b", "sheet_key": "quantitative_reductions", "page_nums": [1],
                "model_requested": True, "status": "ok", "rows": 0}]
    result, _ = compare_one(row=False, records=records)
    assert result["extraction"]["records"][0]["model_requested"] is True
    assert result["extraction"]["records"][0]["association"] == "page_association_only"
    assert result["extraction"]["exact_cell_content_exposure"] == "not_verified"


def test_persistence_repeat_ids_and_no_implicit_overwrite(tmp_path):
    first, summary = compare([table()], "doc", {})
    second, _ = compare([table()], "doc", {})
    assert first == second
    saved = persist(tmp_path / "audit", first, summary)
    reread = [json.loads(line) for line in Path(saved["missing_candidates_path"]).read_text(encoding="utf-8").splitlines()]
    assert reread == first and saved["journal_records"] == 1
    with pytest.raises(FileExistsError):
        persist(tmp_path / "audit", second, summary)


def make_pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    xs, ys = [30, 220, 300, 430, 570], [100, 135, 170, 205]
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    rows = [["사업명", "연도", "예상감축량", "단위"],
            ["Green roof", "2024", "12.5", "tCO2eq"], ["Green roof", "2025", "0", "tCO2eq"]]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            page.insert_text((xs[c] + 6, ys[r] + 23), value, fontsize=10, fontname="korea")
    doc.save(path)
    doc.close()


def fixed_response(prompt, **kwargs):
    if "municipality_name" in prompt:
        return '{"municipality_name":"테스트시"}'
    keys = re.findall(r'\{\s*"([a-z_]+)"\s*:\s*\[', prompt)
    if not keys:
        return '{"quality_level":"보통","assessment":"offline fixture"}'
    key = keys[-1]
    return json.dumps({key: [extracted()] if key == "quantitative_reductions" else []}, ensure_ascii=False)


def settings(monkeypatch, mode):
    values = {"SOURCE_COVERAGE_MODE": mode, "FULL_DOCUMENT_SCAN": True,
              "SELECTIVE_OCR_ENABLED": False, "EXTRACTION_OBJECT_BATCHING_ENABLED": False,
              "MAX_BATCH_CHARS": None, "MAX_BATCH_TABLE_CELLS": None,
              "SEMANTIC_ROUTING_ENABLED": False, "RUN_STATE_ENABLED": False,
              "EXTRACTION_RESUME": False, "EXTRACTION_RETRY_FAILED_ONLY": False}
    for key, value in values.items():
        monkeypatch.setattr(config, key, value)


def run_fixture(source, destination):
    with patch("utils.llm_client.call_text", side_effect=fixed_response) as calls, \
         patch("utils.llm_client.call_vision", side_effect=AssertionError("external Vision forbidden")):
        supervisor = Supervisor()
        result = supervisor.run(source, destination, include_images=False, max_pipeline_retries=1)
    return result, supervisor, [(c.args, c.kwargs) for c in calls.call_args_list]


def business_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        return {sheet: [[(cell.value, cell.data_type) for cell in row] for row in wb[config.SHEET_KEY_TO_NAME[sheet]]]
                for sheet in config.EXTRACTION_SHEETS}
    finally:
        wb.close()


def assert_same_extraction_calls(left, right):
    # 최종 보조 검수는 감사 ON/OFF의 범위를 설명하므로 입력이 달라져야 한다.
    # 외부 호출 수와 그 이전 지자체명/업무 추출 프롬프트는 그대로 유지한다.
    assert len(left) == len(right) and len(left) > 1
    assert left[:-1] == right[:-1]
    for args, kwargs in (left[-1], right[-1]):
        assert kwargs.get("system") == QUALITY_SYSTEM
        assert '"verification_notes"' in args[0]
    assert left[-1][0][0] != right[-1][0][0]


def test_real_pdf_organizer_excel_roundtrip_and_unchanged_calls(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    parsed = extract_pdf(source, render_graph_pages=False)
    facts, _, _ = itemize(build_document_objects(parsed.pages), sha256_file(source))
    assert len(facts) == 2
    settings(monkeypatch, "off")
    off, _, off_calls = run_fixture(source, tmp_path / "off.xlsx")
    settings(monkeypatch, "audit")
    on, supervisor, on_calls = run_fixture(source, tmp_path / "on.xlsx")
    assert_same_extraction_calls(off_calls, on_calls)
    assert business_rows(off) == business_rows(on)
    assert not (tmp_path / "off_coverage_summary.json").exists()
    saved = supervisor.last_outcome["source_coverage"]
    assert saved["status"] == "saved" and saved["counts"] == {"covered": 1, "missing_candidate": 1}
    records = [json.loads(v) for v in Path(saved["missing_candidates_path"]).read_text(encoding="utf-8").splitlines()]
    covered = next(r for r in records if r["status"] == "covered")
    missing = next(r for r in records if r["status"] == "missing_candidate")
    assert covered["stages"]["excel"]["matches"][0]["cells"]["예상감축량"].endswith("J2")
    assert missing["identity"]["year"] == 2025 and missing["raw_value"] == "0"
    assert missing["stages"]["raw"]["status"] == "absent"
    assert missing["extraction"]["records"][0]["model_requested"] is True
    audit = json.loads(on.with_name("on_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["context"]["source_coverage"]["coverage_summary_path"] == saved["coverage_summary_path"]
    assert audit["ledgers"]["source_coverage_snapshot"]["raw"]["quantitative_reductions"]
    # 재실행이 아니라 저장된 결과의 감사만 수행한다.
    repeated = audit_saved_run(source, on, on.with_name("on_pipeline_audit.json"), tmp_path / "standalone")
    assert repeated["counts"] == saved["counts"]


def test_revenue_exclusion_in_supervisor_and_standalone_keeps_model_calls_and_business_cells(tmp_path, monkeypatch):
    source = tmp_path / "revenue.pdf"
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((30, 75), "[표 1-1] 전력자원 도입 수익금", fontsize=11, fontname="korea")
    xs, ys = [30, 220, 300, 430, 570], [100, 135, 170]
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for r, values in enumerate([["사업명", "예산단위", "2024", "2025"],
                               ["전력 정산금", "백만원", "1000", "2000"]]):
        for c, value in enumerate(values):
            page.insert_text((xs[c] + 6, ys[r] + 23), value, fontsize=10, fontname="korea")
    doc.save(source)
    doc.close()
    parsed = build_document_objects(extract_pdf(source, render_graph_pages=False).pages)
    source_facts, diagnostics, _ = itemize(parsed, sha256_file(source))
    assert source_facts == []
    assert sum(r["reason"] == "non_investment_revenue_outside_budget_scope" for r in diagnostics) == 2
    settings(monkeypatch, "off")
    off, _, off_calls = run_fixture(source, tmp_path / "off.xlsx")
    settings(monkeypatch, "audit")
    on, supervisor, on_calls = run_fixture(source, tmp_path / "on.xlsx")
    assert_same_extraction_calls(off_calls, on_calls)
    assert business_rows(off) == business_rows(on)
    saved = supervisor.last_outcome["source_coverage"]
    assert saved["status"] == "saved" and saved["comparable_facts"] == 0
    journal = [json.loads(line) for line in Path(saved["missing_candidates_path"]).read_text(encoding="utf-8").splitlines()]
    assert sum(r["reason"] == "non_investment_revenue_outside_budget_scope" for r in journal) == 2
    standalone = audit_saved_run(source, on, on.with_name("on_pipeline_audit.json"), tmp_path / "standalone")
    assert standalone["counts"] == saved["counts"]


def test_audit_failure_does_not_invalidate_saved_excel(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    settings(monkeypatch, "audit")
    with patch("utils.source_coverage.persist", side_effect=PermissionError("fixture denied")):
        result, supervisor, _ = run_fixture(source, tmp_path / "failure.xlsx")
    assert result.exists() and supervisor.last_outcome["excel_saved"]
    assert supervisor.last_outcome["source_coverage"]["status"] == "failed"
    assert supervisor.last_outcome["warnings"]


def test_model_boundary_empty_skip_and_fail_records(monkeypatch):
    settings(monkeypatch, "audit")
    agent = ExtractorAgent()
    obj = table()
    with patch("utils.llm_client.call_text", return_value='{"quantitative_reductions":[]}') as call:
        agent._run_extraction_batch("quantitative_reductions", "fixture", "City", "", obj, "text_chunk", [1])
    assert call.call_count == 1
    assert agent.extraction_records[-1]["model_requested"] is True
    assert agent.extraction_records[-1]["rows"] == 0
    monkeypatch.setattr(config, "FULL_DOCUMENT_SCAN", False)
    with patch("utils.llm_client.call_text", side_effect=AssertionError("must not call")):
        agent._run_extraction_batch("quantitative_reductions", "nothing", "City", "", obj, "text_chunk", [1])
    assert agent.extraction_records[-1]["model_requested"] is False
    assert agent.extraction_records[-1]["no_call_reason"] == "keyword_gate"
    monkeypatch.setattr(config, "FULL_DOCUMENT_SCAN", True)
    with patch("utils.llm_client.call_text", side_effect=TimeoutError("fixture")):
        agent._run_extraction_batch("quantitative_reductions", "fixture", "City", "", obj, "text_chunk", [1])
    assert agent.extraction_records[-1]["model_requested"] is True
    assert agent.extraction_records[-1]["status"] == "call_fail"


def test_standalone_cli_no_config_dotenv_model_network_or_process(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    settings(monkeypatch, "audit")
    excel, _, _ = run_fixture(source, tmp_path / "stored.xlsx")
    wrapper = '''
import runpy, sys
def block(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise AssertionError("forbidden external action: " + event)
sys.addaudithook(block)
runpy.run_path("scripts/audit_source_coverage.py", run_name="__main__")
assert "config" not in sys.modules
assert "dotenv" not in sys.modules
assert "utils.llm_client" not in sys.modules
'''
    # runpy __main__ exits before assertions, therefore enforce imports via a guard too.
    wrapper = wrapper.replace('runpy.run_path("scripts/audit_source_coverage.py", run_name="__main__")',
        'module = runpy.run_path("scripts/audit_source_coverage.py"); module["main"]()')
    result = subprocess.run([sys.executable, "-B", "-c", wrapper,
        "--input", str(source), "--excel", str(excel),
        "--pipeline-audit", str(excel.with_name("stored_pipeline_audit.json")),
        "--output-prefix", str(tmp_path / "cli")],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, PYTHON_DOTENV_DISABLED="1"),
        encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    assert result.returncode == 0, result.stdout
    assert (tmp_path / "cli_coverage_summary.json").exists()


def test_document_and_excel_hash_mismatch_rejected(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    settings(monkeypatch, "audit")
    excel, _, _ = run_fixture(source, tmp_path / "stored.xlsx")
    audit = excel.with_name("stored_pipeline_audit.json")
    another = tmp_path / "different.pdf"
    another.write_bytes(b"different source")
    with pytest.raises(ValueError, match="문서 SHA256"):
        audit_saved_run(another, excel, audit, tmp_path / "bad_document")
    with excel.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="Excel SHA256"):
        audit_saved_run(source, excel, audit, tmp_path / "bad_excel")


def test_legacy_audit_missing_snapshots_never_asserts_raw_loss(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    settings(monkeypatch, "off")
    excel, _, _ = run_fixture(source, tmp_path / "legacy.xlsx")
    result = audit_saved_run(source, excel, excel.with_name("legacy_pipeline_audit.json"), tmp_path / "legacy_audit")
    assert result["stage_availability"] == {"raw": False, "cleaned": False, "excel": True}
    assert result["provenance"]["excel_binding"] == "legacy_filename_only_unverified_hash"


def test_abbreviated_year_repeated_header_and_total_not_annualized():
    cover = DocumentObject("cover", "text", 1, 1, text="테스트시 기본계획(2024~2033)")
    reference = DocumentObject("reference", "text", 5, 1, text="다른 기본계획(2021~2025)")
    obj = table([["사업명", "예산단위", "’24", "’25", "합계"],
                 ["Task", "백만원", "10", "20", "30"],
                 ["사업명", "예산단위", "’30", "’31", "합계"],
                 ["Task", "백만원", "30", "40", "70"]])
    obj.page_number = 10
    facts, diagnostics, counts = itemize([cover, reference, obj], "doc")
    assert [f["identity"]["year"] for f in facts] == [2024, 2025, 2030, 2031]
    assert facts[-1]["source"]["year_header_row"] == 3
    assert facts[-1]["source"]["year_scope_evidence"]["object_id"] == "cover"
    assert counts["repeated_year_header"] == 1
    facts, diagnostics, _ = itemize([obj], "doc")
    assert facts == [] and diagnostics[0]["reason"] == "no_explicit_year_or_metric"
    obj.nearby_text = "기본계획(2024~2033), 다른 기본계획(2021~2025)"
    assert itemize([cover, obj], "doc")[0] == []  # 문서 기간으로 모호한 현지 문맥을 덮지 않음


def test_repeated_blank_name_not_inherited_and_currency_scope_conservative():
    obj = table([["사업명", "구분", "2024", "2025"], ["Task", "계", "10", "20"],
                 ["", "국비", "5", "10"]])
    obj.caption = "투자계획"
    obj.nearby_text = "투자계획 (단위: 백만원)"
    facts, diagnostics, _ = itemize([obj], "doc")
    assert len(facts) == 2 and all(f["identity"]["context"]["재원구분"] == "계" for f in facts)
    assert any(r["reason"] == "merged_business_identity_unresolved" for r in diagnostics)
    obj.caption = "활동량 계획"
    assert itemize([obj], "doc")[0] == []  # 다른 표의 화폐 단위를 활동 표로 가져오지 않음


def test_clear_merged_multiheader_supported_and_bad_structure_held():
    structure = parse_html_table_structure('''<table><thead>
      <tr><th rowspan="2">사업명</th><th rowspan="2">단위</th><th colspan="2">예상감축량</th></tr>
      <tr><th>2024</th><th>2025</th></tr></thead>
      <tr><td>Task</td><td>tCO2eq</td><td>1</td><td>2</td></tr></table>''')
    obj = table(structure[0], header_row_count=structure[1])
    facts, _, _ = itemize([obj], "doc")
    assert len(facts) == 2
    obj.metadata["table_parse_error"] = "overlapping span"
    facts, diagnostics, _ = itemize([obj], "doc")
    assert facts == [] and diagnostics[0]["reason"] == "unsupported_table_structure"


def test_real_organizer_and_excel_for_all_three_schemas(tmp_path):
    from agents.organizer_agent import OrganizerAgent
    from agents.excel_agent import ExcelAgent
    objects = [
        table([["사업명", "연도", "활동량", "활동단위"], ["Capacity", "2024", "10", "MW"]]),
        table([["사업명", "연도", "예산액", "예산단위", "재원구분"], ["Budget", "2024", "1", "억원", "계"]]),
        table([["과제명", "대응기반영역", "담당부서"], ["Cooperate", "국제협력", "Dept"]]),
    ]
    raw = {"municipality_name": "테스트시", "quantitative_reductions": [
            {"사업명": "Capacity", "연도": "2024", "활동량": "10000", "활동단위": "kW", "출처페이지": 1}],
           "financial_plan": [{"사업명": "Budget", "연도": "2024", "예산액": "100", "예산단위": "백만원", "재원구분": "합계", "출처페이지": 1}],
           "foundation_measures": [{"과제명": "Cooperate", "대응기반영역": "국제협력", "주관부서": "Dept", "출처페이지": 1}]}
    before = deepcopy(raw)
    cleaned = OrganizerAgent().organize(raw)
    excel = ExcelAgent().write(cleaned, tmp_path / "three.xlsx")
    journal, summary = compare(objects, "doc", read_excel(excel), raw=snapshot(before), cleaned=snapshot(cleaned))
    assert summary["counts"] == {"covered": 3}
    assert {r["sheet"] for r in journal} == {"quantitative_reductions", "financial_plan", "foundation_measures"}
    assert all(r["stages"]["excel"]["matches"][0]["cells"] for r in journal)
    assert raw == before


def test_successful_resume_preserves_results_and_records_no_second_model_request(tmp_path, monkeypatch):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    settings(monkeypatch, "audit")
    monkeypatch.setattr(config, "RUN_STATE_ENABLED", True)
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "EXTRACTION_RESUME", True)
    first, _, first_calls = run_fixture(source, tmp_path / "first.xlsx")
    second, supervisor, second_calls = run_fixture(source, tmp_path / "second.xlsx")
    assert len(first_calls) > len(second_calls)
    assert business_rows(first) == business_rows(second)
    saved = supervisor.last_outcome["source_coverage"]
    assert saved["extraction_records"]["boundary_requested"] == 0
    assert saved["extraction_records"]["no_call_reasons"]["success_restored"] == 16


def test_natural_page_route_exclusion_is_recorded_without_model_batch(monkeypatch):
    settings(monkeypatch, "audit")
    monkeypatch.setattr(config, "FULL_DOCUMENT_SCAN", False)
    agent = ExtractorAgent()
    with patch("utils.llm_client.call_text", side_effect=fixed_response) as external:
        agent.extract([PageContent(1, "unrelated fixture content", [], [])], "fixture", {})
    assert external.call_count == 1  # 기존 지자체명 추출만 수행
    assert agent.extraction_records == []
    assert all(r["not_selected_pages"] == [1] for r in agent.coverage_routes)


def test_independent_audit_never_overwrites_source_pipeline_even_with_flag(tmp_path):
    source_audit = tmp_path / "run_pipeline_audit.json"
    source_audit.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="원본 pipeline_audit"):
        audit_saved_run("not-read.pdf", "not-read.xlsx", source_audit, tmp_path / "run", overwrite=True)
    assert source_audit.read_text(encoding="utf-8") == "{}"


def test_native_source_snapshot_does_not_follow_ocr_or_model_mutations():
    original = table()
    captured = source_snapshot([original])
    original.rows[-1][-2] = "999"
    original.metadata["engine"] = "ocr"
    assert captured[0]["rows"][-1][-2] == "12.5"
    assert captured[0]["metadata"].get("engine") != "ocr"


def test_ambiguous_same_named_plan_not_counted_as_covered_even_on_same_page():
    journal, _ = compare([table()], "doc", {"quantitative_reductions": [
        extracted(계획구분="감축"), extracted(계획구분="적응")]})
    assert journal[0]["status"] == "ambiguous_review"


def test_budget_unit_not_borrowed_from_another_activity_table():
    obj = table([["사업명", "연도", "활동량"], ["Task", "2024", "10"]])
    obj.caption = "활동 계획"
    obj.nearby_text = "다른 투자표 (단위: 백만원)"
    facts, diagnostics, _ = itemize([obj], "doc")
    assert facts == [] and diagnostics[0]["reason"] == "explicit_unit_evidence_required"


def test_explicit_cli_flag_reaches_real_supervisor(tmp_path):
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    wrapper = '''
import builtins, json, re, sys
original_import = builtins.__import__
installed = False
def response(prompt, **kwargs):
    if "municipality_name" in prompt:
        return '{"municipality_name":"City"}'
    keys = re.findall(r'\\{\\s*"([a-z_]+)"\\s*:\\s*\\[', prompt)
    return json.dumps({keys[-1]: []}) if keys else '{"quality_level":"fixture"}'
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    global installed
    module = original_import(name, globals, locals, fromlist, level)
    if name == "agents.supervisor" and not installed:
        installed = True
        import utils.llm_client as boundary
        boundary.call_text = response
        boundary.call_vision = lambda *a, **k: (_ for _ in ()).throw(AssertionError("vision forbidden"))
    return module
builtins.__import__ = guarded_import
import main
raise SystemExit(main.main())
'''
    environment = dict(os.environ, PYTHON_DOTENV_DISABLED="1", SOURCE_COVERAGE_MODE="off",
        FULL_DOCUMENT_SCAN="0", RUN_STATE_ENABLED="0", SELECTIVE_OCR_ENABLED="0",
        MAX_BATCH_CHARS="0", MAX_BATCH_TABLE_CELLS="0", EXTRACTION_OBJECT_BATCHING_ENABLED="0")
    result = subprocess.run([sys.executable, "-B", "-c", wrapper, str(source), "--output", str(tmp_path / "cli_main.xlsx"),
        "--no-images", "--full-scan", "--retries", "1", "--source-coverage", "audit"],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout
    summary = json.loads((tmp_path / "cli_main_coverage_summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "audit" and summary["comparable_facts"] == 2
