"""분류 규칙 V2. 수치만 같은 오연결/단위 자동 동일시를 차단한다."""
from copy import deepcopy

import pytest

from utils.document_objects import DocumentObject
from utils.source_coverage import compare, itemize, source_snapshot


def budget(subjects=("Child retrofit",), caption="표 2-3 Public retrofit 투자계획"):
    return DocumentObject("b-table", "table", 7, 1, caption=caption,
        rows=[["사업명", "연도", "예산액", "예산단위", "재원구분"]] +
             [[name, "2025", "500", "백만원", "계"] for name in subjects],
        bbox=(10, 100, 300, 200), metadata={"evidence_id": "ev-b-table", "header_row_count": 1})


def row(**extra):
    return {"사업명": "Public retrofit 투자계획", "연도": 2025, "예산액": 500,
            "예산단위": "백만원", "재원구분": "합계", "출처페이지": 7, **extra}


def statuses(obj, rows):
    journal, _ = compare([obj], "doc", {"financial_plan": rows})
    return [r for r in journal if "identity" in r]


def test_single_subject_caption_is_an_explicit_identity_link_not_fuzzy_name():
    fact = statuses(budget(), [row()])[0]
    assert fact["status"] == "covered"
    assert fact["stages"]["excel"]["matches"][0]["identity_link"]["method"] == "single_subject_table_caption"


@pytest.mark.parametrize("changes", [{"출처페이지": 99}, {"연도": 2026},
    {"사업명": "Unrelated retrofit", "출처페이지": 7}, {"재원구분": "국비"}])
def test_equal_numbers_do_not_override_wrong_source_year_project_or_funding(changes):
    assert statuses(budget(), [row(**changes)])[0]["status"] == "missing_candidate"


def test_multi_subject_caption_without_a_subproject_binding_is_held():
    facts = statuses(budget(("East", "West")), [row()])
    assert [r["status"] for r in facts] == ["ambiguous_review", "ambiguous_review"]


def test_subproject_column_binds_one_child_and_excludes_the_other_child():
    facts = statuses(budget(("East", "West")), [row(부문="East"), row(부문="West", 예산액=700)])
    assert [r["status"] for r in facts] == ["covered", "conflict_review"]
    assert facts[0]["stages"]["excel"]["matches"][0]["identity_link"]["method"] == "caption_and_subproject_field"


def test_two_different_values_of_a_caption_linked_fact_remain_a_conflict():
    assert statuses(budget(), [row(), row(예산액=999)])[0]["status"] == "conflict_review"


def test_subproject_identity_in_sector_column_is_not_a_competing_sector_enum():
    rows = [row(사업명="Child retrofit", 부문="Buildings"), row(부문="Child retrofit")]
    fact = statuses(budget(), rows)[0]
    assert fact["status"] == "covered"
    assert len(fact["stages"]["excel"]["matches"]) == 2  # 기존 중복은 기록만 하고 수정하지 않음


def test_linked_caption_with_wrong_unit_is_review_not_covered():
    assert statuses(budget(), [row(예산단위="개")])[0]["status"] == "unit_review"


def hierarchy_fixture():
    activity = DocumentObject("activity", "table", 10, 1, caption="표 3-1 City parks 추진계획",
        rows=[["사업명", "연도", "활동량", "활동단위"], ["East park", "2024", "45000", "㎡"]],
        metadata={"header_row_count": 1})
    reduction = DocumentObject("reduction", "table", 11, 1, caption="표 3-2 City parks 온실가스 감축효과",
        rows=[["사업명", "연도", "예상감축량", "단위"], ["East park", "2024", "31.1", "tCO2"]],
        metadata={"header_row_count": 1})
    data = {"annual_implementation": [{"사업명": "East park", "관리번호": "F1-1", "연도": 2024,
              "목표물량": 45000, "목표단위": "㎡", "출처페이지": 10}],
            "quantitative_reductions": [{"사업명": "Overall city green programme", "관리번호": "F1-1", "연도": 2024,
              "활동량": 45000, "활동단위": "㎡", "예상감축량": 31.1, "단위": "tCO2eq", "출처페이지": 11}]}
    return [activity, reduction], data


def reduction_status(objects, data):
    journal, _ = compare(objects, "doc", data)
    return next(r for r in journal if r.get("field") == "예상감축량")


def test_annual_activity_code_link_exposes_unit_change_without_claiming_coverage():
    objects, data = hierarchy_fixture()
    original = deepcopy(data)
    result = reduction_status(objects, data)
    assert result["status"] == "unit_review"
    ref = result["stages"]["excel"]["ambiguous"][0]
    assert ref["identity_link"]["method"] == "source_activity_and_annual_code"
    assert ref["expected_unit"] == "tCO2"
    assert data == original


def test_hierarchy_link_with_matching_unit_can_verify_the_same_fact():
    objects, data = hierarchy_fixture()
    data["quantitative_reductions"][0]["단위"] = "tCO2"
    assert reduction_status(objects, data)["status"] == "covered"


@pytest.mark.parametrize("wrong_evidence", ["activity", "annual_page", "target_page", "peer_caption", "management_code"])
def test_management_code_alone_is_never_a_global_identity(wrong_evidence):
    objects, data = hierarchy_fixture()
    if wrong_evidence == "activity":
        data["quantitative_reductions"][0]["활동량"] = 12345
    elif wrong_evidence == "annual_page":
        data["annual_implementation"][0]["출처페이지"] = 99
    elif wrong_evidence == "target_page":
        data["quantitative_reductions"][0]["출처페이지"] = 99
    elif wrong_evidence == "peer_caption":
        objects[0].caption = "표 3-1 Different project 추진계획"
    else:
        data["quantitative_reductions"][0]["관리번호"] = "OTHER"
    assert reduction_status(objects, data)["status"] == "missing_candidate"


def unit_table():
    return DocumentObject("unit-table", "table", 7, 1, caption="표 4-1 Library retrofit 투자계획",
        rows=[["사업명", "2025", "2026"], ["Library", "10", "20"]],
        bbox=(10, 100, 300, 200), metadata={"header_row_count": 1})


def unit_block(value="백만원", bbox=(230, 83, 290, 95)):
    return DocumentObject("unit-text", "text", 7, 1, text=f"(단위: {value})", bbox=bbox)


def test_explicit_native_unit_is_spatially_linked_and_survives_source_snapshot():
    objects = [unit_table(), unit_block()]
    captured = source_snapshot(objects)
    facts, _, _ = itemize(captured, "doc")
    assert len(facts) == 2
    assert facts[0]["raw_unit"] == "백만원"
    assert facts[0]["source"]["unit_origin"] == "spatial_explicit_native_unit"
    assert facts[0]["source"]["unit_evidence"][0]["object_id"] == "unit-text"


@pytest.mark.parametrize("blocks", [[], [unit_block(bbox=(230, 1, 290, 10))],
    [unit_block(), unit_block("억원")]])
def test_absent_far_or_conflicting_units_are_not_inferred_from_output(blocks):
    facts, diagnostics, _ = itemize([unit_table(), *blocks], "doc")
    assert facts == []
    assert any(r["reason"] == "explicit_unit_evidence_required" for r in diagnostics)


def test_currency_unit_cannot_turn_an_activity_measure_into_a_budget():
    obj = unit_table()
    obj.rows = [["사업명", "연도", "활동량"], ["Library", "2025", "10"]]
    obj.caption = "활동 계획"
    facts, diagnostics, _ = itemize([obj, unit_block()], "doc")
    assert facts == [] and diagnostics[0]["reason"] == "source_unit_metric_conflict"


def test_render_proxy_is_excluded_but_embedded_image_remains_held():
    proxy = DocumentObject("proxy", "image", 7, 1, metadata={"render_proxy": True})
    embedded = DocumentObject("embedded", "image", 7, 2)
    journal, summary = compare(source_snapshot([proxy, embedded]), "doc", {})
    assert [(r["status"], r["reason"]) for r in journal] == [("excluded", "page_render_proxy"), ("unassessable", "image_or_caption_only")]
    assert journal[0]["recall_policy"] == "excluded_from_recall"
    assert summary["coverage_counts"]["excluded_diagnostics_not_fact_count"] == 1


def test_empty_numeric_cells_and_empty_merged_rows_are_not_recall_candidates():
    obj = budget()
    obj.rows = [["사업명", "예산단위", "2025", "2026"], ["Library", "백만원", "0", ""],
                ["", "", "", ""]]
    journal, _ = compare([obj], "doc", {})
    assert sum(r["status"] == "missing_candidate" for r in journal) == 1  # 명시 0만 비교
    assert sum(r["status"] == "excluded" for r in journal) == 2


def test_performance_schema_and_caption_only_business_context_have_distinct_reasons():
    performance = DocumentObject("performance", "table", 7, 1, caption="표 1-1 2023년 이행성과",
        rows=[["사업명", "성과지표 달성도"], ["Library", "배터리 그림"]], metadata={"header_row_count": 1})
    caption = DocumentObject("caption-context", "table", 8, 1, caption="표 1-2 Library 온실가스 감축효과",
        rows=[["성과지표", "2025"], ["LED(tCO2)", "10"]], metadata={"header_row_count": 1})
    journal, _ = compare([performance, caption], "doc", {})
    assert [(r["status"], r["reason"]) for r in journal] == [
        ("excluded", "outside_supported_performance_schema"),
        ("unassessable", "caption_business_context_required")]
