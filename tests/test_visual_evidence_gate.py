from __future__ import annotations

import json

import config
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent
from utils.evidence_merge import build_evidence_catalog


def _object(object_id: str, evidence_id: str, status: str = "extracted") -> dict:
    return {
        "object_id": object_id,
        "object_type": "chart",
        "page_number": 188,
        "metadata": {
            "evidence_id": evidence_id,
            "final_status": status,
        },
    }


def _observation(*, evidence_ids=None, value=100.0) -> dict:
    fields = {
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접",
        "연도": 2030,
    }
    evidence_ids = list(evidence_ids or [])
    return {
        "지자체명": "서울특별시",
        "페이지": 188,
        "대상시트": "emissions_management",
        "그래프유형": "표",
        "제목": "관리권한 배출량",
        "단위": "천톤CO2eq",
        "항목": "건물",
        "연도": 2030,
        "값": value,
        "신뢰도": "high",
        "반영여부": "검토",
        "근거": json.dumps(fields, ensure_ascii=False),
        "판독필드": fields,
        "근거ID": evidence_ids[0] if len(evidence_ids) == 1 else "",
        "근거ID목록": evidence_ids,
    }


def _organize(observation: dict | list[dict], objects: list[dict], text_rows=None) -> dict:
    raw = {
        "municipality_name": "서울특별시",
        "chart_observations": observation if isinstance(observation, list) else [observation],
        "document_objects": objects,
        "object_triage": [],
    }
    if text_rows is not None:
        raw["emissions_management"] = text_rows
    return OrganizerAgent().organize(raw)


def test_image_agent_propagates_evidence_and_defers_operational_merge(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    objects = [_object("obj-1", "ev-1")]
    catalog = build_evidence_catalog(objects)
    analysis = {
        "type": "chart_table",
        "target_sheet": "emissions_management",
        "chart_type": "표",
        "title": "관리권한 배출량",
        "unit": "천톤CO2eq",
        "confidence": "high",
        "page_number": 188,
        "municipality": "서울특별시",
        "source_evidence_ids": ["ev-1"],
        "table": [{
            "항목": "건물",
            "연도": 2030,
            "값": 100.0,
            "단위": "천톤CO2eq",
            "fields": {
                "관리부문": "건물",
                "세부부문": "공공",
                "직간접구분": "직접",
                "연도": 2030,
            },
        }],
    }

    result = ImageAgent()._merge_image_results(
        {}, [analysis], "서울특별시", evidence_catalog=catalog
    )

    assert result.get("emissions_management", []) == []
    candidate = result["chart_observations"][0]
    assert candidate["근거ID"] == "ev-1"
    assert candidate["근거매칭상태"] == "exact"
    assert candidate["병합상태"] == "candidate"
    assert candidate["반영여부"] == "검토"


def test_exact_evidence_match_is_merged_and_propagated_to_output(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)

    cleaned = _organize(_observation(evidence_ids=["ev-1"]), [_object("obj-1", "ev-1")])

    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["emissions_management"][0]["근거ID"] == "ev-1"
    assert cleaned["chart_observations"][0]["병합상태"] == "accept"
    assert cleaned["visual_inventory"][0]["근거ID"] == "ev-1"
    assert cleaned["visual_inventory"][0]["근거매칭상태"] == "exact"


def test_explicit_reference_flag_blocks_auto_merge_but_keeps_inventory(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    observation = _observation(evidence_ids=["ev-1"])
    observation.update({
        "참고자료여부": True,
        "참고자료근거": "OECD",
        "자동병합정책": "block_auto_merge",
    })

    cleaned = _organize(observation, [_object("obj-1", "ev-1")])

    assert cleaned["emissions_management"] == []
    assert cleaned["chart_observations"][0]["병합상태"] == "reject"
    assert "G4 참고자료/사례 판정" in cleaned["chart_observations"][0]["병합차단사유"]
    inventory = cleaned["visual_inventory"][0]
    assert inventory["참고자료여부"] is True
    assert inventory["참고자료근거"] == "OECD"
    assert inventory["자동병합정책"] == "block_auto_merge"


def test_missing_multiple_and_ambiguous_evidence_are_isolated(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    cases = [
        (_observation(evidence_ids=[]), [_object("obj-1", "ev-1")], "missing"),
        (_observation(evidence_ids=["ev-1", "ev-2"]), [_object("obj-1", "ev-1")], "multiple_ids"),
        (
            _observation(evidence_ids=["ev-1"]),
            [_object("obj-1", "ev-1"), _object("obj-2", "ev-1")],
            "multiple_objects",
        ),
    ]

    for observation, objects, expected_status in cases:
        cleaned = _organize(observation, objects)
        assert cleaned["emissions_management"] == []
        result = cleaned["chart_observations"][0]
        assert result["근거매칭상태"] == expected_status
        assert result["병합상태"] == "needs_review"
        assert result["병합차단사유"]


def test_organizer_reassigns_wrong_evidence_to_unique_numbered_object(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    monkeypatch.setattr(
        config, "VISUAL_EXACT_OBJECT_RESOLUTION_ENABLED", True, raising=False
    )
    observation = _observation(evidence_ids=["ev-60"])
    observation["제목"] = "그림 2-61 관리권한 배출량"
    observation["병합차단사유"] = "복수 근거 ID(2개)"
    objects = [
        {
            **_object("obj-60", "ev-60"),
            "number": "그림 2-60",
            "caption": "그림 2-60 다른 배출량",
        },
        {
            **_object("obj-61", "ev-61"),
            "number": "그림 2-61",
            "caption": "그림 2-61 관리권한 배출량",
        },
    ]

    cleaned = _organize(observation, objects)
    result = cleaned["chart_observations"][0]

    assert result["근거ID"] == "ev-61"
    assert result["근거객체ID"] == "obj-61"
    assert result["근거분리방식"] == "object_number_explicit"
    assert result["근거교정여부"] is True
    assert result["병합상태"] == "accept"
    assert cleaned["visual_inventory"][0]["근거분리방식"] == "object_number_explicit"


def test_organizer_keeps_mixed_table_and_figure_observation_in_review(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    monkeypatch.setattr(
        config, "VISUAL_EXACT_OBJECT_RESOLUTION_ENABLED", True, raising=False
    )
    observation = _observation(evidence_ids=["ev-figure"])
    observation["제목"] = "그림 2-87 배출량 / 표 2-35 관리권한 배출량"
    objects = [
        {
            **_object("obj-figure", "ev-figure"),
            "number": "그림 2-87",
            "caption": "그림 2-87 배출량",
        },
        {
            **_object("obj-table", "ev-table"),
            "object_type": "table",
            "number": "표 2-35",
            "caption": "표 2-35 관리권한 배출량",
        },
    ]

    cleaned = _organize(observation, objects)
    result = cleaned["chart_observations"][0]

    assert result["근거매칭상태"] == "mixed_objects"
    assert result["근거분리방식"] == "mixed_object_numbers"
    assert result["병합상태"] == "needs_review"
    assert "복수 표·그림 번호" in result["병합차단사유"]
    assert cleaned["emissions_management"] == []


def test_all_null_visual_value_is_isolated(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)

    cleaned = _organize(
        _observation(evidence_ids=["ev-1"], value=None),
        [_object("obj-1", "ev-1")],
    )

    assert cleaned["emissions_management"] == []
    result = cleaned["chart_observations"][0]
    assert result["병합상태"] == "needs_review"
    assert "전부 null" in result["병합차단사유"]


def test_text_conflict_is_isolated_but_exact_duplicate_is_verified(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    base = {
        "지자체명": "서울특별시",
        "인벤토리출처": "본문",
        "관리부문": "건물",
        "세부부문": "공공",
        "직간접구분": "직접",
        "연도": 2030,
        "배출량": 120.0,
        "단위": "천톤CO2eq",
    }

    conflict = _organize(
        _observation(evidence_ids=["ev-1"], value=100.0),
        [_object("obj-1", "ev-1")],
        [base],
    )
    duplicate = _organize(
        _observation(evidence_ids=["ev-1"], value=120.0),
        [_object("obj-1", "ev-1")],
        [base],
    )

    assert conflict["chart_observations"][0]["병합상태"] == "needs_review"
    assert "텍스트-시각 값 불일치" in conflict["chart_observations"][0]["병합차단사유"]
    assert duplicate["chart_observations"][0]["병합상태"] == "duplicate"
    assert len(duplicate["emissions_management"]) == 1


def test_explicit_graph_label_passes_but_axis_estimate_is_isolated(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)
    explicit = _observation(evidence_ids=["ev-1"])
    explicit["그래프유형"] = "막대"
    explicit["값근거"] = "명시라벨"
    explicit["판독필드"]["값근거"] = "명시라벨"

    estimated = _observation(evidence_ids=["ev-1"])
    estimated["그래프유형"] = "막대"
    estimated["값근거"] = "축추정"
    estimated["판독필드"]["값근거"] = "축추정"

    explicit_result = _organize(explicit, [_object("obj-1", "ev-1")])
    estimated_result = _organize(estimated, [_object("obj-1", "ev-1")])

    assert explicit_result["chart_observations"][0]["병합상태"] == "accept"
    assert estimated_result["emissions_management"] == []
    assert estimated_result["chart_observations"][0]["병합상태"] == "needs_review"
    assert "값근거" in estimated_result["chart_observations"][0]["병합차단사유"]


def test_explicit_total_mismatch_blocks_every_row_before_merge(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)

    def aggregate_observation(item: str, value: float, level: str) -> dict:
        row = _observation(evidence_ids=["ev-1"], value=value)
        row["항목"] = item
        row["값근거"] = "표셀"
        row["판독필드"].update({
            "관리부문": item,
            "세부부문": "",
            "값근거": "표셀",
            "집계수준": level,
            "합계그룹": "관리권한 배출량",
        })
        return row

    observations = [
        aggregate_observation("합계", 100.0, "합계"),
        aggregate_observation("건물", 60.0, "세부"),
        aggregate_observation("수송", 30.0, "세부"),
    ]
    cleaned = _organize(observations, [_object("obj-1", "ev-1")])

    assert cleaned["emissions_management"] == []
    assert all(row["병합상태"] == "needs_review" for row in cleaned["chart_observations"])
    assert all("합계 불일치" in row["병합차단사유"] for row in cleaned["chart_observations"])


def test_matching_explicit_total_and_details_pass_value_gate(monkeypatch) -> None:
    monkeypatch.setattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True, raising=False)

    def aggregate_observation(item: str, value: float, level: str) -> dict:
        row = _observation(evidence_ids=["ev-1"], value=value)
        row["항목"] = item
        row["값근거"] = "표셀"
        row["판독필드"].update({
            "관리부문": item,
            "세부부문": "",
            "값근거": "표셀",
            "집계수준": level,
            "합계그룹": "관리권한 배출량",
        })
        return row

    observations = [
        aggregate_observation("합계", 90.0, "합계"),
        aggregate_observation("건물", 60.0, "세부"),
        aggregate_observation("수송", 30.0, "세부"),
    ]
    cleaned = _organize(observations, [_object("obj-1", "ev-1")])

    assert len(cleaned["emissions_management"]) == 3
    assert all(row["값검증상태"] == "aggregate_pass" for row in cleaned["chart_observations"])
