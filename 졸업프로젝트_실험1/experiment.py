from __future__ import annotations

import argparse
import sys
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EXPERIMENT_ROOT.parent
for path in (str(EXPERIMENT_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from uocr_experiment.baseline import extract_baseline
from uocr_experiment.evaluation import evaluate_run, write_golden_template
from uocr_experiment.hybrid import build_hybrid
from uocr_experiment.project_bridge import export_project_objects
from uocr_experiment.reporting import write_reports
from uocr_experiment.sampling import prepare_sample
from uocr_experiment.uocr_client import run_uocr
from uocr_experiment.uocr_parser import parse_raw_directory


def parse_pages(value: str | None) -> list[int] | None:
    if not value:
        return None
    pages: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            pages.update(range(int(start), int(end) + 1))
        else:
            pages.add(int(token))
    return sorted(page for page in pages if page > 0)


def add_manifest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, help="prepare가 생성한 sample_manifest.json")


def add_server_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server-url", default="http://127.0.0.1:10000")
    parser.add_argument("--model", default=None, help="생략하면 /v1/models의 첫 모델 사용")
    parser.add_argument("--image-mode", default="gundam")
    parser.add_argument("--prompt", default="document parsing.")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--processor-file", default=None)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--retries", type=int, default=4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="현재 프로젝트와 Unlimited-OCR 비교 실험")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="층화 표본 선정 및 페이지 이미지 렌더링")
    prepare.add_argument("--pdf", required=True)
    prepare.add_argument("--inventory", default=None, help="21_원문객체인벤토리 시트가 있는 기존 결과 XLSX")
    prepare.add_argument("--workspace", default="runs/seoul")
    prepare.add_argument("--pages", default=None, help="수동 페이지 예: 101,120-125")
    prepare.add_argument("--sample-size", type=int, default=50)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--dpi", type=int, default=300)

    baseline = subparsers.add_parser("baseline", help="PyMuPDF 기준선 객체 추출")
    add_manifest(baseline)

    uocr = subparsers.add_parser("uocr", help="SGLang Unlimited-OCR 호출 및 응답 파싱")
    add_manifest(uocr)
    add_server_options(uocr)

    import_uocr = subparsers.add_parser("import-uocr", help="공식 infer.py의 페이지별 Markdown 가져오기")
    add_manifest(import_uocr)
    import_uocr.add_argument("--raw-dir", required=True)

    hybrid = subparsers.add_parser("hybrid", help="기준선과 Unlimited-OCR 객체 규칙 병합")
    add_manifest(hybrid)

    golden = subparsers.add_parser("golden-template", help="사람이 검수할 골든셋 템플릿 생성")
    add_manifest(golden)

    evaluate = subparsers.add_parser("evaluate", help="세 엔진 비교 및 JSON/Markdown/XLSX 보고서 생성")
    add_manifest(evaluate)
    evaluate.add_argument("--golden", default=None)

    export_project = subparsers.add_parser("export-project", help="현재 프로젝트 DocumentObject JSONL로 변환")
    add_manifest(export_project)
    export_project.add_argument("--engine", choices=("pymupdf", "unlimited_ocr", "hybrid"), default="hybrid")

    run = subparsers.add_parser("run", help="prepare부터 평가까지 전체 실행")
    run.add_argument("--pdf", required=True)
    run.add_argument("--inventory", default=None)
    run.add_argument("--workspace", default="runs/seoul")
    run.add_argument("--pages", default=None)
    run.add_argument("--sample-size", type=int, default=50)
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--dpi", type=int, default=300)
    run.add_argument("--raw-dir", default=None, help="지정하면 서버 호출 대신 기존 Markdown 사용")
    run.add_argument("--golden", default=None)
    add_server_options(run)
    return parser


def call_uocr(args: argparse.Namespace, manifest: Path) -> None:
    raw_dir = run_uocr(
        manifest,
        server_url=args.server_url,
        model=args.model,
        image_mode=args.image_mode,
        prompt=args.prompt,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        processor_file=args.processor_file,
        concurrency=args.concurrency,
        retries=args.retries,
    )
    path, objects = parse_raw_directory(manifest, raw_dir)
    print(f"[Unlimited-OCR] {len(objects)}개 객체: {path}")


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        path = prepare_sample(
            args.pdf,
            args.workspace,
            inventory_path=args.inventory,
            pages=parse_pages(args.pages),
            sample_size=args.sample_size,
            seed=args.seed,
            dpi=args.dpi,
        )
        print(f"[준비 완료] {path}")
        return 0

    if args.command == "baseline":
        path, objects = extract_baseline(args.manifest)
        print(f"[PyMuPDF] {len(objects)}개 객체: {path}")
        return 0

    if args.command == "uocr":
        call_uocr(args, Path(args.manifest))
        return 0

    if args.command == "import-uocr":
        path, objects = parse_raw_directory(args.manifest, args.raw_dir)
        print(f"[Unlimited-OCR 가져오기] {len(objects)}개 객체: {path}")
        return 0

    if args.command == "hybrid":
        path, objects, decisions = build_hybrid(args.manifest)
        print(f"[하이브리드] {len(objects)}개 객체, {len(decisions)}개 판정: {path}")
        return 0

    if args.command == "golden-template":
        path = write_golden_template(args.manifest)
        print(f"[골든셋 템플릿] {path}")
        return 0

    if args.command == "evaluate":
        summaries, matches = evaluate_run(args.manifest, args.golden)
        outputs = write_reports(args.manifest, summaries, matches)
        print("[평가 완료] " + ", ".join(str(path) for path in outputs))
        return 0
    if args.command == "export-project":
        path = export_project_objects(args.manifest, args.engine)
        print(f"[프로젝트 객체 변환] {path}")
        return 0

    if args.command == "run":
        manifest = prepare_sample(
            args.pdf,
            args.workspace,
            inventory_path=args.inventory,
            pages=parse_pages(args.pages),
            sample_size=args.sample_size,
            seed=args.seed,
            dpi=args.dpi,
        )
        baseline_path, baseline_objects = extract_baseline(manifest)
        print(f"[PyMuPDF] {len(baseline_objects)}개 객체: {baseline_path}")
        if args.raw_dir:
            uocr_path, uocr_objects = parse_raw_directory(manifest, args.raw_dir)
            print(f"[Unlimited-OCR 가져오기] {len(uocr_objects)}개 객체: {uocr_path}")
        else:
            call_uocr(args, manifest)
        hybrid_path, hybrid_objects, decisions = build_hybrid(manifest)
        print(f"[하이브리드] {len(hybrid_objects)}개 객체, {len(decisions)}개 판정: {hybrid_path}")
        project_path = export_project_objects(manifest, "hybrid")
        print(f"[프로젝트 객체 변환] {project_path}")
        summaries, matches = evaluate_run(manifest, args.golden)
        outputs = write_reports(manifest, summaries, matches)
        print("[전체 완료] " + ", ".join(str(path) for path in outputs))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
