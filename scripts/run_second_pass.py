"""확정 누락의 계획·제한 판독·후단 연결. 기본 동작은 모델 호출 없는 계획 생성."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.second_pass import prepare, save_plan, execute, read_json, replay_saved


def main(argv=None):
    parser = argparse.ArgumentParser(description="확정 예산 누락의 제한적 2차 판독. 기본은 dry-run, 실제 호출은 --execute")
    parser.add_argument("--targets", required=True)
    parser.add_argument("--input", required=True, help="원본 PDF")
    parser.add_argument("--output-dir", required=True, help="전용 새 출력 폴더")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true", help="기준 실행의 모델로 실제 호출")
    mode.add_argument("--fixture-responses", help="group_id -> JSON 응답 파일. 모델 성능 측정 아님")
    mode.add_argument("--reuse-run", help="기존 실행 폴더의 검증된 응답만 새 후단으로 재평가. 모델 호출 없음")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--call-timeout", type=float, default=90)
    parser.add_argument("--codex-command", help="Codex 실행 파일 절대경로")
    args = parser.parse_args(argv)
    try:
        run = prepare(args.targets, args.input, max_calls=args.max_calls, max_seconds=args.max_seconds, call_timeout=args.call_timeout)
        directory = Path(args.output_dir).resolve() if args.reuse_run else save_plan(run, args.output_dir)
        mode_label = "기존 응답 재평가, 새 모델 호출 0회" if args.reuse_run else f"호출 상한 {args.max_calls}"
        print(f"계획: 대상 {run['plan']['target_count']}개 / 표 객체 {run['plan']['group_count']}개 / {mode_label}", flush=True)
        print(f"기준 모델: {run['plan']['provider']} / {run['plan']['model']}; 예상 금액은 모델에 전달하지 않음", flush=True)
        if not args.execute and not args.fixture_responses and not args.reuse_run:
            print(f"dry-run 완료, 모델 호출 0회: {directory / 'plan.json'}")
            return 0
        if args.reuse_run:
            if args.resume:
                raise ValueError("--reuse-run은 새 출력 폴더로 실행하며 --resume과 함께 사용하지 않습니다.")
            result = replay_saved(run,args.reuse_run,directory,progress=lambda message:print(message,flush=True))
        elif args.fixture_responses:
            responses = read_json(args.fixture_responses)
            def reader(prompt, image, timeout, group):
                value = responses[group["group_id"]]
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        else:
            from utils.second_pass_transport import CodexReader
            reader = CodexReader(run["plan"]["model"], Path(__file__).resolve().parents[1], args.codex_command)
        if not args.reuse_run:
            result = execute(run, directory, resume=args.resume, reader=reader, fixture=bool(args.fixture_responses),
                             progress=lambda message:print(message, flush=True))
        for warning in result.get("warnings", []):
            print("경고: " + warning, file=sys.stderr)
        print(json.dumps({k:result[k] for k in ["excel_path","new_rows","status_counts","model_requests","reused_responses","exit_code"]}, ensure_ascii=False))
        print("분류 보완: " + json.dumps(result["context_summary"],ensure_ascii=False))
        return result["exit_code"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"2차 실행 중단: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("사용자 중단. 호출 전 기록과 완료 응답은 보존됩니다. --resume은 미완료 호출을 자동 반복하지 않습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
