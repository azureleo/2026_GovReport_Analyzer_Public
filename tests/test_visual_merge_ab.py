from __future__ import annotations

from utils.visual_merge_ab import (
    compare_exact_object_resolution,
    compare_g4_reference_gate,
    compare_regional_enrichment,
    compare_visual_field_composition,
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


def test_regional_enrichment_ab_reuses_snapshot_without_llm() -> None:
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": [{
            "지자체명": "서울특별시",
            "페이지": 126,
            "대상시트": "regional_conditions",
            "그래프유형": "막대그래프",
            "제목": "연료별 자동차 등록 대수",
            "단위": "천대",
            "항목": "휘발유",
            "연도": 2020,
            "값": 100,
            "값근거": "explicit_label",
            "신뢰도": "high",
            "반영여부": "검토",
            "판독필드": {
                "값근거": "explicit_label",
                "지표명": "연료별 자동차 등록 대수",
                "연도": 2020,
            },
            "근거ID목록": ["ev-126"],
            "병합상태": "candidate",
            "병합차단사유": "",
        }],
        "document_objects": [{
            "object_id": "obj-126",
            "object_type": "chart",
            "page_number": 126,
            "metadata": {"evidence_id": "ev-126", "final_status": "extracted"},
        }],
        "object_triage": [],
    }

    payload = compare_regional_enrichment({"snapshot_version": 1, "data": raw})

    assert payload["before"]["metrics"]["regional_visual_output_rows"] == 0
    assert payload["after"]["metrics"]["regional_visual_output_rows"] == 1
    assert payload["comparison"]["regional_visual_output_row_delta"] == 1
    assert payload["after"]["metrics"]["regional_visual_output_duplicate_rows"] == 0
    assert payload["before"]["metrics"]["llm_calls"] == 0
    assert payload["after"]["metrics"]["llm_calls"] == 0


def test_g4_reference_refinement_trusts_explicit_false_without_legacy_reason_rescan() -> None:
    raw = _raw()
    observation = raw["chart_observations"][0]
    observation.update({
        "대상시트": "regional_conditions",
        "제목": "서울시 가구 수 추이",
        "항목": "서울시",
        "단위": "천가구",
        "연도": 2021,
        "값": 4047,
        "판독필드": {
            "값근거": "표셀",
            "지표범주": "인문사회",
            "지표명": "가구 수",
            "항목": "서울시",
            "연도": 2021,
        },
    })
    observation["근거ID목록"] = ["ev-1"]
    observation["참고자료여부"] = False
    observation["자동병합정책"] = "standard"
    observation["근거"] = "관리권한 직접값; 세계도시 사례 동향과 비교"
    observation["병합상태"] = "candidate"
    observation["병합차단사유"] = ""

    payload = compare_g4_reference_gate({"snapshot_version": 1, "data": raw})

    assert payload["g4_reference_blockers"]["before"] == 1
    assert payload["g4_reference_blockers"]["after"] == 0
    assert payload["comparison"]["visual_output_row_delta"] == 1
    assert payload["comparison"]["visual_output_duplicate_row_delta"] == 0
    assert payload["comparison"]["llm_call_delta"] == 0


def test_g4_reference_refinement_keeps_external_comparison_row_blocked() -> None:
    raw = _raw()
    observation = raw["chart_observations"][0]
    observation.update({
        "대상시트": "regional_conditions",
        "제목": "전국 및 서울 가구 수 추이",
        "항목": "전국",
        "단위": "천가구",
        "연도": 2021,
        "값": 20400,
        "판독필드": {
            "값근거": "표셀",
            "지표범주": "인문사회",
            "지표명": "가구 수",
            "항목": "전국",
            "연도": 2021,
        },
        "근거ID목록": ["ev-1"],
        "참고자료여부": False,
        "자동병합정책": "standard",
        "근거": "서울시 직접값과 전국 비교",
        "병합상태": "candidate",
        "병합차단사유": "",
    })

    payload = compare_g4_reference_gate({"snapshot_version": 1, "data": raw})

    assert payload["g4_reference_blockers"]["before"] == 0
    assert payload["g4_reference_blockers"]["after"] == 1
    assert payload["comparison"]["visual_output_row_delta"] == -1


def test_g4_reference_refinement_keeps_explicit_reference_blocked() -> None:
    raw = _raw()
    observation = raw["chart_observations"][0]
    observation["근거ID목록"] = ["ev-1"]
    observation["참고자료여부"] = True
    observation["참고자료근거"] = "목차"
    observation["자동병합정책"] = "block_auto_merge"
    observation["병합상태"] = "candidate"
    observation["병합차단사유"] = ""

    payload = compare_g4_reference_gate({"snapshot_version": 1, "data": raw})

    assert payload["g4_reference_blockers"]["before"] == 1
    assert payload["g4_reference_blockers"]["after"] == 1
    assert payload["comparison"]["visual_output_row_delta"] == 0


def test_exact_object_resolution_ab_corrects_and_isolates_without_llm() -> None:
    raw = _raw()
    first = raw["chart_observations"][0]
    first.update({
        "페이지": 10,
        "제목": "그림 2-61 관리권한 배출량",
        "근거ID목록": ["ev-60"],
        "근거ID": "ev-60",
        "병합상태": "candidate",
        "병합차단사유": "",
    })
    mixed = {**first, "제목": "그림 2-61 배출량 / 표 2-35 관리권한 배출량"}
    raw["chart_observations"] = [first, mixed]
    raw["document_objects"] = [
        {
            "object_id": "obj-60",
            "object_type": "chart",
            "page_number": 10,
            "number": "그림 2-60",
            "caption": "그림 2-60 다른 배출량",
            "metadata": {"evidence_id": "ev-60", "final_status": "extracted"},
        },
        {
            "object_id": "obj-61",
            "object_type": "chart",
            "page_number": 10,
            "number": "그림 2-61",
            "caption": "그림 2-61 관리권한 배출량",
            "metadata": {"evidence_id": "ev-61", "final_status": "extracted"},
        },
        {
            "object_id": "obj-table",
            "object_type": "table",
            "page_number": 10,
            "number": "표 2-35",
            "caption": "표 2-35 관리권한 배출량",
            "metadata": {"evidence_id": "ev-table", "final_status": "extracted"},
        },
    ]

    payload = compare_exact_object_resolution({"snapshot_version": 1, "data": raw})

    assert payload["before"]["metrics"]["evidence_corrected_count"] == 0
    assert payload["after"]["metrics"]["evidence_corrected_count"] == 1
    assert payload["after"]["metrics"]["evidence_match_counts"]["mixed_objects"] == 1
    assert payload["comparison"]["corrected_evidence_delta"] == 1
    assert payload["comparison"]["llm_call_delta"] == 0


def test_visual_field_composition_ab_completes_exact_evidence_without_llm() -> None:
    raw = _raw()
    observation = raw["chart_observations"][0]
    observation.update({
        "대상시트": "emissions_forecast",
        "제목": "BAU 부문별 배출전망",
        "캡션": "그림 4-1 BAU 부문별 배출전망",
        "항목": "건물",
        "연도": None,
        "단위": "",
        "X축": {"labels": ["2030"]},
        "Y축": {"unit": "천톤CO2eq"},
        "범례목록": ["건물"],
        "판독필드": {"값근거": "명시라벨"},
        "근거ID": "ev-1",
        "근거ID목록": ["ev-1"],
        "병합상태": "candidate",
        "병합차단사유": "G3 1차 키 누락(시나리오, 부문, 연도)",
    })
    raw["document_objects"][0]["caption"] = "그림 4-1 BAU 부문별 배출전망"

    payload = compare_visual_field_composition({"snapshot_version": 1, "data": raw})

    before = payload["before"]["metrics"]
    after = payload["after"]["metrics"]
    assert before["visual_output_rows"] == 0
    assert after["visual_output_rows"] == 1
    assert after["field_composition_status_counts"]["complete"] == 1
    assert after["field_composition_fill_counts"]["시나리오"] == 1
    assert payload["comparison"]["g3_missing_key_blocker_delta"] == -1
    assert payload["comparison"]["visual_output_duplicate_row_delta"] == 0
    assert payload["comparison"]["llm_call_delta"] == 0
