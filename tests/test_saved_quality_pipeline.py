"""Supervisor 저장 이후 평가 연결과 추가 호출/업무 셀 불변 검증."""
import json
from pathlib import Path
from unittest.mock import patch

import openpyxl

import config
from agents.supervisor import Supervisor
from utils.integration_audit import document_hash
from tests.test_main_v9_end_to_end import prepare_ocr, settings, external_response


def cells(path):
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return {config.SHEET_KEY_TO_NAME[k]: list(wb[config.SHEET_KEY_TO_NAME[k]].values) for k in config.EXTRACTION_SHEETS}
    finally:
        wb.close()


def test_quality_on_off_preserves_extraction_requests_and_business_cells(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "EVALUATION_MODE", "off")
    snapshots, requests = [], []
    for enabled in (False, True):
        monkeypatch.setattr(config, "QUALITY_REPORT_ENABLED", enabled)
        supervisor = Supervisor()
        with patch("utils.llm_client.call_text", side_effect=external_response) as call:
            result = supervisor.run(source, tmp_path / f"quality_{enabled}.xlsx", include_images=False, max_pipeline_retries=1)
        snapshots.append(cells(result))
        # 최종 1회 보조 LLM 검수의 입력에는 새 측정 결과가 포함된다.
        requests.append(call.call_args_list[:-1])
        assert result.with_name(result.stem + "_quality_report.json").exists() == enabled
        assert supervisor.last_outcome["evaluation_status"] == "disabled"
        assert supervisor.last_outcome["quality"]["status"] == ("saved" if enabled else "disabled")
        if enabled:
            report = json.loads(result.with_name(result.stem + "_quality_report.json").read_text(encoding="utf-8"))
            assert report["metrics"]["storage_agreement"]["value"] == 1
            assert report["llm_review"]["role"] == "diagnostic_only"
            audit = json.loads(result.with_name(result.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
            assert audit["context"]["quality"]["paths"]["json"]
    assert snapshots[0] == snapshots[1] and requests[0] == requests[1]
    assert not (tmp_path / "state").exists()


def test_quality_save_failure_preserves_excel_and_outcome(tmp_path, monkeypatch):
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "QUALITY_REPORT_ENABLED", True)
    monkeypatch.setattr(config, "EVALUATION_MODE", "off")
    with patch("utils.llm_client.call_text", side_effect=external_response), patch(
            "utils.quality_evaluation.persist_quality_report", side_effect=PermissionError("fixture report denied")):
        supervisor = Supervisor()
        result = supervisor.run(source, tmp_path / "report_failed.xlsx", include_images=False, max_pipeline_retries=1)
    outcome = json.loads(result.with_name(result.stem + "_run_outcome.json").read_text(encoding="utf-8"))
    assert result.exists() and outcome["excel_saved"] and outcome["quality"]["status"] == "failed"
    assert outcome["review_status"] == "needs_review" and any("report denied" in w for w in outcome["warnings"])


def test_main_cli_evaluation_arguments_apply_before_config_import(tmp_path):
    import os
    import subprocess
    import sys
    source = prepare_ocr(tmp_path)
    dest = tmp_path / "cli_eval.xlsx"
    wrapper = '''
import builtins, sys
original = builtins.__import__
installed = False
def wrapped(name, *args, **kwargs):
    global installed
    module = original(name, *args, **kwargs)
    if name == 'agents.supervisor' and not installed:
        installed = True
        import utils.llm_client as boundary
        from tests.test_main_v9_end_to_end import external_response
        boundary.call_text = external_response
    return module
builtins.__import__ = wrapped
import main
sys.argv = ['main.py', *sys.argv[1:]]
raise SystemExit(main.main())
'''
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", SELECTIVE_OCR_ENABLED="0", RUN_STATE_ENABLED="0", EXTRACTION_RESUME="0",
               EXTRACTION_OBJECT_BATCHING_ENABLED="0", SEMANTIC_ROUTING_ENABLED="0", SOURCE_COVERAGE_MODE="off")
    completed = subprocess.run([sys.executable, "-c", wrapper, str(source), "--output", str(dest), "--no-images", "--retries", "1",
        "--quality-report", "on", "--evaluate", "--evaluation-manifest", str(tmp_path / "absent.json")],
        cwd=Path(__file__).resolve().parents[1], env=env, encoding="utf-8", errors="replace", text=True, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    outcome = json.loads(dest.with_name(dest.stem + "_run_outcome.json").read_text(encoding="utf-8"))
    assert outcome["evaluation_status"] == "required_unavailable" and outcome["excel_saved"]
    assert "최종 품질 점수: 100" not in completed.stdout


def test_approved_golden_reaches_pipeline_audit_and_outcome(tmp_path, monkeypatch):
    from utils.evaluation_v1.golden_score_contract import 계약헤더, 골든열
    source = prepare_ocr(tmp_path)
    settings(monkeypatch, tmp_path)
    gold = tmp_path / "golden.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "05_배출전망"
    headers = 계약헤더(ws.title) + 골든열
    ws.append(headers)
    record = {"지자체명": "테스트시", "전망범위": "지역전체", "시나리오": "BAU", "부문": "건물", "세부부문": "가정",
              "연도": 2030, "전망값": 987.6, "단위": "천톤CO2eq", "골든_출처유형": "텍스트표", "골든_출처페이지": "1"}
    ws.append([record.get(f) for f in headers])
    wb.save(gold)
    wb.close()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"datasets": [{"id": "pipeline-fixture", "role": "development", "review_status": "approved",
        "locked": True, "source_sha256": document_hash(source), "golden": gold.name, "golden_sha256": document_hash(gold)}]}), encoding="utf-8")
    monkeypatch.setattr(config, "QUALITY_REPORT_ENABLED", True)
    monkeypatch.setattr(config, "EVALUATION_MODE", "auto")
    monkeypatch.setattr(config, "EVALUATION_MANIFEST_PATH", str(registry))
    with patch("utils.llm_client.call_text", side_effect=external_response):
        supervisor = Supervisor()
        path = supervisor.run(source, tmp_path / "automatic.xlsx", include_images=False, max_pipeline_retries=1)
    assert supervisor.last_outcome["evaluation_status"] == "evaluated", supervisor.last_outcome["evaluation"]
    assert supervisor.last_outcome["evaluation"]["metrics"]["cell_accuracy"] == 1
    audit = json.loads(path.with_name(path.stem + "_pipeline_audit.json").read_text(encoding="utf-8"))
    assert audit["model_accuracy_evaluated"] and audit["context"]["evaluation"]["sha256"]
