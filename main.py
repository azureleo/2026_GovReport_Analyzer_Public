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
        help="로컬 에이전트 1회 호출 제한 시간(초, 기본값: 900)",
    )
    parser.add_argument(
        "--api-key", "-k",
        default=None,
        help="선택한 API 백엔드 키 (gemini/openai, 로컬 에이전트 실행에는 불필요)",
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
    parser.add_argument(
        "--hybrid-review",
        action="store_true",
        help="기본 추출 후 보조 모델(Gemini 기본값)로 고위험 시트 누락 후보를 별도 검수",
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
    if args.full_scan:
        os.environ["FULL_DOCUMENT_SCAN"] = "1"
        os.environ.setdefault("MAX_IMAGES", "0")
    if args.hybrid_review:
        os.environ["HYBRID_REVIEW_ENABLED"] = "1"
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

    # API 키 설정
    if args.api_key:
        selected_provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
        if selected_provider in ("openai", "openai-api", "gpt"):
            os.environ["OPENAI_API_KEY"] = args.api_key
        else:
            os.environ["GEMINI_API_KEY"] = args.api_key

    import config

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
    print(f"  이미지 분석: {'비활성' if args.no_images else '활성'}")
    if args.no_images:
        print("  이미지 상한: 해당 없음")
    else:
        image_limit = "없음" if config.MAX_IMAGES is None else str(config.MAX_IMAGES)
        print(f"  이미지 상한: {image_limit}")
        print(f"  이미지 배치 크기: {config.IMAGE_ANALYSIS_BATCH_SIZE}")
    print(f"  LLM 캐시: {'활성' if config.LLM_CACHE_ENABLED else '비활성'}")
    print(f"  라우팅 샤프닝: {'활성' if config.ROUTE_DROP_UBIQUITOUS_WEAK else '비활성'}")
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
