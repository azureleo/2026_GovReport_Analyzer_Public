from __future__ import annotations

from agents.organizer_agent import OrganizerAgent


def test_forecast_method_prefers_long_markal_macro_keyword() -> None:
    # Given: MARKAL-MACRO와 MARKAL 원문이 각각 있는 전망 행
    raw = {
        "municipality_name": "서울특별시",
        "emissions_forecast": [
            {"시나리오": "BAU", "전망방법원문": "MARKAL-MACRO 모형 활용", "부문": "건물", "연도": 2030, "전망값": 10},
            {"시나리오": "BAU", "전망방법원문": "MARKAL 최적화", "부문": "수송", "연도": 2030, "전망값": 20},
            {"시나리오": "BAU", "전망방법원문": "ENPEP-Balance", "부문": "폐기물", "연도": 2030, "전망값": 30},
        ],
    }

    # When: organizer가 전망방법 코드를 보강하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 긴 MARKAL-MACRO 키워드가 MARKAL보다 먼저 적용된다.
    codes = {row["부문"]: row["전망방법코드"] for row in cleaned["emissions_forecast"]}
    assert codes["건물"] == "bottom_up_hybrid_MARKAL_MACRO"
    assert codes["수송"] == "bottom_up_optimization_MARKAL"
    assert codes["폐기물"] == "bottom_up_simulation_ENPEP"


def test_sector_raw_is_preserved_when_normalization_changes_value() -> None:
    # Given: 표준부문으로 정규화되는 원문 부문 표기가 들어오면
    raw = {
        "municipality_name": "서울특별시",
        "mitigation_projects": [{"부문": "도시건물", "사업명": "그린리모델링"}],
        "emissions_management": [{"관리부문": "도시건물", "세부부문": "공공", "연도": 2030, "배출량": 10}],
    }

    # When: organizer가 부문을 정규화하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 엑셀 미출력 내부 원문 필드가 보존된다.
    assert cleaned["mitigation_projects"][0]["부문"] == "건물"
    assert cleaned["mitigation_projects"][0]["부문원문"] == "도시건물"
    assert cleaned["emissions_management"][0]["관리부문"] == "건물"
    assert cleaned["emissions_management"][0]["관리부문원문"] == "도시건물"


def test_management_energy_source_is_not_remapped_to_transition_and_is_reported() -> None:
    # Given: 관리권한 인벤토리의 에너지원 표기가 관리부문에 들어오면
    raw = {
        "municipality_name": "서울특별시",
        "emissions_management": [
            {"관리부문": "에너지", "세부부문": "합계", "직간접구분": "", "연도": 2005, "배출량": 44173},
            {"관리부문": "전력", "세부부문": "가정", "연도": 2030, "배출량": 10},
        ],
    }

    # When: 관리권한 시트를 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 전환으로 바꾸지 않고 간접배출 점검 정보를 남긴다.
    sectors = {row["관리부문"]: row for row in cleaned["emissions_management"]}
    assert sectors["에너지"]["직간접구분"] == "간접"
    assert sectors["전력"]["직간접구분"] == "간접"
    assert "전환" not in sectors
    assert any("관리부문에 에너지원 표기" in row["항목"] for row in cleaned["validation_report"])


def test_reduction_target_dedup_key_keeps_different_target_scopes() -> None:
    # Given: 목표범위만 다른 GIR 기준과 자체 인벤토리 기준 행
    raw = {
        "municipality_name": "서울특별시",
        "reduction_targets": [
            {"목표수준": "총괄", "목표범위": "GIR 기준", "부문": "합계", "목표연도": 2030, "목표배출량": 100},
            {"목표수준": "총괄", "목표범위": "자체 인벤토리 기준", "부문": "합계", "목표연도": 2030, "목표배출량": 80},
        ],
    }

    # When: 감축목표를 dedup하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 서로 다른 인벤토리 기준 행은 합쳐지지 않는다.
    assert len(cleaned["reduction_targets"]) == 2
    assert {row["목표범위"] for row in cleaned["reduction_targets"]} == {"GIR 기준", "자체 인벤토리 기준"}


def test_document_meta_merges_to_one_row_with_target_year_union_and_report() -> None:
    # Given: 같은 지자체의 문서메타가 배치별 후보 3행으로 들어오면
    raw = {
        "municipality_name": "서울특별시",
        "document_meta": [
            {"지자체명": "서울특별시", "계획명": "서울 기본계획", "기준연도": None, "목표연도": "2033"},
            {"지자체명": "서울특별시", "계획명": "서울 기본계획 상세", "기준연도": 2018, "목표연도": "2030,2033,2050"},
            {"지자체명": "서울특별시", "계획명": "서울 기본계획", "기준연도": 2018, "목표연도": "2030,2050"},
        ],
    }

    # When: 문서메타를 정제하면
    cleaned = OrganizerAgent().organize(raw)

    # Then: 지자체당 1행으로 병합하고 목표연도는 정렬된 합집합으로 보존한다.
    assert len(cleaned["document_meta"]) == 1
    row = cleaned["document_meta"][0]
    assert row["기준연도"] == 2018
    assert row["목표연도"] == "2030,2033,2050"
    assert row["계획명"] == "서울 기본계획 상세"
    assert any("문서메타 병합" in issue["항목"] for issue in cleaned["validation_report"])
