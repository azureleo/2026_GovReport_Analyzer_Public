"""Execution boundary for the opt-in saved Vision recovery experiment."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import base64
import importlib.util
import os
from pathlib import Path
import sys
import time

from utils.vision_recovery_workflow import (
    canonical, evidence_ids, file_hash, prepare_bindings, projected_cell,
    verify_saved_workbook, write_json,
)

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def offline_guard():
    """Audit hooks cannot be removed; disable this closure after the scoped run."""
    active = [True]
    def audit(event, args):
        if active[0] and (event.startswith(("socket.connect", "socket.getaddrinfo", "subprocess."))
                          or event in ("os.system", "os.startfile")):
            raise PermissionError("Offline replay prohibits network and subprocess calls")
    sys.addaudithook(audit)
    try:
        yield
    finally:
        active[0] = False


@contextmanager
def settings(**values):
    import config
    sentinel = object()
    previous = {k: getattr(config, k, sentinel) for k in values}
    try:
        for k, v in values.items():
            setattr(config, k, v)
        yield
    finally:
        for k, v in previous.items():
            if v is sentinel:
                delattr(config, k)
            else:
                setattr(config, k, v)


def organize(raw, improved):
    from agents.organizer_agent import OrganizerAgent
    prepared, events = prepare_bindings(raw) if improved else (deepcopy(raw), [])
    with settings(VISION_RECOVERY_BINDINGS_ENABLED=improved):
        result = OrganizerAgent().organize(prepared)
    return result, events


def save_and_verify(raw, out, improved):
    """Use the project's writer, not a parallel replacement Excel implementation."""
    import config
    from agents.excel_agent import ExcelAgent
    from utils.semantic_contract_guard import apply_semantic_contract_guards
    out.mkdir(parents=True, exist_ok=False)
    cleaned, events = organize(raw, improved)
    guards = apply_semantic_contract_guards(cleaned)
    path = ExcelAgent().write(deepcopy(cleaned), out / "result.xlsx")
    if Path(path).resolve() != (out / "result.xlsx").resolve():
        raise RuntimeError("Writer selected an unexpected output path")
    cells = verify_saved_workbook(path, cleaned, config)
    # A second deterministic pass starts with fresh copies, not mutated rows.
    again, _ = organize(raw, improved)
    apply_semantic_contract_guards(again)
    comparable = lambda x: {k: x.get(k, []) for k in config.EXTRACTION_SHEETS}
    cells["deterministic"] = canonical(comparable(cleaned)) == canonical(comparable(again))
    cells["passed"] = cells["passed"] and cells["deterministic"]
    write_json(out / "backend_output.json", cleaned)
    write_json(out / "binding_changes.json", events)
    write_json(out / "visual_semantic_audit.json", cleaned.get("visual_semantic_audit", []))
    write_json(out / "visual_context_audit.json", cleaned.get("visual_context_audit", []))
    write_json(out / "semantic_guards.json", guards)
    write_json(out / "cell_validation.json", cells)
    return cleaned, cells


def load_scorer(path):
    """Reuse the frozen, previously reviewed sample evaluator, never execute main()."""
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("_vision_frozen_sample_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def score_reading(scorer, model, raw, workbook):
    """Sample-specific score; unsupported final-cell identities remain unmeasured."""
    predictions, exclusions, extraction_errors = [], [], []
    try:
        predictions, exclusions = scorer.extract(model, raw.get("chart_observations", []))
    except (KeyError, ValueError, TypeError, StopIteration, IndexError) as exc:
        # Fail the scoring gate rather than fabricate zero/partial accuracy.
        raise ValueError("Frozen evaluator cannot interpret new reading; manual review required: "
                         + type(exc).__name__ + ": " + str(exc)) from exc
    aligned, extras = scorer.align(predictions)
    business, _ = scorer.load_xlsx(workbook)
    comparisons = []
    for gold in scorer.GOLD:
        matches = aligned.get(gold["id"], [])
        pred = matches[0] if matches else None
        flags = {
            "value_ok": bool(pred and scorer.is_equal(pred["value"], gold["value"])),
            "unit_ok": bool(pred and (not gold["unit"] or scorer.unit(pred["unit"]) == scorer.unit(gold["unit"]))),
            "time_ok": bool(pred and (gold["time"] is None or scorer.is_equal(pred["time"], gold["time"]))),
            "context_ok": bool(pred and pred["context_ok"]),
        }
        found, partial = scorer.final_match(gold, business)
        comparisons.append({"id": gold["id"], "model": model, "page": gold["page"],
                            "pred_id": pred["id"] if pred else "", "present": bool(pred),
                            **flags, "complete": all(flags.values()), "duplicate_candidates": len(matches),
                            "final_ok": bool(found),
                            "final_cells": [f"{r['sheet']}!A{r['row']}" for r in found],
                            "final_status": "matched" if found else "name_only" if partial else "not_confirmed"})
    return {"comparisons": comparisons, "gold_count": len(scorer.GOLD),
            "value_correct": sum(x["value_ok"] for x in comparisons),
            "complete": sum(x["complete"] for x in comparisons),
            "final_confirmed": sum(x["final_ok"] for x in comparisons),
            "present": sum(x["present"] for x in comparisons),
            "exclusions": exclusions, "extraction_errors": extraction_errors,
            "extra_predictions": [{k: v for k, v in x.items() if k != "raw"} for x in extras],
            "limitations": ["AI-reviewed draft gold; development sample, not holdout",
                            "Final-cell matcher covers only the identities supported by the original evaluator",
                            "Unconfirmed final cells are not automatically factual errors",
                            "Parsed observations, not raw model response, are scored"]}


def cell_losses(before, after):
    """Ignore row position; report vanished business rows rather than silent success."""
    from collections import Counter
    def index(check):
        result = Counter()
        for row in check["trace"]:
            fields = {k: v["value"] for k, v in row["cells"].items()}
            result[canonical([row["sheet_key"], fields])] += 1
        return result
    return list((index(before)-index(after)).elements())


def observations_from_response(parsed, task):
    """Rebuild candidates from a successful response, never from stale decisions.

    `extracted` describes response availability, NOT factual correctness. All
    confidence, value, unit, scope and semantic gates still run normally.
    """
    from agents.image_agent import ImageAgent
    from utils.evidence_merge import build_evidence_catalog

    if (not isinstance(parsed, dict) or parsed.get("type") != "chart_table"
            or not isinstance(parsed.get("table"), list) or not parsed["table"]
            or not all(isinstance(row, dict) for row in parsed["table"])):
        raise ValueError("Successful structured response required")
    page = task.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("Invalid response task page")
    for field in ("object_ids", "evidence_ids"):
        ids = task.get(field)
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0]:
            raise ValueError("Exactly one physical object and evidence ID required")
    if (type(parsed.get("page_number")) is not int or parsed["page_number"] != page
            or parsed.get("source_evidence_ids") != task["evidence_ids"]
            or parsed.get("source_object_ids") != task["object_ids"]
            or parsed.get("source_bbox") != task.get("bbox")):
        raise ValueError("Saved response provenance differs from the frozen task")
    objects = [{"object_id": task["object_ids"][0], "page_number": page,
                "object_type": "image", "bbox": deepcopy(task.get("bbox")),
                "metadata": {"evidence_id": task["evidence_ids"][0],
                             "final_status": "extracted", "ocr_status": "completed",
                             "terminal_reason": "Validated successful Vision response"}}]
    target = {"municipality_name": "알 수 없음", "document_objects": objects,
              "chart_observations": []}
    # Recompute evidence/merge decisions, while preserving the saved response.
    with settings(VISUAL_EVIDENCE_MERGE_ENABLED=True):
        ImageAgent()._merge_image_results(
            target, [deepcopy(parsed)], "알 수 없음",
            evidence_catalog=build_evidence_catalog(objects, []))
    if not target["chart_observations"]:
        raise ValueError("Response produced no observations; keep original readings")
    return target["chart_observations"]


def read_one_object(source, task, model, timeout_seconds):
    """Exactly one bounded existing Vision call. No gold, old values, or answer hints."""
    import fitz
    import config
    from agents.image_agent import ImageAgent
    from utils import llm_client
    with fitz.open(source) as pdf:
        if not 1 <= task["page"] <= len(pdf):
            raise ValueError("Page outside source PDF")
        page = pdf[task["page"]-1]
        # Keep headings/footnotes. This sample has one physical object per page.
        pix = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        image = {"base64": base64.b64encode(pix.tobytes("png")).decode("ascii"),
                 "width": pix.width, "height": pix.height,
                 "source_evidence_ids": task["evidence_ids"],
                 "source_object_ids": task["object_ids"], "bbox": task.get("bbox")}
    providers, models = dict(config.STAGE_PROVIDERS), dict(config.STAGE_MODELS)
    providers["vision"], models["vision"] = "codex", model["model"]
    with settings(STAGE_PROVIDERS=providers, STAGE_MODELS=models,
                  CODEX_COMMAND=model["command"], MAX_RETRIES=1,
                  LLM_CACHE_ENABLED=False, LLM_CAPACITY_FALLBACK_ENABLED=False,
                  VISION_NEGATIVE_REVALIDATION_ENABLED=False):
        agent = ImageAgent()
        llm_client.reset_llm_stats()
        with llm_client.limited_vision_call(timeout_seconds):
            parsed = agent._chart_to_table(image, task["page"], "알 수 없음", fail_fast=True)
        observations = observations_from_response(parsed, task)
        return observations, {"parsed_response": parsed, "llm_stats": llm_client.get_llm_stats()}
