"""Frozen-response recovery through real cleaning, validation and Excel IO."""
from copy import deepcopy
import json
from itertools import permutations
from pathlib import Path
import subprocess

import fitz
import pytest

import config
from agents.excel_agent import ExcelAgent
from agents.extractor_agent import _SHEET_CONFIGS, _build_page_text
from agents.guideline_agent import GuidelineAgent
from agents.organizer_agent import OrganizerAgent
from utils.run_state import RunState, sha256_json
from utils.saved_extraction_recovery import (_hash_text, _pages, batch_request, execute, prepare,
                                            read_json, sha256_file, write_json, _read_workbook)


def target(**values):
    row = {"지자체명": "테스트시", "목표수준": "부문", "목표범위": "관리권한+추가감축",
           "부문": "건물", "기준연도": 2018, "목표연도": 2030, "목표배출량": 4420,
           "출처페이지": 1, "근거ID": "ev-a", "객체ID": "a"}
    row.update(values)
    return row


def test_target_scope_year_unit_and_conflict_loss_fixed():
    source = [target(), target(목표범위="관리권한", 목표배출량=6005),
              target(기준연도=2020), target(목표범위=None), target(단위="tCO2eq"),
              target(목표배출량=961)]
    for ordered in (source, list(reversed(source))):
        result = OrganizerAgent().organize({"municipality_name": "테스트시", "reduction_targets": ordered})
        rows = result["reduction_targets"]
        assert len(rows) == 6
        assert any(r["목표범위"] == "관리권한" and r["목표배출량"] == 6005 for r in rows)
        assert sum(bool(row.get("_merge_conflict")) for row in rows) == 2
        assert any(r["항목"] == "reduction_target_value_conflict_retained" for r in result["validation_report"])
    assert source[0]["목표배출량"] == 4420
    assert not result["_semantic_contract_quarantine"]
    twice = OrganizerAgent().organize(result)
    assert twice["reduction_targets"] == result["reduction_targets"]


def test_equal_target_fact_merges_evidence_aliases_and_is_idempotent():
    first = target(_extraction_sources=[{"evidence_id": "ev-a"}])
    second = target(출처페이지=2, 근거ID="ev-b", 객체ID="b", _extraction_sources=[{"evidence_id": "ev-b"}])
    original = deepcopy([first, second])
    results = [OrganizerAgent().organize({"reduction_targets": list(order)})["reduction_targets"]
               for order in permutations([first, second])]
    for rows in results:
        assert len(rows) == 1
        assert rows[0]["source_evidence_ids"] == ["ev-a", "ev-b"]
        assert rows[0]["source_object_ids"] == ["a", "b"]
        assert len(json.loads(rows[0]["출처계보"])) == 2
        assert len(OrganizerAgent().organize({"reduction_targets": rows + rows})["reduction_targets"]) == 1
    assert [first, second] == original


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    from utils import llm_client
    monkeypatch.setattr(llm_client, "call_text", lambda *a, **kw: pytest.fail("unexpected model call"))
    pdf = tmp_path / "input.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), "Target 2018 2030 4420 6005 961")
        doc.new_page().insert_text((72, 72), "City gas 2024 4997")
        doc.save(pdf)
    excel = tmp_path / "baseline.xlsx"
    data = {"document_meta": [{"지자체명": "테스트시", "계획명": "Fixture"}],
            "reduction_targets": [target()],
            "visual_inventory": [{"지자체명": "테스트시", "캡션": "frozen Vision"}]}
    ExcelAgent().write(data, excel)
    book = _read_workbook(excel)
    state = tmp_path / "state"
    state.mkdir()
    prompts = GuidelineAgent().get_all_prompts()
    records, logged = {}, []
    run = RunState.__new__(RunState)
    run.run_id = "fixture-run"
    for key, numbers, status, rows in (
        ("reduction_targets", [1], "ok", [target(), target(목표범위="관리권한", 목표배출량=6005)]),
        ("annual_implementation", [2], "parse_fail", []),
    ):
        text = _build_page_text(_pages(pdf, numbers).pages)
        request = json.dumps({"prompt": prompts.get(key, ""), "source": "text_chunk",
            "municipality": "테스트시", "schema_prompt": _SHEET_CONFIGS[key]["prompt"]}, ensure_ascii=False, sort_keys=True)
        bid = run.batch_id(kind="sheet", sheet_keys=[key], page_nums=numbers,
                          batch_text=text, request_fingerprint=_hash_text(key + text + request))
        records[bid] = {"batch_id": bid, "kind": "sheet", "sheet_keys": [key], "page_nums": numbers,
                        "status": status, "result": rows}
        logged.append({"batch_id": bid, "sheet_key": key, "page_nums": numbers, "status": status,
                       "object_id": "batch-a", "evidence_id": "ev-batch-a", "engine": "native",
                       "source": "text_chunk", "input_sha256": _hash_text(text)})
    (state / "ledger.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records.values()), encoding="utf-8")
    manifest = {"run_id": run.run_id, "input_path": str(pdf), "input_sha256": sha256_file(pdf),
        "output_sha256": sha256_file(excel), "state_directory": str(state), "prompt_sha256": sha256_json(prompts),
        "config": {}, "execution_info": {"text_backend": "codex", "text_model": "fixture-model"}}
    write_json(state / "run_manifest.json", manifest)
    write_json(excel.with_name(excel.stem + "_run_manifest.json"), manifest)
    write_json(excel.with_name(excel.stem + "_pipeline_audit.json"), {
        "context": {"input_sha256": sha256_file(pdf)}, "ledgers": {"extraction_records": logged, "document_objects": []}})
    return excel, pdf, book


def annual_response(**overrides):
    row = {"지자체명": "테스트시", "사업명": "City gas", "연도": 2024,
           "목표물량": 4997, "목표단위": "가구", "출처페이지": 2}
    row.update(overrides)
    return json.dumps({"annual_implementation": [row]}, ensure_ascii=False)


def test_fixed_replay_then_only_failed_call_real_excel_and_repeat(saved_run, tmp_path):
    excel, pdf, baseline = saved_run
    prepared = prepare(excel)
    original = dict(prepared["plan"]["input_hashes"])
    output = tmp_path / "recovery"
    first = execute(prepared, output)
    assert first["model_requests_this_execution"] == first["vision_requests"] == 0
    assert first["decision_counts"] == {"already_saved": 1, "saved_verified": 1}
    assert first["batches"][0]["status"] == "deferred"
    calls = []
    def reader(prompt, **kw):
        calls.append(prompt)
        assert "City gas" in prompt and "annual_implementation" in prompt
        return annual_response()
    second = execute(prepared, output, recover_failed=True, reader=reader)
    assert len(calls) == 1 and second["successful_batches_frozen"] == 1
    assert second["decision_counts"]["saved_verified"] == 2
    assert second["exit_code"] == 0 and second["baseline_cells_preserved"]
    actual = _read_workbook(second["excel_path"])
    for name, sheet in baseline.items():
        assert actual[name]["values"][:len(sheet["values"])] == sheet["values"]
    targets = actual[config.SHEET_KEY_TO_NAME["reduction_targets"]]
    assert any(row[targets["headers"].index("목표배출량")] == 6005 for row in targets["values"])
    annual = actual[config.SHEET_KEY_TO_NAME["annual_implementation"]]
    assert annual["values"][0][annual["headers"].index("목표물량")] == 4997
    third = execute(prepared, output, recover_failed=True, reader=lambda *a, **kw: pytest.fail("no repeat"))
    assert third["model_requests_this_execution"] == 0 and third["reused_recovery_responses"] == 1
    assert _read_workbook(third["excel_path"]) == actual
    assert all(sha256_file(p) == value for p, value in original.items())


def test_unreadable_or_bad_response_not_retried_successes_preserved(saved_run, tmp_path):
    prepared = prepare(saved_run[0])
    out = tmp_path / "bad"
    result = execute(prepared, out, recover_failed=True, reader=lambda *a, **kw: '{"financial_plan":[]}')
    assert result["batches"][0]["status"] == "failed" and result["exit_code"] == 2
    assert result["decision_counts"]["saved_verified"] == 1
    repeated = execute(prepared, out, recover_failed=True, reader=lambda *a, **kw: pytest.fail("must reuse invalid reply"))
    assert repeated["model_requests_this_execution"] == 0 and repeated["reused_recovery_responses"] == 1


def test_source_absent_value_held(saved_run, tmp_path):
    result = execute(prepare(saved_run[0]), tmp_path / "absent", recover_failed=True,
                     reader=lambda *a, **kw: annual_response(목표물량=9999))
    assert result["decision_counts"]["held"] == 1
    assert not _read_workbook(result["excel_path"])[config.SHEET_KEY_TO_NAME["annual_implementation"]]["values"]


def test_one_bad_numeric_field_does_not_hide_confirmed_target_cell(saved_run, tmp_path):
    excel, _, _ = saved_run
    prepared = prepare(excel)
    ledger = Path(prepared["manifest"]["state_directory"]) / "ledger.jsonl"
    records = list(prepared["records"].values())
    records[0]["result"][1]["목표감축량"] = 99999
    ledger.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records), encoding="utf-8")
    result = execute(prepare(excel), tmp_path / "partial-field")
    decision = next(row for row in result["decisions"] if row["status"] == "saved_verified")
    assert decision["row"]["목표배출량"] == 6005
    assert decision["row"]["목표감축량"] is None
    assert decision["original_row"]["목표감축량"] == 99999
    assert decision["held_fields"] == {"목표감축량": 99999}
    assert result["exit_code"] == 2


def test_existing_entity_conflict_held_not_overwritten(saved_run):
    from utils.saved_extraction_recovery import _merge_new
    baseline = {"annual_implementation": [{"지자체명": "테스트시", "사업명": "City gas", "연도": 2024,
                                            "목표단위": "가구", "목표물량": 10}]}
    original = deepcopy(baseline)
    decisions = []
    _merge_new(baseline, "annual_implementation", json.loads(annual_response())["annual_implementation"],
               _pages(saved_run[1], [2]), decisions)
    assert baseline == original
    assert decisions[0]["reason"] == "existing_entity_payload_conflict"


def test_blank_plan_placeholders_not_promoted_as_recovered_facts(saved_run, tmp_path):
    payload = json.loads(annual_response())
    payload["annual_implementation"].append({"사업명": "City gas", "기간시작": 2025,
        "기간종료": 2025, "목표물량": None, "연간계획": "-", "출처페이지": 2})
    result = execute(prepare(saved_run[0]), tmp_path / "placeholders", recover_failed=True,
                     reader=lambda *a, **kw: json.dumps(payload))
    assert result["decision_counts"]["saved_verified"] == 2
    assert any(d.get("reason") == "no_measured_or_qualitative_plan" for d in result["decisions"])
    assert len(_read_workbook(result["excel_path"])[config.SHEET_KEY_TO_NAME["annual_implementation"]]["values"]) == 1


def test_timeout_attempt_and_interruption_not_recalled(saved_run, tmp_path):
    prepared = prepare(saved_run[0])
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired("fixture", 1)
    out = tmp_path / "timeout"
    execute(prepared, out, recover_failed=True, reader=timeout)
    result = execute(prepared, out, recover_failed=True, reader=lambda *a, **kw: pytest.fail("no repeat"))
    assert result["model_requests_this_execution"] == 0 and result["batches"][0]["status"] == "failed"
    out2 = tmp_path / "interrupted"
    def interrupt(*a, **kw):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(prepared, out2, recover_failed=True, reader=interrupt)
    result = execute(prepared, out2, recover_failed=True, reader=lambda *a, **kw: pytest.fail("no repeat"))
    assert result["model_requests_this_execution"] == 0 and not (out2 / ".run.lock").exists()


def test_modified_inputs_prompts_or_batch_binding_prevent_call(saved_run, tmp_path):
    prepared = prepare(saved_run[0])
    bid = prepared["plan"]["failed_batch_ids"][0]
    content = _pages(saved_run[1], [2])
    prepared["logged"][bid]["input_sha256"] = "wrong"
    with pytest.raises(ValueError, match="텍스트 해시"):
        batch_request(prepared, bid, content)
    with pytest.raises(ValueError, match="보조 지침"):
        prepare(saved_run[0], extraction_prompts={})
    prepared = prepare(saved_run[0])
    saved_run[1].write_bytes(saved_run[1].read_bytes() + b" ")
    with pytest.raises(ValueError, match="기준 파일"):
        execute(prepared, tmp_path / "tampered", recover_failed=True, reader=lambda *a, **kw: pytest.fail("no call"))
