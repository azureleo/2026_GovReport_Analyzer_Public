"""
로컬 에이전트 기반 지자체 탄소중립 계획 정보 추출 시스템

사용법:
    python main.py <문서경로> [--output 출력경로] [--guideline HWP경로] [--agent codex|claude|auto|gemini]

지원 형식: PDF, HWP, HWPX

예시:
    python main.py "서울특별시_탄소중립계획.pdf"
    python main.py "서울특별시_탄소중립계획.hwp"
    python main.py "서울특별시_탄소중립계획.pdf" --output "서울_추출결과.xlsx"
    python main.py "서울특별시_탄소중립계획.pdf" --agent claude
"""

import argparse
import logging
import os
import sys
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


def main():
    parser = argparse.ArgumentParser(
        description="지자체 탄소중립 기본계획 문서(PDF/HWP/HWPX) → 엑셀 정보 추출 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--quality-report", choices=["on", "off"], help="저장 후 규칙 진단 (모델 추가 호출 없음)")
    parser.add_argument("--evaluation-mode", choices=["auto", "off", "required"], help="승인 골든 평가 정책")
    parser.add_argument("--evaluate", action="store_true", help="골든 평가 required 모드 (Excel/기존 종료 코드 보존)")
    parser.add_argument("--evaluation-manifest", help="골든 등록 JSON 경로")
    parser.add_argument("--evaluation-dataset", help="등록 데이터셋 ID (원문 SHA도 일치해야 함)")
    parser.add_argument("--allow-draft-evaluation", action="store_true", help="검토 전 draft 골든 사용 명시 허용")
    parser.add_argument("--evaluate-holdout", action="store_true", help="holdout 평가 명시 허용")
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
        choices=["codex", "claude", "auto", "gemini"],
        default=None,
        help="LLM 실행 백엔드 (기본값: codex 로컬 에이전트)",
    )
    parser.add_argument(
        "--agent-model",
        default=None,
        help="로컬 에이전트/레거시 Gemini에 전달할 모델명 (선택)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=None,
        help="로컬 에이전트 1회 호출 제한 시간(초, 기본값: 900)",
    )
    parser.add_argument(
        "--api-key", "-k",
        default=None,
        help="레거시 Gemini 백엔드 사용 시 API 키 (기본 로컬 에이전트 실행에는 불필요)",
    )
    parser.add_argument(
        "--retries", "-r",
        type=int,
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
        "--full-scan",
        action="store_true",
        help="시트별 키워드 라우팅/상한에 의존하지 않고 전체 페이지를 추출 후보로 사용",
    )

    parser.add_argument("--selective-ocr", action="store_true", help="저장된 OCR의 변경 객체만 관련 시트로 재추출 (서버 자동 호출 없음)")
    parser.add_argument("--ocr-results-dir", help="사전 생성 OCR Markdown/TXT/JSONL 디렉터리")
    parser.add_argument("--object-batching", action="store_true", help="표/텍스트 객체 청킹 활성화 (복수 헤더 반복)")
    parser.add_argument("--batch-max-chars", type=int, help="추출 입력 문자 상한; 0이면 제한 없음")
    parser.add_argument("--batch-max-table-cells", type=int, help="표 셀 상한; 0이면 제한 없음")
    parser.add_argument("--semantic-routing", choices=["audit", "apply"], help="시트 의미 진단만 또는 근거 있는 전망 행 이동")
    parser.add_argument("--run-state-dir", help="선택적 체크포인트 저장 디렉터리")
    parser.add_argument("--resume", action="store_true", help="동일 입력/설정/구현의 성공 배치만 복원")
    parser.add_argument("--source-coverage", choices=["off", "audit"], help="원문 항목과 최종 저장 셀의 누락 후보 감사만 수행 (추가 모델 호출 없음)")
    args = parser.parse_args()

    setup_logging(args.verbose)
    check_dependencies()

    # LLM/에이전트 백엔드 설정. config.py는 아래 Supervisor import 시점에 읽히므로
    # 여기서 환경변수를 먼저 반영한다.
    if args.agent:
        os.environ["LLM_PROVIDER"] = args.agent
    if args.agent_model:
        os.environ["LOCAL_AGENT_MODEL"] = args.agent_model
        if os.environ.get("LLM_PROVIDER") == "gemini":
            os.environ["GEMINI_MODEL"] = args.agent_model
    if args.agent_timeout:
        os.environ["LOCAL_AGENT_TIMEOUT"] = str(args.agent_timeout)
    if args.max_images is not None:
        os.environ["MAX_IMAGES"] = str(args.max_images)
    if args.full_scan:
        os.environ["FULL_DOCUMENT_SCAN"] = "1"
        os.environ.setdefault("MAX_IMAGES", "0")
    if args.selective_ocr:
        os.environ["SELECTIVE_OCR_ENABLED"] = "1"
    if args.ocr_results_dir:
        if not Path(args.ocr_results_dir).is_dir():
            parser.error("OCR 결과 디렉터리가 없습니다.")
        os.environ["OCR_RESULTS_DIR"] = str(Path(args.ocr_results_dir).resolve())
        os.environ["OCR_BACKEND"] = "directory"
    if args.object_batching:
        os.environ["EXTRACTION_OBJECT_BATCHING_ENABLED"] = "1"
    for value, name in ((args.batch_max_chars, "MAX_BATCH_CHARS"),
                        (args.batch_max_table_cells, "MAX_BATCH_TABLE_CELLS")):
        if value is not None:
            if value < 0:
                parser.error(f"{name}은 0 이상의 정수여야 합니다.")
            os.environ[name] = str(value)
    if args.semantic_routing:
        os.environ["SEMANTIC_ROUTING_ENABLED"] = "1"
        os.environ["SEMANTIC_AUTO_RECLASSIFY"] = "1" if args.semantic_routing == "apply" else "0"
    if args.run_state_dir:
        os.environ["RUN_STATE_ENABLED"] = "1"
        os.environ["RUN_STATE_DIR"] = str(Path(args.run_state_dir).resolve())
    if args.resume:
        os.environ["RUN_STATE_ENABLED"] = "1"
        os.environ["EXTRACTION_RESUME"] = "1"
    if args.source_coverage:
        os.environ["SOURCE_COVERAGE_MODE"] = args.source_coverage

    # 레거시 Gemini API 키 설정
    if args.api_key:
        os.environ["GEMINI_API_KEY"] = args.api_key

    provider = os.environ.get("LLM_PROVIDER", "codex").strip().lower()
    if provider in ("gemini", "gemini-api") and not os.environ.get("GEMINI_API_KEY", ""):
        print("[오류] Gemini 백엔드를 선택했지만 API 키가 설정되지 않았습니다.")
        print("  방법 1: --api-key 옵션 사용")
        print("  방법 2: 환경변수 GEMINI_API_KEY 설정")
        print("  또는 API 없이 실행하려면 --agent codex / --agent claude를 사용하세요.")
        sys.exit(1)

    # config/Supervisor를 가져오기 전에 적용한다. 모델/provider/reasoning은 변경하지 않는다.
    if args.quality_report:
        os.environ["QUALITY_REPORT_ENABLED"] = "1" if args.quality_report == "on" else "0"
    if args.evaluation_mode or args.evaluate:
        os.environ["EVALUATION_MODE"] = "required" if args.evaluate else args.evaluation_mode
    if args.evaluation_manifest:
        os.environ["EVALUATION_MANIFEST_PATH"] = str(Path(args.evaluation_manifest).resolve())
    if args.evaluation_dataset:
        os.environ["EVALUATION_DATASET_ID"] = args.evaluation_dataset
    if args.allow_draft_evaluation:
        os.environ["ALLOW_DRAFT_EVALUATION"] = "1"
    if args.evaluate_holdout:
        os.environ["EVALUATE_HOLDOUT"] = "1"

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

    # 이미지 분석 설정 override
    if args.no_images:
        print("[설정] 이미지·그래프 분석 비활성화")

    file_type = input_path.suffix.upper().lstrip(".")

    print("\n" + "=" * 60)
    print("  로컬 에이전트 기반 탄소중립 계획 정보 추출 시스템")
    print("=" * 60)
    print(f"  입력 파일 ({file_type}): {input_path}")
    print(f"  출력 경로: {output_path}")
    print(f"  가이드라인: {args.guideline or '내장 스키마 사용'}")
    print(f"  LLM 백엔드: {provider}")
    if args.agent_model:
        print(f"  모델: {args.agent_model}")
    if args.full_scan:
        print("  문서 스캔: 전체 페이지 모드")
    if args.max_images is not None:
        print(f"  이미지 상한: {'없음' if args.max_images <= 0 else args.max_images}")
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
        return 0
    except KeyboardInterrupt:
        print("\n[중단] 사용자에 의해 중단되었습니다.")
        return 1
    except Exception as e:
        print(f"\n[오류] 파이프라인 실행 중 오류 발생: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
