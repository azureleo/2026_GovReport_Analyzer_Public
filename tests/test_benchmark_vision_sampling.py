from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from scripts.vision_benchmark_workbook import CandidateSampleInput, build_sample_sheet


def _inventory(path: Path, rows: list[list[str | int]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "시각요소"
    ws.append(["요소ID", "페이지", "요소유형", "제목", "데이터포함", "기대추출", "관련시트", "비고"])
    for row in rows:
        ws.append(row)
    wb.save(path)


def _workbook(path: Path, rows: list[list[str | int]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "16_시각자료목록"
    ws.append(["지자체명", "시각자료ID", "캡션", "유형", "데이터포함여부", "추출값요약", "디지타이징필요", "관련시트"])
    for row in rows:
        ws.append(row)
    wb.save(path)


def _audit_json(path: Path, rows: list[tuple[str, int, str, str]]) -> None:
    path.write_text(
        json.dumps({"details": [
            {"요소ID": element_id, "페이지": page, "요소유형": kind, "상태": status}
            for element_id, page, kind, status in rows
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_sample_sheet_uses_candidate_visual_intersection_and_page_distribution(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.xlsx"
    inv_rows: list[list[str | int]] = []
    audit_rows_a: list[tuple[str, int, str, str]] = []
    audit_rows_b: list[tuple[str, int, str, str]] = []
    workbook_rows_a: list[list[str | int]] = []
    workbook_rows_b: list[list[str | int]] = []
    pages = [1, 2, 3, 40, 41, 42, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89]
    for idx, page in enumerate(pages, start=1):
        kind = "그래프" if idx <= 11 else "이미지"
        element_id = f"E{idx:03d}"
        inv_rows.append([element_id, page, kind, f"제목 {idx}", "Y", "Y", "16_시각자료목록", ""])
        audit_rows_a.append((element_id, page, kind, "기록됨"))
        status_b = "vision_유실" if element_id == "E016" else "기록됨"
        audit_rows_b.append((element_id, page, kind, status_b))
        workbook_rows_a.append(["서울", f"V{page}-001", f"제목 {idx}", kind, "Y", f"A-{element_id}", "N", ""])
        workbook_rows_b.append(["서울", f"V{page}-001", f"제목 {idx}", kind, "Y", f"B-{element_id}", "N", ""])
    _inventory(inventory, inv_rows)
    a_xlsx, b_xlsx = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    a_audit, b_audit = tmp_path / "a.json", tmp_path / "b.json"
    _workbook(a_xlsx, workbook_rows_a)
    _workbook(b_xlsx, workbook_rows_b)
    _audit_json(a_audit, audit_rows_a)
    _audit_json(b_audit, audit_rows_b)

    sheet = build_sample_sheet(inventory, (
        CandidateSampleInput("flashlite", a_xlsx, a_audit),
        CandidateSampleInput("codex", b_xlsx, b_audit),
    ), 15)

    assert len(sheet.rows) == 15
    assert {row.element_id for row in sheet.rows}.isdisjoint({"E016"})
    assert sum(1 for row in sheet.rows if row.element_type == "그래프") >= 10
    assert sum(1 for row in sheet.rows if row.element_type != "그래프") <= 5
    assert {"flashlite", "codex"}.issubset(sheet.candidates)
    assert any(row.values["flashlite"] == "A-E001" for row in sheet.rows)
    assert all(sum(1 for row in sheet.rows if row.page in bucket) >= 3 for bucket in [range(1, 40), range(40, 80), range(80, 100)])


def test_sample_sheet_reports_shortfall_when_intersection_is_under_15(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.xlsx"
    rows = [[f"E{idx:03d}", idx, "그래프", f"제목 {idx}", "Y", "Y", "", ""] for idx in range(1, 4)]
    _inventory(inventory, rows)
    xlsx = tmp_path / "one.xlsx"
    audit_json = tmp_path / "one.json"
    _workbook(xlsx, [["서울", f"V{idx}-001", f"제목 {idx}", "그래프", "Y", f"값 {idx}", "N", ""] for idx in range(1, 4)])
    _audit_json(audit_json, [(f"E{idx:03d}", idx, "그래프", "기록됨") for idx in range(1, 4)])

    sheet = build_sample_sheet(inventory, (CandidateSampleInput("flashlite", xlsx, audit_json),), 15)

    assert len(sheet.rows) == 3
    assert any("15 미만" in note for note in sheet.notes)


def test_sample_sheet_excludes_collapsed_candidate_from_intersection_pool(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.xlsx"
    rows = [[f"E{idx:03d}", idx, "그래프", f"제목 {idx}", "Y", "Y", "", ""] for idx in range(1, 11)]
    _inventory(inventory, rows)

    valid_xlsx = tmp_path / "valid.xlsx"
    valid_audit = tmp_path / "valid.json"
    _workbook(valid_xlsx, [["서울", f"V{idx}-001", f"제목 {idx}", "그래프", "Y", f"valid-{idx}", "N", ""] for idx in range(1, 6)])
    _audit_json(valid_audit, [(f"E{idx:03d}", idx, "그래프", "기록됨") for idx in range(1, 6)])

    collapsed_xlsx = tmp_path / "collapsed.xlsx"
    collapsed_audit = tmp_path / "collapsed.json"
    _workbook(collapsed_xlsx, [["서울", "V1-001", "제목 1", "그래프", "Y", "collapsed-1", "N", ""]])
    _audit_json(collapsed_audit, [("E001", 1, "그래프", "기록됨")])

    sheet = build_sample_sheet(inventory, (
        CandidateSampleInput("valid", valid_xlsx, valid_audit),
        CandidateSampleInput("collapsed", collapsed_xlsx, collapsed_audit),
    ), 15)

    assert {row.element_id for row in sheet.rows} == {f"E{idx:03d}" for idx in range(1, 6)}
    assert any("교집합 계산 제외" in note and "collapsed" in note for note in sheet.notes)
