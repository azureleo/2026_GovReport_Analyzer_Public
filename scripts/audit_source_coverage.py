"""기존 저장 결과를 모델 재호출 없이 감사하는 독립 CLI."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.source_coverage import audit_saved_run


def main():
    parser = argparse.ArgumentParser(description="저장된 문서 객체와 실제 Excel의 누락 후보 감사 (모델 호출 없음)")
    parser.add_argument("--input", required=True, help="원본 문서; SHA256 대응 확인용")
    parser.add_argument("--excel", required=True, help="비교할 기존 Excel")
    parser.add_argument("--pipeline-audit", required=True, help="같은 실행의 pipeline_audit.json")
    parser.add_argument("--output-prefix", required=True, help="새 출력 폴더/실행명 (확장자 제외)")
    parser.add_argument("--overwrite", action="store_true", help="기존 감사 산출물만 명시적으로 교체")
    args = parser.parse_args()
    try:
        result = audit_saved_run(args.input, args.excel, args.pipeline_audit, args.output_prefix,
                                 overwrite=args.overwrite)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"감사 실행 실패: {error}\n")
    print(f"비교 가능 사실: {result['comparable_facts']} / 상태: {result['counts']}")
    print(result["coverage_summary_path"])
    print(result["missing_candidates_path"])
    print(result["pipeline_audit_link_path"])
    print("모델 재호출 0회. 누락 후보는 정답 판정이 아니며 별도 검토가 필요합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
