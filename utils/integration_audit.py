"""Run-linked deterministic diagnostics, not model accuracy measurements."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import re

import config


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported audit value: {type(value).__name__}")


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    temporary.replace(path)
    return path


def document_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ocr_directory_hash(root):
    digest = hashlib.sha256()
    root = Path(root) if root else None
    if root and root.is_dir():
        for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".jsonl", ".md", ".txt"}):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(document_hash(path).encode("ascii"))
    return digest.hexdigest()


def evidence_ids(row):
    result = []
    for field in ("근거ID", "근거ID목록", "evidence_id", "evidence_ids", "source_evidence_ids", "객체ID", "object_id", "object_ids", "source_object_ids", "근거객체ID"):
        value = row.get(field)
        values = value if isinstance(value, (list, tuple, set)) else [value]
        for item in values:
            if item not in (None, "") and str(item) not in result:
                result.append(str(item))
    lineage = row.get("_extraction_sources", [])
    if not lineage:
        lineage = row.get("출처계보", []) or []
        if isinstance(lineage, str):
            try:
                lineage = json.loads(lineage)
            except ValueError:
                lineage = []
    for record in lineage:
        if isinstance(record, dict):
            result.extend(v for v in evidence_ids(record) if v not in result)
    return result


def body_rows(data):
    for key in config.EXTRACTION_SHEETS:
        for row in data.get(key, []):
            if isinstance(row, dict):
                yield key, row


def source_validation(data, document):
    """Only diagnose exact source presence. OCR text is not independent truth."""
    pages = {p.page_number: p.text + "\n" + "\n".join(p.tables) for p in document.pages}
    records = []
    for sheet, row in body_rows(data):
        values = row.get("출처페이지", [])
        numbers = values if isinstance(values, (list, tuple, set)) else re.findall(r"\d+", str(values or ""))
        numbers = [int(n) for n in numbers if str(n).isdigit()]
        text = " ".join(pages.get(n, "") for n in numbers).replace(",", "")
        numeric = {k: v for k, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and k not in {"연도", "기준연도", "목표연도", "기간시작", "기간종료", "점검연도", "출처페이지"}}
        unmatched = [k for k, v in numeric.items() if not re.search(r"(?<![\d.+\-−])" + re.escape(f"{v:g}") + r"(?![\d.])", text)]
        missing_pages = [n for n in numbers if n not in pages]
        records.append({"sheet": sheet, "pages": numbers, "evidence_ids": evidence_ids(row),
                        "status": "missing_source_page" if not numbers or missing_pages else "numeric_review" if unmatched else "source_present",
                        "missing_pages": missing_pages,
                        "unmatched_numeric_fields": unmatched})
    return records


def object_inventory(objects, data, states):
    row_anchors = [set(evidence_ids(row)) for _, row in body_rows(data)]
    rows = []
    for obj in objects:
        meta = obj.metadata or {}
        aliases = {obj.object_id, str(meta.get("evidence_id", "")), *map(str, meta.get("original_object_ids", []))}
        reflected = sum(bool(anchors & (aliases - {""})) for anchors in row_anchors)
        state = states.get(obj.object_id, {})
        rows.append({"지자체명": data.get("municipality_name", ""), "시각자료ID": obj.object_id,
                     "캡션": obj.caption or obj.number or "", "유형": obj.object_type,
                     "데이터포함여부": bool(obj.rows or obj.text),
                     "추출값요약": f"본문 연결 {reflected}행", "디지타이징필요": False,
                     "관련시트": ",".join(state.get("sheets", [])), "객체ID": obj.object_id,
                     "근거ID": meta.get("evidence_id", obj.object_id), "출처페이지": obj.page_number,
                     "엔진": meta.get("engine", "native"), "처리상태": state.get("status", "native_parsed"),
                     "본문반영행수": reflected})
    return rows


def audit_object_evidence(data, objects):
    from utils.object_evidence_match import descriptor, match_object_evidence
    candidates = [descriptor(key=o.object_id, page=o.page_number,
                    evidence_ids=[o.metadata.get("evidence_id", "")], object_ids=[o.object_id],
                    number=o.number, caption=o.caption, content=o.searchable_text()) for o in objects]
    records = []
    for sheet, row in body_rows(data):
        raw_page = row.get("출처페이지")
        pages = raw_page if isinstance(raw_page, (list, tuple)) else re.findall(r"\d+", str(raw_page or ""))
        expected = descriptor(key=sheet, page=pages[0] if len(pages) == 1 else None,
                    evidence_ids=row.get("근거ID목록") or row.get("근거ID"),
                    object_ids=row.get("source_object_ids") or row.get("객체ID"),
                    number=row.get("표번호", ""), caption=row.get("표제목", ""),
                    content=" ".join(str(v) for k, v in row.items() if k in {"사업명", "부문", "지표명", "배출량", "전망값", "연도"}))
        matched = match_object_evidence(expected, candidates)
        records.append({"sheet": sheet, "row_evidence": evidence_ids(row), **asdict(matched)})
    return records


def persist_audit(output_path, input_path, data, context):
    path = Path(output_path).with_name(Path(output_path).stem + "_pipeline_audit.json")
    ledgers = {k: data.get(k, []) for k in sorted(config.INTERNAL_OBJECT_KEYS)}
    annotations = []
    for sheet, row in body_rows(data):
        notes = {k: row[k] for k in ("_axis_original", "_axis_validation", "_forecast_scope_review",
                                    "_merge_conflict", "_forecast_routing_review") if k in row}
        if notes:
            annotations.append({"sheet": sheet, "row": row, "annotations": notes})
    ledgers["row_contract_annotations"] = annotations
    payload = {"schema_version": "main-v9-selective-v1", "input_path": str(input_path),
               "excel_path": str(output_path), "model_accuracy_evaluated": context.get("model_accuracy_evaluated", False),
               "context": context, "ledgers": ledgers,
               "validation_report": data.get("validation_report", [])}
    return save_json(path, payload)
