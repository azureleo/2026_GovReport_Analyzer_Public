"""저장된 운영 스냅샷으로 정확 근거 객체 분리 정책을 무API A/B 비교한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.visual_merge_ab import (  # noqa: E402
    compare_exact_object_resolution,
    load_visual_merge_snapshot,
)


def _markdown(payload: dict) -> str:
    before = payload["before"]["metrics"]
    after = payload["after"]["metrics"]
    comparison = payload["comparison"]
    return "\n".join([
        "# 정확 근거 객체 분리 무API A/B",
        "",
        "동일한 저장 판독값에 객체 분리 계약만 OFF/ON으로 재적용한 결과입니다.",
        "모델 호출은 발생하지 않으며, 정확도 증명은 별도 골든셋 평가가 필요합니다.",
        "",
        "| 지표 | OFF | ON | 차이 |",
        "|---|---:|---:|---:|",
        f"| exact | {before['evidence_match_counts'].get('exact', 0)} | "
        f"{after['evidence_match_counts'].get('exact', 0)} | "
        f"{comparison['exact_match_delta']} |",
        f"| mixed_objects | {before['evidence_match_counts'].get('mixed_objects', 0)} | "
        f"{after['evidence_match_counts'].get('mixed_objects', 0)} | "
        f"{comparison['mixed_object_delta']} |",
        f"| 근거 ID 교정 | {before['evidence_corrected_count']} | "
        f"{after['evidence_corrected_count']} | "
        f"{comparison['corrected_evidence_delta']} |",
        f"| 시각 출력 행 | {before['visual_output_rows']} | "
        f"{after['visual_output_rows']} | "
        f"{comparison['visual_output_row_delta']} |",
        f"| 중복 출력 행 | {before['visual_output_duplicate_rows']} | "
        f"{after['visual_output_duplicate_rows']} | "
        f"{comparison['visual_output_duplicate_row_delta']} |",
        "",
        "## 분리 방식",
        "",
        f"- OFF: `{json.dumps(before['evidence_resolution_method_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- ON: `{json.dumps(after['evidence_resolution_method_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        f"- LLM 호출 차이: `{comparison['llm_call_delta']}`",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="운영 스냅샷의 정확 근거 객체 분리 정책을 API 없이 A/B 비교"
    )
    parser.add_argument("snapshot", help="*_visual_merge_input.json.gz")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = compare_exact_object_resolution(
        load_visual_merge_snapshot(args.snapshot)
    )
    report_json = output_dir / "exact_object_resolution_ab.json"
    report_markdown = output_dir / "exact_object_resolution_ab.md"
    report_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    report_markdown.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "comparison": payload["comparison"],
        "report_json": str(report_json),
        "report_markdown": str(report_markdown),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
