from argparse import Namespace
from copy import deepcopy
import importlib.util
from pathlib import Path
import socket
import subprocess

import pytest

from utils.vision_recovery_workflow import (
    build_retry_queue, merge_fresh, scoped_region, prepare_bindings,
    file_hash, write_json, read_json, validate_budget, verify_saved_workbook,
)
from utils.vision_recovery_runtime import offline_guard, settings

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("recovery_cli", ROOT / "scripts/run_vision_recovery.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def observation(**extra):
    return {"페이지": 1, "항목": "건물", "연도": 2030, "대상시트": "emissions_forecast",
            "원문값": 100, "값": 100, "원문단위": "천tCO2eq", "단위": "천tCO2eq",
            "근거ID목록": ["ev-1"], "판독필드": {"시나리오": "BAU", "부문": "건물"}, **extra}


def raw():
    return {"municipality_name": "알 수 없음", "chart_observations": [observation()],
            "document_objects": [{"object_id": "obj-1", "page_number": 1,
                                  "object_type": "image", "bbox": [0, 0, 100, 100],
                                  "metadata": {"evidence_id": "ev-1"}}]}


def context(region="서울시", **extra):
    return {"_reading": {"객체문맥": {"자료지역": region, "지역근거": "서울시 표 제목",
                                    "적용범위": "표 전체"}, **extra}}


def test_scoped_region_requires_evidence_and_scope():
    assert scoped_region(context(), "알 수 없음") == "서울특별시"
    assert scoped_region({}, "알 수 없음") == "알 수 없음"
    f = context()
    del f["_reading"]["객체문맥"]["적용범위"]
    assert scoped_region(f, "알 수 없음") == "알 수 없음"


def test_scoped_region_rejects_conflicting_or_reference_context():
    assert scoped_region(context(**{"문맥 구분": {"자료지역": "경기도"}}), "알 수 없음") == "알 수 없음"
    assert scoped_region(context(**{"문맥 구분": {"자료구분": "전국 참고자료"}}), "알 수 없음") == "알 수 없음"
    assert scoped_region(context(), "경기도") == "경기도"


def test_bindings_are_copy_only_and_do_not_invent_project():
    source = raw()
    source["chart_observations"] = [observation(대상시트="mitigation_projects", 항목="60% 미만",
                                               범례목록=["60% 미만"], 판독필드={})]
    before = deepcopy(source)
    fixed, events = prepare_bindings(source)
    assert source == before
    assert events[0]["action"] == "non_project_held"
    assert fixed["chart_observations"][0]["자동병합정책"] == "block_structured_visual"


def test_explicit_project_not_suppressed():
    source = raw()
    source["chart_observations"][0].update(대상시트="mitigation_projects", 범례목록=["건물"], 판독필드={"관리번호": "B1"})
    _, events = prepare_bindings(source)
    assert not events


def test_forecast_subsector_is_not_parent_wildcard_in_production_or_recovery():
    from agents.organizer_agent import _VISUAL_MERGE_KEY_FIELDS, _visual_keys_match
    keys = _VISUAL_MERGE_KEY_FIELDS["emissions_forecast"]
    parent = {"지자체명": "서울특별시", "시나리오": "BAU", "부문": "건물", "연도": 2030}
    child = {**parent, "세부부문": "가정"}
    with settings(VISION_RECOVERY_BINDINGS_ENABLED=False):
        assert not _visual_keys_match(parent, child, keys)
    with settings(VISION_RECOVERY_BINDINGS_ENABLED=True):
        assert not _visual_keys_match(parent, child, keys)
        assert _visual_keys_match(child, dict(child), keys)


def test_retry_queue_excludes_backend_only_holds_and_answers():
    source = raw()
    facts = [{"model": "Luna", "id": "G1", "page": 1, "pred_id": "L001",
              "present": True, "complete": True, "final_ok": False},
             {"model": "Luna", "id": "G2", "page": 1, "pred_id": "",
              "present": False, "complete": False, "value": 99999}]
    queue, held = build_retry_queue("Luna", source, facts)
    assert len(queue) == 1 and not held
    assert queue[0]["fact_ids"] == ["G2"]
    assert "99999" not in str(queue)


def test_text_proxy_is_not_second_vision_object():
    source = raw()
    source["document_objects"].append({"object_id": "text-1", "page_number": 1,
                                       "object_type": "text", "metadata": {"evidence_id": "ev-text"}})
    queue, held = build_retry_queue("Astra", source, [{"model": "Astra", "id": "G", "page": 1}])
    assert len(queue) == 1 and not held


def test_multiple_real_objects_are_not_given_one_objects_evidence():
    source = raw()
    source["document_objects"].append({"object_id": "image-2", "page_number": 1,
                                       "object_type": "image", "metadata": {"evidence_id": "ev-2"}})
    queue, held = build_retry_queue("Astra", source, [{"model": "Astra", "id": "G", "page": 1}])
    assert not queue and held[0]["reason"] == "full_page_retry_requires_one_physical_object"


def test_merge_never_overwrites_conflicting_reading():
    old = [observation()]
    incoming = [observation(값=101, 원문값=101), observation(연도=2031)]
    result, audit = merge_fresh(old, incoming, 1, ["ev-1"])
    assert result[0] == old[0] and len(result) == 2
    assert [x["status"] for x in audit] == ["conflict_held", "added"]
    assert len(old) == 1


def test_merge_rejects_wrong_evidence_or_page_and_deduplicates():
    old = [observation()]
    incoming = [observation(), observation(페이지=2), observation(근거ID목록=["another"])]
    result, audit = merge_fresh(old, incoming, 1, ["ev-1"])
    assert result == old
    assert [x["status"] for x in audit] == ["unchanged", "out_of_scope", "out_of_scope"]


def test_empty_retry_keeps_all_old_readings():
    assert merge_fresh([observation()], [], 1, ["ev-1"]) == ([observation()], [])


@pytest.mark.parametrize("field,value", [("max_calls", 0), ("max_calls", 2.5),
                                          ("total_seconds", float("inf")),
                                          ("call_timeout_seconds", -1), ("max_calls", True)])
def test_invalid_budgets(field, value):
    budget = {"max_calls": 4, "call_timeout_seconds": 120, "total_seconds": 600}
    budget[field] = value
    with pytest.raises(ValueError):
        validate_budget(budget)


def test_offline_guard_blocks_process_and_network():
    with offline_guard():
        with pytest.raises(PermissionError):
            subprocess.run(["echo", "blocked"])
        with pytest.raises(PermissionError):
            socket.getaddrinfo("example.com", 443)


def test_frozen_hash_change_detected(tmp_path):
    p = tmp_path / "input.json"
    write_json(p, {"value": 1})
    frozen = {str(p): file_hash(p)}
    cli.assert_hashes(frozen)
    write_json(p, {"value": 2})
    with pytest.raises(ValueError, match="changed"):
        cli.assert_hashes(frozen)


def test_new_output_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        cli.new_directory(tmp_path)


def test_actual_writer_roundtrip_allows_object_shared_by_many_rows(tmp_path):
    import config
    from agents.excel_agent import ExcelAgent
    data = {k: [] for k in config.EXTRACTION_SHEETS}
    data["emissions_forecast"] = [
        {"지자체명": "서울특별시", "시나리오": "BAU", "부문": "건물", "연도": 2030,
         "전망값": 10.0, "단위": "천tCO2eq", "근거ID": "same-object"},
        {"지자체명": "서울특별시", "시나리오": "BAU", "부문": "건물", "연도": 2031,
         "전망값": 11, "단위": "천tCO2eq", "근거ID": "same-object"}]
    with settings(PROVENANCE_ENABLED=True, DATA_STATUS_ENABLED=True, CODEBOOK_SHEET_ENABLED=False):
        path = ExcelAgent().write(data, tmp_path / "actual.xlsx")
        checked = verify_saved_workbook(path, data, config)
    assert checked["passed"], checked["issues"]
    assert len(checked["trace"]) == 2


def test_only_explicit_nonproject_losses_are_allowed():
    from utils.vision_recovery_workflow import canonical
    source = raw()
    source["chart_observations"][0].update(대상시트="mitigation_projects", 항목="60% 미만", 판독필드={},
                                          범례목록=["60% 미만"], _recovery_id="original-000001")
    expected = canonical(["mitigation_projects", {"사업명": "60% 미만", "근거ID": "ev-1", "출처페이지": "1"}])
    unexpected = canonical(["emissions_forecast", {"전망값": 10}])
    assert cli.unexpected_losses(source, [expected, unexpected]) == [unexpected]


def frozen_retry(tmp_path, monkeypatch):
    parent = tmp_path / "replay"
    parent.mkdir()
    spec = {"source": "unused.pdf", "source_sha256": "test-only", "scorer": "unused.py",
            "budget": {"max_calls": 2, "call_timeout_seconds": 120, "total_seconds": 600},
            "models": [{"name": "Luna", "model": "test", "command": "test", "provider": "codex"}]}
    write_json(parent / "experiment.json", spec)
    write_json(parent / "retry_queue.json", [{"id": "Luna:1", "model": "Luna", "page": 1,
                                              "object_ids": ["obj-1"], "evidence_ids": ["ev-1"]}])
    (parent / "Luna").mkdir()
    write_json(parent / "Luna/input_snapshot.json", raw())
    write_json(parent / "Luna/replay_evaluation.json", {"comparisons": []})
    write_json(parent / "manifest.json", {"version": "vision-recovery/1", "retry_allowed": True,
               "input_hashes": {}, "code_hashes": {}, "artifact_hashes": {},
               "effective_config": cli.effective_config()})
    return parent


def test_retry_dry_run_never_creates_output_or_calls(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    result = cli.retry(Namespace(replay_dir=parent, dry_run=True, allow_model_calls=False),
                       reader=lambda *a: pytest.fail("No call permitted"))
    assert result == 0 and not (parent / "retry").exists()


def test_retry_requires_explicit_authorization(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="allow-model-calls"):
        cli.retry(Namespace(replay_dir=parent, dry_run=False, allow_model_calls=False),
                  reader=lambda *a: pytest.fail("No call permitted"))
    assert not (parent / "retry").exists()


def mock_postprocessing(monkeypatch):
    import utils.vision_recovery_runtime as runtime
    monkeypatch.setattr(runtime, "load_scorer", lambda path: object())
    def save(raw, out, improved):
        out.mkdir()
        return raw, {"passed": True}
    monkeypatch.setattr(runtime, "save_and_verify", save)
    monkeypatch.setattr(runtime, "score_reading", lambda *a: {
        "comparisons": [], "gold_count": 0, "value_correct": 0,
        "complete": 0, "final_confirmed": 0, "present": 0})


def test_retry_timeout_preserves_success_and_never_repeats(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    queue = read_json(parent / "retry_queue.json")
    queue.append({**queue[0], "id": "Luna:2", "page": 2})
    write_json(parent / "retry_queue.json", queue)
    mock_postprocessing(monkeypatch)
    calls = []
    def reader(source, task, model, timeout):
        calls.append(task["id"])
        assert timeout <= 120
        if task["page"] == 2:
            raise TimeoutError("fake timeout")
        return [observation(연도=2031)], {"mock": True}
    args = Namespace(replay_dir=parent, dry_run=False, allow_model_calls=True)
    assert cli.retry(args, reader=reader) == 2
    combined = read_json(parent / "retry/Luna_combined_snapshot.json")
    assert len(combined["chart_observations"]) == 2
    assert combined["chart_observations"][0] == raw()["chart_observations"][0]
    assert len(calls) == 2
    with pytest.raises(FileExistsError):
        cli.retry(args, reader=reader)
    assert len(calls) == 2


def test_retry_records_value_conflicts_without_replacing(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    mock_postprocessing(monkeypatch)
    assert cli.retry(Namespace(replay_dir=parent, dry_run=False, allow_model_calls=True),
                     reader=lambda *a: ([observation(값=999, 원문값=999)], {})) == 2
    combined = read_json(parent / "retry/Luna_combined_snapshot.json")
    assert combined["chart_observations"] == raw()["chart_observations"]
    assert read_json(parent / "retry/merge_audit.json")[0]["status"] == "conflict_held"


def test_retry_checks_frozen_queue_before_any_call(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    path = parent / "retry_queue.json"
    manifest = read_json(parent / "manifest.json")
    manifest["artifact_hashes"] = {str(path): file_hash(path)}
    write_json(parent / "manifest.json", manifest)
    write_json(path, [])
    with pytest.raises(ValueError, match="changed"):
        cli.retry(Namespace(replay_dir=parent, dry_run=False, allow_model_calls=True),
                  reader=lambda *a: pytest.fail("No call permitted"))


def test_retry_exhausted_wall_budget_makes_no_extra_calls(tmp_path, monkeypatch):
    parent = frozen_retry(tmp_path, monkeypatch)
    mock_postprocessing(monkeypatch)
    ticks = iter([0.0, 601.0, 602.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    assert cli.retry(Namespace(replay_dir=parent, dry_run=False, allow_model_calls=True),
                     reader=lambda *a: pytest.fail("Budget must block calls")) == 2
    assert read_json(parent / "retry/manifest.json")["calls_attempted"] == 0


def test_single_object_backend_uses_existing_parser_once_without_real_call(monkeypatch):
    import utils.vision_recovery_runtime as runtime
    import utils.llm_client as llm
    import config
    calls = []
    def fake_call(image, prompt, system="", max_retries=1, stage=None):
        calls.append((prompt, system, stage))
        assert "G000" not in prompt and "gold" not in prompt.lower()
        assert config.STAGE_MODELS["vision"] == "mock-model"
        return '{"type":"chart_table","target_sheet":"emissions_forecast","chart_type":"表","title":"BAU","confidence":"high","table":[{"연도":2030,"항목":"건물","값":100,"단위":"천tCO2eq","fields":{"시나리오":"BAU","부문":"건물","값근거":"표셀"}}]}'
    monkeypatch.setattr(llm, "call_vision", fake_call)
    source = ROOT / "테스트파일_2.pdf"
    if not source.exists():
        pytest.skip("Local fixture not shipped in public repository")
    observations, detail = runtime.read_one_object(source,
        {"page": 1, "evidence_ids": ["ev-1"], "object_ids": ["obj-1"], "bbox": [0, 0, 100, 100]},
        {"model": "mock-model", "command": "codex"}, 120)
    assert len(calls) == 1 and observations
    assert observations[0]["근거ID"] == "ev-1"
