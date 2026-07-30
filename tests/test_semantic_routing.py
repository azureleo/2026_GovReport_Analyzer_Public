from __future__ import annotations

from utils.pdf_reader import PDFContent, PageContent
from utils.semantic_routing import validate_and_reclassify
from utils.source_verifier import build_source_object_inventory


def _forecast_document() -> PDFContent:
    table = """
    <table>
      <tr><th>부문</th><th>2021</th><th>2030</th></tr>
      <tr><td>건물</td><td>120</td><td>100</td></tr>
    </table>
    """
    text = (
        "제4장 온실가스 배출 전망\n"
        "표 4-2 부문별 온실가스 배출량 전망(BAU)\n"
    )
    page = PageContent(page_number=10, text=text, tables=[table], images=[])
    return PDFContent(total_pages=10, pages=[page], full_text=text)


def test_future_current_emission_on_forecast_table_is_reclassified() -> None:
    final_data = {
        "document_meta": [{
            "지자체명": "서울특별시",
            "계획명": "기본계획",
            "계획시작연도": 2024,
            "계획종료연도": 2033,
        }],
        "emissions_regional": [
            {
                "지자체명": "서울특별시",
                "배출유형": "직접배출",
                "부문": "건물",
                "연도": 2021,
                "배출량": 120,
                "단위": "천톤CO2eq",
                "출처페이지": 10,
            },
            {
                "지자체명": "서울특별시",
                "배출유형": "직접배출",
                "부문": "건물",
                "연도": 2030,
                "배출량": 100,
                "단위": "천톤CO2eq",
                "출처페이지": 10,
            },
        ],
        "emissions_forecast": [],
    }

    report = validate_and_reclassify(final_data, _forecast_document())

    assert [row["연도"] for row in final_data["emissions_regional"]] == [2021]
    assert final_data["emissions_forecast"][0]["연도"] == 2030
    assert final_data["emissions_forecast"][0]["전망값"] == 100
    assert report.mismatches_before == 1
    assert report.mismatches_after == 0
    assert report.reclassified_rows == 1
    assert report.evaluable_rows == 1
    assert report.error_rate == 0.0


def test_source_object_inventory_reports_wrong_sheet_semantics() -> None:
    final_data = {
        "emissions_regional": [{
            "지자체명": "서울특별시",
            "배출유형": "직접배출",
            "부문": "건물",
            "연도": 2030,
            "배출량": 100,
            "단위": "천톤CO2eq",
            "출처페이지": 10,
        }],
    }

    inventory = build_source_object_inventory(final_data, _forecast_document())

    assert inventory.total_objects == 1
    assert inventory.routing_evaluable_objects == 1
    assert inventory.routing_mismatch_objects == 1
    assert inventory.routing_error_rate == 1.0
    assert inventory.rows[0].expected_sheet == "05_배출전망"
    assert inventory.rows[0].routing_status == "오배치의심"


def test_mixed_semantic_tables_on_one_page_are_not_auto_reclassified() -> None:
    text = (
        "제4장 온실가스 분석\n"
        "표 4-1 지역 온실가스 인벤토리 및 지역 배출량 현황\n"
        "표 4-2 온실가스 배출량 전망(BAU)\n"
    )
    tables = [
        "<table><tr><th>부문</th><th>2021</th></tr>"
        "<tr><td>건물</td><td>120</td></tr></table>",
        "<table><tr><th>부문</th><th>2030</th></tr>"
        "<tr><td>건물</td><td>100</td></tr></table>",
    ]
    document = PDFContent(
        total_pages=10,
        pages=[PageContent(page_number=10, text=text, tables=tables, images=[])],
        full_text=text,
    )
    final_data = {
        "document_meta": [{"계획시작연도": 2024}],
        "emissions_regional": [{
            "지자체명": "서울특별시",
            "배출유형": "직접배출",
            "부문": "건물",
            "연도": 2030,
            "배출량": 100,
            "단위": "천톤CO2eq",
            "출처페이지": 10,
        }],
        "emissions_forecast": [],
    }

    report = validate_and_reclassify(final_data, document)

    assert len(final_data["emissions_regional"]) == 1
    assert final_data["emissions_forecast"] == []
    assert report.reclassified_rows == 0
