"""외부 모델 경계만 mock. 셀 저장/재조회/정규화/평가는 실제 코드를 사용한다."""
import json
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

import config
from utils.excel_writer import write_excel
from utils.integration_audit import document_hash
from utils.quality_adapters import adapt_warnings, deduplicate_issues, pages
from utils.quality_evaluation import evaluate_saved_excel, persist_quality_report
from utils.benchmark_evaluation import evaluate_registered, score_v2
from utils.pdf_reader import PDFContent, PageContent


def finance(**changes):
    return {"지자체명": "서울특별시", "사업명": "그린리모델링", "계획구분": "기본계획", "부문": "건물",
            "재원구분": "국비", "연도": 2030, "예산액": 0, "예산단위": "백만원", "출처페이지": 1,
            "근거ID": "ev-1", **changes}


def saved(tmp_path, rows, **data):
    payload = {"financial_plan": rows, **data}
    path = write_excel(payload, tmp_path / "saved.xlsx")
    return path, payload


def golden(tmp_path, rows):
    from utils.evaluation_v1.golden_score_contract import 계약헤더, 골든열
    path = tmp_path / "golden.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "11_재정투자계획"
    headers = 계약헤더(ws.title) + 골든열
    ws.append(headers)
    for row in rows:
        record = {**row, "골든_출처유형": "텍스트표", "골든_출처페이지": str(row.get("출처페이지", 1))}
        ws.append([record.get(f) for f in headers])
    wb.save(path)
    wb.close()
    return path


def manifest(tmp_path, gold, *, status="approved", role="development", scope=None):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"fixed source hash fixture")
    data = {"id": "fixture-v1", "role": role, "review_status": status, "locked": True,
            "source_sha256": document_hash(source), "golden": gold.name,
            "golden_sha256": document_hash(gold)}
    if scope:
        data["scope"] = scope
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"datasets": [data]}), encoding="utf-8")
    return source, path


def test_zero_valid_source_and_actual_storage(tmp_path):
    path, expected = saved(tmp_path, [finance()])
    doc = PDFContent(total_pages=1, full_text="", pages=[PageContent(page_number=1, text="서울특별시 그린리모델링 건물 2030 0 백만원", tables=[], images=[])])
    report = evaluate_saved_excel(path, expected=expected, municipality="서울시", document=doc,
        objects=[{"object_id": "obj-1", "page_number": 1, "metadata": {"evidence_id": "ev-1"}}])
    assert report["metrics"]["required_field_fill"]["value"] == 1
    assert report["metrics"]["storage_agreement"]["value"] == 1
    assert report["metrics"]["object_binding"]["value"] == 1
    assert not report["issues"] and report["rule_score"] is None
    assert report["accuracy"]["cell_accuracy"] is None


@pytest.mark.parametrize("unit", ["tCO2eq", "일", "개", "개소(가구)"])
def test_non_money_budget_cells_cannot_pass(tmp_path, unit):
    path, _ = saved(tmp_path, [finance(예산단위=unit)])
    report = evaluate_saved_excel(path)
    item = next(i for i in report["issues"] if i["rule_id"] == "budget.non_money_unit")
    assert item["certainty"] == "confirmed" and item["cell"] == "H2"
    assert report["review_status"] == "needs_review"


def test_wrong_city_year_unit_numbers_not_promoted_to_accuracy(tmp_path):
    path, _ = saved(tmp_path, [finance(지자체명="서울특별시 강남구", 연도=129, 예산단위="일")])
    report = evaluate_saved_excel(path, municipality="서울특별시")
    assert {"context.municipality_mismatch", "time.invalid_year", "budget.non_money_unit"} <= report["issues_by_rule"].keys()
    assert report["accuracy"]["cell_accuracy"] is None


def test_qualitative_period_rows_do_not_require_annual_numeric_fields(tmp_path):
    path, _ = saved(tmp_path, [finance(연도="2024~2033 합계")],
        governance_feedback=[{"거버넌스기구": "위원회", "역할": "자문"}],
        annual_implementation=[{"사업명": "제도 정비", "연간계획": "조례 마련", "기간시작": 2024, "기간종료": 2033}])
    report = evaluate_saved_excel(path)
    assert not any(i["rule_id"].startswith(("required.", "time.")) for i in report["issues"])


def test_actual_file_tampering_is_detected_and_input_not_rewritten(tmp_path):
    path, expected = saved(tmp_path, [finance()])
    wb = openpyxl.load_workbook(path)
    wb["11_재정투자계획"]["G2"] = 999
    wb.save(path)
    wb.close()
    before = document_hash(path)
    report = evaluate_saved_excel(path, expected=expected)
    assert report["issues_by_rule"]["storage.cell_mismatch"] == 1
    assert report["metrics"]["storage_agreement"]["value"] < 1
    assert document_hash(path) == before


def test_warning_formats_deduplicated_without_inventing_affected_rows():
    records = [{"영역": "financial_plan", "항목": "충돌", "문제내용": "값 검토", "심각도": "오류"},
               {"대상시트": "11_재정투자계획", "항목": "충돌", "사유": "값 검토"}]
    issues = deduplicate_issues(adapt_warnings({"validation_report": records}))
    assert len(issues) == 1 and issues[0]["occurrences"] == 2
    assert issues[0]["certainty"] == "suspected" and issues[0]["excel_row"] is None


def test_missing_denominators_are_null_and_report_files_round_trip(tmp_path):
    path, _ = saved(tmp_path, [])
    report = evaluate_saved_excel(path)
    assert all(not metric["available"] and metric["value"] is None for metric in report["metrics"].values())
    saved_paths = persist_quality_report(path, report)
    loaded = json.loads(Path(saved_paths["paths"]["json"]).read_text(encoding="utf-8"))
    assert loaded["business_rows"] == 0 and "issues" not in loaded
    assert Path(loaded["issues_path"]).exists()


def test_duplicate_and_conflict_have_actual_excel_rows(tmp_path):
    path, _ = saved(tmp_path, [finance(), finance(), finance(예산액=100)])
    report = evaluate_saved_excel(path)
    assert report["issues_by_rule"]["identity.exact_duplicate"] == 1
    assert report["issues_by_rule"]["identity.value_conflict"] == 3
    assert report["affected_rows"] == 3


def test_money_conflict_respects_unit_scale(tmp_path):
    path, _ = saved(tmp_path, [finance(예산액=2, 예산단위="억원"), finance(예산액=200)])
    assert "identity.value_conflict" not in evaluate_saved_excel(path)["issues_by_rule"]
    path, _ = saved(tmp_path, [finance(예산액=2, 예산단위="억원"), finance(예산액=2)])
    assert evaluate_saved_excel(path)["issues_by_rule"]["identity.value_conflict"] == 2


def test_missing_identity_is_not_silently_fully_checked(tmp_path):
    path, _ = saved(tmp_path, [finance(계획구분=None)])
    assert evaluate_saved_excel(path)["metrics"]["identity_check_coverage"]["value"] == 0


def test_page_parser_and_source_unavailability(tmp_path):
    assert pages("[1, 3]") == ([1, 3], None)
    assert pages("1-3") == ([1, 2, 3], None)
    assert pages("1,broken")[1]
    path, _ = saved(tmp_path, [finance(출처페이지="broken")])
    report = evaluate_saved_excel(path)
    assert "provenance.invalid_page" in report["issues_by_rule"]
    assert report["metrics"]["object_binding"]["value"] is None


def test_batch_failure_is_separate_from_empty_success_and_reused(tmp_path):
    path, _ = saved(tmp_path, [finance()])
    report = evaluate_saved_excel(path, diagnostics={"extraction_records": [
        {"status": "ok", "row_count": 0}, {"status": "reused"}, {"status": "skipped"},
        {"status": "failed", "sheet_key": "financial_plan", "batch_id": "b1"}]})
    assert report["issues_by_rule"]["execution.batch_incomplete"] == 1
    assert report["execution"]["records"] == 4


@pytest.mark.parametrize("status,role,expected", [("draft", "development", "미승인"), ("approved", "holdout", "holdout")])
def test_auto_cannot_open_draft_or_holdout(tmp_path, status, role, expected):
    path, _ = saved(tmp_path, [finance()])
    gold = golden(tmp_path, [finance()])
    source, registry = manifest(tmp_path, gold, status=status, role=role)
    result = evaluate_registered(path, source, manifest_path=registry)
    assert result["status"] == "unassessed" and expected in result["reason"]
    assert result["metrics"]["cell_accuracy"] is None


def test_approved_automatic_and_explicit_draft_both_score(tmp_path):
    path, _ = saved(tmp_path, [finance()])
    gold = golden(tmp_path, [finance()])
    source, registry = manifest(tmp_path, gold)
    before = document_hash(path)
    result = evaluate_registered(path, source, manifest_path=registry)
    assert result["status"] == "evaluated", result
    assert result["metrics"]["cell_accuracy"] == 1
    assert result["legacy_v1"]["metrics"]["cell_accuracy"] == 1
    assert Path(result["path"]).exists() and document_hash(path) == before
    source, registry = manifest(tmp_path, gold, status="draft")
    assert evaluate_registered(path, source, manifest_path=registry, allow_draft=True)["status"] == "evaluated"


def test_no_golden_wrong_hash_and_off_never_score(tmp_path):
    path, _ = saved(tmp_path, [finance()])
    gold = golden(tmp_path, [finance()])
    source, registry = manifest(tmp_path, gold)
    source.write_bytes(b"other municipality")
    result = evaluate_registered(path, source, manifest_path=registry, mode="required")
    assert result["status"] == "required_unavailable" and result["metrics"]["cell_accuracy"] is None
    source, registry = manifest(tmp_path, gold)
    gold.write_bytes(b"bad hash")
    assert "해시 불일치" in evaluate_registered(path, source, manifest_path=registry)["reason"]
    assert evaluate_registered(path, source, manifest_path=registry, mode="off")["status"] == "disabled"


def test_v2_order_and_equivalent_money_units_and_distinct_extra_fact(tmp_path):
    gold = golden(tmp_path, [finance(예산액=2, 예산단위="억원")])
    path, _ = saved(tmp_path, [finance(예산액=200), finance(사업명="다른사업", 예산액=400)])
    result = score_v2(path, gold)
    assert result["metrics"]["cell_accuracy"] == 1
    path, _ = saved(tmp_path, [finance(사업명="다른사업", 예산액=400), finance(예산액=200)])
    assert score_v2(path, gold)["metrics"] == result["metrics"]
    assert result["metrics"]["row_precision"] == 0.5  # 골든 밖 사실을 오답이라고 확정하지 않는다.


def test_v2_does_not_pick_gold_value_from_ambiguous_candidates(tmp_path):
    gold = golden(tmp_path, [finance(예산액=1)])
    path, _ = saved(tmp_path, [finance(예산액=999), finance(예산액=1)])
    result = score_v2(path, gold)
    assert result["counts"]["ambiguous"] == 1 and result["metrics"]["cell_accuracy"] == 0


def test_partial_scope_applied_to_both_sides(tmp_path):
    gold = golden(tmp_path, [finance(예산액=1, 출처페이지=1), finance(사업명="범위밖", 예산액=2, 출처페이지=2)])
    path, _ = saved(tmp_path, [finance(예산액=1, 출처페이지=1), finance(사업명="범위밖", 예산액=999, 출처페이지=2)])
    result = score_v2(path, gold, scope={"kind": "partial", "pages": [1]})
    assert result["counts"]["golden_rows"] == result["counts"]["output_rows"] == 1
    assert result["metrics"]["cell_accuracy"] == 1


def test_invalid_partial_scope_is_not_scored(tmp_path):
    path, _ = saved(tmp_path, [finance()])
    gold = golden(tmp_path, [finance()])
    source, registry = manifest(tmp_path, gold, scope={"kind": "document", "pages": [1]})
    assert "partial" in evaluate_registered(path, source, manifest_path=registry)["reason"]


def test_emission_unit_equivalence_preserves_co2_dimension():
    from utils.benchmark_evaluation import _equivalent
    from utils.evaluation_v1.golden_score_matching import _필드일치
    assert _equivalent("배출량", {"배출량": 1, "단위": "천톤CO2eq"}, {"배출량": 1000, "단위": "tCO2eq"}, _필드일치)
    assert not _equivalent("단위", {"배출량": 1, "단위": "천톤CO2"}, {"배출량": 1000, "단위": "tCO2eq"}, _필드일치)


def test_v2_explicit_project_id_precedes_name_but_not_year_or_id_conflict():
    from utils.benchmark_evaluation import _identity_compatible
    from utils.evaluation_v1.golden_score_contract import 키값
    g = {"관리번호": "1-1", "사업명": "기존 명칭", "연도": 2030}
    o = {"관리번호": "1-1", "사업명": "축약 명칭", "연도": 2030}
    assert _identity_compatible("10_정량감축량", g, o, 키값)
    assert not _identity_compatible("10_정량감축량", g, {**o, "연도": 2031}, 키값)
    assert not _identity_compatible("10_정량감축량", g, {**o, "관리번호": "1-2", "사업명": "기존 명칭"}, 키값)


@pytest.mark.parametrize("unit", ["천톤CO₂eq", "천 톤CO2eq.", "tCO₂eq."])
def test_equivalent_emission_unit_typography_is_not_a_quality_warning(tmp_path, unit):
    path, _ = saved(tmp_path, [], emissions_regional=[{"배출유형": "직접배출", "부문": "건물", "연도": 2030,
        "배출량": 1, "단위": unit}])
    assert "emissions.unit_dimension" not in evaluate_saved_excel(path)["issues_by_rule"]
