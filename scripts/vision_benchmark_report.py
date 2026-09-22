"""M2-3 vision 벤치마크 md/json 리포트와 표본채점지 렌더링."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scripts.vision_benchmark_core import CommandStats, JsonObject, JsonValue, TEXT_MODEL, TEXT_PROVIDER
from scripts.vision_benchmark_workbook import CandidateSampleInput, SampleRow, build_sample_sheet

ROOT: Final = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ReportConfig:
    source: Path
    inventory: Path
    golden: Path | None
    output_dir: Path
    cache_dir: Path
    sample_size: int
    baseline_recall: float


@dataclass(frozen=True, slots=True)
class CandidateReportInput:
    candidate: str
    vision_provider: str
    vision_model: str
    output_xlsx: Path
    pipeline_log: Path
    run: CommandStats
    audit_json: Path | None
    score_json: Path | None
    workbook_metrics: JsonObject
    environment: JsonObject
    skip_reason: str
    reused: bool


def write_reports(config: ReportConfig, results: tuple[CandidateReportInput, ...]) -> JsonObject:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    sample_md, sample_json = _write_value_sample(config, results)
    payload: JsonObject = {
        "source": _rel(config.source),
        "inventory": _rel(config.inventory),
        "golden": _rel(config.golden) if config.golden else None,
        "cache_dir": str(config.cache_dir),
        "text_backend": {"provider": TEXT_PROVIDER, "model": TEXT_MODEL},
        "scoring_llm_calls": 0,
        "candidates": [_result_payload(result, config.baseline_recall) for result in results],
        "value_sample_markdown": _rel(sample_md),
        "value_sample_json": _rel(sample_json),
    }
    md_path = config.output_dir / f"M2-3_vision벤치마크_{stamp}.md"
    json_path = config.output_dir / f"M2-3_vision벤치마크_{stamp}.json"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["markdown"] = str(md_path)
    payload["json"] = str(json_path)
    return payload


def _write_value_sample(config: ReportConfig, results: tuple[CandidateReportInput, ...]) -> tuple[Path, Path]:
    inputs = tuple(
        CandidateSampleInput(result.candidate, result.output_xlsx, result.audit_json)
        for result in results if not result.skip_reason and result.output_xlsx.exists()
    )
    sheet = build_sample_sheet(config.inventory, inputs, config.sample_size)
    md = config.output_dir / "표본채점지.md"
    js = config.output_dir / "표본채점지.json"
    md.write_text(_sample_markdown(sheet.rows, sheet.candidates, sheet.notes), encoding="utf-8")
    js.write_text(json.dumps({"notes": sheet.notes, "rows": [_sample_payload(row) for row in sheet.rows]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js


def _result_payload(result: CandidateReportInput, baseline: float) -> JsonObject:
    audit = _audit_metrics(result.audit_json)
    recall = _as_float(audit.get("recall"))
    return {
        "candidate": result.candidate,
        "vision_provider": result.vision_provider,
        "vision_model": result.vision_model or "백엔드 기본값",
        "output_xlsx": _rel(result.output_xlsx),
        "logs": {"pipeline": _rel(result.pipeline_log)},
        "environment": result.environment,
        "skip_reason": result.skip_reason,
        "reused": result.reused,
        "run_stats": _run_payload(result),
        "inventory_audit": audit,
        "golden_score": _score_metrics(result.score_json),
        "workbook_metrics": result.workbook_metrics,
        "flashlite_baseline": _baseline_payload(result.candidate, recall, baseline),
    }


def _run_payload(result: CandidateReportInput) -> JsonObject:
    return {
        "exit_code": result.run.exit_code,
        "elapsed_seconds": result.run.elapsed_seconds,
        "cache_hit": result.run.cache_hit,
        "cache_miss": result.run.cache_miss,
        "cache_write": result.run.cache_write,
        "llm_total_calls": result.run.llm_total_calls,
        "new_text_calls_upper_bound": result.run.llm_total_calls,
        "output_exists": result.output_xlsx.exists(),
        "cache_miss_blocked": "LLM 캐시 미스 차단" in _read_text(result.pipeline_log),
    }


def _audit_metrics(path: Path | None) -> JsonObject:
    if path is None or not path.exists():
        return {"recall": None, "expected_rows": 0, "recorded_rows": 0, "status_counts": {}, "type_breakdown": {}}
    payload = _load_json(path)
    expected = _as_int(payload.get("expected_rows")) or 0
    recorded = _as_int(payload.get("recorded_rows")) or 0
    return {
        "recall": round(recorded / expected, 3) if expected else None,
        "expected_rows": expected,
        "recorded_rows": recorded,
        "status_counts": payload.get("status_counts", {}),
        "type_breakdown": payload.get("type_breakdown", {}),
    }


def _score_metrics(path: Path | None) -> JsonObject:
    if path is None or not path.exists():
        return {"visual_source_recall": None, "numeric_visual_recall": None, "value_match_rate": None, "page_intersection_rate": None}
    payload = _load_json(path)
    visual_all = _as_mapping(_as_mapping(payload.get("출처유형별_전체")).get("시각 유래"))
    visual_numeric = _as_mapping(_as_mapping(payload.get("출처유형별_수치시트")).get("시각 유래"))
    page_stats = _as_mapping(payload.get("출처페이지통계"))
    return {
        "visual_source_recall": _as_float(visual_all.get("리콜")),
        "numeric_visual_recall": _as_float(visual_numeric.get("리콜")),
        "value_match_rate": _aggregate_value_match(payload),
        "page_intersection_rate": _as_float(page_stats.get("교집합비율")),
    }


def _markdown(payload: JsonObject) -> str:
    lines = _intro(payload)
    lines.extend([
        "## 후보별 비교표\n\n",
        "| 후보 | Vision | 실행 | 스킵사유 | 인벤토리 리콜 | vision_유실 | 16행수 | 데이터포함률 | 자동신뢰율 | 실행 시간(초) | LLM호출 | 캐시 hit/miss | flashlite 0.707 일치 |\n",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|\n",
    ])
    for item in _as_list(payload.get("candidates")):
        lines.append(_candidate_line(_as_mapping(item)))
    lines.extend([
        "\n## 텍스트 신규 호출 증빙\n\n",
        "`new_text_calls_upper_bound`는 전체 실제 LLM 호출 수이므로 vision 호출까지 포함한 보수적 상한입니다. ",
        "텍스트 단계는 후보별로 모델·프로바이더를 바꾸지 않고 공유 캐시를 사용합니다.\n\n",
        f"- 값 정확도 표본채점지: `{payload['value_sample_markdown']}`\n",
    ])
    return "".join(lines)


def _intro(payload: JsonObject) -> list[str]:
    return [
        "# M2-3 Vision 벤치마크 리포트\n\n",
        "## 실행 불변식\n\n",
        f"- 입력 PDF: `{payload['source']}`\n",
        f"- 사람 인벤토리: `{payload['inventory']}`\n",
        f"- 골든셋: `{payload['golden']}`\n",
        f"- 공유 LLM 캐시: `{payload['cache_dir']}`\n",
        "- 텍스트 백엔드 고정: `LLM_PROVIDER=gemini`, `GEMINI_MODEL=gemini-2.5-flash-lite`\n",
        "- 후보별 변형 범위: `STAGE_PROVIDER_VISION` / `STAGE_MODEL_VISION`만 변경\n",
        "- 채점·집계 LLM 호출 0: audit/score/sample은 로컬 xlsx/json 집계만 수행\n\n",
    ]


def _candidate_line(row: JsonObject) -> str:
    audit = _as_mapping(row.get("inventory_audit"))
    stats = _as_mapping(row.get("run_stats"))
    status = _as_mapping(audit.get("status_counts"))
    metrics = _as_mapping(row.get("workbook_metrics"))
    base = _as_mapping(row.get("flashlite_baseline"))
    return (
        f"| {row.get('candidate')} | {row.get('vision_provider')}:{row.get('vision_model')} | {stats.get('exit_code')} | "
        f"{row.get('skip_reason') or '-'} | {_fmt(audit.get('recall'))} | {status.get('vision_유실', 0)} | "
        f"{metrics.get('visual_rows', '-')} | {_fmt(metrics.get('data_included_ratio'))} | {_fmt(metrics.get('auto_trusted_ratio'))} | "
        f"{_fmt(stats.get('elapsed_seconds'))} | {stats.get('llm_total_calls')} | {stats.get('cache_hit')}/{stats.get('cache_miss')} | "
        f"{base.get('matches_0_707')} |\n"
    )


def _sample_markdown(sheet_rows: tuple[SampleRow, ...], candidates: tuple[str, ...], notes: tuple[str, ...]) -> str:
    lines = ["# 값 정확도 표본채점지\n\n"]
    for note in notes:
        lines.append(f"- {note}\n")
    cols = ["요소ID", "페이지", "요소유형", "제목", *candidates, "사람판정", "메모"]
    lines.append("| " + " | ".join(cols) + " |\n")
    lines.append("|" + "---|" * len(cols) + "\n")
    if not sheet_rows:
        lines.append("| - | - | - | - | " + " | ".join("-" for _ in candidates) + " | 표본 없음 | 교집합 기록 요소 없음 |\n")
        return "".join(lines)
    for row in sheet_rows:
        values = [row.element_id, str(row.page), row.element_type, row.title, *[row.values.get(candidate, "") for candidate in candidates], "", ""]
        lines.append("| " + " | ".join(_cell(value) for value in values) + " |\n")
    return "".join(lines)


def _sample_payload(row: SampleRow) -> JsonObject:
    return {"요소ID": row.element_id, "페이지": row.page, "요소유형": row.element_type, "제목": row.title, "후보별_추출값요약": row.values}


def _baseline_payload(candidate: str, recall: float | None, baseline: float) -> JsonObject:
    if candidate != "flashlite" or recall is None:
        return {"baseline_recall": None, "delta": None, "matches_0_707": None}
    return {"baseline_recall": baseline, "delta": round(recall - baseline, 3), "matches_0_707": abs(recall - baseline) <= 0.001}


def _aggregate_value_match(payload: JsonObject) -> float | None:
    total = 0
    matched = 0.0
    for sheet in _as_mapping(payload.get("시트별")).values():
        stat = _as_mapping(sheet)
        rate = _as_float(stat.get("값일치율"))
        pairs = _as_int(stat.get("매칭수")) or 0
        if rate is None or pairs == 0:
            continue
        total += pairs
        matched += rate * pairs
    return round(matched / total, 4) if total else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_json(path: Path) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _as_mapping(value: JsonValue | None) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _as_list(value: JsonValue | None) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _as_float(value: JsonValue | None) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: JsonValue | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _fmt(value: JsonValue | None) -> str:
    if isinstance(value, float):
        return f"{value:.2f}" if value > 1 else f"{value:.3f}"
    if value is None:
        return "-"
    return str(value)


def _cell(value: str) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)
