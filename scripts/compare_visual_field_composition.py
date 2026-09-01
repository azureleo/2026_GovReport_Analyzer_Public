"""저장된 운영 스냅샷으로 캡션·축·범례 필드 조합을 무API A/B 비교한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.visual_merge_ab import (  # noqa: E402
    compare_visual_field_composition,
    load_visual_merge_snapshot,
)


def _markdown(payload: dict) -> str:
    before = payload["before"]["metrics"]
    after = payload["after"]["metrics"]
    comparison = payload["comparison"]
    return "\n".join([
        "# 캡션·축·범례 필드 조합 무API A/B",
        "",
        "동일한 저장 판독값에 필드 조합기만 OFF/ON으로 재적용한 결과입니다.",
        "정확히 하나의 원문 객체로 분리된 행만 조합하며 모델 호출은 발생하지 않습니다.",
        "",
        "| 지표 | OFF | ON | 차이 |",
        "|---|---:|---:|---:|",
        f"| 시각 출력 행 | {before['visual_output_rows']} | {after['visual_output_rows']} | {comparison['visual_output_row_delta']} |",
        f"| 중복 출력 행 | {before['visual_output_duplicate_rows']} | {after['visual_output_duplicate_rows']} | {comparison['visual_output_duplicate_row_delta']} |",
        f"| G3 키 누락 차단 | {before['g3_missing_key_blocker_count']} | {after['g3_missing_key_blocker_count']} | {comparison['g3_missing_key_blocker_delta']} |",
        f"| 조합 감사 대상 | {before['field_composition_candidate_count']} | {after['field_composition_candidate_count']} | {comparison['field_composition_candidate_delta']} |",
        f"| 조합 충돌 | {before['field_composition_conflict_count']} | {after['field_composition_conflict_count']} | {comparison['field_composition_conflict_delta']} |",
        "",
        "## ON 조합 상태",
        "",
        f"`{json.dumps(after['field_composition_status_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## ON 필드별 보완 수",
        "",
        f"`{json.dumps(after['field_composition_fill_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        f"- LLM 호출 차이: `{comparison['llm_call_delta']}`",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="운영 스냅샷의 캡션·축·범례 필드 조합을 API 없이 A/B 비교"
    )
    parser.add_argument("snapshot", help="*_visual_merge_input.json.gz")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = compare_visual_field_composition(
        load_visual_merge_snapshot(args.snapshot)
    )
    report_json = output_dir / "visual_field_composition_ab.json"
    report_markdown = output_dir / "visual_field_composition_ab.md"
    report_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    report_markdown.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "comparison": payload["comparison"],
        "field_composition_status_counts": payload["after"]["metrics"]["field_composition_status_counts"],
        "field_composition_fill_counts": payload["after"]["metrics"]["field_composition_fill_counts"],
        "report_json": str(report_json),
        "report_markdown": str(report_markdown),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
