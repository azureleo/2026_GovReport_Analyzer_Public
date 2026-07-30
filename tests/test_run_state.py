from __future__ import annotations

import json
from pathlib import Path

import config
from utils import llm_cache
from utils.llm_cache import LLMCacheRequest
from utils.run_state import RunState, merge_rows_stably, stable_row_id


def _state(
    tmp_path: Path,
    monkeypatch,
    *,
    resume: bool = False,
    retry_failed_only: bool = False,
) -> RunState:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"stable-input")
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "runs"))
    return RunState.create(
        input_path=source,
        guideline_path=None,
        extraction_prompts={"document_meta": "prompt"},
        execution_info={"text_backend": "test", "text_model": "model"},
        output_path=tmp_path / "result.xlsx",
        resume=resume,
        retry_failed_only=retry_failed_only,
    )


def test_successful_batch_is_persisted_and_restored(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    batch_id = state.batch_id(
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        batch_text="page 1",
    )
    expected = [{"계획명": "서울특별시 탄소중립 기본계획", "출처페이지": 1}]
    state.record_batch(
        batch_id=batch_id,
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        status="ok",
        result=expected,
    )

    resumed = _state(tmp_path, monkeypatch, resume=True)

    assert resumed.restored_result(batch_id) == expected
    assert resumed.should_execute(batch_id) is False


def test_retry_failed_only_executes_failed_but_not_missing(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    failed_id = state.batch_id(
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        batch_text="failed",
    )
    missing_id = state.batch_id(
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[2],
        batch_text="missing",
    )
    state.record_batch(
        batch_id=failed_id,
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        status="call_fail",
        error="timeout",
    )

    resumed = _state(tmp_path, monkeypatch, retry_failed_only=True)

    assert resumed.should_execute(failed_id) is True
    assert resumed.should_execute(missing_id) is False


def test_stable_merge_is_idempotent_and_preserves_entity_conflict() -> None:
    first = {"지자체명": "서울", "계획명": "기본계획", "발간기관": "서울시", "출처페이지": 2}
    conflict = {"지자체명": "서울", "계획명": "기본계획", "발간기관": "환경부", "출처페이지": 3}

    rows, conflicts = merge_rows_stably("document_meta", [first], [first, conflict])
    rows_again, conflicts_again = merge_rows_stably("document_meta", [], [conflict, first, first])

    assert len(rows) == 2
    assert len(conflicts) == 1
    assert [stable_row_id("document_meta", row) for row in rows] == [
        stable_row_id("document_meta", row) for row in rows_again
    ]
    assert len(conflicts_again) == 1


def test_finalize_writes_manifest_with_hashes_and_status(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    batch_id = state.batch_id(
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        batch_text="page 1",
    )
    state.register_expected(batch_id, {"kind": "sheet"})
    state.record_batch(
        batch_id=batch_id,
        kind="sheet",
        sheet_keys=["document_meta"],
        page_nums=[1],
        status="ok",
        result=[{"계획명": "기본계획"}],
    )
    output = tmp_path / "result.xlsx"
    output.write_bytes(b"xlsx")

    sidecar = state.finalize(output_path=output, semantic_result_hash="semantic")
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))

    assert manifest["status"] == "complete"
    assert manifest["input_sha256"]
    assert manifest["output_sha256"]
    assert manifest["semantic_result_sha256"] == "semantic"
    assert manifest["batch_summary"]["ok"] == 1


def test_conflict_log_is_idempotent_across_resume(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    conflict = {
        "sheet_key": "document_meta",
        "entity_id": "entity",
        "kept_row_id": "kept",
        "incoming_row_id": "incoming",
    }
    state.record_conflicts([conflict, conflict])

    resumed = _state(tmp_path, monkeypatch, resume=True)
    resumed.record_conflicts([conflict])

    assert resumed.summary()["conflicts"] == 1
    lines = [line for line in resumed.conflicts_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1


def test_summary_can_score_text_roots_without_vision_roots(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    text_id = state.batch_id(
        kind="sheet", sheet_keys=["document_meta"], page_nums=[1], batch_text="text",
    )
    vision_id = state.batch_id(
        kind="vision", sheet_keys=["visual_inventory"], page_nums=[2], batch_text="image",
    )
    state.register_expected(text_id, {"kind": "sheet"})
    state.register_expected(vision_id, {"kind": "vision"})
    state.record_batch(
        batch_id=text_id, kind="sheet", sheet_keys=["document_meta"], page_nums=[1],
        status="ok", result=[],
    )
    state.record_batch(
        batch_id=vision_id, kind="vision", sheet_keys=["visual_inventory"], page_nums=[2],
        status="call_fail", error="timeout",
    )

    text_summary = state.summary(kinds={"sheet", "cluster"})

    assert text_summary == {"expected": 1, "ok": 1, "failed": 0, "missing": 0, "conflicts": 0}


def test_empty_or_invalid_json_response_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(config, "LLM_CACHE_DIR", str(tmp_path / "cache"))
    llm_cache.reset_cache_stats()
    request = LLMCacheRequest(
        call_kind="text",
        provider="test",
        model="model",
        system="",
        prompt="prompt",
    )

    assert llm_cache.cached_response(request, lambda: "{}") == "{}"
    assert llm_cache.cached_response(request, lambda: '{"document_meta": []}') == '{"document_meta": []}'
    assert llm_cache.cached_response(request, lambda: "not-json") == '{"document_meta": []}'
    stats = llm_cache.get_cache_stats()
    assert stats["write"] >= 1
    assert stats["rejected"] >= 1
