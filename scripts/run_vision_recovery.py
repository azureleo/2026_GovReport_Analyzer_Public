"""Offline replay/reuse and explicitly authorized bounded Vision retry.

No main.py invocation; original snapshots, workbooks and checkpoints stay read-only.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import gzip
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.vision_recovery_workflow import (
    VERSION, build_retry_queue, canonical, file_hash, merge_fresh,
    read_json, record_response_success, validate_budget, write_json,
)

PROFILE = {
    "PYTHON_DOTENV_DISABLED": "1", "READING_PIPELINE_ENABLED": "1",
    "REFERENCE_ENRICHMENT_ENABLED": "0", "PROVENANCE_ENABLED": "1",
    "DATA_STATUS_ENABLED": "1", "CODEBOOK_SHEET_ENABLED": "0",
    "LLM_CACHE_ENABLED": "0", "RUN_STATE_ENABLED": "0",
    "VISUAL_MERGE_LABELED_ENABLED": "1", "VISUAL_EVIDENCE_MERGE_ENABLED": "1",
    "SEMANTIC_OVEREXTRACTION_GUARD_ENABLED": "1",
    "SOURCE_VERIFICATION_ENABLED": "0", "SOURCE_OBJECT_INVENTORY_ENABLED": "0",
    "REDUCTION_TARGET_CONTEXT_MODE": "off",
}


def resolve(value):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()


def new_directory(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=False)
    return path


def code_hashes():
    files = [ROOT / "config.py", Path(__file__), ROOT / "data/reading_mapping_rules_v1.json"]
    files += sorted((ROOT / "agents").glob("*.py")) + sorted((ROOT / "utils").glob("*.py"))
    return {str(p.resolve()): file_hash(p) for p in files}


def effective_config():
    import config
    prefixes = ("VISUAL_", "IMAGE_", "READING_", "SEMANTIC_", "REDUCTION_TARGET_")
    def serial(value):
        if isinstance(value, dict):
            return {str(k): serial(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted((serial(v) for v in value), key=canonical)
        if isinstance(value, (tuple, list)):
            return [serial(v) for v in value]
        return value
    return {name: serial(getattr(config, name)) for name in sorted(vars(config))
            if name in PROFILE or name.startswith(prefixes)}


def unexpected_losses(raw, losses):
    """Only an exact recorded non-project removal is an expected loss."""
    import json
    from utils.vision_recovery_workflow import prepare_bindings, evidence_ids
    _, events = prepare_bindings(raw)
    held = {e["record_id"] for e in events if e["action"] == "non_project_held"}
    allowed = {(str(o.get("페이지")), ev, o.get("항목"))
               for o in raw.get("chart_observations", []) if o.get("_recovery_id") in held
               for ev in evidence_ids(o)}
    unexpected = []
    for encoded in losses:
        sheet, row = json.loads(encoded)
        identity = (str(row.get("출처페이지")), row.get("근거ID"), row.get("사업명"))
        if sheet != "mitigation_projects" or identity not in allowed:
            unexpected.append(encoded)
    return unexpected


def assert_hashes(expected):
    for path, sha in expected.items():
        if not Path(path).is_file() or file_hash(path) != sha:
            raise ValueError("Frozen file changed/missing; run a new replay: " + path)


def load_spec(path):
    spec = read_json(path)
    if spec.get("version") != 1:
        raise ValueError("Unsupported experiment config version")
    validate_budget(spec["budget"])
    if not spec.get("models") or len({m["name"] for m in spec["models"]}) != len(spec["models"]):
        raise ValueError("Models require unique names")
    for m in spec["models"]:
        if m["name"] not in ("Luna", "Astra") or m.get("provider") != "codex":
            raise ValueError("This frozen sample evaluator supports Luna/Astra with bounded codex calls")
        if not m.get("model") or not m.get("command"):
            raise ValueError("Explicit model and command required")
    return spec


def input_hashes(spec, config_path):
    source = resolve(spec["source"])
    if file_hash(source) != spec["source_sha256"]:
        raise ValueError("PDF hash differs from this experiment's source")
    scorer = resolve(spec["scorer"])
    files = [source, Path(config_path).resolve(), scorer, scorer.with_name("gold_spec.py"),
             resolve(spec["evaluation"])]
    evaluation = read_json(resolve(spec["evaluation"]))
    evaluated_hashes = {str(resolve(x["file"])): x["sha256"] for x in evaluation["hashes"]}
    for model in spec["models"]:
        folder = resolve(model["run_dir"])
        model_files = [folder / n for n in ("result_visual_merge_input.json.gz", "result.xlsx", "result_run_manifest.json")]
        files.extend(model_files)
        for p in model_files:
            if evaluated_hashes.get(str(p)) != file_hash(p):
                raise ValueError("Evaluation does not match original model files: " + str(p))
        manifest = read_json(folder / "result_run_manifest.json")
        if manifest["input_sha256"] != spec["source_sha256"]:
            raise ValueError("Model source PDF mismatch")
    return {str(p): file_hash(p) for p in files}


def load_raw(model):
    with gzip.open(resolve(model["run_dir"]) / "result_visual_merge_input.json.gz", "rt", encoding="utf-8") as stream:
        import json
        raw = json.load(stream)["data"]
    for i, row in enumerate(raw.get("chart_observations", []), 1):
        row["_recovery_id"] = f"original-{i:06}"
    return raw


def summary(out, mode, models, queue, gates, **extra):
    payload = {"mode": mode, "version": VERSION, "models": models, "queue_count": len(queue),
               "gates": gates, **extra}
    write_json(out / "summary.json", payload)
    lines = ["# 저장 Vision 복구 검증", "", f"실행 단계: {mode}", "",
             "| 모델 | 값 일치 | 핵심 필드 일치 | 업무 셀 확인(제한 범위) |",
             "|---|---:|---:|---:|"]
    for name, stages in models.items():
        for stage, score in stages.items():
            if isinstance(score, dict) and "gold_count" in score:
                lines.append(f"| {name} / {stage} | {score['value_correct']}/{score['gold_count']} | "
                             f"{score['complete']}/{score['gold_count']} | {score['final_confirmed']} |")
    if mode == "reuse":
        lines += ["", f"모델 호출: 0회 / 저장 성공 응답 재사용: {extra['reused_responses']}개 / "
                  f"실패·미시도 유지: {extra['skipped_calls']}개",
                  "기존 응답을 현재 연결 코드로 재처리한 결과이며, 새 PDF 판독을 수행하지 않았습니다."]
    lines += ["", f"재판독 객체 후보: {len(queue)}개", "",
              "- 정답은 채점에만 사용하며 연결 코드나 모델 프롬프트에 정답값을 넣지 않습니다.",
              "- 기존 평가기의 최종 셀 매칭 지원 범위 밖은 미확인입니다. 전체 Excel 정확도가 아닙니다.",
              "- reference CSV, 텍스트 추출, gap-fill, PDF 원문검증은 실행하지 않습니다.",
              "- 저장 후 셀 일치는 사실 정확도와 별개입니다. 업무 시트의 행/셀을 다시 읽어 검사합니다.",
              "- 후단 개선은 명시 지역 문맥, 전망 세부부문 구분, 명시 범례/구조의 사업 오분류 방지입니다.",
              "- 조직 정보는 명시 구성·기능 계약과 지역·정확한 객체 근거를 모두 충족한 행만 연결합니다. visual_context_audit.json은 후보 투영 기록이며 저장 성공 건수가 아닙니다.",
              "- 이 표본으로 규칙/대상을 선정했으므로 개발 표본의 복구 효과입니다.", "",
              "검증 조건: " + canonical(gates)]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def replay(args):
    from utils.vision_recovery_runtime import (
        offline_guard, load_scorer, save_and_verify, score_reading, cell_losses,
    )
    config_path = resolve(args.config)
    spec = load_spec(config_path)
    inputs = input_hashes(spec, config_path)
    out = new_directory(args.output_dir or ROOT / "output" / ("vision_recovery_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
    write_json(out / "experiment.json", spec)
    manifest = {"version": VERSION, "mode": "replay", "input_hashes": inputs,
                "code_hashes": code_hashes(), "profile": PROFILE, "llm_calls": 0,
                "effective_config": effective_config(),
                "status": "running", "source": str(resolve(spec["source"]))}
    write_json(out / "manifest.json", manifest)
    models, queue, unresolved, losses = {}, [], [], []
    passed = True
    try:
        with offline_guard():
            scorer = load_scorer(resolve(spec["scorer"]))
            for model in spec["models"]:
                name = model["name"]
                raw = load_raw(model)
                model_dir = out / name
                model_dir.mkdir()
                write_json(model_dir / "input_snapshot.json", raw)
                original = score_reading(scorer, name, raw, resolve(model["run_dir"]) / "result.xlsx")
                _, before_check = save_and_verify(raw, model_dir / "baseline", False)
                after, after_check = save_and_verify(raw, model_dir / "replay", True)
                before_score = score_reading(scorer, name, raw, model_dir / "baseline/result.xlsx")
                after_score = score_reading(scorer, name, raw, model_dir / "replay/result.xlsx")
                for label, value in (("original", original), ("baseline", before_score), ("replay", after_score)):
                    write_json(model_dir / (label + "_evaluation.json"), value)
                models[name] = {"original": original, "baseline": before_score, "replay": after_score}
                lost_facts = sorted({x["id"] for x in before_score["comparisons"] if x["final_ok"]} -
                                    {x["id"] for x in after_score["comparisons"] if x["final_ok"]})
                lost_rows = cell_losses(before_check, after_check)
                unexpected = unexpected_losses(raw, lost_rows)
                losses.append({"model": name, "lost_confirmed_gold_facts": lost_facts,
                               "removed_or_changed_business_rows": lost_rows,
                               "unexpected_lost_business_rows": unexpected})
                passed &= before_check["passed"] and after_check["passed"] and not lost_facts and not unexpected
                tasks, task_holds = build_retry_queue(name, raw, after_score["comparisons"])
                queue.extend(tasks)
                unresolved.extend({"model": name, **x} for x in task_holds)
                unresolved.extend({"model": name, "record_id": o.get("_recovery_id"),
                                   "page": o.get("페이지"), "reason": o.get("병합차단사유"),
                                   "category": "downstream_hold_not_automatic_retry"}
                                  for o in after.get("chart_observations", [])
                                  if o.get("병합상태") not in ("accept", "merged", "duplicate", "fix_then_merge"))
        assert_hashes(inputs)
        assert_hashes(manifest["code_hashes"])
        write_json(out / "retry_queue.json", queue)
        write_json(out / "downstream_holds.json", unresolved)
        write_json(out / "preservation.json", losses)
        gates = {"offline": True, "cells_deterministic_and_no_lost_confirmed_facts": bool(passed),
                 "within_call_budget": len(queue) <= spec["budget"]["max_calls"]}
        manifest["retry_allowed"] = all(gates.values())
        manifest["status"] = "completed" if manifest["retry_allowed"] else "review_required"
        manifest["artifact_hashes"] = {str(p.resolve()): file_hash(p) for p in out.rglob("*")
                                       if p.is_file() and p.name != "manifest.json"}
        summary(out, "replay", models, queue, gates, llm_calls=0)
        print(f"\n재생 결과: {out}\n재판독 후보: {len(queue)}개 / 허용 상한: {spec['budget']['max_calls']}개")
        print("선택 재판독 가능" if manifest["retry_allowed"] else "검증 실패/예산 초과: summary.md 확인 후 새 replay 필요")
        return 0 if manifest["retry_allowed"] else 2
    except BaseException as exc:
        manifest.update(status="failed", error=type(exc).__name__ + ": " + str(exc), retry_allowed=False)
        raise
    finally:
        write_json(out / "manifest.json", manifest)


def retry(args, reader=None):
    from utils.vision_recovery_runtime import (
        offline_guard, read_one_object, load_scorer, save_and_verify, score_reading,
    )
    parent = Path(args.replay_dir).resolve()
    frozen = read_json(parent / "manifest.json")
    if frozen.get("version") != VERSION or not frozen.get("retry_allowed"):
        raise ValueError("Replay is not approved for retry; inspect summary.md")
    assert_hashes(frozen["input_hashes"])
    assert_hashes(frozen["code_hashes"])
    assert_hashes(frozen["artifact_hashes"])
    if canonical(effective_config()) != canonical(frozen["effective_config"]):
        raise ValueError("Effective recovery settings changed; start a new replay")
    spec = read_json(parent / "experiment.json")
    validate_budget(spec["budget"])
    queue = read_json(parent / "retry_queue.json")
    if len({q['id'] for q in queue}) != len(queue) or len(queue) > spec["budget"]["max_calls"]:
        raise ValueError("Duplicate retry IDs or call budget exceeded")
    if args.dry_run:
        print(canonical({"llm_calls": 0, "queue": queue, "budget": spec["budget"]}))
        return 0
    if queue and not args.allow_model_calls:
        raise ValueError("Actual calls require --allow-model-calls (use --dry-run to inspect)")
    out = new_directory(parent / "retry")  # Refuse repeat calls/overwriting on a second invocation.
    state = {m["name"]: read_json(parent / m["name"] / "input_snapshot.json") for m in spec["models"]}
    models_by_name = {m["name"]: m for m in spec["models"]}
    reader = reader or read_one_object
    manifest = {"version": VERSION, "mode": "retry", "status": "running",
                "parent": str(parent), "budget": spec["budget"], "calls_attempted": 0,
                "source_sha256": spec["source_sha256"], "profile": PROFILE}
    write_json(out / "manifest.json", manifest)
    journal, merge_audit, stages = [], [], {}
    start = time.monotonic()
    try:
        for task in queue:
            remaining = spec["budget"]["total_seconds"] - (time.monotonic() - start)
            if remaining <= 0:
                journal.append({"task": task["id"], "status": "budget_exhausted"})
                continue
            entry = {"task": task["id"], "status": "started"}
            journal.append(entry)
            manifest["calls_attempted"] += 1
            write_json(out / "call_journal.json", journal)
            write_json(out / "manifest.json", manifest)
            task_start = time.monotonic()
            print(f"[{manifest['calls_attempted']}/{len(queue)}] {task['model']} p{task['page']}", flush=True)
            try:
                fresh, detail = reader(resolve(spec["source"]), task, models_by_name[task["model"]],
                                       min(remaining, spec["budget"]["call_timeout_seconds"]))
                write_json(out / f"call_{manifest['calls_attempted']:03}.json", {"task": task, "detail": detail, "observations": fresh})
                raw = state[task["model"]]
                if detail.get("parsed_response") is not None:
                    record_response_success(raw, task)
                combined, audit = merge_fresh(raw["chart_observations"], fresh, task["page"], task["evidence_ids"])
                raw["chart_observations"] = combined
                merge_audit.extend({"task": task["id"], **event} for event in audit)
                entry["status"] = "completed"
            except Exception as exc:
                entry.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
            finally:
                entry["seconds"] = time.monotonic() - task_start
                write_json(out / "call_journal.json", journal)
                write_json(out / "merge_audit.json", merge_audit)
                for name, raw in state.items():
                    write_json(out / (name + "_combined_snapshot.json"), raw)
        with offline_guard():
            scorer = load_scorer(resolve(spec["scorer"]))
            cell_pass = True
            remaining_queue, unresolved = [], []
            for name, raw in state.items():
                cleaned, cells = save_and_verify(raw, out / name, True)
                score = score_reading(scorer, name, raw, out / name / "result.xlsx")
                write_json(out / name / "evaluation.json", score)
                previous = read_json(parent / name / "replay_evaluation.json")
                lost = sorted({x['id'] for x in previous['comparisons'] if x['final_ok']} -
                              {x['id'] for x in score['comparisons'] if x['final_ok']})
                write_json(out / name / "lost_confirmed_facts.json", lost)
                cell_pass &= cells["passed"] and not lost
                stages[name] = {"before_retry": previous, "after_retry": score}
                tasks, holds = build_retry_queue(name, raw, score["comparisons"])
                remaining_queue.extend(tasks)
                unresolved.extend(holds)
            write_json(out / "remaining_queue.json", remaining_queue)
            write_json(out / "queue_holds.json", unresolved)
        assert_hashes(frozen["input_hashes"])
        assert_hashes(frozen["code_hashes"])
        unresolved_calls = any(x["status"] != "completed" for x in journal)
        conflicts = any(x["status"] in ("conflict_held", "out_of_scope") for x in merge_audit)
        review = not cell_pass or unresolved_calls or conflicts or bool(remaining_queue) or bool(unresolved)
        manifest["status"] = "review_required" if review else "completed"
        summary(out, "retry", stages, remaining_queue, {"saved_cells_and_preservation": bool(cell_pass)},
                calls_attempted=manifest["calls_attempted"], conflict_count=sum(x['status']=='conflict_held' for x in merge_audit))
        print(f"\n선택 재판독 결과: {out}\n실제 호출 시도: {manifest['calls_attempted']} / 남은 후보: {len(remaining_queue)}")
        return 2 if review else 0
    except BaseException as exc:
        manifest.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        manifest["elapsed_seconds"] = time.monotonic() - start
        write_json(out / "manifest.json", manifest)


def saved_responses(parent, spec):
    """Accept only completed calls that exactly match the frozen retry queue."""
    manifest = read_json(parent / "retry/manifest.json")
    if (manifest.get("version") != VERSION or manifest.get("mode") != "retry"
            or manifest.get("status") not in {"completed", "review_required"}
            or Path(manifest.get("parent", "")).resolve() != parent
            or manifest.get("source_sha256") != spec["source_sha256"]):
        raise ValueError("A finished retry from this exact replay/PDF is required")
    queue = read_json(parent / "retry_queue.json")
    journal = read_json(parent / "retry/call_journal.json")
    ids = [q["id"] for q in queue]
    if len(set(ids)) != len(ids) or [e["task"] for e in journal] != ids:
        raise ValueError("Retry journal differs from the frozen queue")
    models = {m["name"] for m in spec["models"]}
    completed, skipped, expected_files = [], [], set()
    attempted = 0
    for task, entry in zip(queue, journal):
        if task["model"] not in models:
            raise ValueError("Unknown model in retry queue")
        status = entry.get("status")
        if status not in {"completed", "failed", "budget_exhausted"}:
            raise ValueError("Retry is incomplete; review its journal first")
        if status == "budget_exhausted":
            skipped.append(deepcopy(entry))
            continue
        attempted += 1
        path = parent / "retry" / f"call_{attempted:03}.json"
        if status == "failed":
            skipped.append(deepcopy(entry))
            continue
        expected_files.add(path)
        payload = read_json(path)
        if canonical(payload.get("task")) != canonical(task):
            raise ValueError("Saved response task differs from frozen queue: " + str(path))
        parsed = (payload.get("detail") or {}).get("parsed_response")
        if not isinstance(parsed, dict):
            raise ValueError("Saved parsed response missing: " + str(path))
        completed.append((task, parsed, path))
    if attempted != manifest.get("calls_attempted"):
        raise ValueError("Retry attempt count differs from journal")
    if set((parent / "retry").glob("call_[0-9]*.json")) != expected_files:
        raise ValueError("Orphan/failed-call response files must be reviewed, not reused")
    if not completed:
        raise ValueError("No successful saved responses to reuse")
    return completed, skipped


def reuse(args):
    """Rebuild saved successful responses offline using CURRENT connection code.

    Historical code hashes are deliberately not enforced for this repair mode;
    immutable experiment artifacts are still checked. No retry authorization is
    propagated: remaining_queue.json is a review list, not an automatic loop.
    """
    from utils.vision_recovery_runtime import (
        offline_guard, observations_from_response, load_scorer,
        save_and_verify, score_reading, cell_losses,
    )
    parent = Path(args.replay_dir).resolve()
    out = Path(args.output_dir or ROOT / "output" / (
        "vision_response_reuse_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))).resolve()
    if out == parent or parent in out.parents:
        raise ValueError("Reuse output must be outside the original experiment folder")
    frozen = read_json(parent / "manifest.json")
    if frozen.get("version") != VERSION or not frozen.get("retry_allowed"):
        raise ValueError("Expected an approved original replay")
    assert_hashes(frozen["input_hashes"])
    assert_hashes(frozen["artifact_hashes"])
    if canonical(effective_config()) != canonical(frozen["effective_config"]):
        raise ValueError("Reuse must keep the original effective recovery settings")
    spec = load_spec(parent / "experiment.json")
    # The old runner did not seal retry response files. Snapshot their hashes now,
    # validate queue/journal/provenance, and check again after the offline run.
    inputs = {str(p.resolve()): file_hash(p) for p in parent.rglob("*") if p.is_file()}
    completed, skipped = saved_responses(parent, spec)
    out = new_directory(out)
    current_code = code_hashes()
    code_changes = {p: {"before": frozen["code_hashes"].get(p), "after": current_code.get(p)}
                    for p in sorted(set(frozen["code_hashes"]) | set(current_code))
                    if frozen["code_hashes"].get(p) != current_code.get(p)}
    manifest = {"version": VERSION, "mode": "reuse", "status": "running",
                "parent": str(parent), "llm_calls": 0, "retry_allowed": False,
                "source_sha256": spec["source_sha256"], "profile": PROFILE,
                "effective_config": effective_config(), "input_hashes": inputs,
                "code_hashes": current_code, "code_changes": code_changes,
                "successful_saved_responses": len(completed), "skipped_calls": skipped,
                "response_integrity": "Hashes captured at reuse start; queue/journal/provenance checked"}
    write_json(out / "manifest.json", manifest)
    start = time.monotonic()
    try:
        with offline_guard():
            state = {m["name"]: read_json(parent / m["name"] / "input_snapshot.json")
                     for m in spec["models"]}
            object_changes, merge_audit, response_audit = [], [], []
            for task, parsed, path in completed:
                raw = state[task["model"]]
                fresh = observations_from_response(parsed, task)
                object_changes.append(record_response_success(raw, task))
                raw["chart_observations"], audit = merge_fresh(
                    raw["chart_observations"], fresh, task["page"], task["evidence_ids"])
                merge_audit.extend({"task": task["id"], **e} for e in audit)
                write_json(out / path.name, {"task": task, "source_file": str(path),
                           "source_sha256": inputs[str(path)], "observations": fresh})
                response_audit.append({"task": task["id"], "status": "reused",
                                       "observations": len(fresh), "source_file": str(path)})
            write_json(out / "response_audit.json", response_audit)
            write_json(out / "object_state_changes.json", object_changes)
            write_json(out / "merge_audit.json", merge_audit)
            scorer = load_scorer(resolve(spec["scorer"]))
            stages, preservation, remaining, holds, downstream = {}, [], [], [], []
            passed = True
            saved_cells_pass = True
            for name, raw in state.items():
                original = read_json(parent / name / "input_snapshot.json")
                original_kept = raw["chart_observations"][:len(original["chart_observations"])] == original["chart_observations"]
                write_json(out / (name + "_combined_snapshot.json"), raw)
                cleaned, cells = save_and_verify(raw, out / name, True)
                score = score_reading(scorer, name, raw, out / name / "result.xlsx")
                before = read_json(parent / "retry" / name / "evaluation.json")
                lost = sorted({x["id"] for x in before["comparisons"] if x["final_ok"]} -
                              {x["id"] for x in score["comparisons"] if x["final_ok"]})
                before_cells = read_json(parent / "retry" / name / "cell_validation.json")
                lost_rows = cell_losses(before_cells, cells)
                preservation.append({"model": name, "original_observations_unchanged": original_kept,
                                     "lost_confirmed_gold_facts": lost,
                                     "removed_or_changed_business_rows": lost_rows})
                passed &= cells["passed"] and original_kept and not lost and not lost_rows
                saved_cells_pass &= cells["passed"]
                write_json(out / name / "evaluation.json", score)
                write_json(out / name / "lost_confirmed_facts.json", lost)
                stages[name] = {"before_reuse": before, "after_reuse": score}
                tasks, task_holds = build_retry_queue(name, raw, score["comparisons"])
                remaining.extend(tasks)
                holds.extend({"model": name, **h} for h in task_holds)
                downstream.extend({"model": name, "record_id": o.get("_recovery_id"),
                                   "page": o.get("페이지"), "reason": o.get("병합차단사유"),
                                   "status": o.get("병합상태")}
                                  for o in cleaned.get("chart_observations", [])
                                  if o.get("병합상태") not in {"accept", "merged", "duplicate", "fix_then_merge"})
            write_json(out / "preservation.json", preservation)
            write_json(out / "remaining_queue.json", remaining)
            write_json(out / "queue_holds.json", holds)
            write_json(out / "downstream_holds.json", downstream)
            conflicts = sum(e["status"] == "conflict_held" for e in merge_audit)
            out_of_scope = sum(e["status"] == "out_of_scope" for e in merge_audit)
            review = not passed or skipped or remaining or holds or downstream or conflicts or out_of_scope
            manifest["status"] = "review_required" if review else "completed"
            summary(out, "reuse", stages, remaining,
                    {"offline": True, "saved_cells_and_preservation": bool(passed),
                     "saved_workbook_cells": bool(saved_cells_pass),
                     "original_observations_preserved": all(p["original_observations_unchanged"] for p in preservation),
                     "confirmed_gold_preserved": not any(p["lost_confirmed_gold_facts"] for p in preservation),
                     "business_rows_unchanged": not any(p["removed_or_changed_business_rows"] for p in preservation)},
                    llm_calls=0, reused_responses=len(completed), skipped_calls=len(skipped),
                    conflict_count=conflicts, out_of_scope_count=out_of_scope,
                    downstream_holds=len(downstream))
        assert_hashes(inputs)
        assert_hashes(frozen["input_hashes"])
        assert_hashes(current_code)
        manifest["inputs_unchanged"] = True
        print(f"\n응답 재사용 결과: {out}\n모델 호출: 0 / 저장 응답 재사용: {len(completed)}개 / 실패·미시도 유지: {len(skipped)}개")
        print("Excel 셀 저장 검증: " + ("통과" if saved_cells_pass else "실패 — cell_validation.json 확인"))
        print("기존 업무 행·정답 보존: " + ("통과" if passed else "변경 있음 — preservation.json과 visual_semantic_audit.json 확인"))
        return 2 if review else 0
    except BaseException as exc:
        manifest.update(status="failed", error=type(exc).__name__ + ": " + str(exc),
                        traceback=traceback.format_exc())
        raise
    finally:
        manifest["elapsed_seconds"] = time.monotonic() - start
        write_json(out / "manifest.json", manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("replay", help="No model calls: saved snapshots -> baseline/replay Excel -> queue")
    p.add_argument("--config", default="data/vision_recovery_11pages.json")
    p.add_argument("--output-dir")
    p = sub.add_parser("retry", help="Only frozen queue, one bounded call per object")
    p.add_argument("--replay-dir", required=True)
    p.add_argument("--allow-model-calls", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("reuse", help="Offline only: rebuild saved successful retry responses with current connection code")
    p.add_argument("--replay-dir", required=True)
    p.add_argument("--output-dir")
    args = parser.parse_args(argv)
    os.environ.update(PROFILE)
    try:
        return {"replay": replay, "retry": retry, "reuse": reuse}[args.mode](args)
    except Exception as exc:
        print(f"[오류] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
