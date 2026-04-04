"""
LLM 기반 지자체 탄소중립 계획 정보 추출 시스템

사용법:
    python main.py <PDF경로> [--output 출력경로] [--guideline HWP경로] [--api-key API키]

예시:
    python main.py "서울특별시_탄소중립계획.pdf"
    python main.py "서울특별시_탄소중립계획.pdf" --output "서울_추출결과.xlsx"
    python main.py "서울특별시_탄소중립계획.pdf" --api-key "sk-ant-..."
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
        import anthropic
    except ImportError:
        missing.append("anthropic")
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
        "pdf_path",
        help="입력 PDF 파일 경로 (지자체 탄소중립 녹색성장 기본계획)",
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

    # PDF 파일 존재 확인
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"[오류] PDF 파일을 찾을 수 없습니다: {pdf_path}")
        sys.exit(1)

    # 출력 경로 결정
    if args.output:
        output_path = Path(args.output)
    else:
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = pdf_path.parent / f"탄소중립_추출결과_{date_str}.xlsx"

    # 이미지 분석 설정 override
    if args.no_images:
        import config as cfg
        # 이미지 에이전트가 이미지를 건너뛰도록 render_graph_pages 비활성화
        print("[설정] 이미지·그래프 분석 비활성화")

    print("\n" + "=" * 60)
    print("  LLM 기반 탄소중립 계획 정보 추출 시스템")
    print("=" * 60)
    print(f"  입력 PDF : {pdf_path}")
    print(f"  출력 경로: {output_path}")
    print(f"  가이드라인: {args.guideline or '내장 스키마 사용'}")
    print(f"  재시도 횟수: {args.retries}")
    print("=" * 60 + "\n")

    # 파이프라인 실행
    from agents.supervisor import Supervisor

    supervisor = Supervisor()

    try:
        result_path = supervisor.run(
            pdf_path=pdf_path,
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
