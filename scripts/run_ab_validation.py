# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# .venv/bin/python scripts/run_ab_validation.py <source.pdf> [--golden 정답.xlsx]
# .venv/bin/python scripts/run_ab_validation.py <source.pdf> --command ".venv/bin/python main.py"
"""
v4 A/B 검증 하네스.

기본 per-sheet 실행과 EXTRACTION_SHEET_CLUSTERING=1 실행을 서로 다른 LLM 캐시 디렉터리로
분리해 순차 수행하고, 기존 compare_extraction_modes.py 결과를 markdown 리포트로 저장한다.
ROUTE_DROP_UBIQUITOUS_STRONG 검증은 정답 xlsx가 제공된 경우 verify_routing_coverage.py로만
실행하며, 어떤 플래그도 기본값으로 활성화하지 않는다.
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


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n```text\n{body.strip()}\n```\n\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="기본/클러스터링 추출 A/B 검증 리포트 생성")
    parser.add_argument("source", help="검증할 PDF/HWP/HWPX 경로")
    parser.add_argument("--golden", help="선택: 라우팅 커버리지 검증용 정답 xlsx")
    parser.add_argument("--command", default=f"{sys.executable} main.py", help="추출 실행 명령")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="결과/리포트 저장 디렉터리")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    base_xlsx = output_dir / f"ab_base_{stamp}.xlsx"
    cluster_xlsx = output_dir / f"ab_cluster_{stamp}.xlsx"
    report_path = output_dir / f"ab_report_{stamp}.md"

    report = [
        f"# A/B 검증 리포트 ({stamp})\n\n",
        f"- source: `{source}`\n",
        f"- command: `{args.command}`\n",
        "- 판정 주체: 이 리포트는 근거를 만들 뿐, 플래그 활성화 결정은 사람이 한다.\n\n",
    ]

    base = _run_command(
        args.command,
        source,
        base_xlsx,
        {
            "EXTRACTION_SHEET_CLUSTERING": "0",
            "LLM_CACHE_DIR": str(output_dir / f"cache_base_{stamp}"),
        },
    )
    report.append(_section("기본 설정 실행", base.stdout + "\n" + base.stderr))
    if base.returncode != 0:
        report.append(f"기본 실행 실패(exit={base.returncode})\n")
        report_path.write_text("".join(report), encoding="utf-8")
        print(report_path)
        return base.returncode

    cluster = _run_command(
        args.command,
        source,
        cluster_xlsx,
        {
            "EXTRACTION_SHEET_CLUSTERING": "1",
            "LLM_CACHE_DIR": str(output_dir / f"cache_cluster_{stamp}"),
        },
    )
    report.append(_section("클러스터링 실행", cluster.stdout + "\n" + cluster.stderr))
    if cluster.returncode != 0:
        report.append(f"클러스터링 실행 실패(exit={cluster.returncode})\n")
        report_path.write_text("".join(report), encoding="utf-8")
        print(report_path)
        return cluster.returncode

    compare = _run_tool([sys.executable, "scripts/compare_extraction_modes.py", str(base_xlsx), str(cluster_xlsx)])
    report.append(_section("시트별 행 수·값 비교", compare.stdout + "\n" + compare.stderr))

    if args.golden:
        routing = _run_tool([sys.executable, "scripts/verify_routing_coverage.py", args.golden, str(source)])
        report.append(_section("ROUTE_DROP_UBIQUITOUS_STRONG 검증", routing.stdout + "\n" + routing.stderr))

    report.append(
        "## 판정 체크리스트\n\n"
        "- [ ] 시트별 행 수 무회귀 여부 확인\n"
        "- [ ] 핵심 수치 시트 값/숫자 셀 무회귀 여부 확인\n"
        "- [ ] 호출 수·소요 시간 절감율 확인\n"
        "- [ ] 정답 xlsx가 있으면 라우팅 커버리지 무회귀 확인\n"
        "- [ ] 위 조건을 사람이 확인하기 전까지 opt-in 플래그 기본값 유지\n"
    )
    report_path.write_text("".join(report), encoding="utf-8")
    print(report_path)
    return compare.returncode


if __name__ == "__main__":
    raise SystemExit(main())
