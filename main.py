"""
LLM 기반 지자체 탄소중립 계획 정보 추출 시스템

사용법:
    python main.py <문서경로> [--output 출력경로] [--guideline HWP경로] [--agent gemini|openai|codex|claude|auto]

지원 형식: PDF, HWP, HWPX

예시:
    python main.py "서울특별시_탄소중립계획.pdf"
    python main.py "서울특별시_탄소중립계획.hwp"
    python main.py "서울특별시_탄소중립계획.pdf" --output "서울_추출결과.xlsx"
    python main.py "서울특별시_탄소중립계획.pdf" --agent gemini
    python main.py "서울특별시_탄소중립계획.pdf" --agent openai --agent-model gpt-5.4-mini
    python main.py "서울특별시_탄소중립계획.pdf" --agent claude
"""
# noqa: SIZE_OK — CLI 옵션·환경 오버라이드·실행 헤더를 한 진입점에 유지하는 기존 main.

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def check_dependencies():
    """필수 패키지 설치 확인"""
    missing = []
    try:
        import fitz
    except ImportError:
        missing.append("pymupdf")
    try:
        import openpyxl
    except ImportError:
        missing.append("openpyxl")
    try:
        from PIL import Image
    except ImportError:
        missing.append("pillow")

    if missing:
        print(f"[오류] 필수 패키지가 설치되지 않았습니다: {', '.join(missing)}")
        print(f"  설치 방법: pip install {' '.join(missing)}")
        sys.exit(1)


def _configure_stdio_errors() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("1 이상이어야 합니다") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 합니다")
    return parsed


def _verify_output_path_writable(output_path: Path) -> bool:
    probe_path: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as probe:
            probe.write("")
            probe_path = Path(probe.name)
        probe_path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"[오류] 출력 경로를 쓸 수 없습니다: {output_path}")
        print(f"  상세: {exc}")
        return False


def main():
    _configure_stdio_errors()

    parser = argparse.ArgumentParser(
        description="지자체 탄소중립 기본계획 문서(PDF/HWP/HWPX) → 엑셀 정보 추출 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input_path",
        help="입력 문서 파일 경로 (PDF, HWP, HWPX 지원)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="출력 엑셀 파일 경로 (기본값: <지자체명>_탄소중립_추출결과_<날짜>.xlsx)",
    )
    parser.add_argument(
        "--guideline", "-g",
        default=None,
        help="환경부 가이드라인 HWP 파일 경로 (선택)",
    )
    parser.add_argument(
        "--agent",
        choices=["codex", "claude", "auto", "gemini", "openai"],
        default=None,
        help="LLM 실행 백엔드 (기본값: gemini API)",
    )
    parser.add_argument(
        "--agent-model",
        default=None,
        help="Gemini/OpenAI 또는 로컬 에이전트에 전달할 모델명 (선택)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=None,
        help="로컬 에이전트 1회 호출 제한 시간(초, 기본값: 300)",
    )
    parser.add_argument(
        "--api-key", "-k",
        default=None,
        help="선택한 API 백엔드 키 (gemini/openai, 로컬 에이전트 실행에는 불필요)",
    )
    parser.add_argument(
        "--retries", "-r",
        type=_positive_int,
        default=2,
        help="품질 미달 시 재시도 횟수 (기본값: 2)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 로그 출력",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="이미지·그래프 분석 건너뜀 (속도 향상, 정확도 감소)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=None,
        help="이미지 분석 후보 상한. 미지정/0이면 triage 통과 후보 전부 분석",
    )
    parser.add_argument(
        "--ocr-backend",
        choices=["vlm", "unlimited_ocr", "none"],
        default=None,
        help="저신뢰 객체 보완 백엔드 (기본값: vlm)",
    )
    parser.add_argument(
        "--ocr-results-dir",
        default=None,
        help="Unlimited-OCR가 생성한 Markdown/JSONL 결과 디렉터리",
    )
    parser.add_argument(
        "--no-selective-ocr",
        action="store_true",
        help="객체 신뢰도 기반 선택적 OCR을 끄고 기존 이미지 triage만 사용",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="시트별 키워드 라우팅/상한에 의존하지 않고 전체 페이지를 추출 후보로 사용",
    )
    parser.add_argument(
        "--hybrid-review",
        action="store_true",
        help="기본 추출 후 보조 모델(Gemini 기본값)로 고위험 시트 누락 후보를 별도 검수",
    )
    parser.add_argument(
        "--sheet-closed-loop",
        action="store_true",
        help="시트별 추출→정제→검수 폐루프 실행 경로 사용 (실험적, 기본 비활성)",
    )
    parser.add_argument(
        "--hybrid-review-max-batches",
        type=int,
        default=None,
        help="보조 모델 검수 시 시트당 최대 배치 수 (기본값: 2)",
    )
    parser.add_argument(
        "--hybrid-adjudication-model",
        default=None,
        help="보조 검수 후보를 최종 판정할 모델명 (기본값: gemini-2.5-pro)",
    )
    parser.add_argument(
        "--hybrid-adjudication-max-candidates",
        type=int,
        default=None,
        help="Pro 판정 후보 최대 개수 (기본값: 0=전체)",
    )
    parser.add_argument(
        "--hybrid-adjudication-batch-size",
        type=int,
        default=None,
        help="시트 단위 후보판정 시 한 번에 묶어 판정할 후보 수 (기본값: 6)",
    )
    parser.add_argument(
        "--legacy-hybrid-flow",
        action="store_true",
        help="시트 단위 검수·묶음 판정 대신 기존 전체 후보 수집 후 판정 방식 사용",
    )
    parser.add_argument(
        "--no-hybrid-adjudication",
        action="store_true",
        help="보조 검수 후보의 Pro 판정 단계를 건너뜀",
    )
    parser.add_argument(
        "--hybrid-auto-merge",
        action="store_true",
        help="Pro가 accept/fix_then_merge + high로 판정하고 규칙 검사를 통과한 후보를 본 시트에 자동 병합",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="동일 입력·프롬프트·모델의 성공 배치를 복원하고 미완료/실패 배치부터 재개",
    )
    parser.add_argument(
        "--retry-failed-only",
        action="store_true",
        help="기존 성공 체크포인트는 복원하고 실패·부분 배치만 다시 실행 (--resume 포함)",
    )
    parser.add_argument(
        "--retry-vision-evidence",
        action="append",
        default=None,
        metavar="EVIDENCE_ID",
        help="성공 체크포인트 중 지정한 근거 ID의 Vision 판독만 강제 재실행 (반복 지정 가능)",
    )
    parser.add_argument(
        "--retry-vision-pages",
        default=None,
        metavar="PAGES",
        help="지정 페이지의 저신뢰 시각 객체만 강제 재실행 (예: 71,89,131)",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="추출 완료 후 고정 골든셋·시각 인벤토리·라우팅 독립 평가 실행",
    )
    parser.add_argument(
        "--evaluation-manifest",
        default=None,
        help="평가 데이터셋 매니페스트 경로",
    )
    parser.add_argument(
        "--evaluation-dataset",
        default=None,
        help="평가 데이터셋 ID. 생략하면 원문 SHA256으로 선택",
    )
    parser.add_argument(
        "--allow-draft-evaluation",
        action="store_true",
        help="사람 확정 전 초벌 골든셋을 테스트 목적으로 허용",
    )
    parser.add_argument(
        "--evaluate-holdout",
        action="store_true",
        help="개발 튜닝과 분리된 홀드아웃 평가를 명시적으로 허용",
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    check_dependencies()

    # LLM/에이전트 백엔드 설정. config.py는 아래 Supervisor import 시점에 읽히므로
    # 여기서 환경변수를 먼저 반영한다.
    if args.agent:
        os.environ["LLM_PROVIDER"] = args.agent
    if args.agent_model:
        os.environ["LOCAL_AGENT_MODEL"] = args.agent_model
        selected_provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
        if selected_provider in ("gemini", "gemini-api"):
            os.environ["GEMINI_MODEL"] = args.agent_model
        elif selected_provider in ("openai", "openai-api", "gpt"):
            os.environ["OPENAI_MODEL"] = args.agent_model
    if args.agent_timeout:
        os.environ["LOCAL_AGENT_TIMEOUT"] = str(args.agent_timeout)
    if args.max_images is not None:
        os.environ["MAX_IMAGES"] = str(args.max_images)
    if args.ocr_backend:
        os.environ["OCR_BACKEND"] = args.ocr_backend
    if args.ocr_results_dir:
        os.environ["OCR_RESULTS_DIR"] = args.ocr_results_dir
    if args.no_selective_ocr:
        os.environ["SELECTIVE_OCR_ENABLED"] = "0"
    if args.full_scan:
        os.environ["FULL_DOCUMENT_SCAN"] = "1"
        os.environ.setdefault("MAX_IMAGES", "0")
    if args.hybrid_review:
        os.environ["HYBRID_REVIEW_ENABLED"] = "1"
    if args.sheet_closed_loop:
        os.environ["SHEET_CLOSED_LOOP_ENABLED"] = "1"
    if args.hybrid_review_max_batches is not None:
        os.environ["HYBRID_REVIEW_MAX_BATCHES_PER_SHEET"] = str(args.hybrid_review_max_batches)
    if args.hybrid_adjudication_model:
        os.environ["HYBRID_ADJUDICATION_MODEL"] = args.hybrid_adjudication_model
    if args.hybrid_adjudication_max_candidates is not None:
        os.environ["HYBRID_ADJUDICATION_MAX_CANDIDATES"] = str(args.hybrid_adjudication_max_candidates)
    if args.hybrid_adjudication_batch_size is not None:
        os.environ["HYBRID_ADJUDICATION_BATCH_SIZE"] = str(args.hybrid_adjudication_batch_size)
    if args.legacy_hybrid_flow:
        os.environ["HYBRID_SHEETWISE_FLOW_ENABLED"] = "0"
    if args.no_hybrid_adjudication:
        os.environ["HYBRID_ADJUDICATION_ENABLED"] = "0"
    if args.hybrid_auto_merge:
        os.environ["HYBRID_AUTO_MERGE_ENABLED"] = "1"
    if args.resume or args.retry_failed_only:
        os.environ["EXTRACTION_RESUME"] = "1"
    if args.retry_failed_only:
        os.environ["EXTRACTION_RETRY_FAILED_ONLY"] = "1"
    if args.retry_vision_evidence:
        evidence_ids = [
            item.strip()
            for value in args.retry_vision_evidence
            for item in str(value or "").split(",")
            if item.strip()
        ]
        os.environ["VISION_RETRY_EVIDENCE_IDS"] = ",".join(dict.fromkeys(evidence_ids))
        os.environ["EXTRACTION_RESUME"] = "1"
    if args.retry_vision_pages:
        os.environ["VISION_RETRY_PAGES"] = args.retry_vision_pages
        os.environ["EXTRACTION_RESUME"] = "1"

    # API 키 설정
    if args.api_key:
        selected_provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
        if selected_provider in ("openai", "openai-api", "gpt"):
            os.environ["OPENAI_API_KEY"] = args.api_key
        else:
            os.environ["GEMINI_API_KEY"] = args.api_key

    import config

    # 테스트처럼 config가 이미 import된 프로세스에서도 CLI 플래그를 즉시 반영한다.
    if args.resume or args.retry_failed_only:
        config.EXTRACTION_RESUME = True
    if args.retry_failed_only:
        config.EXTRACTION_RETRY_FAILED_ONLY = True
    if args.retry_vision_evidence:
        config.VISION_RETRY_EVIDENCE_IDS = list(dict.fromkeys(
            item.strip()
            for value in args.retry_vision_evidence
            for item in str(value or "").split(",")
            if item.strip()
        ))
        config.EXTRACTION_RESUME = True
    if args.retry_vision_pages:
        config.VISION_RETRY_PAGES = {
            int(item.strip())
            for item in str(args.retry_vision_pages).split(",")
            if item.strip().isdigit() and int(item.strip()) > 0
        }
        config.EXTRACTION_RESUME = True
    if args.ocr_backend:
        config.OCR_BACKEND = args.ocr_backend
    if args.ocr_results_dir:
        config.OCR_RESULTS_DIR = args.ocr_results_dir
    if args.no_selective_ocr:
        config.SELECTIVE_OCR_ENABLED = False

    if (
        config.SELECTIVE_OCR_ENABLED
        and config.OCR_BACKEND in {"unlimited_ocr", "uocr", "markdown"}
        and not config.OCR_RESULTS_DIR
    ):
        print("[오류] Unlimited-OCR 백엔드에는 --ocr-results-dir가 필요합니다.")
        return 1

    provider_aliases = {
        "gemini-api": "gemini",
        "openai-api": "openai",
        "gpt": "openai",
        "local": "codex",
        "local-agent": "codex",
        "claude-code": "claude",
    }
    provider = provider_aliases.get(config.LLM_PROVIDER, config.LLM_PROVIDER)
    if provider in ("gemini", "gemini-api") and not config.GEMINI_API_KEY:
        print("[오류] Gemini 백엔드를 선택했지만 API 키가 설정되지 않았습니다.")
        print("  방법 1: --api-key 옵션 사용")
        print("  방법 2: 환경변수 GEMINI_API_KEY 설정")
        print("  또는 다른 백엔드를 사용하려면 --agent openai / codex / claude를 사용하세요.")
        sys.exit(1)
    if provider == "openai" and not config.OPENAI_API_KEY:
        print("[오류] OpenAI 백엔드를 선택했지만 API 키가 설정되지 않았습니다.")
        print("  방법 1: --api-key 옵션 사용")
        print("  방법 2: 환경변수 OPENAI_API_KEY 설정")
        print("  또는 다른 백엔드를 사용하려면 --agent gemini / codex / claude를 사용하세요.")
        sys.exit(1)
    if provider == "openai":
        try:
            import openai  # noqa: F401
        except ImportError:
            print("[오류] OpenAI 백엔드를 사용하려면 openai 패키지가 필요합니다.")
            print("  설치 방법: py -m pip install -r requirements.txt")
            sys.exit(1)

    # 입력 파일 존재 확인
    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"[오류] 입력 파일을 찾을 수 없습니다: {input_path}")
        sys.exit(1)

    # 지원 형식 확인
    supported_ext = {".pdf", ".hwp", ".hwpx"}
    if input_path.suffix.lower() not in supported_ext:
        print(f"[오류] 지원하지 않는 파일 형식: {input_path.suffix}")
        print(f"  지원 형식: {', '.join(supported_ext)}")
        sys.exit(1)

    guideline_path = Path(args.guideline) if args.guideline else None
    if guideline_path and not guideline_path.exists():
        print(f"[오류] 가이드라인 파일을 찾을 수 없습니다: {guideline_path}")
        sys.exit(1)

    # HWP/HWPX 파일인 경우 Node.js 의존성 확인
    needs_node = input_path.suffix.lower() in (".hwp", ".hwpx")
    needs_node = needs_node or (guideline_path is not None and guideline_path.suffix.lower() in (".hwp", ".hwpx"))
    if needs_node:
        import shutil
        if not shutil.which("node"):
            print("[오류] HWP/HWPX 파일 처리를 위해 Node.js가 필요합니다.")
            print("  설치: https://nodejs.org/")
            sys.exit(1)

    # 출력 경로 결정
    if args.output:
        output_path = Path(args.output)
    else:
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = input_path.parent / f"탄소중립_추출결과_{date_str}.xlsx"

    if not _verify_output_path_writable(output_path):
        return 1

    # 이미지 분석 설정 override
    if args.no_images:
        print("[설정] 이미지·그래프 분석 비활성화")

    file_type = input_path.suffix.upper().lstrip(".")

    print("\n" + "=" * 60)
    print("  LLM 기반 탄소중립 계획 정보 추출 시스템")
    print("=" * 60)
    print(f"  입력 파일 ({file_type}): {input_path}")
    print(f"  출력 경로: {output_path}")
    print(f"  가이드라인: {args.guideline or '내장 스키마 사용'}")
    print(f"  LLM 백엔드: {provider}")
    if provider in ("gemini", "gemini-api"):
        print(f"  모델: {config.MODEL}")
    elif provider == "openai":
        print(f"  모델: {config.OPENAI_MODEL}")
    elif config.LOCAL_AGENT_MODEL:
        print(f"  모델: {config.LOCAL_AGENT_MODEL}")
    else:
        print("  모델: 백엔드 기본값")
    if provider in {"codex", "claude"}:
        print(
            f"  로컬 호출 타임아웃: {config.LOCAL_AGENT_TIMEOUT}초 "
            f"(추가 재시도 {config.LOCAL_AGENT_TIMEOUT_RETRIES}회)"
        )
        print(
            "  타임아웃 배치 자동 분할: "
            f"{'활성' if config.EXTRACTION_SPLIT_ON_TIMEOUT else '비활성'} "
            f"(깊이 {config.EXTRACTION_TIMEOUT_MAX_SPLIT_DEPTH}, "
            f"배치별 {config.EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS}초 상한)"
        )
    print(f"  이미지 분석: {'비활성' if args.no_images else '활성'}")
    if args.no_images:
        print("  이미지 상한: 해당 없음")
    else:
        image_limit = "없음" if config.MAX_IMAGES is None else str(config.MAX_IMAGES)
        print(f"  이미지 상한: {image_limit}")
        print(f"  이미지 배치 크기: {config.IMAGE_ANALYSIS_BATCH_SIZE}")
        print(
            "  선택적 OCR/VLM: "
            f"{'활성' if config.SELECTIVE_OCR_ENABLED else '비활성'}"
        )
        if config.SELECTIVE_OCR_ENABLED:
            print(f"  OCR 백엔드: {config.OCR_BACKEND}")
            print(f"  원본 신뢰도 임계값: {config.OCR_NATIVE_CONFIDENCE_THRESHOLD:g}")
            if config.OCR_BACKEND in {"unlimited_ocr", "uocr", "markdown"}:
                print(f"  OCR 결과 디렉터리: {config.OCR_RESULTS_DIR or '미지정'}")
    print(f"  LLM 캐시: {'활성' if config.LLM_CACHE_ENABLED else '비활성'}")
    if config.RUN_STATE_ENABLED:
        resume_mode = (
            "실패 배치만 재실행"
            if config.EXTRACTION_RETRY_FAILED_ONLY
            else "중단 지점부터 재개"
            if config.EXTRACTION_RESUME
            else "새 실행"
        )
        print(f"  영속 배치 복구: 활성 ({resume_mode})")
    else:
        print("  영속 배치 복구: 비활성")
    print(
        "  텍스트 사전 분할: "
        f"{config.EXTRACTION_MAX_BATCH_CHARS:,}자 "
        f"(복구 {config.EXTRACTION_RECOVERY_MAX_BATCH_CHARS:,}자, "
        f"표 {config.EXTRACTION_TABLE_ROWS_PER_BATCH}행)"
    )
    if not args.no_images:
        print(
            "  Vision 체크포인트: "
            f"{'활성' if config.VISION_CHECKPOINT_ENABLED else '비활성'}"
        )
    print(f"  라우팅 샤프닝: {'활성' if config.ROUTE_DROP_UBIQUITOUS_WEAK else '비활성'}")
    print(f"  결정론적 원문 대조: {'활성' if config.SOURCE_VERIFICATION_ENABLED else '비활성'}")
    print(
        "  원문 객체 인벤토리: "
        f"{'활성' if config.SOURCE_OBJECT_INVENTORY_ENABLED else '비활성'}"
    )
    print(
        "  시트 의미 검증: "
        f"{'활성' if config.SEMANTIC_ROUTING_ENABLED else '비활성'}"
        + (
            " (안전 범위 자동 재분류)"
            if config.SEMANTIC_ROUTING_ENABLED
            and config.SEMANTIC_ROUTING_AUTO_RECLASSIFY
            else ""
        )
    )
    if config.SOURCE_VERIFICATION_ENABLED:
        print(f"  원문 마킹 PDF: {'생성' if config.SOURCE_VERIFICATION_MARK_PDF else '생략'}")
        print(f"  품질 통과 기준: {config.QUALITY_THRESHOLD:g}/100")
    print(f"  시트별 폐루프: {'활성' if config.SHEET_CLOSED_LOOP_ENABLED else '비활성'}")
    print(f"  보조 모델 검수: {'활성' if config.HYBRID_REVIEW_ENABLED else '비활성'}")
    if config.HYBRID_REVIEW_ENABLED:
        print(f"  보조 검수 백엔드: {config.HYBRID_REVIEW_PROVIDER}")
        print(f"  보조 검수 모델: {config.HYBRID_REVIEW_MODEL or '백엔드 기본값'}")
        print(f"  보조 검수 방식: {'시트 단위 탐색·묶음 판정' if config.HYBRID_SHEETWISE_FLOW_ENABLED else '전체 후보 수집 후 판정'}")
        print(f"  보조 검수 시트당 배치: {config.HYBRID_REVIEW_MAX_BATCHES_PER_SHEET}")
        print(f"  보조 후보 Pro 판정: {'활성' if config.HYBRID_ADJUDICATION_ENABLED else '비활성'}")
        if config.HYBRID_ADJUDICATION_ENABLED:
            print(f"  보조 후보 판정 모델: {config.HYBRID_ADJUDICATION_MODEL or '백엔드 기본값'}")
            adjudication_limit = (
                "전체" if config.HYBRID_ADJUDICATION_MAX_CANDIDATES <= 0
                else str(config.HYBRID_ADJUDICATION_MAX_CANDIDATES)
            )
            print(f"  보조 후보 판정 상한: {adjudication_limit}")
            print(f"  보조 후보 판정 묶음 크기: {config.HYBRID_ADJUDICATION_BATCH_SIZE}")
            print(f"  보조 후보 자동 병합: {'활성' if config.HYBRID_AUTO_MERGE_ENABLED else '비활성'}")
    if provider == "gemini":
        print(f"  Gemini 최대 재시도: {config.GEMINI_MAX_RETRIES}")
        print(f"  Gemini 503 빈 결과 처리: {'활성' if config.GEMINI_FAIL_SOFT_ON_TRANSIENT else '비활성'}")
    elif provider == "openai":
        print(f"  OpenAI 최대 재시도: {config.OPENAI_MAX_RETRIES}")
        print(f"  OpenAI 일시 오류 빈 결과 처리: {'활성' if config.OPENAI_FAIL_SOFT_ON_TRANSIENT else '비활성'}")
        print(f"  OpenAI 최대 출력 토큰: {config.OPENAI_MAX_OUTPUT_TOKENS}")
    doc_meta_limit = config.DOCUMENT_ROUTE_MAX_PAGES.get("document_meta")
    print(f"  문서메타 후보 상한: {'없음' if doc_meta_limit is None else doc_meta_limit}")
    if args.full_scan:
        print("  문서 스캔: 전체 페이지 모드")
    print(f"  재시도 횟수: {args.retries}")
    print("=" * 60 + "\n")

    # 파이프라인 실행
    from agents.supervisor import Supervisor

    supervisor = Supervisor()

    try:
        result_path = supervisor.run(
            input_path=input_path,
            output_path=output_path,
            hwp_path=args.guideline,
            max_pipeline_retries=args.retries,
            include_images=not args.no_images,
        )
        print(f"\n완료! 결과 파일: {result_path}")
        if args.evaluate:
            from utils.benchmark_evaluation import (
                EvaluationContractError,
                evaluate_benchmark,
            )

            manifest_path = args.evaluation_manifest or config.EVALUATION_MANIFEST_PATH
            print("\n[평가] 고정 골든셋·객체·라우팅 독립 평가 실행...")
            try:
                evaluation = evaluate_benchmark(
                    result_path,
                    input_path,
                    manifest_path=manifest_path,
                    dataset_id=args.evaluation_dataset,
                    allow_draft=args.allow_draft_evaluation,
                    allow_holdout=args.evaluate_holdout,
                )
            except EvaluationContractError as exc:
                print(f"[평가 오류] {exc}")
                return 2
            metrics = evaluation.metrics
            print(
                "[평가] 완료: "
                f"셀 정확도={metrics.get('cell_accuracy')}, "
                f"객체 재현율={metrics.get('object_recall')}, "
                f"라우팅 오류율={metrics.get('routing_error_rate')}"
            )
            print(f"[평가] 리포트: {evaluation.report_markdown}")
        return 0
    except KeyboardInterrupt:
        supervisor.mark_interrupted("사용자 중단")
        print("\n[중단] 사용자에 의해 중단되었습니다.")
        return 1
    except Exception as e:
        supervisor.mark_interrupted(str(e))
        print(f"\n[오류] 파이프라인 실행 중 오류 발생: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
