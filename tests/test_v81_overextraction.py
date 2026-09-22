"""v8-1 S2 — 과잉 추출 격리 확대: 추출 원천 태깅·표 우선 병합·배치 상한 분리·가드 확장."""

from __future__ import annotations

import config
from unittest.mock import patch

from agents.extractor_agent import ExtractorAgent, _batch_char_limit, _build_semantic_batches
from utils.pdf_reader import PageContent
from agents.organizer_agent import OrganizerAgent
from utils.run_state import (
    EXTRACTION_SOURCE_FIELD,
    EXTRACTION_SOURCE_TABLE_OBJECT,
    EXTRACTION_SOURCE_TEXT_CHUNK,
    merge_rows_stably,
    stable_row_id,
)

PREFER = (EXTRACTION_SOURCE_TABLE_OBJECT, EXTRACTION_SOURCE_TEXT_CHUNK)


def _mgmt_row(value: float, source: str | None, page: int = 10) -> dict:
    row = {
        "지자체명": "테스트시", "관리부문": "건물", "직간접구분": "직접", "연도": 2018,
        "배출량": value, "단위": "천톤CO2eq", "출처페이지": page,
    }
    if source:
        row[EXTRACTION_SOURCE_FIELD] = source
    return row


def test_table_object_row_wins_over_text_chunk_row_of_same_entity_and_page() -> None:
    table_row = _mgmt_row(100, EXTRACTION_SOURCE_TABLE_OBJECT)
    text_row = _mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK)

    rows, conflicts = merge_rows_stably("emissions_management", [table_row], [text_row], prefer=PREFER)
    assert [row["배출량"] for row in rows] == [100]
    assert conflicts[0]["resolution"] == "table_object_preferred"

    # 순서가 바뀌어도(본문청크가 먼저 있어도) 표객체 행이 남는다.
    rows, conflicts = merge_rows_stably("emissions_management", [text_row], [table_row], prefer=PREFER)
    assert [row["배출량"] for row in rows] == [100]
    assert conflicts[0]["resolution"] == "table_object_preferred"
    assert conflicts[0]["kept_row_id"] == stable_row_id("emissions_management", table_row)


def test_conflicts_from_different_pages_or_without_tags_keep_both_rows() -> None:
    table_row = _mgmt_row(100, EXTRACTION_SOURCE_TABLE_OBJECT, page=10)
    text_row = _mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK, page=11)
    rows, conflicts = merge_rows_stably("emissions_management", [table_row], [text_row], prefer=PREFER)
    assert len(rows) == 2 and "resolution" not in conflicts[0]

    rows, conflicts = merge_rows_stably(
        "emissions_management", [_mgmt_row(100, None)], [_mgmt_row(101, None)], prefer=PREFER,
    )
    assert len(rows) == 2 and "resolution" not in conflicts[0]

    # prefer 미지정이면 태그가 있어도 기존 동작(둘 다 보존).
    rows, _ = merge_rows_stably("emissions_management", [table_row], [_mgmt_row(101, EXTRACTION_SOURCE_TEXT_CHUNK)])
    assert len(rows) == 2


def test_source_tag_is_volatile_for_row_identity_and_stripped_by_organizer() -> None:
    assert stable_row_id("emissions_management", _mgmt_row(1, EXTRACTION_SOURCE_TABLE_OBJECT)) == stable_row_id(
        "emissions_management", _mgmt_row(1, None)
    )
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [_mgmt_row(1, EXTRACTION_SOURCE_TABLE_OBJECT)],
    })
    assert EXTRACTION_SOURCE_FIELD not in cleaned["emissions_management"][0]
    assert all(EXTRACTION_SOURCE_FIELD not in headers for headers in config.EXCEL_HEADERS.values())


def test_child_rows_are_tagged_by_object_id() -> None:
    table_rows = ExtractorAgent._tag_child_rows({"object_id": "p3_table_2"}, [{"a": 1}])
    text_rows = ExtractorAgent._tag_child_rows({"object_id": "p3_text"}, [{"a": 1}])
    plain_rows = ExtractorAgent._tag_child_rows({}, [{"a": 1}])
    assert table_rows[0][EXTRACTION_SOURCE_FIELD] == EXTRACTION_SOURCE_TABLE_OBJECT
    assert text_rows[0][EXTRACTION_SOURCE_FIELD] == EXTRACTION_SOURCE_TEXT_CHUNK
    assert EXTRACTION_SOURCE_FIELD not in plain_rows[0]


def _pages(count: int, chars: int) -> list[PageContent]:
    return [PageContent(page_number=i, text="가" * chars, tables=[], images=[]) for i in range(1, count + 1)]


def test_api_backend_has_no_batch_char_limit_by_default_but_local_agent_keeps_it() -> None:
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _batch_char_limit(0) is None
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) == 1
        assert _batch_char_limit(1) == config.EXTRACTION_RECOVERY_MAX_BATCH_CHARS
    with patch.object(config, "LLM_PROVIDER", "codex"), patch.object(config, "STAGE_PROVIDERS", {}):
        assert _batch_char_limit(0) == config.EXTRACTION_MAX_BATCH_CHARS
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) > 1
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "EXTRACTION_MAX_BATCH_CHARS_API", 12000):
        assert _batch_char_limit(0) == 12000
        assert len(_build_semantic_batches(_pages(15, 5000), 15)) > 1
    # 단계별 백엔드 지정이 우선한다.
    with patch.object(config, "LLM_PROVIDER", "gemini"), \
            patch.object(config, "STAGE_PROVIDERS", {"extraction": "codex"}):
        assert _batch_char_limit(0) == config.EXTRACTION_MAX_BATCH_CHARS


# ── S2-4 격리 가드 확장 ──────────────────────────────────────────────
import json

from utils.semantic_contract_guard import (
    document_year_bounds,
    guard_annual_implementation,
    guard_emission_rows,
    summarize_quarantine,
    write_quarantine_ledger,
)


def _row04(**overrides) -> dict:
    row = {"관리부문": "건물", "세부부문": "가정", "직간접구분": "직접", "연도": 2018, "배출량": 10, "출처페이지": 10}
    row.update(overrides)
    return row


def test_emission_guard_quarantines_only_contract_breaches() -> None:
    rows = [
        _row04(),
        _row04(연도=None),
        _row04(배출량=None),
        _row04(관리부문="총배출량", 데이터상태="visual_only"),
        _row04(관리부문="직접배출량", 데이터상태="visual_only"),
        _row04(관리부문="총배출량"),  # 텍스트 유래 합계 라벨 행은 정식 행(골든 03/04 계약)
        _row04(연도=2100),
    ]
    kept, records = guard_emission_rows(rows, "emissions_management")
    assert [row["연도"] for row in kept] == [2018, 2018, 2018]
    reasons = [record["reason_codes"] for record in records]
    assert reasons == [
        ["missing_key_contract"], ["missing_key_contract"],
        ["metric_label_as_sector"], ["year_out_of_document_range"],
    ]
    # 문서 메타의 계획종료연도가 있으면 상한이 넓어진다.
    assert document_year_bounds([{"계획종료연도": 2090}]) == (1990, 2110)
    kept, _ = guard_emission_rows([_row04(연도=2100)], "emissions_management", year_bounds=(1990, 2110))
    assert len(kept) == 1


def test_same_key_value_conflicts_are_kept_not_quarantined() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [_row04(배출량=10), _row04(배출량=11)],
    })
    rows = cleaned["emissions_management"]
    assert sorted(row["배출량"] for row in rows) == [10.0, 11.0]
    assert all(row["데이터상태"] == "conflicting" for row in rows)
    assert not [r for r in cleaned["semantic_contract_review"] if r["sheet_key"] == "emissions_management"]


def _row09(**overrides) -> dict:
    row = {"관리번호": "B12", "사업명": "LED 교체", "연도": 2025, "연간계획": "LED 교체 사업 추진", "목표물량": 100, "출처페이지": 20}
    row.update(overrides)
    return row


def test_annual_guard_identity_content_and_near_duplicate_text() -> None:
    rows = [
        _row09(),
        _row09(관리번호="", 사업명=""),
        _row09(연간계획="", 목표물량=None, 규제혁신계획="", 입법계획="", 기간시작=None, 기간종료=None),
        _row09(연간계획="LED 교체 사업 추진 및 점검"),
        _row09(연간계획="LED 교체 사업 추진 및 점검 확대", 목표물량=120),
    ]
    kept, records = guard_annual_implementation(rows)
    reasons = sorted(",".join(record["reason_codes"]) for record in records)
    assert reasons == ["missing_identity", "near_duplicate_plan_text", "no_plan_content"]
    kept_plans = sorted(row["연간계획"] for row in kept)
    # 포함 관계의 짧은 문장은 격리, 목표물량이 다른 행은 보존.
    assert kept_plans == ["LED 교체 사업 추진 및 점검", "LED 교체 사업 추진 및 점검 확대"]


def test_organize_09_dedups_exact_rows_and_absorbs_blank_project_id() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "annual_implementation": [
            _row09(), _row09(), _row09(관리번호=""),
            _row09(관리번호="B13", 사업명="보일러 교체", 연간계획="보일러 교체 추진", 연도=2026, 목표물량=None),
        ],
    })
    rows = cleaned["annual_implementation"]
    assert len(rows) == 2
    assert {row["관리번호"] for row in rows} == {"B12", "B13"}


def test_contract_drops_land_in_ledger_and_validation_report() -> None:
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [_row04(), _row04(관리부문="")],
        "emissions_forecast": [{"시나리오": "BAU", "부문": "건물", "연도": 2030, "전망값": None, "출처페이지": 3}],
    })
    ledger = cleaned["semantic_contract_review"]
    assert {(r["sheet_key"], tuple(r["reason_codes"])) for r in ledger} == {
        ("emissions_management", ("missing_key_contract",)),
        ("emissions_forecast", ("missing_key_contract",)),
    }
    grouped = [i for i in cleaned["validation_report"] if i["항목"] == "의미 계약 과잉 추출 격리"]
    assert {i["대상시트키"] for i in grouped} == {"emissions_management", "emissions_forecast"}
    assert summarize_quarantine(ledger) == {
        "emissions_management": {"missing_key_contract": 1},
        "emissions_forecast": {"missing_key_contract": 1},
    }


def test_quarantine_ledger_is_written_next_to_output(tmp_path) -> None:
    output = tmp_path / "서울_v8.xlsx"
    records = [{"sheet_key": "annual_implementation", "reason_codes": ["missing_identity"], "row": {"연도": 2025}}]
    path = write_quarantine_ledger(records, output)
    assert path.name == "서울_v8_격리원장.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"] == {"annual_implementation": {"missing_identity": 1}}
    assert payload["records"][0]["row"]["연도"] == 2025


def test_guard_disable_switch_covers_new_sheets(monkeypatch) -> None:
    monkeypatch.setattr(config, "SEMANTIC_OVEREXTRACTION_GUARD_ENABLED", False)
    cleaned = OrganizerAgent().organize({
        "municipality_name": "테스트시",
        "emissions_management": [_row04(연도=2100)],
    })
    assert len(cleaned["emissions_management"]) == 1
    assert cleaned["semantic_contract_review"] == []


def _table_page(page_number: int, cells: int, chars: int = 500) -> PageContent:
    cols = 10
    rows_count = max(1, cells // cols)
    records = [{"cell_count": rows_count * cols, "row_count": rows_count, "column_count": cols}]
    return PageContent(
        page_number=page_number, text="가" * chars, tables=["<table></table>"],
        images=[], table_records=records if cells else [],
    )


def test_table_cell_cap_closes_batches_independently_of_chars() -> None:
    pages = [_table_page(i, cells=400) for i in range(1, 5)] + [_table_page(5, cells=0)]
    with patch.object(config, "LLM_PROVIDER", "codex"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "EXTRACTION_MAX_BATCH_CHARS", 60000), \
            patch.object(config, "EXTRACTION_MAX_BATCH_TABLE_CELLS", 700):
        batches = _build_semantic_batches(pages, 15)
    # 400+400 > 700 → 두 페이지씩 끊기고, 표 없는 페이지는 마지막 배치에 붙는다.
    assert [[p.page_number for p in b] for b in batches] == [[1], [2], [3], [4, 5]]
    # 상한 0이면 기존 동작(문자 상한만).
    with patch.object(config, "LLM_PROVIDER", "codex"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "EXTRACTION_MAX_BATCH_CHARS", 60000), \
            patch.object(config, "EXTRACTION_MAX_BATCH_TABLE_CELLS", 0):
        batches = _build_semantic_batches(pages, 15)
    assert [[p.page_number for p in b] for b in batches] == [[1, 2, 3, 4, 5]]
    # API 백엔드(문자 상한 없음)에서도 셀 상한은 독립적으로 동작한다.
    with patch.object(config, "LLM_PROVIDER", "gemini"), patch.object(config, "STAGE_PROVIDERS", {}), \
            patch.object(config, "EXTRACTION_MAX_BATCH_TABLE_CELLS", 700):
        batches = _build_semantic_batches(pages, 15)
    assert len(batches) == 4
