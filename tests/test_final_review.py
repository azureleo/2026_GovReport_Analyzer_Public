"""최종 검토가 평가 불가와 실제 규칙 위반을 혼합하거나 정답률을 만들지 않는지 검사."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

import config
from agents.supervisor import Supervisor
from utils.final_review import build_review_context, normalize_review, review_log_lines, review_prompt
from tests.test_main_v9_end_to_end import prepare_ocr, settings, external_response


def quality(issues=None, comparable=0):
    return {"business_rows": 3, "issues": issues or [],
            "source_coverage": {"status": "saved", "comparable_facts": comparable},
            "metrics": {"object_binding": {"available": True, "numerator": 0, "denominator": 3, "value": 0},
                        "identity_check_coverage": {"available": True, "numerator": 1, "denominator": 3, "value": 0.333333}}}


OFF = {"status": "disabled", "reason": "골든 평가 비활성", "metrics": {"cell_accuracy": None}}


@pytest.mark.parametrize("old_grade", ["우수", "보통", "미흡"])
def test_legacy_grade_and_unverified_accuracy_never_become_final_status(old_grade):
    ctx = build_review_context(quality(), OFF)
    raw = {"quality_level": old_grade, "assessment": "사실 정확도는 0%이고 모두 오답"}
    normalized = normalize_review(raw, ctx)
    log = "\n".join(review_log_lines(normalized, ctx))
    assert normalized["rule_check"]["status"] == "no_findings"
    assert normalized["verification_scope"]["status"] == "insufficient"
    assert normalized["fact_accuracy"]["status"] == "disabled"
    assert "골든 평가 OFF" in log
    assert "사실 정확도는 0%" not in log and "검수 결과:" not in log
    assert normalized["raw_response"] == raw


def test_provenance_unavailable_and_source_text_miss_are_verification_limits():
    report = quality([
        {"rule_id": "provenance.object_unassessed", "certainty": "unassessed"},
        {"rule_id": "source.context_not_located", "certainty": "suspected"}])
    ctx = build_review_context(report, OFF)
    assert ctx["rule_check"]["status"] == "no_findings"
    assert ctx["rule_check"]["confirmed"] == ctx["rule_check"]["suspected"] == 0
    assert ctx["verification_scope"]["diagnostic_counts"] == {"unassessed": 1, "suspected": 1}


def test_actual_rule_violation_remains_even_if_llm_claims_excellent():
    ctx = build_review_context(quality([{"rule_id": "budget.non_money_unit", "certainty": "confirmed"}]), OFF)
    review = normalize_review({"quality_level": "우수", "assessment": "모두 정확"}, ctx)
    assert review["rule_check"]["status"] == "needs_review" and review["rule_check"]["confirmed"] == 1
    assert "모두 정확" not in "\n".join(review_log_lines(review, ctx))


def test_confirmed_invalid_provenance_is_not_hidden_as_verification_limit():
    ctx = build_review_context(quality([{"rule_id": "provenance.invalid_page", "certainty": "confirmed"}]), OFF)
    assert ctx["rule_check"]["status"] == "needs_review"
    assert ctx["rule_check"]["confirmed"] == 1
    assert ctx["rule_check"]["available_rule_ids"] == {"provenance.invalid_page": 1}


@pytest.mark.parametrize("status,expected", [("disabled", "disabled"), ("unassessed", "unassessed"),
                                           ("failed", "failed"), ("required_unavailable", "required_unavailable")])
def test_no_golden_status_does_not_lower_data_quality(status, expected):
    ctx = build_review_context(quality(), {"status": status, "reason": "fixture"})
    assert ctx["rule_check"]["status"] == "no_findings"
    assert ctx["fact_accuracy"]["status"] == expected
    assert not ctx["fact_accuracy"]["metrics"]


@pytest.mark.parametrize("n,status", [(None, "unassessed"), (0, "insufficient"), (14, "partial")])
def test_comparable_scope_is_never_full_accuracy(n, status):
    ctx = build_review_context(quality(comparable=n), OFF)
    assert ctx["verification_scope"]["status"] == status
    assert ctx["fact_accuracy"]["metrics"]["cell_accuracy"] is None


@pytest.mark.parametrize("state,label", [("disabled", "규칙 검사 미실행"), ("failed", "규칙 검사 실패")])
def test_missing_quality_results_are_not_clean_passes(state, label):
    ctx = build_review_context(None, OFF, {"status": state})
    assert ctx["rule_check"]["label"] == label
    assert ctx["verification_scope"]["status"] == "unassessed"


def test_saved_report_without_detail_is_not_treated_as_no_findings():
    ctx = build_review_context({"business_rows": 3, "issue_counts": {"confirmed": 8}}, OFF)
    assert ctx["rule_check"]["status"] == "unassessed"


def test_benchmark_scores_remain_separate_and_unchanged():
    evaluated = {"status": "evaluated", "metrics": {"cell_accuracy": 0.338498}, "contract_version": "fixture-v2"}
    ctx = build_review_context(quality(), evaluated)
    assert ctx["fact_accuracy"]["metrics"] == evaluated["metrics"]
    assert ctx["rule_check"]["status"] == "no_findings"


def test_new_model_findings_are_filtered_and_do_not_own_status():
    ctx = build_review_context(quality([{"rule_id": "budget.non_money_unit", "certainty": "confirmed"}]), OFF)
    raw = {"data_findings": [{"rule_id": "invented", "note": "wrong"}, {"rule_id": [], "note": "wrong"},
                             {"rule_id": "budget.non_money_unit", "note": "예산 단위 확인"}],
           "verification_notes": [{"topic": "object_binding", "note": "추적 근거 확인"}, {"topic": [], "note": "bad"}],
           "next_checks": ["source_coverage", "budget.non_money_unit", "invented", {}, "source_coverage"]}
    before = deepcopy(raw)
    review = normalize_review(raw, ctx)
    assert len(review["data_findings"]) == len(review["verification_notes"]) == 1
    assert review["next_checks"] == ["source_coverage", "budget.non_money_unit"]
    assert review["rule_check"]["status"] == "needs_review" and raw == before


def test_llm_failure_keeps_measured_diagnostics_and_unknown_accuracy():
    ctx = build_review_context(quality([{"rule_id": "type.numeric", "certainty": "confirmed"}]), OFF)
    review = normalize_review(None, ctx, error="fixture timeout")
    assert review["llm_status"] == "failed" and review["rule_check"]["status"] == "needs_review"
    assert "규칙 진단 유지" in "\n".join(review_log_lines(review, ctx))
    assert normalize_review(None, ctx, error="")["llm_status"] == "failed"


def test_prompt_requests_referenced_notes_instead_of_overall_grade():
    prompt = review_prompt("강원특별자치도", {"financial_plan": 3}, build_review_context(quality(), OFF))
    assert '"quality_level"' not in prompt
    assert '"data_findings"' in prompt and '"verification_notes"' in prompt
    assert "골든 OFF/미등록은 사용 조건" in prompt


def test_actual_pipeline_golden_off_separates_final_review_and_saves_reports(tmp_path, monkeypatch, capsys):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EVALUATION_MODE", "off")
    monkeypatch.setattr(config, "QUALITY_REPORT_ENABLED", True)
    supervisor = Supervisor()
    with patch("utils.llm_client.call_text", side_effect=external_response) as call, patch.object(
            Supervisor, "_llm_quality_review", return_value={"quality_level": "미흡", "assessment": "정확도 0%"}) as final_call:
        path = supervisor.run(source, tmp_path / "new_review.xlsx", include_images=False, max_pipeline_retries=1)
    assert final_call.call_count == 1 and call.call_count > 0
    log = capsys.readouterr().out
    assert "검수 결과: 미흡" not in log and "정확도 0%" not in log
    assert "미평가 (골든 평가 OFF" in log
    outcome = json.loads(path.with_name(path.stem + "_run_outcome.json").read_text(encoding="utf-8"))
    report = json.loads(path.with_name(path.stem + "_quality_report.json").read_text(encoding="utf-8"))
    audit = json.loads(path.with_name(path.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    result = report["llm_review"]["result"]
    assert outcome["final_review"]["fact_accuracy"]["status"] == "disabled"
    assert audit["context"]["llm_review"]["result"] == result
    assert result["raw_response"]["quality_level"] == "미흡" and "quality_level" not in result
    assert report["metrics"]["storage_agreement"]["value"] == 1
    text = path.with_name(path.stem + "_quality_report.md").read_text(encoding="utf-8")
    assert "최종 검토 요약" in text and "검수 결과: 미흡" not in text
    assert "미평가 (골든 평가 OFF)" in text


def test_actual_pipeline_llm_failure_still_saves_excel_and_measured_review(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EVALUATION_MODE", "off")
    monkeypatch.setattr(config, "QUALITY_REPORT_ENABLED", True)
    with patch("utils.llm_client.call_text", side_effect=external_response), patch.object(
            Supervisor, "_llm_quality_review", side_effect=TimeoutError("fixture opinion timeout")):
        supervisor = Supervisor()
        path = supervisor.run(source, tmp_path / "opinion_failure.xlsx", include_images=False, max_pipeline_retries=1)
    assert path.exists() and supervisor.last_outcome["excel_saved"]
    assert supervisor.last_outcome["final_review"]["llm_status"] == "failed"
    assert supervisor.last_outcome["final_review"]["fact_accuracy"]["status"] == "disabled"
    assert supervisor.last_outcome["quality"]["status"] == "saved"
