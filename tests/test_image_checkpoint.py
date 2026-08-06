from pathlib import Path

import pytest

import config
from agents.image_agent import ImageAgent
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
