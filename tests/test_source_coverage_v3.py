"""수입/투자, 제한적 차량 별칭, 공란/총계, 사업명 포함 반복 머리글의 회귀 계약."""
from copy import deepcopy

import pytest

from utils.document_objects import DocumentObject
from utils.source_coverage import compare, itemize, read_excel, snapshot, source_snapshot


def money_table(caption="시설 투자계획", name="시설 개선"):
    return DocumentObject("money", "table", 7, 1, caption=caption,
        rows=[["사업명", "구분", "2024", "2025", "합계"],
              [name, "계", "1000", "1100", "2100"]],
        metadata={"unit": "백만원", "header_row_count": 1, "evidence_id": "ev-money"})


def facts(journal):
    return [r for r in journal if "identity" in r]


def test_revenue_excluded_from_comparison_but_original_value_and_fact_id_preserved():
    source = money_table()
    before, _ = compare([source], "doc", {})
    source.caption = "전력자원 도입 수익금"
    source.rows[1][0] = "시설 개선"  # 사업명은 고정; 제목에 명시된 역할만 변경
    journal, summary = compare([source], "doc", {})
    assert summary["comparable_facts"] == 0 and facts(journal) == []
    assert {r["candidate_id"] for r in journal} == {r["candidate_id"] for r in before}
    assert all(r["status"] == "excluded" and r["reason"] == "non_investment_revenue_outside_budget_scope" for r in journal)
    assert journal[0]["raw_value"] == "1000" and journal[0]["raw_unit"] == "백만원"
    assert journal[0]["source"]["row"] == 2 and journal[0]["source"]["column"] == 3
    assert journal[0]["recall_policy"] == "excluded_from_recall"


def test_settlement_named_project_with_investment_caption_is_not_a_revenue_exclusion():
    journal, _ = compare([money_table("지원 투자계획", "정산금 지원 사업")], "doc", {})
    assert all(r["status"] == "missing_candidate" for r in facts(journal))


def test_duplicate_revenue_objects_keep_one_stable_record_and_all_source_aliases():
    source = money_table("발전 수익금")
    duplicate = deepcopy(source)
    duplicate.object_id = "revenue-alias"
    journal, _ = compare([source, duplicate], "doc", {})
    assert len(journal) == len({r["candidate_id"] for r in journal}) == 2
    assert all("revenue-alias" in r["source"]["aliases"] and len(r["source_instances"]) == 2 for r in journal)


@pytest.mark.parametrize("explicit_column", [False, True])
def test_conflicting_revenue_and_spending_context_is_held_not_silently_excluded(explicit_column):
    source = money_table("수익금 및 투자계획")
    if explicit_column:
        source.caption = "수익금"
        source.rows = [["사업명", "연도", "예산액", "예산단위"], ["Facility", "2024", "1000", "백만원"]]
    journal, summary = compare([source], "doc", {})
    assert summary["comparable_facts"] == 0
    assert all(r["status"] == "unassessable" and r["reason"] == "mixed_revenue_spending_context" for r in journal)


def vehicles(name="청소차(CNG, 전기, 수소) 도입"):
    source = money_table("차량 도입 투자계획", name)
    source.rows += [["", "국비", "400", "440", "840"],
                    ["", "시비", "600", "660", "1260"]]
    rows = [{"사업명": "청소차 도입", "연도": 2024, "재원구분": funding,
             "예산액": value, "예산단위": "백만원", "계획구분": "온실가스감축대책", "부문": "수송",
             "출처페이지": [6, 7, 8]} for funding, value in [("합계", 1000), ("국비", 400), ("시비", 600)]]
    return source, {"financial_plan": rows}


def current_fact(objects, data):
    journal, _ = compare(objects, "doc", data)
    return next(r for r in facts(journal) if r["identity"]["year"] == 2024)


def test_known_vehicle_qualifier_omission_requires_source_and_two_funding_components():
    source, data = vehicles()
    original = deepcopy(data)
    result = current_fact([source], data)
    assert result["status"] == "covered" and data == original
    link = result["stages"]["excel"]["matches"][0]["identity_link"]
    assert link["method"] == "vehicle_fuel_qualifier_omission"
    assert len(link["funding_proof"]) == 2
    assert result["source"]["funding_components"][0]["source_row"] == 3


@pytest.mark.parametrize("case", ["different_page", "different_year", "missing_component", "wrong_component",
    "conflicting_component", "different_context", "inconsistent_source_sum", "one_source_component"])
def test_vehicle_alias_does_not_bypass_missing_or_contradictory_evidence(case):
    source, data = vehicles()
    rows = data["financial_plan"]
    if case == "different_page":
        for row in rows:
            row["출처페이지"] = 99
    elif case == "different_year":
        for row in rows:
            row["연도"] = 2026
    elif case == "missing_component":
        rows.pop()
    elif case == "wrong_component":
        rows[1]["예산액"] = 999
    elif case == "conflicting_component":
        rows.append({**rows[1], "예산액": 999})
    elif case == "different_context":
        rows[1]["계획구분"] = "다른계획"
    elif case == "inconsistent_source_sum":
        source.rows[2][2] = "999"
    else:
        source.rows.pop()
    assert current_fact([source], data)["status"] != "covered"


@pytest.mark.parametrize("name", ["청소차(동부권) 도입", "청소차(1단계) 도입", "시설(CNG,전기) 도입", "청소차(전기) 도입"])
def test_arbitrary_regional_phase_or_single_fuel_qualifiers_are_not_deleted(name):
    source, data = vehicles(name)
    assert current_fact([source], data)["status"] == "missing_candidate"


def test_competing_qualified_vehicle_subjects_cannot_share_one_omitted_name():
    source, data = vehicles("청소차(CNG,전기) 도입")
    other = deepcopy(source)
    other.object_id = "other"
    other.page_number = 8
    other.rows[1][0] = "청소차(CNG,수소) 도입"
    journal, _ = compare([source, other], "doc", data)
    assert all(r["status"] != "covered" for r in facts(journal))


def test_alias_total_conflict_and_wrong_dimension_remain_review_cases():
    source, data = vehicles()
    data["financial_plan"][0]["예산액"] = 999
    assert current_fact([source], data)["status"] == "conflict_review"
    data["financial_plan"][0].update({"예산액": 1000, "예산단위": "개"})
    assert current_fact([source], data)["status"] == "unit_review"


def test_annual_blanks_are_excluded_before_missing_unit_and_total_is_preserved():
    source = money_table()
    source.metadata.pop("unit")
    source.rows[1] = ["시설 개선", "계", "", "", "2100"]
    journal, summary = compare([source], "doc", {})
    assert summary["comparable_facts"] == 0 and len(journal) == 1
    assert journal[0]["status"] == "excluded"
    assert journal[0]["reason"] == "source_annual_values_blank_total_only"
    assert journal[0]["aggregate_values"] == [{"source_column":5,"header":"합계","raw_value":"2100"}]
    assert journal[0]["raw_row"][-1] == "2100"


def test_explicit_annual_zero_is_not_an_empty_row_or_a_period_total():
    source = money_table()
    source.rows[1] = ["시설 개선", "계", "0", "", "0"]
    journal, _ = compare([source], "doc", {})
    assert len(facts(journal)) == 1 and facts(journal)[0]["raw_value"] == "0"
    assert facts(journal)[0]["identity"]["year"] == 2024


def test_business_labelled_repeated_header_changes_years_without_becoming_a_value():
    cover = DocumentObject("cover", "text", 1, 1, text="테스트시 기본계획(2024~2033)")
    source = money_table()
    source.rows = [["사업명", "구분", "’30", "’31", "합계"], ["기존", "계", "10", "20", "30"],
                   ["후속 사업", "구분", "’24", "’25", "합계"], ["후속 사업", "계", "40", "50", "90"]]
    journal, summary = compare(source_snapshot([cover, source]), "doc", {})
    assert [(r["identity"]["name"],r["identity"]["year"],r["raw_value"]) for r in facts(journal)] == [
        ("기존",2030,"10"),("기존",2031,"20"),("후속 사업",2024,"40"),("후속 사업",2025,"50")]
    excluded = [r for r in journal if r["reason"] == "repeated_header_with_merged_business_label"]
    assert len(excluded) == 2 and all(r["status"] == "excluded" for r in excluded)
    assert excluded[0]["source_row"] == 3 and excluded[0]["source_column"] == 3
    assert facts(journal)[-1]["source"]["year_header_row"] == 3


def test_numeric_costs_equal_to_year_numbers_are_not_mistaken_for_headers():
    source = money_table()
    source.rows[1] = ["시설 개선", "계", "2024", "2025", "4049"]
    journal, _ = compare([source], "doc", {})
    assert [r["raw_value"] for r in facts(journal)] == ["2024", "2025"]


def test_real_organizer_excel_and_source_snapshot_keep_cells_unchanged(tmp_path):
    from agents.organizer_agent import OrganizerAgent
    from agents.excel_agent import ExcelAgent
    source, data = vehicles()
    data["municipality_name"] = "테스트시"
    before = deepcopy(data)
    clean = OrganizerAgent().organize(data)
    path = ExcelAgent().write(clean, tmp_path / "vehicle.xlsx")
    saved = read_excel(path)
    result = current_fact(source_snapshot([source]), saved)
    assert result["status"] == "covered"
    assert result["stages"]["excel"]["matches"][0]["cells"]["예산액"].startswith("'11_재정투자계획'!")
    journal, _ = compare(source_snapshot([source]), "doc", saved, raw=snapshot(before), cleaned=snapshot(clean))
    assert next(r for r in facts(journal) if r["identity"]["year"] == 2024)["stages"]["cleaned"]["status"] == "covered"
    assert saved == read_excel(path)
