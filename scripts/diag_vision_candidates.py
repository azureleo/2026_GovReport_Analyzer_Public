"""vision 후보 결정론 진단 — LLM 호출·렌더 없이 판독 필요 객체와 후보 수를 집계한다.

사용: python scripts/diag_vision_candidates.py <pdf> [--label 이름] [--json 경로]
출력(JSON): 판독 필요 객체 유형별·사유 상위·렌더 변형별·해시 축소 전 후보 수·배치 수.
게이트 문서의 "보조 관찰"에 그대로 쓴다(v8-1 S3-4).
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz  # noqa: E402

import config  # noqa: E402
from agents.image_agent import _vision_batch_size  # noqa: E402
from utils.document_objects import build_document_objects  # noqa: E402
from utils.object_routing import deduplicate_evidence_objects  # noqa: E402
from utils.pdf_reader import extract_pdf  # noqa: E402
from utils.selective_ocr import build_triage_plan, reconstruct_candidate_regions  # noqa: E402


def diagnose(pdf_path: str | Path, label: str = "") -> dict:
    started = time.time()
    document = extract_pdf(str(pdf_path), render_graph_pages=True)
    pages = document.pages
    raw_objects = build_document_objects(pages)
    objects, duplicates = deduplicate_evidence_objects(raw_objects)
    plan = build_triage_plan(
        objects,
        backend="vlm",
        confidence_threshold=float(getattr(config, "OCR_NATIVE_CONFIDENCE_THRESHOLD", 0.78)),
        page_texts={page.page_number: page.text or "" for page in pages},
    )
    required = [row for row in plan if row.action == "ocr_required"]
    page_map = {page.page_number: page for page in pages}
    object_map = {obj.object_id: obj for obj in objects}
    by_page: dict[int, list] = collections.defaultdict(list)
    for obj in objects:
        by_page[obj.page_number].append(obj)

    variants: collections.Counter = collections.Counter()
    total_regions = 0
    with fitz.open(str(pdf_path)) as pdf:
        for row in required:
            obj = object_map.get(row.object_id)
            page = page_map.get(row.page_number)
            if obj is None or page is None or not (1 <= row.page_number <= len(pdf)):
                continue
            regions = reconstruct_candidate_regions(pdf[row.page_number - 1], page, obj, by_page[row.page_number])
            total_regions += len(regions)
            for region in regions:
                variants[region.variant] += 1

    batch_size = _vision_batch_size()
    reasons = collections.Counter(reason for row in required for reason in row.reasons)
    return {
        "label": label or Path(pdf_path).stem,
        "pages": len(pages),
        "raw_objects": len(raw_objects),
        "physical_objects": len(objects),
        "physical_duplicates_merged": duplicates,
        "objects_by_type": dict(collections.Counter(obj.object_type for obj in objects)),
        "action_by_type": dict(collections.Counter(f"{row.object_type}:{row.action}" for row in plan)),
        "ocr_required": len(required),
        "ocr_required_by_type": dict(collections.Counter(row.object_type for row in required)),
        "skipped_index_captions": sum(row.action == "skip_index_caption" for row in plan),
        "required_caption_only": sum(row.caption_only for row in required),
        "required_missing_native": sum(row.missing_native for row in required),
        "required_reason_top": reasons.most_common(25),
        "render_regions_total": total_regions,
        "render_variants": dict(variants),
        "vision_batch_size": batch_size,
        "vision_batches_estimate": -(-total_regions // batch_size) if total_regions else 0,
        "seconds": round(time.time() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("--label", default="")
    parser.add_argument("--json", default="", help="결과 JSON 저장 경로(생략 시 stdout)")
    args = parser.parse_args()
    result = diagnose(args.pdf, args.label)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
        print(f"저장: {args.json}")
    print(text)


if __name__ == "__main__":
    main()
