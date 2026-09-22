"""Offline regression tests for successful-response provenance and reuse."""
from argparse import Namespace
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from utils.vision_recovery_runtime import observations_from_response, settings, offline_guard
from utils.vision_recovery_workflow import (
    file_hash, read_json, write_json, record_response_success, merge_fresh,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("response_reuse_cli", ROOT / "scripts/run_vision_recovery.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def task(page=1):
    return {"id": f"Luna:p{page}", "model": "Luna", "page": page,
            "object_ids": [f"obj-{page}"], "evidence_ids": [f"ev-{page}"],
            "bbox": [0, 0, 100, 100]}


def response():
    return {"type": "chart_table", "target_sheet": "emissions_forecast",
            "title": "BAU", "chart_type": "표", "confidence": "high",
            "page_number": 1, "source_object_ids": ["obj-1"],
            "source_evidence_ids": ["ev-1"], "source_bbox": [0, 0, 100, 100],
            "table": [{"연도": 2030, "항목": "건물", "값": 100, "단위": "천tCO2eq",
                       "fields": {"시나리오": "BAU", "부문": "건물", "값근거": "table_cell"}}]}


def raw():
    return {"municipality_name": "서울특별시", "chart_observations": [],
            "document_objects": [
                {"object_id": f"obj-{p}", "page_number": p, "object_type": "image",
                 "bbox": [0, 0, 100, 100],
                 "metadata": {"evidence_id": f"ev-{p}", "final_status": "needs_review"}}
                for p in (1, 2)],
            "object_triage": [{"object_id": f"obj-{p}", "evidence_id": f"ev-{p}",
                               "page_number": p, "final_status": "needs_review"}
                              for p in (1, 2)]}


def test_response_rebuild_is_offline_copy_only_and_exact(monkeypatch):
    from agents.image_agent import ImageAgent
    monkeypatch.setattr(ImageAgent, "_chart_to_table", lambda *a, **kw: pytest.fail("No reading allowed"))
    parsed = response()
    before = deepcopy(parsed)
    with offline_guard():
        rows = observations_from_response(parsed, task())
    assert parsed == before
    assert rows[0]["근거매칭상태"] == "exact"
    assert "extracted 아님" not in rows[0]["병합차단사유"]
    assert rows[0]["원문값"] == 100


@pytest.mark.parametrize("field,value", [
    ("page_number", 2), ("page_number", True), ("page_number", 1.0),
    ("source_evidence_ids", ["wrong"]),
    ("source_object_ids", ["wrong"]), ("source_bbox", [0, 0, 20, 20]),
    ("table", []), ("table", ["not a row"]), ("type", "error"),
])
def test_response_scope_and_structure_fail_closed(field, value):
    parsed = response()
    parsed[field] = value
    with pytest.raises(ValueError):
        observations_from_response(parsed, task())


@pytest.mark.parametrize("field,value", [("page", True), ("page", 0),
    ("object_ids", ["obj-1", "obj-2"]), ("evidence_ids", []), ("evidence_ids", [""])])
def test_ambiguous_task_not_promoted(field, value):
    bad = task()
    bad[field] = value
    with pytest.raises(ValueError):
        observations_from_response(response(), bad)


def test_success_is_not_confidence_or_numeric_approval():
    parsed = response()
    parsed["confidence"] = "low"
    parsed["table"][0]["값"] = None
    rows = observations_from_response(parsed, task())
    assert rows[0]["근거매칭상태"] == "exact"
    assert rows[0]["병합상태"] not in {"candidate", "merged"}
    assert rows[0]["병합차단사유"]


def test_success_updates_only_exact_object_and_not_other_failures():
    source = raw()
    untouched = deepcopy(source["document_objects"][1])
    event = record_response_success(source, task())
    assert source["document_objects"][0]["metadata"]["final_status"] == "extracted"
    assert source["object_triage"][0]["final_status"] == "extracted"
    assert source["document_objects"][1] == untouched
    assert source["object_triage"][1]["final_status"] == "needs_review"
    assert event["before"]["final_status"] == "needs_review"


def test_source_object_mismatch_cannot_be_promoted():
    source = raw()
    before = deepcopy(source)
    bad = task()
    bad["page"] = 2
    with pytest.raises(ValueError, match="original source object"):
        record_response_success(source, bad)
    assert source == before


def test_metadata_and_value_conflicts_are_both_held_not_overwritten():
    old = observations_from_response(response(), task())
    changed_meta = deepcopy(old[0])
    changed_meta["판독필드"]["설명"] = "additional explanation"
    changed_value = deepcopy(old[0])
    changed_value["원문값"] = 101
    merged, audit = merge_fresh(old, [changed_meta, changed_value], 1, ["ev-1"])
    assert merged == old
    assert [e["status"] for e in audit] == ["conflict_held", "conflict_held"]
    assert [e["difference_kind"] for e in audit] == ["metadata_or_unit_difference", "value_difference"]


@pytest.fixture
def saved_run(tmp_path):
    parent = tmp_path / "saved"
    (parent / "retry").mkdir(parents=True)
    (parent / "Luna").mkdir()
    # Deliberately not a PDF: reuse must never render/read it as a document.
    source = tmp_path / "not_rendered.pdf"
    source.write_bytes(b"offline source identity only")
    config = {"version": 1, "source": str(source), "source_sha256": file_hash(source),
              "scorer": "unused.py", "budget": {"max_calls": 2, "call_timeout_seconds": 120, "total_seconds": 600},
              "models": [{"name": "Luna", "provider": "codex", "model": "unused", "command": "unused"}]}
    write_json(parent / "experiment.json", config)
    write_json(parent / "retry_queue.json", [task(1), task(2)])
    write_json(parent / "Luna/input_snapshot.json", raw())
    write_json(parent / "retry/manifest.json", {
        "version": cli.VERSION, "mode": "retry", "status": "review_required",
        "parent": str(parent), "source_sha256": config["source_sha256"], "calls_attempted": 2})
    write_json(parent / "retry/call_journal.json", [
        {"task": task(1)["id"], "status": "completed"},
        {"task": task(2)["id"], "status": "failed", "error": "timeout"}])
    write_json(parent / "retry/call_001.json", {
        "task": task(), "detail": {"parsed_response": response()},
        "observations": [{"병합차단사유": "old stale failure: NEVER consume this field"}]})
    frozen = {str(p.resolve()): file_hash(p) for p in [
        parent / "experiment.json", parent / "retry_queue.json", parent / "Luna/input_snapshot.json"]}
    write_json(parent / "manifest.json", {
        "version": cli.VERSION, "retry_allowed": True, "input_hashes": {str(source): file_hash(source)},
        "artifact_hashes": frozen, "code_hashes": {"historical.py": "old-version"},
        "effective_config": cli.effective_config()})
    return parent, config


def test_loader_uses_only_completed_journal_entries(saved_run):
    parent, config = saved_run
    completed, skipped = cli.saved_responses(parent, config)
    assert len(completed) == len(skipped) == 1
    assert completed[0][0] == task(1)
    assert skipped[0]["task"] == task(2)["id"]


@pytest.mark.parametrize("damage", ["task", "orphan", "missing", "journal", "running", "count"])
def test_loader_rejects_inconsistent_saved_calls(saved_run, damage):
    parent, config = saved_run
    if damage == "task":
        payload = read_json(parent / "retry/call_001.json")
        payload["task"]["page"] = 2
        write_json(parent / "retry/call_001.json", payload)
    elif damage == "orphan":
        write_json(parent / "retry/call_002.json", {})
    elif damage == "missing":
        (parent / "retry/call_001.json").unlink()
    elif damage == "journal":
        write_json(parent / "retry/call_journal.json", [])
    else:
        m = read_json(parent / "retry/manifest.json")
        m["status" if damage == "running" else "calls_attempted"] = "running" if damage == "running" else 99
        write_json(parent / "retry/manifest.json", m)
    with pytest.raises((ValueError, FileNotFoundError)):
        cli.saved_responses(parent, config)


def test_reuse_refuses_writes_inside_source_experiment(saved_run):
    parent, _ = saved_run
    with pytest.raises(ValueError, match="outside"):
        cli.reuse(Namespace(replay_dir=parent, output_dir=parent / "changed"))
    assert not (parent / "changed").exists()


def test_reuse_detects_original_artifact_changes(saved_run, tmp_path):
    parent, _ = saved_run
    write_json(parent / "Luna/input_snapshot.json", {})
    with pytest.raises(ValueError, match="changed"):
        cli.reuse(Namespace(replay_dir=parent, output_dir=tmp_path / "out"))


def test_reuse_reaches_real_excel_without_model_or_pdf_calls(saved_run, tmp_path, monkeypatch):
    import utils.vision_recovery_runtime as runtime
    from utils import llm_client
    from agents.image_agent import ImageAgent
    parent, _ = saved_run
    def prohibited(*args, **kwargs):
        pytest.fail("Offline reuse must not read a PDF or invoke a model")
    monkeypatch.setattr(runtime, "read_one_object", prohibited)
    monkeypatch.setattr(llm_client, "call_vision", prohibited)
    monkeypatch.setattr(ImageAgent, "_chart_to_table", prohibited)
    monkeypatch.setattr(runtime, "load_scorer", lambda *a: None)
    score = {"comparisons": [], "gold_count": 0, "value_correct": 0,
             "complete": 0, "final_confirmed": 0, "present": 0}
    monkeypatch.setattr(runtime, "score_reading", lambda *a: deepcopy(score))
    (parent / "retry/Luna").mkdir()
    write_json(parent / "retry/Luna/evaluation.json", score)
    write_json(parent / "retry/Luna/cell_validation.json", {"passed": True, "trace": []})
    source_hashes = {p: file_hash(p) for p in parent.rglob("*") if p.is_file()}
    out = tmp_path / "reused"
    with settings(READING_PIPELINE_ENABLED=True, REFERENCE_ENRICHMENT_ENABLED=False,
                  VISUAL_MERGE_LABELED_ENABLED=True, VISUAL_EVIDENCE_MERGE_ENABLED=True,
                  PROVENANCE_ENABLED=True, DATA_STATUS_ENABLED=True, CODEBOOK_SHEET_ENABLED=False):
        frozen = read_json(parent / "manifest.json")
        frozen["effective_config"] = cli.effective_config()
        write_json(parent / "manifest.json", frozen)
        source_hashes[parent / "manifest.json"] = file_hash(parent / "manifest.json")
        assert cli.reuse(Namespace(replay_dir=parent, output_dir=out)) == 2  # failed call remains held
    check = read_json(out / "Luna/cell_validation.json")
    assert check["passed"] and check["deterministic"]
    assert len(check["trace"]) == 1  # Not merely a successful empty workbook!
    assert check["trace"][0]["sheet_key"] == "emissions_forecast"
    assert (out / "Luna/result.xlsx").is_file()
    new = read_json(out / "Luna_combined_snapshot.json")
    assert new["document_objects"][0]["metadata"]["final_status"] == "extracted"
    assert new["document_objects"][1]["metadata"]["final_status"] == "needs_review"
    assert "old stale failure" not in str(new)
    manifest = read_json(out / "manifest.json")
    assert manifest["llm_calls"] == 0 and manifest["inputs_unchanged"]
    assert len(manifest["skipped_calls"]) == 1 and manifest["code_changes"]
    assert all(file_hash(p) == sha for p, sha in source_hashes.items())
    with pytest.raises(FileExistsError):
        cli.new_directory(out)
