"""Conservative extraction merges: deduplicate exact facts and retain every conflict."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


def canonical_evidence_id(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip().strip("\"'"))
    return text.casefold() if text.lower().startswith("ev-") else text


def normalize_evidence_ids(*values: Any) -> list[str]:
    output = []
    def append(value):
        if isinstance(value, (list, tuple, set)):
            for item in value:
                append(item)
        elif isinstance(value, str) and value.strip().startswith("["):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                append(parsed)
            else:
                add(value)
        elif isinstance(value, str) and "|" in value:
            append(value.split("|"))
        elif value is not None:
            add(value)
    def add(value):
        text = canonical_evidence_id(value)
        if text and text not in output:
            output.append(text)
    for value in values:
        append(value)
    return output


ENTITY_FIELDS = {
    "regional_conditions": ("지자체명", "지표범주", "지표세부범주", "지표명", "연도", "단위"),
    "emissions_regional": ("지자체명", "인벤토리출처", "배출범위", "배출유형", "부문", "세부부문", "연도", "단위"),
    "emissions_management": ("지자체명", "인벤토리출처", "관리부문", "세부부문", "직간접구분", "연도", "단위"),
    "emissions_forecast": ("지자체명", "시나리오", "전망범위", "부문", "세부부문", "연도", "단위"),
    "reduction_targets": ("지자체명", "목표수준", "목표범위", "부문", "기준연도", "목표연도", "단위"),
    "annual_implementation": ("지자체명", "관리번호", "사업명", "연도", "기간시작", "기간종료", "목표단위"),
    "quantitative_reductions": ("지자체명", "관리번호", "사업명", "연도", "단위", "감축량유형", "시간기준"),
    "financial_plan": ("지자체명", "계획구분", "부문", "사업명", "재원구분", "연도", "예산단위"),
    "mitigation_projects": ("지자체명", "관리번호", "사업명", "부문"),
    "monitoring_performance": ("지자체명", "점검연도", "관리번호", "사업명", "부문"),
}
_PROVENANCE = {"출처페이지", "출처페이지추정", "source_object_ids", "object_id", "evidence_id",
               "evidence_ids", "근거ID", "근거ID목록", "객체ID", "근거객체ID", "물리객체ID",
               "추출원천", "추출엔진", "OCR보완여부", "출처계보", "원본근거ID", "원본객체ID",
               "source_evidence_ids", "source_evidence_id"}
_PROVENANCE.update({"source_engines", "ocr_source_file", "ocr_source_line"})


def _payload(row):
    return {key: value for key, value in row.items() if not key.startswith("_")
            and key not in _PROVENANCE and value not in (None, "")}


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _lineage(row):
    values = row.get("_extraction_sources") or row.get("출처계보") or []
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = [{"legacy_lineage_unparsed": values}]
    if values:
        return values if isinstance(values, list) else [values]
    legacy = {key: row[key] for key in ("객체ID", "근거ID", "출처페이지", "source_object_ids", "source_evidence_ids")
              if row.get(key) not in (None, "")}
    return [legacy] if legacy else []


def _merge_lineage(target, source):
    records = {_serialize(item): item for item in _lineage(target) + _lineage(source)}
    if records:
        target["_extraction_sources"] = [records[key] for key in sorted(records)]
        target["출처계보"] = json.dumps(target["_extraction_sources"], ensure_ascii=False, sort_keys=True)
    for key in ("source_object_ids", "evidence_ids", "source_evidence_ids", "근거ID목록", "출처페이지", "source_engines"):
        values = []
        for row in (target, source):
            value = row.get(key)
            for item in value if isinstance(value, list) else ([value] if value is not None else []):
                if item not in values:
                    values.append(item)
        if values:
            target[key] = values
    if source.get("OCR보완여부"):
        target["OCR보완여부"] = True
    # Single evidence/object IDs also occur in older checkpoints without arrays.
    # Retain those aliases when exact business facts are deduplicated.
    for array_key, keys in (
        ("source_evidence_ids", ("근거ID", "근거ID목록", "evidence_id", "evidence_ids", "source_evidence_ids")),
        ("source_object_ids", ("객체ID", "object_id", "source_object_ids")),
    ):
        values = normalize_evidence_ids(*(row.get(key) for row in (target, source) for key in keys))
        if values:
            target[array_key] = sorted(values)


def merge_extraction_rows(sheet_key: str, existing: list[dict], incoming: list[dict],
                          conflicts: list[dict] | None = None) -> list[dict]:
    """No source rank wins a value conflict. Strict keys include scope/scenario/unit."""
    output = [deepcopy(row) for row in existing if isinstance(row, dict)]
    known_conflicts = {_serialize(item) for item in conflicts or []}
    for row in incoming:
        if not isinstance(row, dict):
            continue
        row = deepcopy(row)
        payload = _payload(row)
        duplicate = next((candidate for candidate in output if _payload(candidate) == payload), None)
        if duplicate is not None:
            _merge_lineage(duplicate, row)
            continue
        fields = ENTITY_FIELDS.get(sheet_key, ())
        entity = tuple(_serialize("" if row.get(field) in (None, "") else row.get(field)) for field in fields)
        if fields and any(row.get(field) not in (None, "") for field in fields if field != "지자체명"):
            for candidate in output:
                if tuple(_serialize("" if candidate.get(field) in (None, "") else candidate.get(field)) for field in fields) != entity:
                    continue
                differences = {field: {"existing": candidate[field], "incoming": value}
                               for field, value in payload.items()
                               if field in candidate and candidate[field] not in (None, "")
                               and value not in (None, "") and candidate[field] != value}
                if differences:
                    conflict = {
                        "sheet_key": sheet_key, "status": "value_conflict_retained",
                        "reason": "same strict entity key, conflicting source values; no priority overwrite",
                        "entity_key": dict(zip(fields, [row.get(field) for field in fields])),
                        "differences": differences,
                        "existing": {key: deepcopy(value) for key, value in candidate.items() if key != "_merge_conflicts"},
                        "incoming": {key: deepcopy(value) for key, value in row.items() if key != "_merge_conflicts"},
                    }
                    fingerprint = hashlib.sha256(_serialize(conflict).encode()).hexdigest()
                    conflict["conflict_id"] = fingerprint
                    serialized = _serialize(conflict)
                    if conflicts is not None and serialized not in known_conflicts:
                        conflicts.append(conflict)
                        known_conflicts.add(serialized)
                    row.setdefault("_merge_conflicts", []).append(fingerprint)
                    candidate.setdefault("_merge_conflicts", []).append(fingerprint)
        output.append(row)
    return output
