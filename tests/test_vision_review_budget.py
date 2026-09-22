import subprocess
from types import SimpleNamespace

import pytest

import config
from agents.image_agent import ImageAgent, VisionBatchContractError
from utils import llm_client
from utils.document_objects import DocumentObject
from utils.pdf_reader import PDFContent, PageContent
from utils.run_state import RunState
from utils.selective_ocr import build_triage_plan, apply_triage_metadata
from utils.source_verifier import assess_quality, build_source_object_inventory, extraction_outcome
from utils.vision_review import ReviewBudget, promote_review_candidates


@pytest.fixture
def bounded(monkeypatch):
    for name, value in {
        "VISION_REVIEW_ENABLED": True, "VISION_REVIEW_MAX_OBJECTS": 16,
        "VISION_REVIEW_MAX_PER_PAGE": 1, "VISION_REVIEW_MAX_CALLS": 16,
        "VISION_REVIEW_MAX_SECONDS": 600, "VISION_REVIEW_CALL_TIMEOUT": 120,
        "VISION_EXPECTED_CANDIDATE_SHA256": "", "SELECTIVE_OCR_ENABLED": True,
        "OCR_BACKEND": "vlm", "OCR_RESULTS_DIR": "", "MAX_IMAGES": None,
    }.items():
        monkeypatch.setattr(config, name, value)


def sample(count=3):
    pages, objects = [], []
    for page in range(1, count + 1):
        pages.append(PageContent(page, "", [], [{"base64": f"image{page}", "width": 600, "height": 800,
            "bbox": [0, 0, 600, 800], "source_kind": "embedded"}], width=600, height=800))
        objects.append(DocumentObject(f"p{page}_image_1", "image", page, 1, bbox=(0, 0, 600, 800)))
        objects.append(DocumentObject(f"p{page}_text_1", "text", page, 1))
    return PDFContent(count, pages, ""), objects


def decisions(document, objects):
    rows = build_triage_plan(objects, backend="vlm", confidence_threshold=.78)
    promote_review_candidates(rows, objects, document.pages)
    return rows


def test_sparse_pages_promote_once_and_preserve_initial_classification(bounded):
    document, objects = sample(11)
    rows = decisions(document, objects)
    assert sum(row.action == "ocr_required" for row in rows) == 11
    assert sum(row.action == "review_required" for row in rows) == 11
    apply_triage_metadata(objects, rows)
    assert objects[0].metadata["initial_triage_action"] == "review_required"
    assert objects[0].metadata["promotion_reason"] == "sparse_page_large_image"
    assert all(row.attempt_count == 0 for row in rows)


def test_promotion_is_bounded_and_explicit_photo_is_not_promoted(bounded, monkeypatch):
    document, objects = sample(4)
    objects[0].caption = "행사 사진"
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_OBJECTS", 2)
    rows = decisions(document, objects)
    assert rows[0].action == "skip_non_data"
    assert sum(row.action == "ocr_required" for row in rows) == 2
    assert "review_hold:selection_limit" in rows[-2].reasons


def test_small_structured_diagram_with_sparse_native_header_is_candidate(bounded):
    document, objects = sample(1)
    document.pages[0].text = "서울특별시 기본계획"
    objects[0].bbox = (100, 100, 300, 250)
    objects[0].caption = "그림 4-1 비전 체계도"
    rows = decisions(document, objects)
    assert rows[0].promotion_reason == "structured_caption"


def test_zero_budget_means_disabled_not_unlimited(bounded, monkeypatch):
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_CALLS", 0)
    budget = ReviewBudget()
    assert budget.claim("id", 1, "hash")[0] == 0


def test_budget_resume_charges_interrupted_reservation(bounded, tmp_path):
    state = SimpleNamespace(root=tmp_path, resume=False)
    budget = ReviewBudget(state)
    assert budget.claim("a", 1, "hash")[0] == 120
    state.resume = True
    restored = ReviewBudget(state)
    assert restored.snapshot()["seconds_charged"] == 120
    assert restored.claim("a", 1, "hash")[0] == 0
    restored.finish("a", 42, "failed")
    assert ReviewBudget(state).snapshot()["seconds_charged"] == 42


def test_time_call_and_page_budgets_are_independent(bounded, monkeypatch):
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_SECONDS", 150)
    budget = ReviewBudget()
    assert budget.claim("a", 1, "h")[0] == 120
    assert budget.claim("b", 1, "h")[0] == 0
    assert budget.claim("c", 2, "h")[0] == 30
    assert budget.claim("d", 3, "h")[0] == 0


def test_preflight_is_same_queue_and_never_calls_model(bounded, monkeypatch):
    document, objects = sample(11)
    def forbidden(*a, **kw):
        pytest.fail("preflight called model")
    monkeypatch.setattr(llm_client, "call_vision_batch_json", forbidden)
    agent = ImageAgent()
    first = agent.extract(document.pages, {}, "서울", document, objects, preflight_only=True)["vision_preflight"]
    second = ImageAgent().extract(document.pages, {}, "서울", document, objects, preflight_only=True)["vision_preflight"]
    assert first["candidate_sha256"] == second["candidate_sha256"]
    assert first["review_inputs"] == 11
    assert first["expected_root_batches"] == 11
    assert agent.review_budget.snapshot()["calls_reserved"] == 0


def test_input_hash_mismatch_stops_before_calls(bounded, monkeypatch):
    document, objects = sample(1)
    monkeypatch.setattr(config, "VISION_EXPECTED_CANDIDATE_SHA256", "wrong")
    with pytest.raises(ValueError, match="후보 해시"):
        ImageAgent().extract(document.pages, {}, "서울", document, objects)


def test_image_agent_budget_holds_unattempted_objects(bounded, monkeypatch):
    document, objects = sample(3)
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_CALLS", 2)
    calls = []
    def mock_read(self, batch, municipality, fail_fast=False):
        calls.append(batch)
        raise llm_client.LLMTimeoutError("test timeout")
    monkeypatch.setattr(ImageAgent, "_chart_to_table_batch", mock_read)
    budget = ReviewBudget()
    for _ in range(2):
        agent = ImageAgent(review_budget=budget)
        result = agent.extract(document.pages, {}, "서울", document, objects)
        visual = [r for r in result["object_triage"] if r["object_type"] == "image"]
        assert all(r["final_status"] == "needs_review" for r in visual)
        assert [r["attempt_count"] for r in visual] == [1, 1, 0]
        assert agent.split_batches == 0
    assert len(calls) == 2


def test_limited_json_does_not_retry_invalid_json(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_client, "call_vision", lambda *a, **k: calls.append(1) or "invalid")
    with llm_client.limited_vision_call(10):
        assert llm_client.call_vision_json("image", "prompt")[1] is False
    assert len(calls) == 1


@pytest.mark.parametrize("error", [llm_client.LLMQuotaExceededError("quota"), llm_client.LLMCallError("bad"), subprocess.TimeoutExpired("cmd", 1)])
def test_limited_cli_no_hidden_retry_or_quota_wait(monkeypatch, error):
    calls = []
    def call():
        calls.append(1)
        raise error
    monkeypatch.setattr(llm_client.time, "sleep", lambda *a: pytest.fail("unexpected wait"))
    with llm_client.limited_vision_call(10), pytest.raises(llm_client.LLMCallError):
        llm_client._retry_local_call(call, max_retries=5, label="test")
    assert len(calls) == 1


def test_limited_transport_clamps_timeout_and_blocks_second_command(monkeypatch, tmp_path):
    seen = []
    def run(command, prompt, *, cwd, timeout):
        seen.append(timeout)
        return subprocess.CompletedProcess(command, 0, "{}", "")
    monkeypatch.setattr(llm_client, "run_local_command", run)
    with llm_client.limited_vision_call(7):
        llm_client._run_command(["mock"], "", cwd=tmp_path, timeout=999)
        with pytest.raises(llm_client.LLMTimeoutError):
            llm_client._run_command(["mock"], "", cwd=tmp_path, timeout=999)
    assert 0 < seen[0] <= 7
    assert len(seen) == 1


def test_empty_and_review_only_metrics_are_not_accuracy(bounded):
    document, objects = sample(1)
    rows = decisions(document, objects)
    apply_triage_metadata(objects, rows)
    inventory = build_source_object_inventory({}, document, document_objects=objects)
    assessment = assess_quality({}, source_inventory=inventory)
    assert inventory.coverage_ratio is None
    assert inventory.extraction_denominator_objects == 0
    assert inventory.review_candidate_objects == 1
    assert inventory.evaluation_metrics()["vision_attempt_rate"] == 0
    assert "평가 불가" in inventory.summary_text()
    assert assessment.score == 0
    assert assessment.metrics["extraction_success_ratio"] is None
    assert assessment.metrics["cell_accuracy"] is None
    assert extraction_outcome(assessment.metrics)["exit_code"] == 2


def test_explicit_exclusion_is_separate_from_review_and_empty_denominator(bounded):
    document, objects = sample(1)
    objects[0].caption = "행사 사진"
    apply_triage_metadata(objects, decisions(document, objects))
    report = build_source_object_inventory({}, document, document_objects=objects)
    assert report.coverage_ratio is None
    assert report.review_candidate_objects == 0
    assert report.explicit_non_data_objects == 1
    assert report.evaluation_metrics()["source_object_explicit_exclusion_rate"] == 1


@pytest.mark.parametrize("metrics,batch,code", [
    ({"business_rows": 0, "score": 100}, "complete", 2),
    ({"business_rows": 2, "score": 100, "source_object_held_objects": 1}, "complete", 3),
    ({"business_rows": 2, "score": 100}, "partial", 3),
    ({"business_rows": 2, "score": 100}, "complete", 0),
])
def test_saved_workbook_is_not_automatically_success(metrics, batch, code):
    outcome = extraction_outcome(metrics, batch)
    assert outcome["file_saved"]
    assert outcome["exit_code"] == code


def test_vision_model_and_review_policy_change_run_fingerprint(bounded, monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"test")
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "states"))
    def create(model):
        return RunState.create(input_path=source, guideline_path=None, extraction_prompts={},
            execution_info={"vision_model": model}, output_path=tmp_path / "result.xlsx").run_id
    first = create("luna")
    second = create("astra")
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_CALLS", 2)
    assert len({first, second, create("astra")}) == 3


def test_promoted_partial_result_is_preserved_without_recursive_recovery(bounded, monkeypatch):
    document, objects = sample(1)
    agent = ImageAgent()
    calls = []
    partial = {"title": "kept", "source_evidence_ids": ["ev-test"]}
    def read(*args, **kwargs):
        calls.append(1)
        raise VisionBatchContractError("missing", partial_rows=[partial], unresolved_indexes=[1])
    monkeypatch.setattr(agent, "_chart_to_table_batch", read)
    image = {**document.pages[0].images[0], "review_promoted": True, "source_evidence_ids": ["ev-test"]}
    task = {"batch": [(document.pages[0], image)], "batch_num": 1, "split_depth": 0, "review_promoted": True}
    result = agent._run_bounded_review_task(task, "서울")
    assert result.rows == [partial] and not result.complete
    assert agent._run_bounded_review_task(task, "서울").rows == [partial]
    assert len(calls) == 1 and agent.split_batches == 0


def test_resume_reuses_success_but_does_not_repeat_failed_probe(bounded, monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"test")
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "states"))
    def state(resume):
        return RunState.create(input_path=source, guideline_path=None, extraction_prompts={},
            execution_info={"vision_model": "mock"}, output_path=tmp_path / "r.xlsx", resume=resume)
    document, objects = sample(2)
    first = ImageAgent(state(False))
    def read(batch, *args, **kwargs):
        if batch[0][0].page_number == 2:
            raise llm_client.LLMTimeoutError("failed")
        return [{"title": "saved", "source_evidence_ids": ["ev1"]}]
    monkeypatch.setattr(first, "_chart_to_table_batch", read)
    def task(page):
        image = {**document.pages[page - 1].images[0], "review_promoted": True, "source_evidence_ids": [f"ev{page}"]}
        return {"batch": [(document.pages[page - 1], image)], "batch_num": page, "split_depth": 0, "review_promoted": True}
    assert first._run_bounded_review_task(task(1), "서울").complete
    with pytest.raises(llm_client.LLMTimeoutError):
        first._run_bounded_review_task(task(2), "서울")
    resumed = ImageAgent(state(True))
    monkeypatch.setattr(resumed, "_chart_to_table_batch", lambda *a, **k: pytest.fail("resume must not call"))
    assert resumed._run_bounded_review_task(task(1), "서울").rows[0]["title"] == "saved"
    assert not resumed._run_bounded_review_task(task(2), "서울").complete
    assert resumed.review_budget.snapshot()["calls_reserved"] == 2


def test_limited_capacity_does_not_switch_model(monkeypatch):
    monkeypatch.setattr(llm_client, "cached_response_if_present", lambda *a: None)
    monkeypatch.setattr(llm_client, "cached_response", lambda request, producer: producer())
    monkeypatch.setattr(llm_client, "_stage_fallback_model", lambda *a: "fallback")
    calls = []
    def call(*args, **kwargs):
        calls.append(kwargs["model"])
        raise llm_client.LLMCapacityError("full")
    monkeypatch.setattr(llm_client, "_call_local_agent", call)
    with llm_client.limited_vision_call(10), pytest.raises(llm_client.LLMCapacityError):
        llm_client._call_local_with_capacity(kind="vision", provider="codex", stage="vision",
            primary_model="primary", prompt="p", system="", max_retries=9, images_b64=["i"])
    assert calls == ["primary"]


def test_bounded_negative_revalidation_never_adds_a_call(bounded):
    agent = ImageAgent()
    assert agent._claim_negative_revalidation({"review_promoted": True, "caption": "2030년 배출량 100 tCO2eq"}, 1) == []


def test_mixed_inventory_keeps_known_missing_data_in_coverage_denominator(bounded):
    document, objects = sample(2)
    objects[0].caption = "2030년 배출량 100 tCO2eq"
    apply_triage_metadata(objects, decisions(document, objects))
    inventory = build_source_object_inventory({}, document, document_objects=objects)
    assert inventory.extraction_denominator_objects == 1
    assert inventory.review_candidate_objects == 1
    assert inventory.coverage_ratio == 0


def test_a_saved_review_result_requires_matching_sidecar(tmp_path):
    import json
    from utils.pipeline_result import saved_review_result
    path = tmp_path / "out.xlsx"
    path.touch()
    assert not saved_review_result(3, path)
    path.with_name("out_run_outcome.json").write_text(json.dumps({"file_saved": True, "exit_code": 3, "status": "needs_review"}), encoding="utf-8")
    assert saved_review_result(3, path)
    assert not saved_review_result(2, path)


def test_zero_call_budget_cannot_be_reported_ready(bounded, monkeypatch):
    document, objects = sample(1)
    monkeypatch.setattr(config, "VISION_REVIEW_MAX_CALLS", 0)
    plan = ImageAgent().extract(document.pages, {}, "서울", document, objects, preflight_only=True)["vision_preflight"]
    assert plan["review_inputs"] == 1
    assert not plan["comparison_ready"]


def test_review_render_uses_composite_not_context_only(bounded, monkeypatch):
    document, objects = sample(1)
    document.pages[0].images = []
    def render(doc, native, required, **kwargs):
        row = required[0]
        return [(doc.pages[0], {"base64": f"render-{variant}", "width": 600, "height": 800,
                "source_evidence_ids": [row.evidence_id], "source_physical_object_ids": [row.physical_object_id],
                "source_object_ids": [row.object_id], "render_variant": variant, "ocr_required": True})
                for variant in ("panel", "composite", "full_page_context")]
    monkeypatch.setattr("agents.image_agent.render_ocr_candidate_images", render)
    plan = ImageAgent().extract(document.pages, {}, "서울", document, objects, preflight_only=True)["vision_preflight"]
    assert plan["input_count"] == 1
    assert plan["inputs"][0]["render_variant"] == "composite"


def test_unsupported_api_is_blocked_and_call_policy_is_reset(monkeypatch):
    monkeypatch.setattr(llm_client, "_resolve_provider", lambda stage: "openai")
    with llm_client.limited_vision_call(10), pytest.raises(llm_client.LLMCallError, match="CLI"):
        llm_client._claim_limited_vision_request("vision")
    assert llm_client._limited_vision.get() is None


def test_forced_retry_cannot_bypass_review_budget(bounded, monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"test")
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "states"))
    def state(resume):
        return RunState.create(input_path=source, guideline_path=None, extraction_prompts={},
            execution_info={}, output_path=tmp_path / "r.xlsx", resume=resume)
    document, _ = sample(1)
    image = {**document.pages[0].images[0], "review_promoted": True, "source_evidence_ids": ["ev1"]}
    def task():
        return {"batch": [(document.pages[0], image)], "batch_num": 1, "split_depth": 0, "review_promoted": True}
    first = ImageAgent(state(False))
    monkeypatch.setattr(first, "_chart_to_table_batch", lambda *a, **k: [{"title": "saved", "source_evidence_ids": ["ev1"]}])
    assert first._run_bounded_review_task(task(), "서울").complete
    resumed = ImageAgent(state(True))
    monkeypatch.setattr(resumed, "_forced_retry_evidence_ids", lambda task: ["ev1"])
    monkeypatch.setattr(resumed, "_chart_to_table_batch", lambda *a, **k: pytest.fail("budget bypass"))
    result = resumed._run_bounded_review_task(task(), "서울")
    assert not result.complete
    assert result.rows[0]["title"] == "saved"
