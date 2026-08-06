from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .geometry import bbox_iou
from .io_utils import load_manifest, load_objects, save_objects, write_jsonl
from .models import ExperimentObject


_HARD_CATEGORIES = {"unconfirmed", "partial", "complex_table", "visual", "manual"}


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", (value or "").lower())


def _text_similarity(left: ExperimentObject, right: ExperimentObject) -> float:
    a = _normalize(left.searchable_text())
    b = _normalize(right.searchable_text())
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:5000], b[:5000]).ratio()


def object_similarity(left: ExperimentObject, right: ExperimentObject) -> float:
    if left.page_number != right.page_number:
        return 0.0
    compatible = {left.object_type, right.object_type} <= {"figure", "image"}
    if left.object_type != right.object_type and not compatible:
        return 0.0
    text_score = _text_similarity(left, right)
    iou = bbox_iou(left.bbox_norm, right.bbox_norm)
    if not left.bbox_norm or not right.bbox_norm:
        return text_score
    return 0.7 * text_score + 0.3 * iou


def _nonempty_cells(obj: ExperimentObject) -> int:
    return sum(1 for row in obj.rows for cell in row if str(cell or "").strip())


def merge_objects(
    baseline: list[ExperimentObject],
    uocr: list[ExperimentObject],
    manifest: dict[str, Any],
) -> tuple[list[ExperimentObject], list[dict[str, Any]]]:
    """보수적 규칙으로 native text를 유지하고 어려운 표/그림만 보완한다."""
    categories = {
        int(item["page_number"]): set(item.get("categories") or [])
        for item in manifest["pages"]
    }
    output = list(baseline)
    decisions: list[dict[str, Any]] = []

    def decision(candidate: ExperimentObject, action: str, reason: str, matched: ExperimentObject | None = None) -> None:
        decisions.append({
            "page_number": candidate.page_number,
            "candidate_id": candidate.object_id,
            "candidate_type": candidate.object_type,
            "matched_baseline_id": matched.object_id if matched else "",
            "action": action,
            "reason": reason,
            "categories": sorted(categories.get(candidate.page_number, set())),
            "candidate_cells": _nonempty_cells(candidate),
            "baseline_cells": _nonempty_cells(matched) if matched else 0,
        })

    for candidate in uocr:
        page_objects = [item for item in output if item.page_number == candidate.page_number]
        matches = sorted(
            ((object_similarity(candidate, item), item) for item in page_objects),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = matches[0] if matches else (0.0, None)
        hard_page = bool(categories.get(candidate.page_number, set()) & _HARD_CATEGORIES)

        if best is not None and best_score >= 0.88:
            decision(candidate, "skip_duplicate", f"기준선 객체와 유사도 {best_score:.3f}", best)
            continue

        if candidate.object_type == "table":
            baseline_tables = [item for item in page_objects if item.object_type == "table"]
            table_matches = sorted(
                ((object_similarity(candidate, item), item) for item in baseline_tables),
                key=lambda pair: pair[0],
                reverse=True,
            )
            table_score, table_match = table_matches[0] if table_matches else (0.0, None)
            candidate_cells = _nonempty_cells(candidate)
            baseline_cells = _nonempty_cells(table_match) if table_match else 0
            if not candidate.rows or candidate_cells < 2:
                decision(candidate, "reject", "파싱 가능한 표 셀이 부족함", table_match)
            elif table_match is None:
                output.append(candidate)
                decision(candidate, "add", "PyMuPDF에 대응 표가 없음")
            elif hard_page and candidate_cells >= max(baseline_cells + 5, int(baseline_cells * 1.20)):
                output = [item for item in output if item.object_id != table_match.object_id]
                candidate.metadata["replaced_object_id"] = table_match.object_id
                output.append(candidate)
                decision(candidate, "replace", f"어려운 페이지에서 유효 셀 {baseline_cells}→{candidate_cells}", table_match)
            elif table_score < 0.35 and hard_page:
                output.append(candidate)
                decision(candidate, "add", f"별도 표로 판단(유사도 {table_score:.3f})", table_match)
            else:
                decision(candidate, "keep_baseline", "기준선 표를 대체할 정량 근거 부족", table_match)
            continue

        if candidate.object_type in {"figure", "image"}:
            if hard_page and best_score < 0.50:
                output.append(candidate)
                decision(candidate, "add", "어려운 페이지의 미검출 시각 객체", best)
            else:
                decision(candidate, "keep_baseline", "대응 시각 객체가 있거나 실험 대상 페이지가 아님", best)
            continue

        if candidate.object_type in {"text", "title"}:
            baseline_chars = sum(len(item.text) for item in page_objects if item.object_type in {"text", "title"})
            if baseline_chars < 100 and len(candidate.text.strip()) >= 20:
                output.append(candidate)
                decision(candidate, "add", f"native text가 희박함({baseline_chars}자)", best)
            else:
                decision(candidate, "keep_baseline", "검색 가능한 PDF의 native text 우선", best)
            continue

        if hard_page and best_score < 0.45:
            output.append(candidate)
            decision(candidate, "add", "어려운 페이지의 신규 객체 유형", best)
        else:
            decision(candidate, "reject", "병합 규칙에 해당하지 않음", best)

    output.sort(key=lambda item: (item.page_number, item.object_type, item.sequence, item.object_id))
    return output, decisions


def build_hybrid(manifest_path: str | Path) -> tuple[Path, list[ExperimentObject], list[dict[str, Any]]]:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    baseline = load_objects(run_dir / "outputs" / "pymupdf" / "objects.jsonl")
    uocr = load_objects(run_dir / "outputs" / "unlimited_ocr" / "objects.jsonl")
    objects, decisions = merge_objects(baseline, uocr, manifest)
    output_dir = run_dir / "outputs" / "hybrid"
    output_path = output_dir / "objects.jsonl"
    save_objects(output_path, objects)
    write_jsonl(output_dir / "decisions.jsonl", decisions)
    return output_path, objects, decisions
