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
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    return {
        "status": payload.get("status"),
        "quality_score": payload.get("quality_score"),
        "text_seconds": float(timings.get("텍스트 추출", 0.0) or 0.0),
        "total_calls": int(calls.get("total_calls", 0) or 0),
        "failures": int(calls.get("failures", 0) or 0),
        "retries": int(calls.get("retries", 0) or 0),
        "timeouts": int(calls.get("timeouts", 0) or 0),
        "input_chars": sum(int(value or 0) for value in (calls.get("input_chars") or {}).values()),
        "manifest": str(path),
    }


def _run_arm(
    name: str,
    command: list[str],
    output_dir: Path,
    *,
    clustering: bool,
    use_cache: bool,
    dry_run: bool,
) -> int:
    environment = os.environ.copy()
    environment.update({
        "EXTRACTION_SHEET_CLUSTERING": "1" if clustering else "0",
        "RUN_STATE_ENABLED": "1",
        "RUN_STATE_DIR": str(output_dir / "run_state" / name),
        "LLM_CACHE_ENABLED": "1" if use_cache else "0",
        # The A/B test measures text extraction only. Keep optional review paths
        # off even if a developer's .env enables them.
        "HYBRID_REVIEW_ENABLED": "0",
    })
    printable = subprocess.list2cmdline(command)
    print(f"[{name}] EXTRACTION_SHEET_CLUSTERING={int(clustering)}")
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vision을 끄고 텍스트 시트 클러스터링만 변경하는 A/B 실행기",
    )
    parser.add_argument("input", type=Path, help="입력 PDF/HWP/HWPX")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--guideline", type=Path)
    parser.add_argument("--agent", choices=["codex", "claude", "auto", "gemini", "openai"])
    parser.add_argument("--agent-model")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--evaluation-manifest", type=Path)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.exists():
        parser.error(f"입력 파일을 찾을 수 없습니다: {input_path}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (args.output_dir or ROOT / "output" / f"text_optimization_ab_{timestamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs = {
        "baseline": output_dir / "baseline_per_sheet.xlsx",
        "clustered": output_dir / "clustered_sheets.xlsx",
    }
    for name, clustering in (("baseline", False), ("clustered", True)):
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
        )
        if code:
            print(f"[{name}] 실행 실패(exit={code}). 다음 arm은 실행하지 않습니다.")
            return code

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
        "contract": {
            "vision_enabled": False,
            "changed_variable": "EXTRACTION_SHEET_CLUSTERING",
            "cache_enabled": args.use_cache,
        },
        "baseline": _manifest_metrics(manifests["baseline"]),
        "clustered": _manifest_metrics(manifests["clustered"]),
    }
    summary["text_speedup"] = _speedup(summary["baseline"], summary["clustered"])
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
    return compare.returncode


if __name__ == "__main__":
    raise SystemExit(main())
