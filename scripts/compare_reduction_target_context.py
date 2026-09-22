"""Replay a snapshot with legacy/context reduction-target classifiers, without APIs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from agents.organizer_agent import OrganizerAgent
from utils.excel_writer import write_excel
from utils.visual_merge_ab import load_visual_merge_snapshot


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ) + "\n")


def summarize_decisions(cleaned: dict[str, Any]) -> dict[str, Any]:
    ledger = [
        row for row in cleaned.get("reduction_target_context", [])
        if isinstance(row, dict)
    ]
    output_rows = [
        row for row in cleaned.get("reduction_targets", [])
        if isinstance(row, dict)
    ]
    retag_directions = Counter(
        f"{row.get('original_level') or 'blank'}->{row.get('suggested_level') or 'blank'}"
        for row in ledger if row.get("status") == "retag"
    )
    review_levels = Counter(
        str(row.get("original_level") or "blank")
        for row in ledger if row.get("status") == "needs_review"
    )
    return {
        "input_rows": len(ledger),
        "output_rows": len(output_rows),
        "level_counts": dict(sorted(Counter(
            str(row.get("목표수준") or "blank") for row in output_rows
        ).items())),
        "decision_counts": dict(sorted(Counter(
            str(row.get("status") or "unknown") for row in ledger
        ).items())),
        "confidence_counts": dict(sorted(Counter(
            str(row.get("confidence") or "unknown") for row in ledger
        ).items())),
        "retag_directions": dict(sorted(retag_directions.items())),
        "needs_review_original_levels": dict(sorted(review_levels.items())),
    }


def _run_mode(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    previous = getattr(config, "REDUCTION_TARGET_CONTEXT_MODE", "context")
    try:
        config.REDUCTION_TARGET_CONTEXT_MODE = mode
        return OrganizerAgent().organize(deepcopy(snapshot["data"]))
    finally:
        config.REDUCTION_TARGET_CONTEXT_MODE = previous


def _markdown(payload: dict[str, Any]) -> str:
    legacy = payload["modes"]["legacy"]
    context = payload["modes"]["context"]
    lines = [
        "# 06_감축목표 문맥 분류 무API A/B",
        "",
        "동일한 추출·시각 객체 스냅샷에 후처리 분류기만 바꿔 적용한 결과입니다.",
        "정확도 판정은 함께 생성된 두 워크북을 같은 골든셋으로 평가해야 합니다.",
        "",
        "| 지표 | legacy | context |",
        "|---|---:|---:|",
        f"| 입력 원행 | {legacy['input_rows']} | {context['input_rows']} |",
        f"| 정제 후 06 행 | {legacy['output_rows']} | {context['output_rows']} |",
        f"| 자동 재태깅 | {legacy['decision_counts'].get('retag', 0)} | {context['decision_counts'].get('retag', 0)} |",
        f"| needs_review | {legacy['decision_counts'].get('needs_review', 0)} | {context['decision_counts'].get('needs_review', 0)} |",
        "",
        f"- legacy 수준: `{json.dumps(legacy['level_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- context 수준: `{json.dumps(context['level_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- context 재태깅 방향: `{json.dumps(context['retag_directions'], ensure_ascii=False, sort_keys=True)}`",
        f"- context 보류 원수준: `{json.dumps(context['needs_review_original_levels'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## 산출물",
        "",
        f"- legacy 워크북: `{payload['workbooks']['legacy']}`",
        f"- context 워크북: `{payload['workbooks']['context']}`",
        f"- 판정 원장: `{payload['decision_ledgers']['legacy']}`, `{payload['decision_ledgers']['context']}`",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="동일 스냅샷의 감축목표 legacy/context 분류를 API 없이 비교",
    )
    parser.add_argument("snapshot", help="*_visual_merge_input.json.gz")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    snapshot = load_visual_merge_snapshot(args.snapshot)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned_by_mode = {
        mode: _run_mode(snapshot, mode)
        for mode in ("legacy", "context")
    }
    workbook_paths = {
        mode: write_excel(cleaned, output_dir / f"{mode}.xlsx").resolve()
        for mode, cleaned in cleaned_by_mode.items()
    }
    ledger_paths: dict[str, Path] = {}
    for mode, cleaned in cleaned_by_mode.items():
        path = output_dir / f"{mode}_decisions.jsonl"
        _write_jsonl(path, cleaned.get("reduction_target_context", []))
        ledger_paths[mode] = path.resolve()

    payload = {
        "snapshot": str(Path(args.snapshot).expanduser().resolve()),
        "modes": {
            mode: summarize_decisions(cleaned)
            for mode, cleaned in cleaned_by_mode.items()
        },
        "workbooks": {mode: str(path) for mode, path in workbook_paths.items()},
        "decision_ledgers": {mode: str(path) for mode, path in ledger_paths.items()},
    }
    report_json = output_dir / "target_context_ab.json"
    report_markdown = output_dir / "target_context_ab.md"
    report_json.write_text(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ), encoding="utf-8")
    report_markdown.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "report_json": str(report_json.resolve()),
        "report_markdown": str(report_markdown.resolve()),
        "legacy_workbook": str(workbook_paths["legacy"]),
        "context_workbook": str(workbook_paths["context"]),
        "legacy": payload["modes"]["legacy"],
        "context": payload["modes"]["context"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
