# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# .venv/bin/python scripts/run_ab_validation.py <source.pdf> [--golden 정답.xlsx]
# .venv/bin/python scripts/run_ab_validation.py <source.pdf> --command ".venv/bin/python main.py"
"""
v5 가이드라인 주입 A/B 검증 하네스.

스니펫 주입(GUIDELINE_STRUCTURED_INJECTION=0)과 구조 주입(=1)을 서로 다른 LLM 캐시
디렉터리로 분리해 순차 수행하고, compare_extraction_modes.py 결과를 markdown 리포트로
저장한다. ROUTE_DROP_UBIQUITOUS_STRONG 검증은 정답 xlsx가 제공된 경우
verify_routing_coverage.py로만 실행하며, 라우팅 관련 opt-in 플래그 기본값은 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output"


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _run_command(command: str, source: Path, output: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_extra)
    args = [*shlex.split(command), str(source), "--output", str(output)]
    return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)


def _run_tool(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def _run_golden_score(excel_path: Path, golden_path: str, output_dir: Path, label: str) -> subprocess.CompletedProcess[str]:
    return _run_tool(
        [
            sys.executable,
            "scripts/score_against_golden.py",
            str(excel_path),
            golden_path,
            "--report-dir",
            str(output_dir),
            "--label",
            label,
            "--json",
        ]
    )


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n```text\n{body.strip()}\n```\n\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="스니펫/구조 가이드라인 주입 A/B 검증 리포트 생성")
    parser.add_argument("source", help="검증할 PDF/HWP/HWPX 경로")
    parser.add_argument("--golden", help="선택: 라우팅 커버리지 검증용 정답 xlsx")
    parser.add_argument("--command", default=f"{sys.executable} main.py", help="추출 실행 명령")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="결과/리포트 저장 디렉터리")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    snippet_xlsx = output_dir / f"ab_guideline_snippet_{stamp}.xlsx"
    structured_xlsx = output_dir / f"ab_guideline_structured_{stamp}.xlsx"
    report_path = output_dir / f"ab_report_{stamp}.md"

    report = [
        f"# A/B 검증 리포트 ({stamp})\n\n",
        f"- source: `{source}`\n",
        f"- command: `{args.command}`\n",
        "- 판정 주체: 이 리포트는 근거를 만들 뿐, 플래그 활성화 결정은 사람이 한다.\n\n",
    ]

    snippet = _run_command(
        args.command,
        source,
        snippet_xlsx,
        {
            "GUIDELINE_STRUCTURED_INJECTION": "0",
            "LLM_CACHE_DIR": str(output_dir / f"cache_guideline_snippet_{stamp}"),
        },
    )
    report.append(_section("스니펫 주입 실행", snippet.stdout + "\n" + snippet.stderr))
    if snippet.returncode != 0:
        report.append(f"스니펫 주입 실행 실패(exit={snippet.returncode})\n")
        report_path.write_text("".join(report), encoding="utf-8")
        print(report_path)
        return snippet.returncode

    structured = _run_command(
        args.command,
        source,
        structured_xlsx,
        {
            "GUIDELINE_STRUCTURED_INJECTION": "1",
            "LLM_CACHE_DIR": str(output_dir / f"cache_guideline_structured_{stamp}"),
        },
    )
    report.append(_section("구조 주입 실행", structured.stdout + "\n" + structured.stderr))
    if structured.returncode != 0:
        report.append(f"구조 주입 실행 실패(exit={structured.returncode})\n")
        report_path.write_text("".join(report), encoding="utf-8")
        print(report_path)
        return structured.returncode

    compare = _run_tool([sys.executable, "scripts/compare_extraction_modes.py", str(snippet_xlsx), str(structured_xlsx)])
    report.append(_section("시트별 행 수·값 비교", compare.stdout + "\n" + compare.stderr))

    if args.golden:
        routing = _run_tool([sys.executable, "scripts/verify_routing_coverage.py", args.golden, str(source)])
        report.append(_section("ROUTE_DROP_UBIQUITOUS_STRONG 검증", routing.stdout + "\n" + routing.stderr))
        snippet_score = _run_golden_score(snippet_xlsx, args.golden, output_dir, f"스니펫_{stamp}")
        report.append(_section("골든셋 채점 — 스니펫", snippet_score.stdout + "\n" + snippet_score.stderr))
        structured_score = _run_golden_score(structured_xlsx, args.golden, output_dir, f"구조_{stamp}")
        report.append(_section("골든셋 채점 — 구조", structured_score.stdout + "\n" + structured_score.stderr))

    report.append(
        "## 판정 체크리스트\n\n"
        "- [ ] 시트별 행 수 무회귀 여부 확인\n"
        "- [ ] 핵심 수치 시트 값/숫자 셀 무회귀 여부 확인\n"
        "- [ ] 검증리포트 경고 수 증가 여부 확인\n"
        "- [ ] 구조 주입으로 늘어난 입력 토큰 대비 품질 개선 여부 확인\n"
        "- [ ] 정답 xlsx가 있으면 라우팅 커버리지 무회귀 확인\n"
        "- [ ] 문제가 관찰되면 GUIDELINE_STRUCTURED_INJECTION=0으로 되돌릴 수 있음\n"
    )
    report_path.write_text("".join(report), encoding="utf-8")
    print(report_path)
    return compare.returncode


if __name__ == "__main__":
    raise SystemExit(main())
