"""Build or validate a locked visual regression fixture from selected PDF pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.visual_regression_fixture import (
    VisualRegressionFixtureError,
    build_visual_regression_fixture,
    validate_visual_regression_fixture,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P0 시각 회귀 표본 생성·검증")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="원본 페이지와 객체 crop을 해시 고정")
    build.add_argument("source_pdf")
    build.add_argument("--cases", required=True)
    build.add_argument("--dataset-id", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--dpi", type=int, default=108)
    build.add_argument("--force", action="store_true")

    validate = subparsers.add_parser("validate", help="고정 표본 파일·해시 검증")
    validate.add_argument("manifest")
    validate.add_argument("--source")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_visual_regression_fixture(
                args.source_pdf,
                args.cases,
                args.output_dir,
                dataset_id=args.dataset_id,
                dpi=args.dpi,
                force=args.force,
            )
            report = validate_visual_regression_fixture(
                manifest, source_override=args.source_pdf
            )
            print(json.dumps({"manifest": str(manifest), **report}, ensure_ascii=False))
            return 0 if report["valid"] else 2
        report = validate_visual_regression_fixture(
            args.manifest, source_override=args.source
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 2
    except (
        VisualRegressionFixtureError,
        FileNotFoundError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"시각 회귀 표본 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
