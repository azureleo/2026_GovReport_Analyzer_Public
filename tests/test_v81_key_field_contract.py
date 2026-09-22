"""v8-1 S1 — 키 필드 공란 금지: 표 청크 다단 헤더 보존·프롬프트 규칙·정제기 결정론 보완."""

from __future__ import annotations

import config
from agents import extractor_agent
from agents.organizer_agent import OrganizerAgent
from utils.document_objects import (
    DocumentObject,
    render_table_object_chunks,
    table_header_row_count,
    table_to_markdown,
)


def _table(rows: list[list[str]], **metadata) -> DocumentObject:
    return DocumentObject(
        object_id="p10_table_1",
        object_type="table",
        page_number=10,
        sequence=1,
        rows=rows,
        caption="관리권한 인벤토리 배출량",
        number="표 3-1",
        metadata=metadata,
    )


_TWO_HEADER_ROWS = [
    ["구분", "직접배출", "", "간접배출", "", "합계"],
    ["", "2018", "2019", "2018", "2019", ""],
    *[[f"세부{i}", str(100 + i), str(110 + i), str(200 + i), str(210 + i), str(300 + i)] for i in range(1, 8)],
]


def test_header_row_count_detects_two_row_header_and_caps_at_three() -> None:
    assert table_header_row_count(_TWO_HEADER_ROWS) == 2
    assert table_header_row_count([["구분", "값"], ["a", "1"]]) == 1
    all_header = [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]]
    assert table_header_row_count(all_header) == 3
    assert table_header_row_count([]) == 0


def test_two_row_header_is_repeated_in_every_chunk() -> None:
    chunks = render_table_object_chunks(_table(_TWO_HEADER_ROWS), rows_per_chunk=4)
    assert len(chunks) == 3
    for chunk in chunks:
        assert "| 구분 | 직접배출 |  | 간접배출 |  | 합계 |" in chunk
        assert "|  | 2018 | 2019 | 2018 | 2019 |  |" in chunk
        assert "header_rows: 2" in chunk
        assert "key_fields_hint:" in chunk
    # 본문 행은 청크 사이에 중복되지 않는다.
    bodies = ["".join(line for line in chunk.splitlines() if "세부" in line) for chunk in chunks]
    assert bodies[0] and "세부1" in bodies[0] and "세부1" not in bodies[1]


def test_single_header_table_keeps_prior_markdown_shape() -> None:
    rows = [["구분", "2018"], ["건물", "10"], ["수송", "20"]]
    chunk = render_table_object_chunks(_table(rows), rows_per_chunk=20)[0]
    assert "header_rows: 1" in chunk
    assert table_to_markdown(rows, header_rows=1) == table_to_markdown(rows)
    assert "| 구분 | 2018 |\n| --- | --- |\n| 건물 | 10 |" in chunk


def test_prompts_carry_key_field_rule() -> None:
    assert "값(수치) 필드만 확인 불가 시 null" in extractor_agent.EXTRACTION_SYSTEM
    management = extractor_agent._SHEET_CONFIGS["emissions_management"]["prompt"]
    regional = extractor_agent._SHEET_CONFIGS["emissions_regional"]["prompt"]
    assert "직간접구분은 행 대사 키" in management
    assert "배출유형은 행 대사 키" in regional
    # 다른 시트 프롬프트는 불변(규칙 문구가 새지 않는다).
    assert "행 대사 키" not in extractor_agent._SHEET_CONFIGS["emissions_forecast"]["prompt"]


def _organize(sheet_key: str, rows: list[dict]) -> dict:
    return OrganizerAgent().organize({"municipality_name": "테스트시", sheet_key: rows})


def test_04_sink_sector_fills_key_as_absorption() -> None:
    cleaned = _organize("emissions_management", [
        {"관리부문": "흡수원", "세부부문": "산림", "연도": 2018, "배출량": -50, "단위": "천톤CO2eq", "출처페이지": 10},
    ])
    row = cleaned["emissions_management"][0]
    assert row["직간접구분"] == "흡수"
    assert row["derivation_type"] == "normalized"


def test_04_energy_carrier_tokens_fill_key_and_mark_inferred() -> None:
    cleaned = _organize("emissions_management", [
        {"관리부문": "건물", "세부부문": "가정(전력)", "연도": 2018, "배출량": 1, "단위": "천톤CO2eq", "출처페이지": 10},
        {"관리부문": "건물", "세부부문": "상업/공공 도시가스", "연도": 2018, "배출량": 2, "단위": "천톤CO2eq", "출처페이지": 10},
        {"관리부문": "건물", "세부부문": "전력+연료", "연도": 2018, "배출량": 3, "단위": "천톤CO2eq", "출처페이지": 10},
    ])
    by_sub = {row["세부부문"]: row for row in cleaned["emissions_management"]}
    assert by_sub["가정(전력)"]["직간접구분"] == "간접"
    assert by_sub["가정(전력)"]["derivation_type"] == "inferred"
    assert by_sub["상업/공공 도시가스"]["직간접구분"] == "직접"
    assert by_sub["전력+연료"].get("직간접구분", "") == ""


def test_04_sibling_unique_value_propagates_but_mixed_does_not() -> None:
    cleaned = _organize("emissions_management", [
        {"관리부문": "수송", "세부부문": "도로수송", "직간접구분": "direct", "연도": 2018, "배출량": 1, "단위": "천톤CO2eq", "출처페이지": 10},
        {"관리부문": "수송", "세부부문": "도로수송", "연도": 2019, "배출량": 2, "단위": "천톤CO2eq", "출처페이지": 10},
        {"관리부문": "건물", "세부부문": "가정", "직간접구분": "direct", "연도": 2018, "배출량": 3, "단위": "천톤CO2eq", "출처페이지": 11},
        {"관리부문": "건물", "세부부문": "가정", "직간접구분": "indirect", "연도": 2018, "배출량": 4, "단위": "천톤CO2eq", "출처페이지": 11},
        {"관리부문": "건물", "세부부문": "가정", "연도": 2019, "배출량": 5, "단위": "천톤CO2eq", "출처페이지": 11},
        {"관리부문": "합계", "세부부문": "합계", "연도": 2018, "배출량": 9, "단위": "천톤CO2eq", "출처페이지": 12},
    ])
    rows = cleaned["emissions_management"]
    transport_2019 = next(r for r in rows if r["세부부문"] == "도로수송" and r["연도"] == 2019)
    assert transport_2019["직간접구분"] == "직접"
    assert transport_2019["derivation_type"] == "inferred"
    home_2019 = next(r for r in rows if r["세부부문"] == "가정" and r["연도"] == 2019)
    assert home_2019.get("직간접구분", "") == ""
    total = next(r for r in rows if r["관리부문"] == "합계")
    assert total.get("직간접구분", "") == ""
    issues = [i for i in cleaned["validation_report"] if i["항목"] == "키 필드 공란"]
    assert any("직간접구분" in i["문제내용"] for i in issues)
    metrics = cleaned["key_field_blank_ratio"]
    assert metrics["emissions_management.직간접구분"]["blank"] == 2
    assert metrics["emissions_management.직간접구분"]["filled_by_rule"] == 1


def test_03_emission_type_uses_same_rules_with_regional_labels() -> None:
    cleaned = _organize("emissions_regional", [
        {"부문": "에너지", "세부부문": "전력", "연도": 2018, "배출량": 1, "단위": "천톤CO2eq", "출처페이지": 5},
        {"부문": "LULUCF", "세부부문": "산림지", "연도": 2018, "배출량": -1, "단위": "천톤CO2eq", "출처페이지": 5},
        {"부문": "폐기물", "세부부문": "소각", "연도": 2018, "배출량": 2, "단위": "천톤CO2eq", "출처페이지": 5},
    ])
    by_sub = {row["세부부문"]: row for row in cleaned["emissions_regional"]}
    assert by_sub["전력"]["배출유형"] == "간접배출"
    assert by_sub["산림지"]["배출유형"] == "흡수원"
    assert by_sub["소각"].get("배출유형", "") == ""


def test_golden_04_fixture_is_never_contradicted_by_rules() -> None:
    """서울 골든 04의 (관리부문, 세부부문)→직간접구분 표본: 규칙이 골든과 모순되는 값을 만들지 않는다."""
    fixture = [
        ("건물", "가정", "direct"), ("건물", "가정(열)", "indirect"), ("건물", "가정(전력)", "indirect"),
        ("건물", "상업/공공", "direct"), ("건물", "전력 상업/공공", "indirect"), ("건물", "열-가정", "indirect"),
        ("농축산", "농업", "direct"), ("수송", "도로수송", "direct"), ("수송", "철도", "direct"),
        ("폐기물", "폐기물", "indirect"), ("합계", "총배출량", "direct"), ("합계", "간접배출 계", "indirect"),
        ("흡수원", "LULUCF", "sink"), ("흡수원", "흡수 및 제거", "sink"),
    ]
    rows = [
        {"관리부문": sector, "세부부문": sub, "연도": 2018, "배출량": 1, "단위": "천톤CO2eq", "출처페이지": index}
        for index, (sector, sub, _expected) in enumerate(fixture, start=1)
    ]
    cleaned = _organize("emissions_management", rows)
    expected_by_sub = {sub: exp for _s, sub, exp in fixture}
    normalized = {"direct": "직접", "indirect": "간접", "sink": "흡수"}
    for row in cleaned["emissions_management"]:
        filled = row.get("직간접구분", "")
        if filled:
            assert filled == normalized[expected_by_sub[row["세부부문"]]], row


def test_05_and_09_key_blank_ratios_are_only_reported() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_forecast": [
            {"시나리오": "", "부문": "건물", "연도": 2030, "전망값": 1, "단위": "천톤CO2eq", "출처페이지": 3},
        ],
        "annual_implementation": [
            {"관리번호": "", "사업명": "사업A", "연도": 2025, "연간계획": "계획", "출처페이지": 4},
        ],
    })
    assert cleaned["emissions_forecast"][0]["시나리오"] == ""
    assert cleaned["annual_implementation"][0]["관리번호"] == ""
    metrics = cleaned["key_field_blank_ratio"]
    assert metrics["emissions_forecast.시나리오"]["blank"] == 1
    assert metrics["annual_implementation.관리번호"]["blank"] == 1
    assert config.EXCEL_HEADERS  # 시트 계약 존재 확인(헤더 불변은 다른 테스트가 담당)
