"""Persist and replay visual-merge inputs without another LLM call."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import config


SNAPSHOT_VERSION = 1


def _compact_document_object(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(row.get(key))
        for key in (
            "object_id", "object_type", "page_number", "sequence", "caption",
            "number", "section", "bbox", "rows", "metadata",
        )
        if key in row
    }


def snapshot_payload(raw_data: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "municipality_name": raw_data.get("municipality_name", "알 수 없음"),
    }
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = raw_data.get(sheet_key)
        if isinstance(rows, list):
            data[sheet_key] = deepcopy([row for row in rows if isinstance(row, dict)])
    observations = raw_data.get("chart_observations")
    data["chart_observations"] = deepcopy(
        [row for row in observations if isinstance(row, dict)]
        if isinstance(observations, list) else []
    )
    objects = raw_data.get("document_objects")
    data["document_objects"] = [
        _compact_document_object(row)
        for row in objects if isinstance(row, dict)
    ] if isinstance(objects, list) else []
    triage = raw_data.get("object_triage")
    data["object_triage"] = deepcopy(
        [row for row in triage if isinstance(row, dict)]
        if isinstance(triage, list) else []
    )
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "pipeline_version": getattr(config, "PIPELINE_VERSION", ""),
        "data": data,
    }


def write_visual_merge_snapshot(path: str | Path, raw_data: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot_payload(raw_data)
    with gzip.open(target, "wt", encoding="utf-8") as stream:
        json.dump(
            payload,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return target


def load_visual_merge_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    opener = gzip.open if source.suffix.casefold() == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("시각 병합 스냅샷 형식이 아닙니다")
    return payload


def _reset_legacy_observations(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["반영여부"] = "검토"
        row["병합상태"] = "candidate"
        row["병합차단사유"] = ""
        row["근거매칭상태"] = "legacy"
        row["근거객체ID"] = ""


def _visual_rows(cleaned: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        for row in cleaned.get(sheet_key, []):
            if not isinstance(row, dict) or str(row.get("데이터상태") or "") != "visual_only":
                continue
            rows.append({"_sheet": sheet_key, **deepcopy(row)})
    return rows


def _organize_merge_policy(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in {"legacy", "evidence"}:
        raise ValueError(f"지원하지 않는 병합 모드: {mode}")
    from agents.organizer_agent import OrganizerAgent

    raw = deepcopy(snapshot["data"])
    evaluation_objects = deepcopy(raw.get("document_objects", []))
    evaluation_triage = deepcopy(raw.get("object_triage", []))
    observations = raw.get("chart_observations", [])
    if mode == "legacy":
        raw.pop("document_objects", None)
        raw.pop("object_triage", None)
        _reset_legacy_observations(observations)

    previous_labeled = getattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    previous_evidence = getattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True)
    try:
        config.VISUAL_MERGE_LABELED_ENABLED = True
        config.VISUAL_EVIDENCE_MERGE_ENABLED = mode == "evidence"
        cleaned = OrganizerAgent().organize(raw)
    finally:
        config.VISUAL_MERGE_LABELED_ENABLED = previous_labeled
        config.VISUAL_EVIDENCE_MERGE_ENABLED = previous_evidence
    # 병합 정책만 A/B 변수로 두고 평가용 원문 객체 분모는 양쪽에 동일하게 유지한다.
    cleaned["document_objects"] = evaluation_objects
    cleaned["object_triage"] = evaluation_triage
    return cleaned


def _merge_policy_result(cleaned: dict[str, Any], mode: str) -> dict[str, Any]:

    candidate_rows = [
        deepcopy(row) for row in cleaned.get("chart_observations", [])
        if isinstance(row, dict)
    ]
    statuses = Counter(str(row.get("병합상태") or "needs_review") for row in candidate_rows)
    matching = Counter(str(row.get("근거매칭상태") or "") for row in candidate_rows)
    review_rows = [row for row in candidate_rows if row.get("병합상태") == "needs_review"]
    output_rows = _visual_rows(cleaned)
    return {
        "mode": mode,
        "metrics": {
            "candidate_count": len(candidate_rows),
            "visual_output_rows": len(output_rows),
            "decision_counts": dict(sorted(statuses.items())),
            "evidence_match_counts": dict(sorted(matching.items())),
            "needs_review_reason_coverage": (
                round(sum(bool(row.get("병합차단사유")) for row in review_rows) / len(review_rows), 4)
                if review_rows else None
            ),
        },
        "rows": output_rows,
        "candidates": candidate_rows,
    }


def run_merge_policy(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    return _merge_policy_result(_organize_merge_policy(snapshot, mode), mode)


def compare_merge_policies_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """정책 지표와 평가용 전체 정제 데이터셋을 함께 반환한다."""
    legacy_data = _organize_merge_policy(snapshot, "legacy")
    evidence_data = _organize_merge_policy(snapshot, "evidence")
    legacy = _merge_policy_result(legacy_data, "legacy")
    evidence = _merge_policy_result(evidence_data, "evidence")
    legacy_rows = legacy["metrics"]["visual_output_rows"]
    evidence_rows = evidence["metrics"]["visual_output_rows"]
    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "legacy": legacy,
        "evidence": evidence,
        "comparison": {
            "visual_output_row_delta": evidence_rows - legacy_rows,
            "legacy_visual_output_rows": legacy_rows,
            "evidence_visual_output_rows": evidence_rows,
        },
    }, {
        "legacy": legacy_data,
        "evidence": evidence_data,
    })


def compare_merge_policies(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_merge_policies_with_data(snapshot)
    return payload
