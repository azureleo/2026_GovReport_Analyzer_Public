from __future__ import annotations
from copy import deepcopy

from utils.visual_contract import (
    comparison_equal,
    normalize_quantity,
    normalize_visual_table_rows,
    quantities_conflict,
)


def test_comparison_normalizes_harmless_spacing_and_unit_typography() -> None:
    assert comparison_equal("가락시장 현대화", "가락시장현대화", "항목")
    assert comparison_equal("천 톤CO₂eq.", "천톤CO2eq", "단위")


def test_period_is_preserved_instead_of_expanded_to_wrong_year() -> None:
    rows = normalize_visual_table_rows([{
        "연도": "21~30년",
        "항목": "강수강도",
        "값": 17.1,
        "단위": "mm/일",
        "fields": {"값근거": "명시라벨"},
    }], chart_type="막대", title="강수강도 전망")

    assert rows[0]["연도"] is None
    assert rows[0]["fields"]["기간원문"] == "21~30년"


def test_visible_negative_sign_is_restored() -> None:
    rows = normalize_visual_table_rows([{
        "연도": 2030,
        "항목": "감축률",
        "값": 17,
        "단위": "%",
        "fields": {"값근거": "명시라벨", "원문표기": "-17%"},
    }], chart_type="막대")

    assert rows[0]["값"] == -17
    assert rows[0]["fields"]["부호보정근거"] == "-17%"


def test_nested_quantitative_fields_become_atomic_rows() -> None:
    rows = normalize_visual_table_rows([{
        "연도": 2030,
        "항목": "총괄",
        "값": None,
        "단위": "천톤CO2eq",
        "fields": {
            "값근거": "표셀",
            "시나리오": "목표",
            "BAU": 100,
            "감축량": 30,
            "감축률": "30%",
        },
    }], chart_type="표", title="감축 목표")

    assert [row["값"] for row in rows] == [100, 30, 30]
    assert {row["fields"]["값역할"] for row in rows} == {
        "배출전망", "목표감축량", "감축률",
    }
    for row in rows:
        assert "BAU" not in row["fields"]
        assert "감축량" not in row["fields"]
        assert "감축률" not in row["fields"]


def test_quantity_scale_is_normalized_without_losing_reported_value() -> None:
    quantity = normalize_quantity(31.5, "천tCO2eq")

    assert quantity["raw_value"] == 31.5
    assert quantity["raw_unit"] == "천tCO2eq"
    assert quantity["value"] == 31500
    assert quantity["unit"] == "tCO2eq"
    assert quantities_conflict(31.5, "천tCO2eq", 31500, "tCO2eq") is False


def test_visual_rows_keep_raw_and_normalized_quantity_metadata() -> None:
    rows = normalize_visual_table_rows([{
        "연도": 2030,
        "항목": "합계",
        "값": 31.5,
        "단위": "천tCO2eq",
        "fields": {"값근거": "명시라벨"},
    }], chart_type="막대")

    fields = rows[0]["fields"]
    assert fields["원문값"] == 31.5
    assert fields["원문단위"] == "천tCO2eq"
    assert fields["정규화값"] == 31500
    assert fields["정규화단위"] == "tCO2eq"


def test_null_functions_and_composition_roles_are_not_deduplicated():
    functions = [{"항목": "전체회의", "값": None, "fields": {
        "정보유형": "조직기능", "역할": "전체회의", "값원문": text}}
        for text in ("정책 심의", "계획 수립 심의", "점검평가")]
    counts = [{"항목": "위원회", "값": 2, "단위": "명", "fields": {
        "정보유형": "기관구성", "항목원문": role}} for role in ("당연", "위촉")]
    rows = functions + counts
    before = deepcopy(rows)
    result = normalize_visual_table_rows(rows + deepcopy(rows), chart_type="표")
    assert len(result) == 5
    assert rows == before


def test_same_value_with_different_context_is_preserved_for_conflict_review():
    row = {"항목": "위원회", "값": 2, "단위": "명", "fields": {"담당부서": "부서A"}}
    other = deepcopy(row); other['fields']['담당부서'] = '부서B'
    assert len(normalize_visual_table_rows([row, other], chart_type='표')) == 2
