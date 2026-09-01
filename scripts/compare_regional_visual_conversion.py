"""02_지역여건 시각 행 보완을 고정 스냅샷으로 무 API A/B 평가한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from scripts.compare_operational_visual_merge import _prepare_evaluation_data
from utils.benchmark_evaluation import EvaluationContractError, evaluate_benchmark
from utils.excel_writer import write_excel
from utils.pdf_reader import extract_pdf
from utils.visual_merge_ab import (
    compare_regional_enrichment_with_data,
    load_visual_merge_snapshot,
)


_SEVEN_METRICS = (
    "regional_visual_row_recall",
    "row_precision",
    "visual_row_conversion_rate",
    "regional_review_reject_candidates",
    "regional_duplicate_output_rows",
    "cell_accuracy",
    "llm_calls",
)


def _source_stat(payload: dict[str, Any], *path: str) -> dict[str, Any]:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return {}
        value = value.get(key)
    return value if isinstance(value, dict) else {}


def _evaluation_metrics(
    result,
    policy_metrics: dict[str, Any],
) -> dict[str, Any]:
    golden_path = result.artifacts.get("golden_score_json")
    golden = (
        json.loads(Path(golden_path).read_text(encoding="utf-8"))
        if golden_path else {}
    )
    regional_visual = _source_stat(
        golden,
        "시트별",
        "02_지역여건",
        "출처유형별",
        "시각 유래",
    )
    all_visual = _source_stat(golden, "출처유형별_전체", "시각 유래")
    return {
        "regional_visual_row_recall": regional_visual.get("리콜"),
        "regional_visual_expected_rows": regional_visual.get("골든행수", 0),
        "regional_visual_matched_rows": regional_visual.get("매칭수", 0),
        "row_precision": result.metrics.get("row_precision"),
        "visual_row_conversion_rate": all_visual.get("리콜"),
        "visual_expected_rows": all_visual.get("골든행수", 0),
        "visual_matched_rows": all_visual.get("매칭수", 0),
        "regional_review_reject_candidates": policy_metrics.get(
            "regional_review_reject_candidates", 0
        ),
        "regional_needs_review_candidates": policy_metrics.get(
            "regional_decision_counts", {}
        ).get("needs_review", 0),
        "regional_reject_candidates": policy_metrics.get(
            "regional_decision_counts", {}
        ).get("reject", 0),
        "regional_duplicate_output_rows": policy_metrics.get(
            "regional_visual_output_duplicate_rows", 0
        ),
        "regional_visual_output_rows": policy_metrics.get(
            "regional_visual_output_rows", 0
        ),
        "cell_accuracy": result.metrics.get("cell_accuracy"),
        "llm_calls": 0,
        "benchmark_report_json": str(result.report_json),
        "benchmark_report_markdown": str(result.report_markdown),
    }


def _delta(before: Any, after: Any) -> float | int | None:
    if not isinstance(before, (int, float)) or isinstance(before, bool):
        return None
    if not isinstance(after, (int, float)) or isinstance(after, bool):
        return None
    value = after - before
    return round(value, 6) if isinstance(value, float) else value


def _markdown(payload: dict[str, Any]) -> str:
    before = payload["metrics"]["before"]
    after = payload["metrics"]["after"]
    comparison = payload["comparison"]
    labels = {
        "regional_visual_row_recall": "02 시각 유래 행 재현율",
        "row_precision": "전체 행 정밀도",
        "visual_row_conversion_rate": "전체 시각 행 전환율",
        "regional_review_reject_candidates": "02 needs_review+reject",
        "regional_duplicate_output_rows": "02 시각 중복 출력 행",
        "cell_accuracy": "전체 셀 정확도",
        "llm_calls": "LLM 호출 수",
    }
    lines = [
        "# 02_지역여건 시각 행 보완 무API A/B",
        "",
        "동일한 저장 판독값과 근거 객체를 사용하고 지역여건 계약 보완만 OFF/ON으로 비교했습니다.",
        "",
        "| 지표 | 보완 OFF | 보완 ON | 차이(ON-OFF) |",
        "|---|---:|---:|---:|",
    ]
    for key in _SEVEN_METRICS:
        lines.append(
            f"| {labels[key]} | {before.get(key)} | {after.get(key)} | "
            f"{comparison.get(key)} |"
        )
    lines.extend([
        "",
        "- 재현율·전환율·정밀도·셀 정확도는 높을수록 좋습니다.",
        "- needs_review+reject와 중복 출력 행은 증가하지 않는 것이 안전합니다.",
        "- LLM 호출 수는 양쪽 모두 0이어야 고정 스냅샷 A/B 계약을 충족합니다.",
        "",
        f"- 보완 OFF 워크북: `{payload['workbooks']['before']}`",
        f"- 보완 ON 워크북: `{payload['workbooks']['after']}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="고정 시각 스냅샷에서 02_지역여건 보완만 무 API A/B 평가"
    )
    parser.add_argument("snapshot", help="*_visual_merge_input.json.gz")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source", required=True, help="원본 PDF")
    parser.add_argument("--dataset", required=True, help="평가 데이터셋 ID")
    parser.add_argument(
        "--manifest",
        default=config.EVALUATION_MANIFEST_PATH,
        help="평가 데이터셋 매니페스트",
    )
    parser.add_argument("--allow-draft-evaluation", action="store_true")
    parser.add_argument("--evaluate-holdout", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source = Path(args.source).expanduser().resolve()
    snapshot = load_visual_merge_snapshot(args.snapshot)
    payload, policy_data = compare_regional_enrichment_with_data(snapshot)

    # 이 스크립트는 저장된 판독값의 결정론적 후처리만 수행한다.
    document = extract_pdf(source, render_graph_pages=False)
    workbooks: dict[str, Path] = {}
    evaluations: dict[str, Any] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for mode in ("before", "after"):
        prepared = _prepare_evaluation_data(policy_data[mode], document)
        workbook = write_excel(prepared, output_dir / f"{mode}.xlsx").resolve()
        workbooks[mode] = workbook
        try:
            evaluation = evaluate_benchmark(
                workbook,
                source,
                manifest_path=args.manifest,
                dataset_id=args.dataset,
                allow_draft=args.allow_draft_evaluation,
                allow_holdout=args.evaluate_holdout,
                report_dir=output_dir / f"{mode}_evaluation",
            )
        except EvaluationContractError as exc:
            print(f"[평가 오류] {exc}", file=sys.stderr)
            return 2
        evaluations[mode] = {
            "dataset": evaluation.dataset.dataset_id,
            "role": evaluation.dataset.role,
            "report_json": str(evaluation.report_json),
            "report_markdown": str(evaluation.report_markdown),
        }
        metrics[mode] = _evaluation_metrics(
            evaluation,
            payload[mode]["metrics"],
        )

    comparison = {
        key: _delta(metrics["before"].get(key), metrics["after"].get(key))
        for key in _SEVEN_METRICS
    }
    policy_comparison = dict(payload.get("comparison", {}))
    payload.update({
        "contract": {
            "independent_variable": "REGIONAL_VISUAL_ENRICHMENT_ENABLED",
            "fixed_input": str(Path(args.snapshot).expanduser().resolve()),
            "llm_calls": 0,
        },
        "workbooks": {key: str(value) for key, value in workbooks.items()},
        "evaluations": evaluations,
        "metrics": metrics,
        "policy_comparison": policy_comparison,
        "comparison": comparison,
    })
    report_json = output_dir / "regional_visual_conversion_ab.json"
    report_markdown = output_dir / "regional_visual_conversion_ab.md"
    report_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str),
        encoding="utf-8",
    )
    report_markdown.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "report_json": str(report_json),
        "report_markdown": str(report_markdown),
        "metrics": metrics,
        "comparison": comparison,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
