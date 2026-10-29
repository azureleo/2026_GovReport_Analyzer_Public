"""Deterministic PDF -> saved OCR -> actual business Excel, no paid model calls.

Only llm_client.call_text is replaced. This is a connection fixture, not a
measurement of model/OCR accuracy or production inference speed.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# This developer fixture must never inherit a parent checkout's .env.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import fitz
import openpyxl
import config
from agents.supervisor import Supervisor
from utils.document_objects import build_document_objects, consolidate_document_objects
from utils.integration_audit import save_json
from utils.pdf_reader import extract_pdf


def create_fixture(directory):
    source = directory / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 40), "Carbon emissions forecast BAU", fontsize=12)
        for y in (100, 130, 160):
            page.draw_line((40, y), (460, y))
        for x in (40, 180, 320, 460):
            page.draw_line((x, 100), (x, 160))
        for x, value in ((50, "Sector"), (190, "Year"), (330, "Value")):
            page.insert_text((x, 120), value)
        page.insert_text((50, 150), "building")
        page.insert_text((190, 150), "2030")
        doc.save(source)
    document = extract_pdf(source, render_graph_pages=False)
    objects = consolidate_document_objects(build_document_objects(document.pages))
    table = next(obj for obj in objects if obj.object_type == "table")
    obj = table.to_dict()
    obj.update(object_id="offline-ocr-table", text="배출량 전망 BAU 지역전체 987.6 천톤CO2eq", caption="배출량 전망 BAU 지역전체")
    obj["rows"][-1][-1] = "987.6"
    obj["metadata"].update(engine="unlimited_ocr", source_object_ids=[table.object_id],
        target_sheets=["emissions_forecast"], document_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    ocr = directory / "ocr"
    ocr.mkdir()
    (ocr / "project_document_objects.jsonl").write_text(json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")
    save_json(directory / "fixture_contract.json", {"version": "offline-connection-v1", "human_reviewed": False,
        "native_missing_value": True, "saved_ocr_supplies": 987.6,
        "expected_sheet": "05_배출전망", "expected_scope": "지역전체",
        "note": "Synthetic connection assertion, not a real-document golden set."})
    return source, ocr


def fixed_external_response(prompt, **kwargs):
    if "지자체명만" in prompt or "municipality_name" in prompt:
        return json.dumps({"municipality_name": "테스트시", "지자체명": "테스트시"}, ensure_ascii=False)
    keys = re.findall(r'\{\s*"([a-z_]+)"\s*:\s*\[', prompt)
    if not keys:
        return '{"assessment":"offline fixture only","quality_level":"보통"}'
    key = keys[-1]
    rows = []
    if key == "document_meta":
        rows = [{"지자체명": "테스트시", "계획명": "기본계획", "계획시작연도": 2024, "계획종료연도": 2033}]
    elif key == "emissions_forecast" and "987.6" in prompt:
        rows = [{"지자체명": "테스트시", "전망범위": "지역전체", "시나리오": "BAU", "부문": "건물",
                 "세부부문": "가정", "연도": 2030, "전망값": 987.6, "단위": "천톤CO2eq", "출처페이지": 1}]
    return json.dumps({key: rows}, ensure_ascii=False)


def read_forecasts(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=False)
    values = list(wb["05_배출전망"].values)
    wb.close()
    return [dict(zip(values[0], row)) for row in values[1:]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if directory.exists():
        parser.error("Use a new output directory; the fixture never overwrites prior results.")
    directory.mkdir(parents=True)
    source, ocr = create_fixture(directory)
    fixture_settings = {"FULL_DOCUMENT_SCAN": True, "OCR_RESULTS_DIR": str(ocr), "OCR_BACKEND": "directory",
        # 1.01 deliberately selects the synthetic missing-cell table, not a production recommendation.
        "OCR_NATIVE_CONFIDENCE_THRESHOLD": 1.01, "RUN_STATE_ENABLED": False,
        "SEMANTIC_ROUTING_ENABLED": False, "EXTRACTION_OBJECT_BATCHING_ENABLED": False,
        "MAX_BATCH_CHARS": None, "MAX_BATCH_TABLE_CELLS": None}
    summary = {"fixture_version": "offline-connection-v1", "model_calls_are_mocked": True,
               "model_accuracy_evaluated": False, "production_speed_evaluated": False,
               "conditions": {"same_pdf": True, "same_expected_fixture": True, "image_calls": False}, "runs": []}
    with patch.multiple(config, **fixture_settings):
        for name, enabled in (("baseline_ocr_off", False), ("integrated_ocr_on", True)):
            destination = directory / f"{name}.xlsx"
            started = time.perf_counter()
            with patch.object(config, "SELECTIVE_OCR_ENABLED", enabled), \
                 patch("utils.llm_client.call_text", side_effect=fixed_external_response) as boundary, \
                 (directory / f"{name}.log").open("w", encoding="utf-8") as log, redirect_stdout(log):
                supervisor = Supervisor()
                result = supervisor.run(source, destination, include_images=False, max_pipeline_retries=1)
            rows = read_forecasts(result)
            summary["runs"].append({"name": name, "excel": str(result), "forecast_rows": rows,
                "expected_value_reflected": len(rows) == 1 and rows[0]["전망값"] == 987.6,
                "external_boundary_calls": boundary.call_count, "local_fixture_seconds": time.perf_counter() - started,
                "outcome": supervisor.last_outcome})
    assert not summary["runs"][0]["forecast_rows"], "Disabled OCR unexpectedly injected a row"
    assert summary["runs"][1]["expected_value_reflected"], "OCR value did not reach the business Excel cell"
    save_json(directory / "comparison.json", summary)
    print(json.dumps({"comparison": str(directory / "comparison.json"),
                      "status": "connection_passed", "real_model_accuracy": "not_evaluated"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
