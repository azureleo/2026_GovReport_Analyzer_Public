"""Inspect the exact ImageAgent input queue without LLM calls or Excel extraction."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description="모델 호출 없는 Vision 후보 사전 점검")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enable-review", action="store_true", help="이번 점검에만 제한적 판독 후보 활성화")
    parser.add_argument("--toc-mode", choices=("off", "audit", "exclude"), help="이번 점검의 목차 정책: 기록만 또는 호출 제외")
    args = parser.parse_args(argv)
    if not args.input.is_file():
        parser.error(f"입력 파일을 찾을 수 없습니다: {args.input}")
    if args.output.exists():
        parser.error("기존 결과를 덮어쓰지 않습니다. 새 --output 이름을 지정하세요.")

    import config
    from agents.image_agent import ImageAgent
    from utils import llm_client
    from utils.pdf_reader import extract_pdf
    from utils.run_state import sha256_file

    def forbidden(*_args, **_kwargs):
        raise AssertionError("사전 점검에서 모델 호출 금지")

    with ExitStack() as stack:
        if args.toc_mode:
            stack.enter_context(patch.object(config, "VISION_TOC_MODE", args.toc_mode))
        if args.enable_review:
            stack.enter_context(patch.object(config, "VISION_REVIEW_ENABLED", True))
        for name in ("call_text", "call_text_json", "call_vision", "call_vision_json", "call_vision_batch", "call_vision_batch_json"):
            stack.enter_context(patch.object(llm_client, name, forbidden))
        document = extract_pdf(str(args.input.resolve()))
        result = ImageAgent().extract(document.pages, {}, "알 수 없음", document=document, preflight_only=True)
        plan = result["vision_preflight"]
    plan.update(input_path=str(args.input.resolve()), input_sha256=sha256_file(args.input), model_calls=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    print(f"사전 점검 저장: {args.output}")
    print(f"일반 입력 {plan['regular_inputs']}개 / 제한적 판독 입력 {plan['review_inputs']}개 / 보류 시각 객체 {plan['held_visual_objects']}개")
    print(f"후보 해시: {plan['candidate_sha256']}")
    print(plan["warning"])
    return 0 if plan["comparison_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
