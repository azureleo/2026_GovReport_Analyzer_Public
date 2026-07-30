"""고정 골든셋/홀드아웃 평가 계약을 실행하는 CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from utils.benchmark_evaluation import (  # noqa: E402
    EvaluationContractError,
    evaluate_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="고정 골든셋·시각 인벤토리·시트 의미 라우팅을 한 번에 평가"
    )
    parser.add_argument("output", help="파이프라인 출력 xlsx")
    parser.add_argument("source", help="원본 PDF")
    parser.add_argument(
        "--manifest",
        default=config.EVALUATION_MANIFEST_PATH,
        help="평가 데이터셋 매니페스트",
    )
    parser.add_argument("--dataset", default=None, help="평가 데이터셋 ID")
    parser.add_argument(
        "--allow-draft-evaluation",
        action="store_true",
        help="사람 확정 전 초벌 골든셋을 테스트 목적으로 허용",
    )
    parser.add_argument(
        "--evaluate-holdout",
        action="store_true",
        help="개발 중 일상 평가와 분리된 홀드아웃 평가를 명시적으로 허용",
    )
    parser.add_argument("--report-dir", default=None, help="평가 리포트 저장 경로")
    args = parser.parse_args()

    try:
        result = evaluate_benchmark(
            args.output,
            args.source,
            manifest_path=args.manifest,
            dataset_id=args.dataset,
            allow_draft=args.allow_draft_evaluation,
            allow_holdout=args.evaluate_holdout,
            report_dir=args.report_dir,
        )
    except EvaluationContractError as exc:
        print(f"[평가 오류] {exc}", file=sys.stderr)
        return 2

    print(json.dumps({
        "dataset": result.dataset.dataset_id,
        "role": result.dataset.role,
        "metrics": result.metrics,
        "json": str(result.report_json),
        "markdown": str(result.report_markdown),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
