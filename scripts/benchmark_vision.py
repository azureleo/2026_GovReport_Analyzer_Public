# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# .venv/bin/python scripts/benchmark_vision.py 서울특별시_탄소중립계획.pdf --candidates flashlite
# .venv/bin/python scripts/benchmark_vision.py 서울특별시_탄소중립계획.pdf --candidates flashlite --output-dir output/vision_benchmark_smoke
"""M2-3 vision 후보 벤치마크 오케스트레이터."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.vision_benchmark_core import (
    DEFAULT_CACHE_DIR,
    DEFAULT_GOLDEN,
    DEFAULT_INVENTORY,
    DEFAULT_SOURCE,
    BenchmarkConfig,
    Candidate,
    CandidateResult,
    CommandStats,
    JsonObject,
    build_variant_env,
    candidate_list,
    resolve_candidate,
    should_skip_candidate,
    tag_list,
)
from scripts.vision_benchmark_report import CandidateReportInput, ReportConfig, write_reports
from scripts.vision_benchmark_workbook import workbook_metrics

DEFAULT_COMMAND: Final = f"{sys.executable} main.py"


def _run_logged(args: list[str], env: dict[str, str], log_path: Path) -> CommandStats:
    started = time.monotonic()
    completed = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    elapsed = round(time.monotonic() - started, 2)
    body = completed.stdout + "\n" + completed.stderr
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(body, encoding="utf-8")
    parsed = _parse_run_stats(body)
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"\n[benchmark_vision] exit={completed.returncode}, elapsed_seconds={elapsed:.2f}\n")
    return CommandStats(completed.returncode, log_path, elapsed, parsed.cache_hit, parsed.cache_miss, parsed.cache_write, parsed.llm_total_calls)


def _parse_run_stats(text: str) -> CommandStats:
    cache = re.search(r"LLM 캐시:\s*hit\s*(\d+),\s*miss\s*(\d+),\s*write\s*(\d+),\s*disabled\s*(\d+)", text)
    calls = re.search(r"LLM 호출:\s*(\d+)회", text)
    return CommandStats(
        0,
        Path(""),
        0.0,
        int(cache.group(1)) if cache else None,
        int(cache.group(2)) if cache else None,
        int(cache.group(3)) if cache else None,
        int(calls.group(1)) if calls else None,
    )


def _run_candidate(config: BenchmarkConfig, candidate: Candidate) -> CandidateResult:
    candidate_dir = config.output_dir / candidate.name
    candidate_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = candidate_dir / f"서울_vision_{candidate.name}.xlsx"
    env = build_variant_env(os.environ, candidate, config.cache_dir)
    skip_reason = should_skip_candidate(candidate, config)
    if skip_reason:
        run = _skip_stats(candidate_dir / "pipeline.log", skip_reason)
        return _candidate_result(candidate, xlsx_path, run, None, config, env, skip_reason)
    reused = xlsx_path.exists() and not config.force_run
    run = _reuse_stats(candidate_dir / "pipeline.log") if reused else _run_logged(_pipeline_command(config, xlsx_path), env, candidate_dir / "pipeline.log")
    audit_json = _run_audit(config, candidate_dir, xlsx_path) if xlsx_path.exists() else None
    score_json = _run_score(config, candidate_dir, xlsx_path) if xlsx_path.exists() and config.golden else None
    return _candidate_result(candidate, xlsx_path, run, audit_json, config, env, "", reused, score_json)


def _candidate_result(
    candidate: Candidate,
    xlsx_path: Path,
    run: CommandStats,
    audit_json: Path | None,
    config: BenchmarkConfig,
    env: dict[str, str],
    skip_reason: str,
    reused: bool = False,
    score_json: Path | None = None,
) -> CandidateResult:
    metrics = workbook_metrics(xlsx_path) if xlsx_path.exists() else {}
    return CandidateResult(candidate, xlsx_path, run, audit_json, score_json, metrics, _env_payload(env), skip_reason, reused)


def _skip_stats(log_path: Path, reason: str) -> CommandStats:
    log_path.write_text(f"[benchmark_vision] skipped={reason}\n", encoding="utf-8")
    return CommandStats(0, log_path, 0.0, None, None, None, None)


def _reuse_stats(log_path: Path) -> CommandStats:
    log_path.write_text("[benchmark_vision] 기존 출력 재사용\n", encoding="utf-8")
    return CommandStats(0, log_path, 0.0, None, None, None, None)


def _pipeline_command(config: BenchmarkConfig, xlsx_path: Path) -> list[str]:
    if config.cache_only:
        return [sys.executable, str(_cache_guard_script(config.output_dir)), str(config.source), "--output", str(xlsx_path)]
    return [*shlex.split(config.command), str(config.source), "--output", str(xlsx_path)]


def _cache_guard_script(output_dir: Path) -> Path:
    wrapper = output_dir / "cache_only_main.py"
    if wrapper.exists():
        return wrapper
    source = '''from __future__ import annotations
import runpy, sys
from pathlib import Path

ROOT = Path(__ROOT__)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils import llm_cache

def cached_response(request, producer):
    cached = llm_cache._read_response(request)
    if cached is None:
        llm_cache._bump_stat("miss")
        raise RuntimeError(f"LLM 캐시 미스 차단: {request.call_kind}/{request.provider}/{request.model}")
    llm_cache._bump_stat("hit")
    return cached

def print_stats():
    stats = llm_cache.get_cache_stats()
    llm_stats = llm_client.get_llm_stats()
    print(
        "  - LLM 캐시: "
        f"hit {stats.get('hit', 0)}, miss {stats.get('miss', 0)}, "
        f"write {stats.get('write', 0)}, disabled {stats.get('disabled', 0)}"
    )
    print(
        "  - LLM 호출: "
        f"{llm_stats.get('total_calls', 0)}회"
        f"(실패 {llm_stats.get('failures', 0)}, 재시도 {llm_stats.get('retries', 0)}, "
        f"타임아웃 {llm_stats.get('timeouts', 0)}), quota 대기 누적 0.0초"
    )

llm_cache.cached_response = cached_response
from utils import llm_client
llm_client.cached_response = cached_response
sys.argv = [str(ROOT / "main.py"), *sys.argv[1:]]
try:
    runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
finally:
    print_stats()
'''.replace("__ROOT__", repr(str(ROOT)))
    wrapper.write_text(source, encoding="utf-8")
    return wrapper


def _run_audit(config: BenchmarkConfig, candidate_dir: Path, xlsx_path: Path) -> Path | None:
    report_dir = candidate_dir / "audit"
    result = _run_logged([
        sys.executable, "scripts/audit_visual_inventory.py", "audit",
        str(config.inventory), str(xlsx_path), str(config.source), "--report-dir", str(report_dir),
    ], os.environ.copy(), candidate_dir / "audit.log")
    if result.exit_code != 0:
        return None
    json_path = _first_json_object(result.log_path).get("json")
    return Path(json_path) if isinstance(json_path, str) else None


def _run_score(config: BenchmarkConfig, candidate_dir: Path, xlsx_path: Path) -> Path | None:
    report_dir = candidate_dir / "score"
    label = candidate_dir.name
    result = _run_logged([
        sys.executable, "scripts/score_against_golden.py", str(xlsx_path), str(config.golden),
        "--report-dir", str(report_dir), "--label", label, "--json",
    ], os.environ.copy(), candidate_dir / "score.log")
    json_path = report_dir / f"golden_score_{label}.json"
    return json_path if result.exit_code == 0 and json_path.exists() else None


def _first_json_object(path: Path) -> JsonObject:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _env_payload(env: dict[str, str]) -> JsonObject:
    keys = ["LLM_PROVIDER", "GEMINI_MODEL", "LLM_CACHE_DIR", "LLM_CACHE_ENABLED", "STAGE_PROVIDER_VISION", "STAGE_MODEL_VISION"]
    return {key: env[key] for key in keys if key in env}


def _report_input(result: CandidateResult) -> CandidateReportInput:
    return CandidateReportInput(
        candidate=result.candidate.name,
        vision_provider=result.candidate.provider,
        vision_model=result.candidate.model,
        output_xlsx=result.output_xlsx,
        pipeline_log=result.run.log_path,
        run=result.run,
        audit_json=result.audit_json,
        score_json=result.score_json,
        workbook_metrics=result.workbook_metrics,
        environment=result.environment,
        skip_reason=result.skip_reason,
        reused=result.reused,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="서울 문서 vision 스테이지 후보 벤치마크")
    parser.add_argument("source", nargs="?", default=str(DEFAULT_SOURCE), help="서울 PDF 경로")
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY), help="사람 확정 시각 인벤토리 xlsx")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="값 정확도 채점용 골든셋 xlsx")
    parser.add_argument("--candidates", default="flashlite", help="쉼표/공백 구분 후보")
    parser.add_argument("--skip-run", default="", help="실행·채점에서 제외할 후보 태그")
    parser.add_argument("--force-run", action="store_true", help="기존 후보 출력이 있어도 재실행")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="추출 실행 명령")
    parser.add_argument("--allow-cache-miss", action="store_true", help="실제 LLM 호출을 허용")
    parser.add_argument("--cache-only", action="store_true", help="캐시 미스 시 즉시 실패")
    parser.add_argument("--output-dir", "--out-dir", default="output/vision_benchmark", help="결과 디렉터리")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="공유 LLM 캐시 디렉터리")
    parser.add_argument("--sample-size", type=int, default=15, help="표본채점지 최대 행 수")
    parser.add_argument("--baseline-recall", type=float, default=0.707, help="flashlite 기존 감사 리콜 기준선")
    return parser


def _config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    golden = Path(args.golden).expanduser().resolve() if args.golden else None
    if golden is not None and not golden.exists():
        golden = None
    return BenchmarkConfig(
        source=Path(args.source).expanduser().resolve(),
        inventory=Path(args.inventory).expanduser().resolve(),
        golden=golden,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        cache_dir=Path(args.cache_dir),
        command=str(args.command),
        candidates=candidate_list(str(args.candidates)),
        skip_run=tag_list(str(args.skip_run)),
        sample_size=max(1, int(args.sample_size)),
        baseline_recall=float(args.baseline_recall),
        cache_only=bool(args.cache_only or (not args.allow_cache_miss and str(args.command) == DEFAULT_COMMAND)),
        force_run=bool(args.force_run),
        env_file=ROOT / ".env",
    )


def main(argv: list[str] | None = None) -> int:
    config = _config_from_args(_parser().parse_args(argv))
    if not config.source.exists():
        print(f"입력 PDF를 찾을 수 없습니다: {config.source}", file=sys.stderr)
        return 2
    if not config.inventory.exists():
        print(f"인벤토리를 찾을 수 없습니다: {config.inventory}", file=sys.stderr)
        return 2
    config.output_dir.mkdir(parents=True, exist_ok=True)
    results = tuple(_run_candidate(config, candidate) for candidate in config.candidates)
    report = write_reports(
        ReportConfig(config.source, config.inventory, config.golden, config.output_dir, config.cache_dir, config.sample_size, config.baseline_recall),
        tuple(_report_input(result) for result in results),
    )
    print(json.dumps({"markdown": report["markdown"], "json": report["json"], "value_sample_markdown": report["value_sample_markdown"]}, ensure_ascii=False))
    return 0 if all(_candidate_ok(result) for result in results) else 1


def _candidate_ok(result: CandidateResult) -> bool:
    return bool(result.skip_reason or (result.output_xlsx.exists() and result.audit_json is not None))


if __name__ == "__main__":
    raise SystemExit(main())
