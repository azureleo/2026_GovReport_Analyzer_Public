from __future__ import annotations

from utils.evidence_merge import (
    build_evidence_catalog,
    match_evidence,
    normalize_evidence_ids,
    resolve_observation_evidence,
)


def _object(object_id: str, evidence_id: str, status: str = "extracted") -> dict:
    return {
        "object_id": object_id,
        "object_type": "chart",
        "metadata": {
            "evidence_id": evidence_id,
            "final_status": status,
        },
    }


def test_evidence_ids_are_normalized_without_reordering() -> None:
    assert normalize_evidence_ids(["ev-2", "ev-1"], "ev-2", "") == ["ev-2", "ev-1"]


def test_exact_match_requires_one_extracted_object() -> None:
    catalog = build_evidence_catalog([_object("obj-1", "ev-1")])

    result = match_evidence(["ev-1"], catalog)

    assert result.exact is True
    assert result.status == "exact"
    assert result.object_ids == ("obj-1",)


def test_missing_multiple_unknown_and_non_extracted_are_not_exact() -> None:
    catalog = build_evidence_catalog([
        _object("obj-1", "ev-1"),
        _object("obj-2", "ev-1"),
        _object("obj-3", "ev-review", "needs_review"),
    ])

    assert match_evidence([], catalog).status == "missing"
    assert match_evidence(["ev-1", "ev-2"], catalog).status == "multiple_ids"
    assert match_evidence(["ev-unknown"], catalog).status == "unknown"
    assert match_evidence(["ev-1"], catalog).status == "multiple_objects"
    assert match_evidence(["ev-review"], catalog).status == "object_not_extracted"


def test_catalog_deduplicates_same_object_from_object_and_triage_ledgers() -> None:
    catalog = build_evidence_catalog(
        [_object("obj-1", "ev-1")],
        [{
            "object_id": "obj-1",
            "evidence_id": "ev-1",
            "final_status": "needs_review",
        }],
    )

    assert match_evidence("ev-1", catalog).status == "exact"


def test_caption_proxy_and_image_with_same_evidence_are_one_physical_object() -> None:
    caption = _object("p10_chart_1", "ev-visual")
    caption.update({
        "page_number": 10,
        "object_type": "chart",
        "caption": "그림 2-1 배출 전망",
        "metadata": {
            **caption["metadata"],
            "caption_only": True,
        },
    })
    image = _object("p10_image_1", "ev-visual")
    image.update({
        "page_number": 10,
        "object_type": "image",
        "caption": "그림 2-1 배출 전망",
    })

    result = match_evidence("ev-visual", build_evidence_catalog([caption, image]))

    assert result.status == "exact"
    assert set(result.object_ids) == {"p10_chart_1", "p10_image_1"}


def test_transport_proxy_in_legacy_triage_is_merged_without_downgrading_status() -> None:
    chart = _object("p140_chart_1", "ev-p140-visual")
    chart.update({
        "page_number": 140,
        "object_type": "chart",
        "caption": "그림 4-2 온실가스 감축 경로",
    })
    triage_proxy = {
        "object_id": "p140_image_1",
        "evidence_id": "ev-p140-visual",
        "object_type": "image",
        "page_number": 140,
        "action": "transport_only",
        "reasons": ["page_render_transport"],
        "final_status": "not_relevant",
    }

    result = match_evidence(
        "ev-p140-visual",
        build_evidence_catalog([chart], [triage_proxy]),
    )

    assert result.status == "exact"
    assert result.final_statuses == ("extracted",)
    assert set(result.object_ids) == {"p140_chart_1", "p140_image_1"}


def test_missing_native_table_triage_row_is_an_alias_of_the_canonical_table() -> None:
    table = {
        "object_id": "p108_table_2",
        "object_type": "table",
        "page_number": 108,
        "number": "표 3-8",
        "caption": "표 3-8 부문별 배출량",
        "metadata": {
            "evidence_id": "ev-p108-table",
            "final_status": "extracted",
        },
    }
    stale_triage = {
        "object_id": "p108_table_3",
        "evidence_id": "ev-p108-table",
        "object_type": "table",
        "page_number": 108,
        "number": "표 3-8",
        "caption": "표 3-8 부문별 배출량",
        "reasons": ["native_table_missing"],
        "final_status": "extracted",
    }

    result = match_evidence(
        "ev-p108-table",
        build_evidence_catalog([table], [stale_triage]),
    )

    assert result.status == "exact"
    assert set(result.object_ids) == {"p108_table_2", "p108_table_3"}


def test_same_evidence_id_does_not_merge_independent_objects_without_physical_support() -> None:
    left = _object("p20_chart_1", "ev-collision")
    left.update({
        "page_number": 20,
        "caption": "전력 소비 추이",
        "bbox": [20, 100, 250, 300],
    })
    right = _object("p20_chart_2", "ev-collision")
    right.update({
        "page_number": 20,
        "caption": "자동차 등록 추이",
        "bbox": [300, 100, 550, 300],
    })

    result = match_evidence("ev-collision", build_evidence_catalog([left, right]))

    assert result.status == "multiple_objects"


def _numbered_object(
    object_id: str,
    evidence_id: str,
    number: str,
    caption: str,
    *,
    page: int = 116,
    physical_id: str = "",
) -> dict:
    row = _object(object_id, evidence_id)
    row.update({
        "page_number": page,
        "number": number,
        "caption": caption,
    })
    if physical_id:
        row["metadata"]["physical_object_id"] = physical_id
    return row


def test_explicit_figure_number_corrects_wrong_carried_evidence_id() -> None:
    catalog = build_evidence_catalog([
        _numbered_object(
            "p116_chart_1", "ev-figure-60", "그림 2-60", "그림 2-60 인구 추이"
        ),
        _numbered_object(
            "p116_image_2", "ev-figure-61", "그림 2-61", "그림 2-61 가구 추이"
        ),
    ])

    result = resolve_observation_evidence({
        "페이지": 116,
        "제목": "[그림 2-61] 가구 추이",
        "근거ID목록": ["ev-figure-60"],
    }, catalog)

    assert result.exact is True
    assert result.evidence_id == "ev-figure-61"
    assert result.object_ids == ("p116_image_2",)
    assert result.method == "object_number_explicit"
    assert result.corrected is True


def test_multiple_object_numbers_are_isolated_instead_of_forced_to_one_id() -> None:
    catalog = build_evidence_catalog([
        _numbered_object(
            "p132_table_1", "ev-table-35", "표 2-35", "표 2-35 에너지 소비",
            page=132,
        ),
        _numbered_object(
            "p132_figure_1", "ev-figure-87", "그림 2-87", "그림 2-87 에너지 흐름",
            page=132,
        ),
    ])

    result = resolve_observation_evidence({
        "페이지": 132,
        "제목": "[그림 2-87] 에너지 흐름 / [표 2-35] 에너지 소비",
        "근거ID목록": ["ev-figure-87"],
    }, catalog)

    assert result.status == "mixed_objects"
    assert result.exact is False
    assert set(result.evidence_ids) == {"ev-table-35", "ev-figure-87"}
    assert result.method == "mixed_object_numbers"


def test_explicit_number_field_wins_over_caption_derived_number() -> None:
    catalog = build_evidence_catalog([
        _numbered_object(
            "p130_table_1", "ev-table", "", "[그림 2-84] 지역내총생산",
            page=130,
        ),
        _numbered_object(
            "p130_image_1", "ev-figure", "그림 2-84", "그림 2-84 지역내총생산",
            page=130,
        ),
    ])

    result = resolve_observation_evidence({
        "페이지": 130,
        "제목": "그림 2-84 지역내총생산",
        "근거ID목록": ["ev-table"],
    }, catalog)

    assert result.exact is True
    assert result.evidence_id == "ev-figure"
    assert result.object_ids == ("p130_image_1",)


def test_unique_caption_corrects_evidence_when_number_is_not_in_title() -> None:
    catalog = build_evidence_catalog([
        _numbered_object(
            "p133_chart_1", "ev-domestic", "그림 2-89",
            "그림 2-89 국내 1차에너지 원별 공급 현황", page=133,
            physical_id="physical:domestic",
        ),
        _numbered_object(
            "p133_chart_2", "ev-seoul", "그림 2-90",
            "그림 2-90 서울 1차에너지 원별 공급 현황", page=133,
            physical_id="physical:seoul",
        ),
    ])

    result = resolve_observation_evidence({
        "페이지": 133,
        "제목": "국내 1차에너지 원별 공급 현황",
        "근거ID목록": ["ev-seoul"],
        "물리객체ID목록": ["physical:seoul"],
    }, catalog)

    assert result.exact is True
    assert result.evidence_id == "ev-domestic"
    assert result.method == "caption_unique"
    assert result.corrected is True


def test_source_object_id_disambiguates_multiple_carried_evidence_ids() -> None:
    catalog = build_evidence_catalog([
        _numbered_object("p20_chart_1", "ev-1", "", "배출량", page=20),
        _numbered_object("p20_chart_2", "ev-2", "", "흡수량", page=20),
    ])

    result = resolve_observation_evidence({
        "페이지": 20,
        "제목": "값",
        "source_evidence_ids": ["ev-1", "ev-2"],
        "source_object_ids": ["p20_chart_2"],
    }, catalog)

    assert result.exact is True
    assert result.evidence_id == "ev-2"
    assert result.method == "source_object_id"
