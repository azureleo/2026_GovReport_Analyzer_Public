"""16_시각자료목록 기반 벤치마크 지표와 표본채점지 표본 선정."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from scripts.vision_benchmark_core import JsonObject, JsonValue

VISUAL_SHEET = "16_시각자료목록"
_PAGE_RE = re.compile(r"^V(?P<page>\d+)-\d+")
_TRUE_VALUES = {"y", "yes", "true", "1", "예", "참"}
_FALSE_VALUES = {"n", "no", "false", "0", "아니오", "거짓"}


@dataclass(frozen=True, slots=True)
class InventoryElement:
    element_id: str
    page: int
    element_type: str
    title: str
    expected: bool


@dataclass(frozen=True, slots=True)
class VisualRecord:
    page: int | None
    caption: str
    visual_type: str
    data_included: str
    summary: str
    digitizing_needed: str


@dataclass(frozen=True, slots=True)
class CandidateSampleInput:
    candidate: str
    output_xlsx: Path
    audit_json: Path | None


@dataclass(frozen=True, slots=True)
class SampleRow:
    element_id: str
    page: int
    element_type: str
    title: str
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class SampleSheet:
    rows: tuple[SampleRow, ...]
    candidates: tuple[str, ...]
    notes: tuple[str, ...]


def workbook_metrics(path: Path) -> JsonObject:
    records = load_visual_records(path)
    total = len(records)
    data_yes = sum(1 for record in records if _truthy(record.data_included))
    auto_ok = sum(1 for record in records if _falsey(record.digitizing_needed))
    return {
        "visual_rows": total,
        "data_included_ratio": round(data_yes / total, 4) if total else None,
        "auto_trusted_ratio": round(auto_ok / total, 4) if total else None,
    }


def build_sample_sheet(inventory_path: Path, inputs: tuple[CandidateSampleInput, ...], limit: int) -> SampleSheet:
    candidates = tuple(item.candidate for item in inputs if item.output_xlsx.exists())
    inventory = {item.element_id: item for item in load_inventory(inventory_path) if item.expected}
    records = {item.candidate: load_visual_records(item.output_xlsx) for item in inputs if item.output_xlsx.exists()}
    common_ids, pool_notes = _common_recorded_ids(inventory, inputs, records)
    selected = _select_elements([inventory[element_id] for element_id in sorted(common_ids)], limit)
    rows = tuple(_sample_row(item, candidates, records) for item in selected)
    return SampleSheet(rows=rows, candidates=candidates, notes=_sample_notes(rows, common_ids, limit, pool_notes))


def load_inventory(path: Path) -> list[InventoryElement]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["시각요소"]
        headers = [_text(cell.value) for cell in ws[1]]
        rows: list[InventoryElement] = []
        for values in ws.iter_rows(min_row=2, values_only=True):
            row = {header: value for header, value in zip(headers, values)}
            element_id = _text(row.get("요소ID"))
            if not element_id:
                continue
            rows.append(InventoryElement(
                element_id=element_id,
                page=_int(row.get("페이지")),
                element_type=_text(row.get("요소유형")),
                title=_text(row.get("제목")),
                expected=_truthy(_text(row.get("기대추출"))),
            ))
        return rows
    finally:
        wb.close()


def load_visual_records(path: Path) -> list[VisualRecord]:
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except OSError:
        return []
    try:
        if VISUAL_SHEET not in wb.sheetnames:
            return []
        ws = wb[VISUAL_SHEET]
        headers = [_text(cell.value) for cell in ws[1]]
        records: list[VisualRecord] = []
        for values in ws.iter_rows(min_row=2, values_only=True):
            row = {header: value for header, value in zip(headers, values)}
            records.append(VisualRecord(
                page=_visual_page(_text(row.get("시각자료ID"))),
                caption=_text(row.get("캡션")),
                visual_type=_text(row.get("유형")),
                data_included=_text(row.get("데이터포함여부")),
                summary=_text(row.get("추출값요약")),
                digitizing_needed=_text(row.get("디지타이징필요")),
            ))
        return records
    finally:
        wb.close()


def _common_recorded_ids(
    inventory: dict[str, InventoryElement],
    inputs: tuple[CandidateSampleInput, ...],
    records: dict[str, list[VisualRecord]],
) -> tuple[set[str], tuple[str, ...]]:
    pools: list[set[str]] = []
    notes: list[str] = []
    expected_total = len(inventory)
    for item in inputs:
        if item.candidate not in records:
            continue
        recorded = _recorded_ids_from_audit(item.audit_json)
        if not recorded:
            recorded = _recorded_ids_from_pages(inventory, records[item.candidate])
        if expected_total and len(recorded) / expected_total < 0.2:
            notes.append(f"교집합 계산 제외: {item.candidate} 기록 요소 {len(recorded)}/{expected_total}(기대치의 20% 미만).")
            continue
        pools.append(recorded)
    if not pools:
        return set(), tuple(notes)
    common = set(pools[0])
    for pool in pools[1:]:
        common &= pool
    return common, tuple(notes)


def _recorded_ids_from_audit(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    details = payload.get("details") if isinstance(payload, dict) else []
    if not isinstance(details, list):
        return set()
    return {
        _text(row.get("요소ID")) for row in details
        if isinstance(row, dict) and _text(row.get("상태")) == "기록됨" and _text(row.get("요소ID"))
    }


def _recorded_ids_from_pages(inventory: dict[str, InventoryElement], records: list[VisualRecord]) -> set[str]:
    pages = {record.page for record in records if record.page is not None}
    return {item.element_id for item in inventory.values() if item.page in pages}


def _select_elements(elements: list[InventoryElement], limit: int) -> tuple[InventoryElement, ...]:
    ordered = sorted(elements, key=lambda item: (item.page, item.element_id))
    selected: list[InventoryElement] = []
    for phase in range(3):
        _fill_phase(ordered, selected, phase, limit)
    for item in ordered:
        if len(selected) >= limit:
            break
        if item not in selected and _can_add(item, selected):
            selected.append(item)
    return tuple(selected[:limit])


def _fill_phase(pool: list[InventoryElement], selected: list[InventoryElement], phase: int, limit: int) -> None:
    while len(selected) < limit and _phase_count(selected, phase, pool) < 3:
        choice = next((item for item in pool if item not in selected and _phase(item, pool) == phase and _can_add(item, selected)), None)
        if choice is None:
            return
        selected.append(choice)


def _can_add(item: InventoryElement, selected: list[InventoryElement]) -> bool:
    if item.element_type == "그래프":
        return True
    return sum(1 for row in selected if row.element_type != "그래프") < 5


def _phase_count(rows: list[InventoryElement], phase: int, pool: list[InventoryElement]) -> int:
    return sum(1 for item in rows if _phase(item, pool) == phase)


def _phase(item: InventoryElement, pool: list[InventoryElement]) -> int:
    pages = [row.page for row in pool]
    lo, hi = min(pages), max(pages)
    if hi <= lo:
        return 1
    ratio = (item.page - lo) / (hi - lo)
    if ratio < 1 / 3:
        return 0
    if ratio < 2 / 3:
        return 1
    return 2


def _sample_row(item: InventoryElement, candidates: tuple[str, ...], records: dict[str, list[VisualRecord]]) -> SampleRow:
    values = {candidate: _summary_for(item, records.get(candidate, [])) for candidate in candidates}
    return SampleRow(item.element_id, item.page, item.element_type, item.title, values)


def _summary_for(item: InventoryElement, records: list[VisualRecord]) -> str:
    page_records = [record for record in records if record.page == item.page]
    typed = [record for record in page_records if record.visual_type == item.element_type]
    chosen = typed or page_records
    summaries = [record.summary for record in chosen if record.summary]
    return " / ".join(dict.fromkeys(summaries))


def _sample_notes(
    rows: tuple[SampleRow, ...],
    common_ids: set[str],
    limit: int,
    pool_notes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    notes: list[str] = list(pool_notes)
    if len(common_ids) < limit:
        notes.append(f"교집합 표본 풀이 {len(common_ids)}개라 목표 {limit}개(기본 15) 미만, 즉 15 미만입니다.")
    if sum(1 for row in rows if row.element_type == "그래프") < min(10, len(rows)):
        notes.append("그래프 표본 10개를 채우지 못했습니다.")
    return tuple(notes)


def _visual_page(visual_id: str) -> int | None:
    match = _PAGE_RE.match(visual_id.strip())
    return int(match.group("page")) if match else None


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in _TRUE_VALUES


def _falsey(value: str) -> bool:
    return str(value).strip().lower() in _FALSE_VALUES


def _int(value: JsonValue | None) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _text(value)
    return int(text) if text.isdigit() else 0


def _text(value: JsonValue | None) -> str:
    return "" if value is None else str(value).strip()
