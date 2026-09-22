from pathlib import Path

import pytest

import config
from agents.image_agent import ImageAgent, VisionBatchContractError, VisionQuotaError
from utils import llm_client
from utils.pdf_reader import PageContent
from utils.run_state import RunState


def _state(tmp_path: Path, monkeypatch, *, resume: bool = False) -> RunState:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"vision-input")
    monkeypatch.setattr(config, "RUN_STATE_DIR", str(tmp_path / "runs"))
    return RunState.create(
        input_path=source,
        guideline_path=None,
        extraction_prompts={"visual_inventory": "prompt"},
        execution_info={"text_backend": "test", "text_model": "model"},
        output_path=tmp_path / "result.xlsx",
        resume=resume,
    )


def _batch() -> list[tuple[PageContent, dict]]:
    return [
        (
            PageContent(page_number=page, text=f"그림 {page}-1 배출량", tables=[], images=[]),
            {"base64": f"image-{page}", "width": 640, "height": 480, "caption": "배출량"},
        )
        for page in (10, 11)
    ]


def _object_batch(count: int = 4) -> list[tuple[PageContent, dict]]:
    return [
        (
            PageContent(page_number=page, text=f"그림 {page}-1 배출량", tables=[], images=[]),
            {
                "base64": f"image-{page}",
                "width": 640,
                "height": 480,
                "caption": "배출량",
                "source_evidence_ids": [f"ev-{page}"],
            },
        )
        for page in range(1, count + 1)
    ]


def test_successful_vision_batch_resumes_without_llm_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_CHECKPOINT_ENABLED", True)
    first = ImageAgent(run_state=_state(tmp_path, monkeypatch))
    monkeypatch.setattr(
        first,
        "_chart_to_table_batch",
        lambda batch, municipality, fail_fast=False: [{"title": "배출량", "page_number": 10}],
    )
    expected = first._run_vision_task(
        {"batch": _batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0},
        "서울특별시",
    )

    resumed = ImageAgent(run_state=_state(tmp_path, monkeypatch, resume=True))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Vision LLM 호출이 발생하면 안 됨")

    monkeypatch.setattr(resumed, "_chart_to_table_batch", fail_if_called)
    actual = resumed._run_vision_task(
        {"batch": _batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0},
        "서울특별시",
    )

    assert actual == expected
    assert resumed.resumed_batches == 1


def test_failed_vision_batch_splits_and_preserves_successful_children(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch))

    def fake_extract(batch, municipality, fail_fast=False):
        if len(batch) > 1:
            raise llm_client.LLMTimeoutError("timeout")
        page = batch[0][0].page_number
        return [{"title": f"p{page}", "page_number": page}]

    monkeypatch.setattr(agent, "_chart_to_table_batch", fake_extract)
    task = {"batch": _batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0}
    rows = agent._run_vision_task(task, "서울특별시")
    root_record = agent.run_state.record_for(task["batch_id"])

    assert [row["page_number"] for row in rows] == [10, 11]
    assert agent.split_batches == 1
    assert root_record["status"] == "ok"
    assert root_record["recovered"] is True


def test_vision_batch_rejects_duplicate_indexes_even_when_all_indexes_exist(monkeypatch) -> None:
    agent = ImageAgent()
    monkeypatch.setattr(
        llm_client,
        "call_vision_batch_json",
        lambda *args, **kwargs: ({
            "analyses": [
                {"image_index": 1, "type": "해당없음"},
                {"image_index": 1, "type": "해당없음"},
                {"image_index": 2, "type": "해당없음"},
            ],
        }, True),
    )

    with pytest.raises(llm_client.LLMCallError, match="객체 계약 위반"):
        agent._chart_to_table_batch(_batch(), "서울특별시", fail_fast=True)


def test_vision_batch_contract_error_exposes_only_unambiguous_rows(monkeypatch) -> None:
    agent = ImageAgent()
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", False)
    monkeypatch.setattr(
        llm_client,
        "call_vision_batch_json",
        lambda *args, **kwargs: ({
            "analyses": [
                {"image_index": 1, "type": "해당없음"},
                {"image_index": 1, "type": "해당없음"},
                {"image_index": 2, "type": "해당없음"},
            ],
        }, True),
    )

    with pytest.raises(VisionBatchContractError) as caught:
        agent._chart_to_table_batch(_batch(), "서울특별시", fail_fast=True)

    assert caught.value.unresolved_indexes == [1]
    assert [row["page_number"] for row in caught.value.partial_rows] == [11]


def test_partial_vision_success_retries_only_missing_objects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 2)
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", False)
    calls: list[int] = []

    def partial_then_success(batch, municipality, fail_fast=False):
        calls.append(len(batch))
        if len(batch) == 2:
            raise VisionBatchContractError(
                "missing index 2",
                partial_rows=[{"page_number": 10, "type": "해당없음"}],
                unresolved_indexes=[2],
            )
        return [{"page_number": batch[0][0].page_number, "type": "해당없음"}]

    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch))
    monkeypatch.setattr(agent, "_chart_to_table_batch", partial_then_success)
    task = {"batch": _batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0}

    rows = agent._run_vision_task(task, "서울특별시")

    assert calls == [2, 1]
    assert [row["page_number"] for row in rows] == [10, 11]
    assert agent.split_batches == 1
    assert agent.run_state.record_for(task["batch_id"])["status"] == "ok"


def test_partial_vision_success_survives_failed_missing_object_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(config, "VISION_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 1)
    monkeypatch.setattr(config, "VISION_NEGATIVE_REVALIDATION_ENABLED", False)

    def partial_then_timeout(batch, municipality, fail_fast=False):
        if len(batch) == 2:
            raise VisionBatchContractError(
                "missing index 2",
                partial_rows=[{"page_number": 10, "type": "해당없음"}],
                unresolved_indexes=[2],
            )
        raise llm_client.LLMTimeoutError("singleton timeout")

    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch))
    monkeypatch.setattr(agent, "_chart_to_table_batch", partial_then_timeout)
    task = {"batch": _batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0}

    rows = agent._run_vision_task(task, "서울특별시")

    assert [row["page_number"] for row in rows] == [10]
    assert agent.run_state.record_for(task["batch_id"])["status"] == "partial"


def test_object_attempt_limit_stops_before_unbounded_singleton_retries(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 6)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_OBJECT_ATTEMPTS", 2)
    agent = ImageAgent()

    def always_timeout(batch, municipality, fail_fast=False):
        raise llm_client.LLMTimeoutError("timeout")

    monkeypatch.setattr(agent, "_chart_to_table_batch", always_timeout)
    rows = agent._run_vision_task(
        {"batch": _object_batch(), "batch_num": 1, "batch_total": 1, "split_depth": 0},
        "서울특별시",
    )

    assert rows == []
    assert agent.split_batches == 1
    assert all(
        agent._object_outcomes.outcome(f"ev-{page}").attempt_count == 2
        for page in range(1, 5)
    )
    assert all(
        agent._object_outcomes.outcome(f"ev-{page}").status == "needs_review"
        for page in range(1, 5)
    )


def test_resume_can_replace_only_selected_vision_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config, "VISION_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(config, "VISION_RETRY_EVIDENCE_IDS", [], raising=False)
    monkeypatch.setattr(config, "VISION_RETRY_PAGES", set(), raising=False)
    task = {
        "batch": _object_batch(2),
        "batch_num": 1,
        "batch_total": 1,
        "split_depth": 0,
    }
    first = ImageAgent(run_state=_state(tmp_path, monkeypatch))
    monkeypatch.setattr(first, "_chart_to_table_batch", lambda *args, **kwargs: [
        {"title": "old-1", "source_evidence_ids": ["ev-1"]},
        {"title": "old-2", "source_evidence_ids": ["ev-2"]},
    ])
    first._run_vision_task(task, "서울특별시")

    monkeypatch.setattr(config, "VISION_RETRY_EVIDENCE_IDS", ["ev-1"], raising=False)
    resumed = ImageAgent(run_state=_state(tmp_path, monkeypatch, resume=True))

    def retry_one(batch, municipality, fail_fast=False):
        assert len(batch) == 1
        assert batch[0][1]["source_evidence_ids"] == ["ev-1"]
        return [{"title": "new-1", "source_evidence_ids": ["ev-1"]}]

    monkeypatch.setattr(resumed, "_chart_to_table_batch", retry_one)
    rows = resumed._run_vision_task(task, "서울특별시")

    assert {(row["title"], tuple(row["source_evidence_ids"])) for row in rows} == {
        ("new-1", ("ev-1",)),
        ("old-2", ("ev-2",)),
    }


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("success_pages", [{1, 2, 5, 6}, set(), set(range(1, 9))])
def test_nested_child_completeness_reaches_root(tmp_path, monkeypatch, checkpoint, success_pages):
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 2)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_OBJECT_ATTEMPTS", 3)
    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch) if checkpoint else None)

    def extract(batch, municipality, fail_fast=False):
        pages = [page.page_number for page, _ in batch]
        if len(batch) > 2 or not set(pages) <= success_pages:
            raise llm_client.LLMTimeoutError("leaf timeout")
        return [{"page_number": page, "source_evidence_ids": [f"ev-{page}"]} for page in pages]

    monkeypatch.setattr(agent, "_chart_to_table_batch", extract)
    task = {"batch": _object_batch(8), "batch_num": 1, "split_depth": 0}
    outcome = agent._run_vision_task_result(task, "서울특별시")
    assert {row["page_number"] for row in outcome.rows} == success_pages
    assert outcome.complete == (len(success_pages) == 8)
    if checkpoint:
        root = agent.run_state.record_for(task["batch_id"])
        assert root["status"] == ("ok" if outcome.complete else "partial")
        assert root["result"] == outcome.rows
        summary = agent.run_state.summary(kinds={"vision"})
        assert summary["ok"] == int(outcome.complete)
        assert summary["failed"] == int(not outcome.complete)
        if not outcome.complete:
            agent.run_state.resume = True
            assert agent.run_state.restored_result(task["batch_id"]) is None
            assert agent.run_state.should_execute(task["batch_id"])


def test_contract_partial_child_keeps_distinct_checkpoint_and_incomplete_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 2)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_OBJECT_ATTEMPTS", 3)
    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch))

    def extract(batch, municipality, fail_fast=False):
        pages = [page.page_number for page, _ in batch]
        if len(batch) == 4:
            raise llm_client.LLMTimeoutError("root timeout")
        if pages == [1, 2]:
            raise VisionBatchContractError("missing 2", partial_rows=[{"page_number": 1}], unresolved_indexes=[2])
        if pages == [2]:
            raise llm_client.LLMTimeoutError("missing object timeout")
        return [{"page_number": page} for page in pages]

    monkeypatch.setattr(agent, "_chart_to_table_batch", extract)
    task = {"batch": _object_batch(4), "batch_num": 1, "split_depth": 0}
    outcome = agent._run_vision_task_result(task, "서울특별시")
    assert not outcome.complete
    assert [row["page_number"] for row in outcome.rows] == [1, 3, 4]
    parent_child = agent._split_vision_task(task)[0]
    child_record = agent.run_state.record_for(agent._vision_batch_id(parent_child))
    assert child_record["status"] == "partial"
    assert child_record["page_nums"] == [1, 2]
    missing_child = {"batch": [task["batch"][1]], "batch_num": "1.1.missing", "split_depth": 2}
    missing_id = agent._vision_batch_id(missing_child)
    assert missing_id != agent._vision_batch_id(parent_child)
    assert agent.run_state.record_for(missing_id)["status"] == "call_fail"
    assert agent.run_state.record_for(task["batch_id"])["status"] == "partial"


def test_quota_preserves_successful_siblings_and_marks_parent_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 2)
    agent = ImageAgent(run_state=_state(tmp_path, monkeypatch))

    def extract(batch, municipality, fail_fast=False):
        if len(batch) > 1:
            raise llm_client.LLMTimeoutError("root timeout")
        page = batch[0][0].page_number
        if page == 2:
            raise llm_client.LLMQuotaExceededError("quota")
        return [{"page_number": page}]

    monkeypatch.setattr(agent, "_chart_to_table_batch", extract)
    task = {"batch": _object_batch(2), "batch_num": 1, "split_depth": 0}
    with pytest.raises(VisionQuotaError) as caught:
        agent._run_vision_task_result(task, "서울특별시")
    assert caught.value.partial_rows == [{"page_number": 1}]
    root = agent.run_state.record_for(task["batch_id"])
    assert root["status"] == "partial"
    assert root["result"] == caught.value.partial_rows


def test_empty_failed_batch_is_not_reported_as_no_relevant_data(monkeypatch, capsys):
    monkeypatch.setattr(config, "IMAGE_TRIAGE_ENABLED", False)
    monkeypatch.setattr(config, "IMAGE_CHART_TABLE_EXTRACTION", True)
    monkeypatch.setattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "VISION_SPLIT_ON_FAILURE", True)
    monkeypatch.setattr(config, "VISION_RECOVERY_MAX_SPLIT_DEPTH", 1)
    monkeypatch.setattr(config, "MAX_IMAGES", None)
    agent = ImageAgent()
    def timeout(*args, **kwargs):
        raise llm_client.LLMTimeoutError("timeout")
    monkeypatch.setattr(agent, "_chart_to_table_batch", timeout)
    pages = [PageContent(page_number=page.page_number, text="", tables=[], images=[image])
             for page, image in _batch()]
    agent.extract(pages, {"chart_observations": []}, "서울특별시")
    output = capsys.readouterr().out
    assert "부분 실패/미완료" in output
    assert "관련 있는 표/그래프 없음" not in output
    assert "루트 1건" in output
