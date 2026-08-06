from __future__ import annotations

from utils.visual_merge_ab import (
    compare_merge_policies,
    compare_merge_policies_with_data,
    load_visual_merge_snapshot,
    write_visual_merge_snapshot,
)


def _raw() -> dict:
    fields = {
        "값근거": "표셀",
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접",
        "연도": 2030,
    }
    return {
        "municipality_name": "서울특별시",
        "chart_observations": [{
            "지자체명": "서울특별시",
            "페이지": 10,
            "대상시트": "emissions_management",
            "그래프유형": "표",
            "제목": "관리권한 배출량",
            "단위": "천tCO2eq",
            "항목": "건물",
            "연도": 2030,
            "값": 100.0,
            "값근거": "표셀",
            "신뢰도": "high",
            "반영여부": "검토",
            "판독필드": fields,
            "근거ID목록": ["ev-1", "ev-2"],
            "병합상태": "needs_review",
            "병합차단사유": "복수 근거",
        }],
        "document_objects": [{
            "object_id": "obj-1",
            "object_type": "chart",
            "page_number": 10,
            "metadata": {"evidence_id": "ev-1", "final_status": "extracted"},
        }],
        "object_triage": [],
    }


def test_operational_snapshot_replays_merge_policies_without_llm(tmp_path) -> None:
    snapshot_path = write_visual_merge_snapshot(tmp_path / "input.json.gz", _raw())
    payload = compare_merge_policies(load_visual_merge_snapshot(snapshot_path))

    assert payload["legacy"]["metrics"]["decision_counts"]["accept"] == 1
    assert payload["evidence"]["metrics"]["decision_counts"]["needs_review"] == 1
    assert payload["comparison"]["visual_output_row_delta"] == -1


def test_operational_snapshot_materializes_both_evaluation_datasets() -> None:
    payload, datasets = compare_merge_policies_with_data({
        "snapshot_version": 1,
        "data": _raw(),
    })

    assert set(datasets) == {"legacy", "evidence"}
    assert payload["comparison"]["visual_output_row_delta"] == -1
    assert any(
        row.get("데이터상태") == "visual_only"
        for row in datasets["legacy"].get("emissions_management", [])
    )
    assert not any(
        row.get("데이터상태") == "visual_only"
        for row in datasets["evidence"].get("emissions_management", [])
    )
    assert datasets["legacy"]["document_objects"] == _raw()["document_objects"]
    assert datasets["evidence"]["document_objects"] == _raw()["document_objects"]


def test_snapshot_preserves_table_rows_for_offline_inventory(tmp_path) -> None:
    raw = _raw()
    raw["document_objects"][0]["object_type"] = "table"
    raw["document_objects"][0]["rows"] = [["연도", "값"], ["2030", "100"]]

    snapshot_path = write_visual_merge_snapshot(tmp_path / "input.json.gz", raw)
    restored = load_visual_merge_snapshot(snapshot_path)["data"]["document_objects"][0]

    assert restored["rows"] == [["연도", "값"], ["2030", "100"]]
