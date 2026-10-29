"""Object-level OCR supplementation and final-output lineage (no model calls)."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

import config
from utils.document_objects import DocumentObject
from utils.selective_ocr import evidence_id


def select_supplement_objects(native_objects, merged_objects, *, processed_fingerprints=None):
    """Return only selected, changed OCR payloads linked to native candidates.

    The caller extracts native text first and sends these objects to the
    object's extraction path. Merely loading or enriching an inventory entry
    does not count as a domain-data extraction.
    """
    prior = {obj.object_id: obj for obj in native_objects}
    consumed = set(processed_fingerprints or [])
    result = []
    for obj in merged_objects:
        status = str(obj.metadata.get("ocr_status") or "")
        application = str(obj.metadata.get("ocr_application_status") or "")
        if not obj.metadata.get("ocr_source_file") or application.startswith("held_"):
            continue
        if status not in {"replaced_low_confidence_native", "filled_native_cells", "enriched_native", "added_missing_object"}:
            continue
        aliases = set(obj.metadata.get("source_object_ids") or []) | {obj.object_id}
        native = prior.get(obj.object_id) or next((prior[a] for a in aliases if a in prior), None)
        if native is None and not obj.metadata.get("source_evidence_id"):
            continue
        if native is not None and native.rows == obj.rows and native.text == obj.text:
            continue
        payload = json.dumps([evidence_id(obj), obj.rows, obj.text], ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        obj.metadata["ocr_extraction_fingerprint"] = fingerprint
        if fingerprint in consumed:
            obj.metadata["ocr_reextract_status"] = "skipped_unchanged"
            continue
        consumed.add(fingerprint)
        obj.metadata["ocr_reextract_status"] = "pending"
        result.append(obj)
    return result


def _anchors(row):
    result = set()
    for key in ("근거ID", "표ID", "객체ID", "근거객체ID", "object_id", "evidence_id",
                "source_object_ids", "source_evidence_id", "source_evidence_ids"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            result.update(str(v) for v in value if v)
        elif value:
            result.update(re.split(r"[,;|\s]+", str(value)))
    lineage = row.get("_ocr_lineage")
    if isinstance(lineage, dict):
        result.update(str(lineage.get(k)) for k in ("object_id", "evidence_id") if lineage.get(k))
    return result


def link_ocr_rows(data, objects):
    """Link explicit extraction anchors, never merely a shared page."""
    for key in config.EXTRACTION_SHEETS:
        for row in data.get(key, []):
            if not isinstance(row, dict):
                continue
            anchors = _anchors(row)
            matches = [o for o in objects if anchors & {
                evidence_id(o), o.object_id, *o.metadata.get("source_object_ids", [])}]
            if len(matches) != 1:
                continue
            obj = matches[0]
            row.setdefault("근거ID", evidence_id(obj))
            row.setdefault("표ID", obj.object_id)
            row.setdefault("출처페이지", obj.page_number)
            row["_ocr_lineage"] = {
                "object_id": obj.object_id, "evidence_id": evidence_id(obj),
                "page": obj.page_number, "bbox": obj.bbox,
                "source_object_ids": obj.metadata.get("source_object_ids", []),
                "engine": obj.metadata.get("engine"),
                "source_engines": obj.metadata.get("source_engines", []),
                "source_file": obj.metadata.get("ocr_source_file"),
                "document_trust": obj.metadata.get("ocr_document_trust"),
                "rules": [obj.metadata.get("ocr_selection_method"), obj.metadata.get("ocr_status")],
            }


def connection_audit(imported, objects, raw_data, final_data):
    """Distinguish import, reading, extraction, and final data-sheet reflection."""
    records = []
    for obj in objects:
        anchors = {obj.object_id, evidence_id(obj), *obj.metadata.get("source_object_ids", [])}
        def linked(data):
            return [{"sheet": k, "row": deepcopy(row)} for k in config.EXTRACTION_SHEETS
                    for row in data.get(k, []) if isinstance(row, dict) and _anchors(row) & anchors]
        extracted, reflected = linked(raw_data), linked(final_data)
        records.append({
            "object_id": obj.object_id, "evidence_id": evidence_id(obj), "page": obj.page_number,
            "bbox": obj.bbox, "aliases": obj.metadata.get("source_object_ids", []),
            "engine": obj.metadata.get("engine"), "source_file": obj.metadata.get("ocr_source_file"),
            "load_success": True, "reading_success": bool(obj.rows or obj.text),
            "business_extraction_success": bool(extracted), "body_reflection_success": bool(reflected),
            "reextract_status": obj.metadata.get("ocr_reextract_status", "pending"),
            "extracted_rows": extracted, "final_rows": reflected,
            "status": "body_reflected" if reflected else "held_no_final_row",
            "note": "Connection evidence only; factual accuracy requires a reviewed golden set.",
        })
    return {
        **deepcopy(imported.get("ocr_import_audit", {})), "connections": records,
        "summary": {
            "loaded": len(imported.get("ocr_document_objects", [])), "consumed": len(objects),
            "business_extracted": sum(r["business_extraction_success"] for r in records),
            "body_reflected": sum(r["body_reflection_success"] for r in records),
        },
    }
