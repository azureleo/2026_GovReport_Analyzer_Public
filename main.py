"""
LLM 기반 지자체 탄소중립 계획 정보 추출 시스템

사용법:
    python main.py <문서경로> [--output 출력경로] [--guideline HWP경로] [--api-key API키]

지원 형식: PDF, HWP, HWPX

예시:
    python main.py "서울특별시_탄소중립계획.pdf"
    python main.py "서울특별시_탄소중립계획.hwp"
    python main.py "서울특별시_탄소중립계획.pdf" --output "서울_추출결과.xlsx"
    python main.py "서울특별시_탄소중립계획.pdf" --api-key "AIza..."
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
        description="지자체 탄소중립 기본계획 PDF → 엑셀 정보 추출 시스템",
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
        "--api-key", "-k",
        default=None,
        help="Anthropic API 키 (환경변수 ANTHROPIC_API_KEY로도 설정 가능)",
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

    args = parser.parse_args()

    setup_logging(args.verbose)
    check_dependencies()

    # API 키 설정
    if args.api_key:
        os.environ["GEMINI_API_KEY"] = args.api_key

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[오류] Gemini API 키가 설정되지 않았습니다.")
        print("  방법 1: --api-key 옵션 사용")
        print("  방법 2: 환경변수 GEMINI_API_KEY 설정")
        print("  방법 3: 프로젝트 폴더 .env 파일에 GEMINI_API_KEY=AIza... 작성")
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

    # HWP/HWPX 파일인 경우 Node.js 의존성 확인
    if input_path.suffix.lower() in (".hwp", ".hwpx"):
        import shutil
        if not shutil.which("node"):
            print("[오류] HWP 파일 처리를 위해 Node.js가 필요합니다.")
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
        import config as cfg
        # 이미지 에이전트가 이미지를 건너뛰도록 render_graph_pages 비활성화
        print("[설정] 이미지·그래프 분석 비활성화")

    file_type = input_path.suffix.upper().lstrip(".")

    print("\n" + "=" * 60)
    print("  LLM 기반 탄소중립 계획 정보 추출 시스템")
    print("=" * 60)
    print(f"  입력 파일 ({file_type}): {input_path}")
    print(f"  출력 경로: {output_path}")
    print(f"  가이드라인: {args.guideline or '내장 스키마 사용'}")
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
