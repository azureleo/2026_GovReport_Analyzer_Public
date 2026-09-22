"""Bounded reading of uncertain objects; no model invocation in selection/preflight."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import config

POLICY_VERSION = "bounded-review-v1"


def policy_snapshot():
    return {
        "version": POLICY_VERSION,
        "enabled": bool(getattr(config, "VISION_REVIEW_ENABLED", False)),
        "max_objects": max(0, int(getattr(config, "VISION_REVIEW_MAX_OBJECTS", 16))),
        "max_per_page": max(0, int(getattr(config, "VISION_REVIEW_MAX_PER_PAGE", 1))),
        "max_calls": max(0, int(getattr(config, "VISION_REVIEW_MAX_CALLS", 16))),
        "max_seconds": max(0.0, float(getattr(config, "VISION_REVIEW_MAX_SECONDS", 600))),
        "call_timeout": max(0.0, float(getattr(config, "VISION_REVIEW_CALL_TIMEOUT", 120))),
    }


def promote_review_candidates(decisions, objects, pages, *, backend="vlm"):
    """Deterministic selection. An image's size is a routing hint, not proof of data."""
    policy = policy_snapshot()
    if not policy["enabled"] or backend != "vlm":
        return
    page_map = {page.page_number: page for page in pages}
    object_map = {obj.object_id: obj for obj in objects}
    ranked = []
    for row in decisions:
        if row.action != "review_required" or row.object_type not in {"image", "figure"}:
            continue
        page = page_map.get(row.page_number)
        obj = object_map.get(row.object_id)
        if page is None or obj is None or row.context_only or row.render_proxy:
            continue
        area = 0.0
        if row.bbox and page.width > 0 and page.height > 0:
            x0, y0, x1, y1 = row.bbox
            area = max(0, min(page.width, x1) - max(0, x0)) * max(0, min(page.height, y1) - max(0, y0)) / (page.width * page.height)
        sparse = len(re.sub(r"\s+", "", page.text or "")) <= 250
        structured = bool(re.search(r"표|도식|체계|비전|전략|조직|구조|흐름|diagram|table|chart", row.caption, re.I))
        if sparse and area >= 0.35:
            reason, priority = "sparse_page_large_image", 0
        elif structured:
            reason, priority = "structured_caption", 1
        elif area >= 0.5:
            reason, priority = "large_image", 2
        else:
            row.reasons.append("review_hold:insufficient_selection_signal")
            continue
        ranked.append((priority, row.page_number, -area, row.object_id, reason, row))
    counts, seen = {}, set()
    for _, page, _, _, reason, row in sorted(ranked):
        identity = row.physical_object_id or row.evidence_id
        if identity in seen:
            row.reasons.append("review_hold:duplicate_physical_object")
            continue
        if len(seen) >= policy["max_objects"] or counts.get(page, 0) >= policy["max_per_page"]:
            row.reasons.append("review_hold:selection_limit")
            continue
        seen.add(identity)
        counts[page] = counts.get(page, 0) + 1
        row.initial_action = row.action
        row.promotion_reason = reason
        row.action = "ocr_required"
        row.backend = backend
        row.status = "promoted_review"
        row.terminal_reason = "제한적 판독 후보: 예산 승인 전"
        row.reasons.append("review_promoted:" + reason)


def candidate_manifest(decisions, images):
    inputs = []
    for page, image in images:
        inputs.append({
            "page": page.page_number,
            "object_ids": image.get("source_object_ids", []),
            "evidence_ids": image.get("source_evidence_ids", []),
            "physical_object_ids": image.get("source_physical_object_ids", []),
            "review_promoted": bool(image.get("review_promoted")),
            "image_sha256": hashlib.sha256(str(image.get("base64", "")).encode("ascii")).hexdigest(),
            "render_variant": image.get("render_variant", ""),
        })
    payload = json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    regular = sum(not item["review_promoted"] for item in inputs)
    extra = len(inputs) - regular
    policy = policy_snapshot()
    can_probe = (policy["enabled"] and policy["max_calls"] > 0
                 and policy["max_seconds"] > 0 and policy["call_timeout"] > 0)
    ready = bool(regular or (extra and can_probe))
    size = max(1, int(getattr(config, "IMAGE_ANALYSIS_BATCH_SIZE", 1)))
    held = sum(row.action == "review_required" and row.object_type in {"image", "figure"} for row in decisions)
    return {
        "policy": policy, "candidate_sha256": hashlib.sha256(payload).hexdigest(),
        "input_count": len(inputs), "regular_inputs": regular, "review_inputs": extra,
        "expected_root_batches": (regular + size - 1) // size + extra,
        "held_visual_objects": held,
        "comparison_ready": ready,
        "warning": "판독 가능 후보 없음 또는 예산 0: 모델 비교 불가" if not ready else "정확도는 별도 정답 표본으로 평가. 실제 시도/보류 수와 예산 소진 여부도 비교하세요.",
        "inputs": inputs, "decisions": [row.to_dict() for row in decisions],
    }


class ReviewBudget:
    """Shared across pipeline retries; durable reservations survive interruptions.

    Review tasks execute serially. Pending reservations conservatively consume their
    full timeout after an interrupted process, preventing a resume from resetting it.
    """
    def __init__(self, run_state=None):
        self.policy = policy_snapshot()
        self.path = Path(run_state.root) / "vision_review_budget.json" if run_state else None
        self.entries = {}
        self.results = {}
        self.lock = threading.RLock()
        if self.path and run_state.resume and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("policy") != self.policy:
                raise ValueError("Vision review budget policy changed; use a new run-state directory")
            self.entries = data["entries"]
        elif self.path:
            self._save()

    def _save(self):
        if self.path:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps({"policy": self.policy, "entries": self.entries}, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    def claim(self, identity, page, image_hash):
        with self.lock:
            if not self.policy["enabled"]:
                return 0.0, "review_hold:disabled"
            if identity in self.entries:
                return 0.0, "review_hold:already_attempted"
            spent = sum(entry["charged_seconds"] for entry in self.entries.values())
            if len(self.entries) >= min(self.policy["max_objects"], self.policy["max_calls"]):
                return 0.0, "review_hold:object_or_call_budget"
            if sum(entry["page"] == page for entry in self.entries.values()) >= self.policy["max_per_page"]:
                return 0.0, "review_hold:page_budget"
            remaining = min(self.policy["call_timeout"], self.policy["max_seconds"] - spent)
            if remaining <= 0:
                return 0.0, "review_hold:time_budget"
            self.entries[identity] = {"page": page, "image_sha256": image_hash, "charged_seconds": remaining, "status": "reserved"}
            self._save()
            return remaining, ""

    def finish(self, identity, seconds, status):
        with self.lock:
            self.entries[identity].update(charged_seconds=max(0, seconds), status=status)
            self._save()

    def snapshot(self):
        with self.lock:
            return {"policy": self.policy, "calls_reserved": len(self.entries),
                    "seconds_charged": round(sum(x["charged_seconds"] for x in self.entries.values()), 3),
                    "entries": {key: dict(value) for key, value in self.entries.items()}}
