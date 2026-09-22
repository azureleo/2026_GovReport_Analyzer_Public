"""Run a text-only A/B test while holding the Vision path completely off.

A uses the current per-sheet extraction path. B changes only
EXTRACTION_SHEET_CLUSTERING=1. Each arm receives its own run-state directory
and, by default, a cold LLM cache so elapsed time and call counts are comparable.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import hashlib
from copy import deepcopy
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.pipeline_result import saved_review_result


def arm_settings(experiment, clustering, routing_mode="audit", input_mode="audit"):
    candidate = bool(clustering)  # retained API name for older callers
    if experiment == "inputs":
        return {"EXTRACTION_SHEET_CLUSTERING": "0",
                "TEXT_ROUTING_MODE": "optimize" if candidate else "off",
                "TEXT_INPUT_MODE": "optimize" if candidate else "off"}
    return {"EXTRACTION_SHEET_CLUSTERING": "1" if candidate else "0",
            "TEXT_ROUTING_MODE": routing_mode, "TEXT_INPUT_MODE": input_mode}


def _arm_environment(name, output_dir, *, clustering, use_cache, experiment="clustering", routing_mode="audit", input_mode="audit"):
    environment = os.environ.copy()
    environment.update(arm_settings(experiment, clustering, routing_mode, input_mode))
    environment.update({
        "RUN_STATE_ENABLED": "1", "RUN_STATE_DIR": str(output_dir / "run_state" / name),
        "LLM_CACHE_ENABLED": "1" if use_cache else "0", "EXTRACTION_RESUME": "0",
        "EXTRACTION_RETRY_FAILED_ONLY": "0", "HYBRID_REVIEW_ENABLED": "0",
        "SHEET_CLOSED_LOOP_ENABLED": "0", "GAP_FILL_ENABLED": "0", "VISION_REVIEW_ENABLED": "0",
        "FULL_DOCUMENT_SCAN": "0", "PYTHONIOENCODING": "utf-8",
    })
    return environment


def _build_command(
    input_path: Path,
    output_path: Path,
    *,
    retries: int,
    guideline: Path | None = None,
    agent: str | None = None,
    agent_model: str | None = None,
    evaluation_manifest: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "main.py"),
        str(input_path),
        "--output",
        str(output_path),
        "--no-images",
        "--retries",
        str(retries),
    ]
    if guideline is not None:
        command.extend(("--guideline", str(guideline)))
    if agent:
        command.extend(("--agent", agent))
    if agent_model:
        command.extend(("--agent-model", agent_model))
    if evaluation_manifest is not None:
        command.extend(("--evaluate", "--evaluation-manifest", str(evaluation_manifest)))
    return command


def _manifest_metrics(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    timings = payload.get("timings_seconds") or {}
    calls = payload.get("llm_call_stats") or {}
    result = {
        "status": payload.get("status"),
        "quality_score": payload.get("quality_score"),
        "evaluation_version": (payload.get("quality_metrics") or {}).get("evaluation_version"),
        "file_saved": payload.get("file_saved"),
        "exit_code": payload.get("exit_code"),
        "text_seconds": float(timings.get("텍스트 추출", 0.0) or 0.0),
        "total_calls": int(calls.get("total_calls", 0) or 0),
        "failures": int(calls.get("failures", 0) or 0),
        "retries": int(calls.get("retries", 0) or 0),
        "timeouts": int(calls.get("timeouts", 0) or 0),
        "input_chars": sum(int(value or 0) for value in (calls.get("input_chars") or {}).values()),
        "manifest": str(path),
    }
    audit_path = Path(payload['text_audit_path']) if payload.get('text_audit_path') else None
    if audit_path and not audit_path.is_absolute():
        audit_path = ROOT / audit_path
    if audit_path and audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding='utf-8'))
        attempts = audit.get('attempts', [])
        result['extraction_entry_calls'] = sum(len(a.get('calls', [])) for a in attempts)
        result['task_statuses'] = dict(Counter(t['status'] for a in attempts for t in a.get('tasks', [])))
        result['final_rows_by_sheet'] = audit.get('final_rows_by_sheet', {})
        result['text_audit'] = str(audit_path)
    return result


def _run_arm(
    name: str,
    command: list[str],
    output_dir: Path,
    *,
    clustering: bool,
    use_cache: bool,
    dry_run: bool,
    experiment: str = "clustering",
    routing_mode: str = "audit",
    input_mode: str = "audit",
) -> int:
    environment = _arm_environment(name, output_dir, clustering=clustering, use_cache=use_cache,
                                   experiment=experiment, routing_mode=routing_mode, input_mode=input_mode)
    printable = subprocess.list2cmdline(command)
    print(f"[{name}] {arm_settings(experiment, clustering, routing_mode, input_mode)}")
    print(f"[{name}] {printable}")
    if dry_run:
        return 0

    log_path = output_dir / f"{name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(f"[{name}] {line}", end="")
            log.write(line)
        return process.wait()


def _speedup(baseline: dict, clustered: dict) -> float | None:
    denominator = clustered["text_seconds"]
    if denominator <= 0:
        return None
    return round(baseline["text_seconds"] / denominator, 3)


def compare_contracts(a, b, experiment):
    """Fail closed when the two live arms changed undeclared variables."""
    issues = []
    for field in ('input_sha256', 'implementation_sha256', 'prompt_sha256', 'guideline_sha256'):
        if field not in a or field not in b or a[field] != b[field]:
            issues.append(field)
    ac, bc = deepcopy(a.get('config', {})), deepcopy(b.get('config', {}))
    if experiment == 'clustering':
        if ac.get('sheet_clustering') is not False or bc.get('sheet_clustering') is not True:
            issues.append('clustering_not_applied')
        ac.pop('sheet_clustering', None)
        bc.pop('sheet_clustering', None)
    else:
        for values, expected in ((ac, 'off'), (bc, 'optimize')):
            policy = values.get('text_optimization_policy', {})
            if any(policy.get(key) != expected for key in ('TEXT_ROUTING_MODE', 'TEXT_INPUT_MODE')):
                issues.append('input_policy_not_applied')
            for key in ('TEXT_ROUTING_MODE', 'TEXT_INPUT_MODE'):
                policy.pop(key, None)
    if ac != bc:
        issues.append('config')
    return {"comparable": not issues, "differences": issues}


def compare_golden_results(a, b):
    am, bm = a['독립평가지표'], b['독립평가지표']
    def denominators(score):
        return {key: [v.get('상태'), v.get('골든행수'), v.get('셀기대수')]
                for key, v in score.get('시트별', {}).items()}
    comparable = (not a['형식오류'] and not b['형식오류']
                  and am.get('cell_expected', 0) > 0
                  and all(am.get(k) == bm.get(k) for k in ('cell_expected', 'golden_rows'))
                  and a.get('생략시트') == b.get('생략시트') and denominators(a) == denominators(b))
    before = {(r['시트'], r['행번호']) for r in a['미매칭상세']['골든']}
    lost = [r for r in b['미매칭상세']['골든'] if (r['시트'], r['행번호']) not in before]
    return {'comparable': comparable, 'baseline': am, 'candidate': bm,
            'previously_matched_rows_lost': lost if comparable else None,
            'review_required': bool(lost) or any(am.get(key) is not None and bm.get(key) is not None and bm[key] < am[key]
                                                for key in ('cell_accuracy', 'row_precision', 'row_recall')),
            'note': 'Official strict/relaxed match losses; a matched row is not proof every cell was correct. See value disagreements in both reports.'}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vision 없이 입력 최적화와 시트 묶음을 분리 비교하는 A/B 실행기",
    )
    parser.add_argument("input", type=Path, help="입력 PDF/HWP/HWPX")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--guideline", type=Path)
    parser.add_argument("--agent", choices=["codex", "claude", "auto", "gemini", "openai"])
    parser.add_argument("--agent-model")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--experiment", choices=["inputs", "clustering"], default="clustering")
    parser.add_argument("--routing-mode", choices=["off", "audit", "optimize"], default="audit")
    parser.add_argument("--input-mode", choices=["off", "audit", "optimize"], default="audit")
    parser.add_argument("--evaluation-manifest", type=Path)
    parser.add_argument("--golden", type=Path, help="선택: 동일 골든셋으로 양쪽 최종 셀/행 지표와 기존 매칭 손실 채점")
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.exists():
        parser.error(f"입력 파일을 찾을 수 없습니다: {input_path}")
    for path in (args.guideline, args.evaluation_manifest, args.golden):
        if path and not path.is_file():
            parser.error(f"첨부 파일을 찾을 수 없습니다: {path}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_dir = (args.output_dir or ROOT / "output" / f"text_optimization_ab_{timestamp}").resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error("기존 결과 보호: 비어 있는 새 출력 폴더를 지정하세요.")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load dotenv once, freezing the same effective values into both subprocesses.
    import config
    from utils.run_state import _implementation_hash
    contract = {
        "experiment": args.experiment, "vision_enabled": False, "cache_enabled": args.use_cache,
        "resume": False, "dry_run": args.dry_run, "python": sys.version,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "implementation_sha256": _implementation_hash(),
        "golden_sha256": hashlib.sha256(args.golden.read_bytes()).hexdigest() if args.golden else None,
        "guideline_sha256": hashlib.sha256(args.guideline.read_bytes()).hexdigest() if args.guideline else None,
        "changed_variables": ["TEXT_ROUTING_MODE", "TEXT_INPUT_MODE"] if args.experiment == "inputs" else ["EXTRACTION_SHEET_CLUSTERING"],
        "settings": {key: getattr(config, key, None) for key in (
            "BATCH_SIZE", "TEXT_WORKERS", "LOCAL_AGENT_TIMEOUT", "LOCAL_AGENT_TIMEOUT_RETRIES", "EXTRACTION_MAX_BATCH_CHARS",
            "EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS", "TEXT_CLUSTER_MEMBER_RETRIES",
            "READING_PIPELINE_ENABLED", "EXTRACTION_SHEET_CLUSTERS", "STAGE_MODELS", "STAGE_PROVIDERS")},
        "codex_command_sha256": hashlib.sha256(config.CODEX_COMMAND.encode()).hexdigest(),
        "arms": {name: arm_settings(args.experiment, candidate, args.routing_mode, args.input_mode)
                 for name, candidate in (("baseline", False), ("clustered", True))},
        "limitations": "dry-run does not invoke models. Quality score/row agreement is not golden accuracy. Cache runs are exploratory only.",
    }
    (output_dir / "contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

    outputs = {
        "baseline": output_dir / "baseline_per_sheet.xlsx",
        "clustered": output_dir / "clustered_sheets.xlsx",
    }
    for name, clustering in (("baseline", False), ("clustered", True)):
        if (_implementation_hash() != contract['implementation_sha256']
                or hashlib.sha256(input_path.read_bytes()).hexdigest() != contract['input_sha256']):
            print('비교 중 코드/PDF가 변경되어 중단합니다. 새 폴더에서 다시 실행하세요.')
            return 2
        command = _build_command(
            input_path,
            outputs[name],
            retries=max(1, args.retries),
            guideline=args.guideline.resolve() if args.guideline else None,
            agent=args.agent,
            agent_model=args.agent_model,
            evaluation_manifest=(
                args.evaluation_manifest.resolve() if args.evaluation_manifest else None
            ),
        )
        code = _run_arm(
            name,
            command,
            output_dir,
            clustering=clustering,
            use_cache=args.use_cache,
            dry_run=args.dry_run,
            experiment=args.experiment,
            routing_mode=args.routing_mode,
            input_mode=args.input_mode,
        )
        if code and not saved_review_result(code, outputs[name]):
            print(f"[{name}] 실행 실패(exit={code}). 다음 arm은 실행하지 않습니다.")
            return code
        if code == 3:
            print(f"[{name}] 결과 저장됨·검토 필요(exit=3). 비교는 계속하되 성공으로 집계하지 않습니다.")

    if args.dry_run:
        print(f"A/B dry-run 준비 완료: {output_dir}")
        return 0

    manifests = {
        name: path.with_name(f"{path.stem}_run_manifest.json")
        for name, path in outputs.items()
    }
    missing = [str(path) for path in manifests.values() if not path.exists()]
    if missing:
        print("실행 매니페스트가 없습니다: " + ", ".join(missing))
        return 2

    summary = {
        "contract": contract,
        "baseline": _manifest_metrics(manifests["baseline"]),
        "clustered": _manifest_metrics(manifests["clustered"]),
    }
    payloads = {k: json.loads(p.read_text(encoding='utf-8')) for k, p in manifests.items()}
    summary['contract_check'] = compare_contracts(payloads['baseline'], payloads['clustered'], args.experiment)
    summary["text_speedup"] = _speedup(summary["baseline"], summary["clustered"]) if summary['contract_check']['comparable'] else None
    if args.golden:
        if hashlib.sha256(args.golden.read_bytes()).hexdigest() != contract['golden_sha256']:
            print('실행 중 골든셋이 변경되어 채점을 중단합니다.')
            return 2
        from scripts.score_against_golden import score_workbooks
        scores = {name: score_workbooks(path, args.golden, report_dir=output_dir / 'golden', label=name, write_json=True)
                  for name, path in outputs.items()}
        summary['golden_comparison'] = compare_golden_results(scores['baseline'], scores['clustered'])
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    compare = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "compare_extraction_modes.py"),
            str(outputs["baseline"]),
            str(outputs["clustered"]),
        ],
        cwd=ROOT,
        check=False,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"A/B 요약: {summary_path}")
    if not summary['contract_check']['comparable'] or not summary.get('golden_comparison', {}).get('comparable', True):
        return 2
    return compare.returncode or (3 if any(summary[name].get("exit_code") == 3 for name in outputs)
                                  or summary.get('golden_comparison', {}).get('review_required') else 0)


if __name__ == "__main__":
    raise SystemExit(main())
