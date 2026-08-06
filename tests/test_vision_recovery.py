from pathlib import Path

from scripts import fixed_visual_sample as fixed_cli
from utils.fixed_visual_sample import FixedSampleDataset, FixedSampleItem
from utils.vision_recovery import (
    ObjectOutcomeLedger,
    has_usable_table_value,
    normalize_terminal_status,
    split_in_half,
)


def test_qualitative_project_row_is_usable_visual_content() -> None:
    assert has_usable_table_value([{
        "값": None,
        "fields": {"관리번호": "B-1", "사업명": "제로에너지건물 전환"},
    }]) is True


def _item(sample_id: str) -> FixedSampleItem:
    return FixedSampleItem(
        sample_id=sample_id,
        page=int(sample_id[-1]),
        element_type="그래프",
        title=sample_id,
        data_included=True,
        expected=True,
        related_sheet="05_배출전망",
        note="",
        role="failure",
        expected_status="extracted",
        source_status="vision_유실",
        image_path=f"{sample_id}.png",
        image_sha256="hash",
        page_text_excerpt="",
    )


def test_shared_terminal_contract_downgrades_all_null_data_to_no_data() -> None:
    assert normalize_terminal_status(
        "extracted",
        response_received=True,
        table=[{"연도": 2030, "값": None}],
        data_expected=True,
    ) == "no_data"
    assert normalize_terminal_status(
        "extracted",
        response_received=True,
        table=[{"연도": 2030, "값": 10}],
        data_expected=True,
    ) == "extracted"
    assert normalize_terminal_status(
        "failed",
        response_received=False,
    ) == "needs_review"


def test_outcome_ledger_upgrades_retry_success_and_guarantees_terminal_row() -> None:
    ledger = ObjectOutcomeLedger(["E1", "E2"])
    ledger.mark_attempt(["E1", "E2"], label="root")
    ledger.record("E1", "needs_review", response_received=False, reason="timeout")
    ledger.mark_attempt(["E1"], label="retry")
    ledger.record("E1", "extracted", response_received=True, reason="recovered")

    recovered = ledger.outcome("E1")
    unresolved = ledger.outcome("E2", unresolved_reason="retry limit")

    assert recovered.status == "extracted"
    assert recovered.attempt_count == 2
    assert recovered.response_received is True
    assert unresolved.status == "needs_review"
    assert unresolved.attempt_count == 1
    assert unresolved.response_received is False


def test_adaptive_retry_splits_failed_batch_and_isolates_terminal_failure() -> None:
    items = [_item(f"E00{index}") for index in range(1, 5)]
    dataset = FixedSampleDataset(Path("manifest.json"), {"dataset_id": "test"}, tuple(items))
    calls: list[list[str]] = []

    def fake_call(_dataset, batch, *, max_retries):
        ids = [item.sample_id for item in batch]
        calls.append(ids)
        if ids in (["E001", "E002"], ["E003"]):
            rows = [
                {
                    "sample_id": sample_id,
                    "status": "extracted",
                    "table": [{"연도": 2030, "값": 10}],
                }
                for sample_id in ids
            ]
        else:
            rows = []
        return rows, {
            "sample_ids": ids,
            "returned_ids": [row["sample_id"] for row in rows],
            "elapsed_seconds": 0.01,
            "error": "" if rows else "timeout",
        }

    outcomes = ObjectOutcomeLedger(item.sample_id for item in items)
    results: dict[str, dict] = {}
    logs: list[dict] = []
    fixed_cli._recover_visual_items(
        dataset,
        items,
        batch_index=1,
        retry_policy="adaptive",
        max_retries=1,
        max_split_depth=6,
        max_object_attempts=3,
        result_by_id=results,
        outcomes=outcomes,
        batch_logs=logs,
        call_batch=fake_call,
    )

    assert calls == [
        ["E001", "E002", "E003", "E004"],
        ["E001", "E002"],
        ["E003", "E004"],
        ["E003"],
        ["E004"],
    ]
    assert set(results) == {"E001", "E002", "E003"}
    assert outcomes.outcome("E001").attempt_count == 2
    assert outcomes.outcome("E003").attempt_count == 3
    terminal = outcomes.outcome("E004", unresolved_reason="retry limit")
    assert terminal.status == "needs_review"
    assert terminal.attempt_count == 3
    assert terminal.response_received is False
    assert [log["split_depth"] for log in logs] == [0, 1, 1, 2, 2]


def test_split_in_half_preserves_order_for_odd_batches() -> None:
    assert split_in_half([1, 2, 3, 4, 5]) == [[1, 2], [3, 4, 5]]


def test_duplicate_object_response_is_retried_instead_of_accepting_first_row() -> None:
    items = [_item("E001")]
    dataset = FixedSampleDataset(Path("manifest.json"), {"dataset_id": "test"}, tuple(items))
    calls = 0

    def fake_call(_dataset, batch, *, max_retries):
        nonlocal calls
        calls += 1
        rows = (
            [
                {"sample_id": "E001", "status": "extracted", "table": [{"값": 1}]},
                {"sample_id": "E001", "status": "extracted", "table": [{"값": 2}]},
            ]
            if calls == 1
            else [{"sample_id": "E001", "status": "extracted", "table": [{"값": 3}]}]
        )
        return rows, {
            "sample_ids": ["E001"],
            "returned_ids": [row["sample_id"] for row in rows],
            "elapsed_seconds": 0.01,
            "error": "",
        }

    outcomes = ObjectOutcomeLedger(["E001"])
    results: dict[str, dict] = {}
    logs: list[dict] = []
    fixed_cli._recover_visual_items(
        dataset,
        items,
        batch_index=1,
        retry_policy="adaptive",
        max_retries=1,
        max_split_depth=6,
        max_object_attempts=3,
        result_by_id=results,
        outcomes=outcomes,
        batch_logs=logs,
        call_batch=fake_call,
    )

    assert calls == 2
    assert len(results["E001"]["table"]) == 1
    assert results["E001"]["table"][0]["값"] == 3
    assert results["E001"]["table"][0]["fields"]["값근거"] == "unknown"
    assert logs[0]["duplicate_ids"] == ["E001"]
    assert outcomes.outcome("E001").attempt_count == 2
