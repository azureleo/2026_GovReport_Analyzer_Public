"""Offline A/B replay for an operational visual-merge snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from utils.benchmark_evaluation import EvaluationContractError, evaluate_benchmark
from utils.excel_writer import write_excel
from utils.pdf_reader import PDFContent, extract_pdf
from utils.semantic_routing import validate_and_reclassify
from utils.source_verifier import build_source_object_inventory, verify_final_data
from utils.visual_merge_ab import (
    compare_merge_policies_with_data,
    load_visual_merge_snapshot,
)


_EVALUATION_METRICS = (
    "cell_accuracy",
    "row_recall",
    "row_precision",
    "object_recall",
    "routing_error_rate",
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ) + "\n")


def _deduplicate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output


def _prepare_evaluation_data(
    cleaned: dict[str, Any],
    document: PDFContent | None,
) -> dict[str, Any]:
    """운영 파이프라인의 결정론적 후처리를 A/B 양쪽에 동일하게 적용한다."""
    prepared = deepcopy(cleaned)
    if document is None:
        return prepared

    municipality = str(prepared.get("municipality_name") or "알 수 없음")
    validation = prepared.setdefault("validation_report", [])
    if not isinstance(validation, list):
        validation = []
        prepared["validation_report"] = validation

    if getattr(config, "SEMANTIC_ROUTING_ENABLED", True):
        routing = validate_and_reclassify(
            prepared,
            document,
            auto_reclassify=bool(
                getattr(config, "SEMANTIC_ROUTING_AUTO_RECLASSIFY", True)
            ),
        )
        validation.extend(routing.validation_issues(municipality))

    verification = verify_final_data(
        prepared,
        document,
        page_radius=max(0, int(getattr(config, "SOURCE_VERIFICATION_PAGE_RADIUS", 1))),
        max_terms=max(1, int(getattr(config, "SOURCE_VERIFICATION_MAX_TERMS", 12))),
        global_search=bool(getattr(config, "SOURCE_VERIFICATION_GLOBAL_SEARCH", True)),
    )
    prepared["source_verification"] = verification.excel_rows(municipality)
    validation.extend(verification.validation_issues(municipality))

    inventory = build_source_object_inventory(prepared, document)
    prepared["source_object_inventory"] = inventory.excel_rows(municipality)
    validation.extend(inventory.validation_issues(municipality))
    prepared["validation_report"] = _deduplicate_rows(validation)
    return prepared


def _write_policy_workbooks(
    output_dir: Path,
    policy_data: dict[str, dict[str, Any]],
    document: PDFContent | None,
) -> dict[str, Path]:
    outputs: dict[str, Path] = {}
    for mode in ("legacy", "evidence"):
        prepared = _prepare_evaluation_data(policy_data[mode], document)
        outputs[mode] = write_excel(
            prepared,
            output_dir / f"{mode}_merge.xlsx",
        ).resolve()
    return outputs


def _evaluate_workbooks(
    workbooks: dict[str, Path],
    source: Path,
    *,
    manifest: str | Path,
    dataset: str | None,
    allow_draft: bool,
    allow_holdout: bool,
    output_dir: Path,
) -> dict[str, Any]:
    evaluations: dict[str, Any] = {}
    for mode in ("legacy", "evidence"):
        result = evaluate_benchmark(
            workbooks[mode],
            source,
            manifest_path=manifest,
            dataset_id=dataset,
            allow_draft=allow_draft,
            allow_holdout=allow_holdout,
            report_dir=output_dir / f"{mode}_evaluation",
        )
        evaluations[mode] = {
            "dataset": result.dataset.dataset_id,
            "role": result.dataset.role,
            "metrics": result.metrics,
            "report_json": str(result.report_json),
            "report_markdown": str(result.report_markdown),
        }

    comparison: dict[str, float | None] = {}
    for metric in _EVALUATION_METRICS:
        legacy = evaluations["legacy"]["metrics"].get(metric)
        evidence = evaluations["evidence"]["metrics"].get(metric)
        comparison[metric] = (
            round(float(evidence) - float(legacy), 6)
            if isinstance(legacy, (int, float)) and isinstance(evidence, (int, float))
            else None
        )
    return {**evaluations, "comparison": comparison}


def _markdown(payload: dict) -> str:
    legacy = payload["legacy"]["metrics"]
    evidence = payload["evidence"]["metrics"]
    lines = [
        "# 운영 시각 병합 무API A/B",
        "",
        "동일한 저장 관찰값에 기존 병합과 근거 기반 병합을 재적용한 결과입니다.",
        "사람 확정 정답이 없는 운영 A/B는 정책 동작 비교이며 정확도 증명이 아닙니다.",
        "",
        "| 지표 | 기존 | 근거 기반 |",
        "|---|---:|---:|",
        f"| 후보 수 | {legacy['candidate_count']} | {evidence['candidate_count']} |",
        f"| 시각 전용 출력 행 | {legacy['visual_output_rows']} | {evidence['visual_output_rows']} |",
        f"| needs_review 사유 보유율 | {legacy['needs_review_reason_coverage']} | {evidence['needs_review_reason_coverage']} |",
        "",
        f"- 기존 판정: `{json.dumps(legacy['decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- 근거 기반 판정: `{json.dumps(evidence['decision_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- 근거 매칭: `{json.dumps(evidence['evidence_match_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 평가용 워크북",
        "",
        f"- 기존 병합: `{payload.get('workbooks', {}).get('legacy', '-')}`",
        f"- 근거 기반 병합: `{payload.get('workbooks', {}).get('evidence', '-')}`",
    ]
    evaluations = payload.get("evaluation")
    if isinstance(evaluations, dict) and "legacy" in evaluations and "evidence" in evaluations:
        lines.extend([
            "",
            "## 독립 평가",
            "",
            "| 지표 | 기존 | 근거 기반 | 차이(근거-기존) |",
            "|---|---:|---:|---:|",
        ])
        for metric in _EVALUATION_METRICS:
            legacy_value = evaluations["legacy"]["metrics"].get(metric)
            evidence_value = evaluations["evidence"]["metrics"].get(metric)
            delta = evaluations["comparison"].get(metric)
            lines.append(
                f"| {metric} | {legacy_value if legacy_value is not None else '-'} | "
                f"{evidence_value if evidence_value is not None else '-'} | "
                f"{delta if delta is not None else '-'} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="운영 시각 병합 결과를 API 없이 A/B 비교")
    parser.add_argument("snapshot", help="*_visual_merge_input.json.gz")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--source",
        default=None,
        help="결정론적 원문대조·객체 인벤토리를 양쪽 워크북에 적용할 원본 PDF",
    )
    parser.add_argument(
        "--manifest",
        default=config.EVALUATION_MANIFEST_PATH,
        help="평가 데이터셋 매니페스트",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="지정하면 두 워크북을 같은 골든셋으로 자동 평가",
    )
    parser.add_argument("--allow-draft-evaluation", action="store_true")
    parser.add_argument("--evaluate-holdout", action="store_true")
    args = parser.parse_args()

    if args.dataset and not args.source:
        parser.error("--dataset 자동 평가에는 --source 원본 PDF가 필요합니다")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload, policy_data = compare_merge_policies_with_data(
        load_visual_merge_snapshot(args.snapshot)
    )
    source = Path(args.source).expanduser().resolve() if args.source else None
    document = extract_pdf(source, render_graph_pages=False) if source else None
    workbooks = _write_policy_workbooks(output_dir, policy_data, document)
    payload["workbooks"] = {mode: str(path) for mode, path in workbooks.items()}
    if source is not None and args.dataset:
        try:
            payload["evaluation"] = _evaluate_workbooks(
                workbooks,
                source,
                manifest=args.manifest,
                dataset=args.dataset,
                allow_draft=args.allow_draft_evaluation,
                allow_holdout=args.evaluate_holdout,
                output_dir=output_dir,
            )
        except EvaluationContractError as exc:
            print(f"[평가 오류] {exc}", file=sys.stderr)
            return 2
    report_json = output_dir / "ab_report.json"
    report_md = output_dir / "ab_report.md"
    report_json.write_text(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ), encoding="utf-8")
    report_md.write_text(_markdown(payload), encoding="utf-8")
    _write_jsonl(output_dir / "legacy_rows.jsonl", payload["legacy"]["rows"])
    _write_jsonl(output_dir / "evidence_rows.jsonl", payload["evidence"]["rows"])
    _write_jsonl(output_dir / "evidence_candidates.jsonl", payload["evidence"]["candidates"])
    print(json.dumps({
        "report_json": str(report_json),
        "report_markdown": str(report_md),
        "workbooks": payload["workbooks"],
        "evaluation": payload.get("evaluation", {}).get("comparison"),
        "comparison": payload["comparison"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
