from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .geometry import bbox_iou
from .io_utils import load_manifest, load_objects, read_jsonl, write_jsonl
from .models import ExperimentObject


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", str(value or "")).lower()
    return re.sub(r"\s+", " ", value).strip()


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def character_accuracy(reference: str, prediction: str) -> float:
    ref = normalize_text(reference)
    pred = normalize_text(prediction)
    if not ref:
        return 1.0 if not pred else 0.0
    return max(0.0, 1.0 - edit_distance(ref, pred) / len(ref))


def _row_key(row: list[str]) -> str:
    return "|".join(normalize_text(cell) for cell in row)


def _object_text(obj: ExperimentObject) -> str:
    return normalize_text(obj.searchable_text())


def _compatible(left: ExperimentObject, right: ExperimentObject) -> bool:
    if left.object_type == right.object_type:
        return True
    return {left.object_type, right.object_type} <= {"figure", "image"}


def match_score(golden: ExperimentObject, predicted: ExperimentObject) -> float:
    if golden.page_number != predicted.page_number or not _compatible(golden, predicted):
        return 0.0
    left, right = _object_text(golden), _object_text(predicted)
    text_score = SequenceMatcher(None, left[:5000], right[:5000]).ratio() if left and right else 0.0
    iou = bbox_iou(golden.bbox_norm, predicted.bbox_norm)
    if golden.bbox_norm and predicted.bbox_norm:
        return 0.7 * text_score + 0.3 * iou
    return text_score


def greedy_match(
    golden: list[ExperimentObject],
    predicted: list[ExperimentObject],
    threshold: float = 0.25,
) -> list[tuple[ExperimentObject, ExperimentObject, float]]:
    candidates: list[tuple[float, int, int]] = []
    for golden_index, expected in enumerate(golden):
        for predicted_index, actual in enumerate(predicted):
            score = match_score(expected, actual)
            if score >= threshold:
                candidates.append((score, golden_index, predicted_index))
    used_golden: set[int] = set()
    used_predicted: set[int] = set()
    matches = []
    for score, golden_index, predicted_index in sorted(candidates, reverse=True):
        if golden_index in used_golden or predicted_index in used_predicted:
            continue
        used_golden.add(golden_index)
        used_predicted.add(predicted_index)
        matches.append((golden[golden_index], predicted[predicted_index], score))
    return matches


def evaluate_engine(
    name: str,
    predicted: list[ExperimentObject],
    golden: list[ExperimentObject],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evaluated_types = {item.object_type for item in golden}
    evaluated_predictions = [
        item for item in predicted
        if item.object_type in evaluated_types
        or (item.object_type in {"figure", "image"} and evaluated_types & {"figure", "image"})
    ]
    matches = greedy_match(golden, evaluated_predictions)
    matched_golden = {expected.object_id for expected, _, _ in matches}
    matched_predicted = {actual.object_id for _, actual, _ in matches}
    recalls = len(matches) / len(golden) if golden else 0.0
    precision = len(matches) / len(evaluated_predictions) if evaluated_predictions else 0.0
    f1 = 2 * precision * recalls / (precision + recalls) if precision + recalls else 0.0

    char_scores: list[float] = []
    ious: list[float] = []
    golden_cells = matched_cells = 0
    golden_rows: set[str] = set()
    predicted_rows: set[str] = set()
    match_rows: list[dict[str, Any]] = []
    for expected, actual, score in matches:
        char_score = character_accuracy(expected.searchable_text(), actual.searchable_text())
        char_scores.append(char_score)
        if expected.bbox_norm and actual.bbox_norm:
            ious.append(bbox_iou(expected.bbox_norm, actual.bbox_norm))
        if expected.rows:
            expected_flat = [normalize_text(cell) for row in expected.rows for cell in row]
            actual_flat = [normalize_text(cell) for row in actual.rows for cell in row]
            golden_cells += len(expected_flat)
            matched_cells += sum(
                1 for index, cell in enumerate(expected_flat)
                if index < len(actual_flat) and cell == actual_flat[index]
            )
            golden_rows.update(_row_key(row) for row in expected.rows if any(row))
            predicted_rows.update(_row_key(row) for row in actual.rows if any(row))
        match_rows.append({
            "engine": name,
            "golden_id": expected.object_id,
            "predicted_id": actual.object_id,
            "page_number": expected.page_number,
            "object_type": expected.object_type,
            "match_score": round(score, 4),
            "character_accuracy": round(char_score, 4),
            "bbox_iou": round(bbox_iou(expected.bbox_norm, actual.bbox_norm), 4),
        })
    row_matches = len(golden_rows & predicted_rows)
    metrics = {
        "engine": name,
        "evaluation_mode": "golden",
        "object_count": len(predicted),
        "evaluated_object_count": len(evaluated_predictions),
        "golden_count": len(golden),
        "object_matches": len(matches),
        "object_recall": round(recalls, 4),
        "object_precision": round(precision, 4),
        "object_f1": round(f1, 4),
        "character_accuracy": round(sum(char_scores) / len(char_scores), 4) if char_scores else 0.0,
        "table_cell_accuracy": round(matched_cells / golden_cells, 4) if golden_cells else None,
        "table_row_recall": round(row_matches / len(golden_rows), 4) if golden_rows else None,
        "table_row_precision": round(row_matches / len(predicted_rows), 4) if predicted_rows else None,
        "mean_bbox_iou": round(sum(ious) / len(ious), 4) if ious else None,
        "unmatched_golden_ids": sorted(obj.object_id for obj in golden if obj.object_id not in matched_golden),
        "unmatched_prediction_ids": sorted(
            obj.object_id for obj in evaluated_predictions if obj.object_id not in matched_predicted
        ),
    }
    return metrics, match_rows


def proxy_metrics(name: str, objects: list[ExperimentObject], manifest: dict[str, Any]) -> dict[str, Any]:
    selected_pages = {int(item["page_number"]) for item in manifest["pages"]}
    expected = sum(len(items) for items in manifest.get("inventory_by_page", {}).values())
    detected_pages = {item.page_number for item in objects}
    return {
        "engine": name,
        "evaluation_mode": "proxy_not_accuracy",
        "object_count": len(objects),
        "selected_pages": len(selected_pages),
        "pages_with_objects": len(detected_pages),
        "page_coverage": round(len(detected_pages) / len(selected_pages), 4) if selected_pages else 0.0,
        "table_count": sum(item.object_type == "table" for item in objects),
        "figure_count": sum(item.object_type in {"figure", "image"} for item in objects),
        "nonempty_table_cells": sum(1 for item in objects for row in item.rows for cell in row if str(cell).strip()),
        "inventory_object_count": expected,
        "warning": "골든셋이 없어 객체 수와 커버리지만 계산했습니다. 정확도 지표로 해석하면 안 됩니다.",
    }


def write_golden_template(manifest_path: str | Path) -> Path:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    all_objects = load_objects(run_dir / "outputs" / "hybrid" / "objects.jsonl")
    for engine in ("pymupdf", "unlimited_ocr"):
        for candidate in load_objects(run_dir / "outputs" / engine / "objects.jsonl"):
            if not any(match_score(item, candidate) >= 0.88 for item in all_objects):
                all_objects.append(candidate)
    rows = []
    for obj in all_objects:
        value = obj.to_dict()
        value.update({
            "include": None,
            "golden_object_type": obj.object_type,
            "golden_text": obj.text,
            "golden_rows": obj.rows,
            "golden_bbox_norm": list(obj.bbox_norm) if obj.bbox_norm else None,
            "review_note": "",
        })
        rows.append(value)
    output = run_dir / "golden_template.jsonl"
    write_jsonl(output, rows)
    return output


def load_golden(path: str | Path) -> list[ExperimentObject]:
    objects: list[ExperimentObject] = []
    for index, row in enumerate(read_jsonl(path), start=1):
        if row.get("include") is not True:
            continue
        value = dict(row)
        value["object_id"] = str(row.get("golden_id") or row.get("object_id") or f"gold-{index}")
        value["engine"] = "golden"
        value["object_type"] = str(row.get("golden_object_type") or row.get("object_type") or "text")
        value["text"] = str(row.get("golden_text") if row.get("golden_text") is not None else row.get("text") or "")
        value["rows"] = row.get("golden_rows") if isinstance(row.get("golden_rows"), list) else row.get("rows", [])
        value["bbox_norm"] = row.get("golden_bbox_norm") or row.get("bbox_norm")
        allowed = {field.name for field in ExperimentObject.__dataclass_fields__.values()}
        objects.append(ExperimentObject.from_dict({key: item for key, item in value.items() if key in allowed}))
    return objects


def evaluate_run(
    manifest_path: str | Path,
    golden_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    golden = load_golden(golden_path) if golden_path else []
    summaries: list[dict[str, Any]] = []
    match_rows: list[dict[str, Any]] = []
    for engine in ("pymupdf", "unlimited_ocr", "hybrid"):
        objects = load_objects(run_dir / "outputs" / engine / "objects.jsonl")
        if golden:
            metrics, matches = evaluate_engine(engine, objects, golden)
            summaries.append(metrics)
            match_rows.extend(matches)
        else:
            summaries.append(proxy_metrics(engine, objects, manifest))
    return summaries, match_rows
