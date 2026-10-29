"""성공 응답을 고정하여 감축목표 정리 재평가와 실패 배치만 복구한다."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.saved_extraction_recovery import CodexTextReader, execute, prepare, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_excel")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pdf")
    parser.add_argument("--state-dir")
    parser.add_argument("--extraction-prompts", help="당시 보조 지침 dict JSON. 원본 해시가 일치해야 합니다.")
    parser.add_argument("--recover-failed", action="store_true", help="실패 배치만 각 1회 실제 호출. 기본은 저장 응답 정리만 수행.")
    parser.add_argument("--max-calls", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--codex-command")
    args = parser.parse_args()
    try:
        prepared = prepare(args.baseline_excel, pdf=args.pdf, state_dir=args.state_dir,
                           extraction_prompts=read_json(args.extraction_prompts) if args.extraction_prompts else None)
        reader = None
        if args.recover_failed:
            if prepared["plan"]["provider"] != "codex":
                raise ValueError("실제 복구는 기준 Codex 실행만 지원합니다. 자동 모델 대체는 하지 않습니다.")
            reader = CodexTextReader(prepared["plan"]["model"], args.codex_command)
        print(f"고정 성공 {prepared['plan']['successful_batches_frozen']}배치 / 실패 {len(prepared['plan']['failed_batch_ids'])}배치", flush=True)
        result = execute(prepared, args.output_dir, recover_failed=args.recover_failed, reader=reader,
                         max_calls=args.max_calls, timeout=args.timeout)
        print(json.dumps({key: result[key] for key in ("excel_path", "decision_counts", "model_requests_this_execution",
            "reused_recovery_responses", "baseline_cells_preserved", "exit_code")}, ensure_ascii=False))
        return result["exit_code"]
    except (ValueError, OSError, KeyError) as error:
        print("복구 중단: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
