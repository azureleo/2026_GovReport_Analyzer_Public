# /// script
# requires-python = ">=3.11"
# dependencies = ["openpyxl"]
# ///
# ─── How to run ───
# .venv/bin/python scripts/d30_agreed_pair_accuracy.py <A산출물.xlsx> <B산출물.xlsx> \
#     data/golden/서울특별시_골든셋_v1.xlsx --report-dir output --label 서울_d30
"""D3-0: 교차 백엔드 값합의쌍의 골든 대비 정답률 q를 측정한다 (LLM 호출 0).

밴드 규칙("추정 값일치율 = 값합의율 + 불일치분×정답비율")은 합의쌍이 전부
정답이라는 q=1 가정을 깔고 있다. 이 스크립트는 골든셋이 있는 문서에서
그 가정을 실측한다:

  1. A↔B를 프로브와 동일한 보수 티어(엄격+완화)로 매칭 → 값합의쌍 추출
  2. 골든↔B를 채점기 본축(엄격+완화, 값비교)으로 매칭
  3. 두 매칭을 B행 동일성으로 교집합 → 합의쌍 중 골든 판정 가능분의
     필드 단위 일치율 = q_필드 (행 단위 전량 일치율 = q_행 병기)

값합의쌍 정의·값비교 산식은 각각 cross_backend_probe·채점기와 동일 코드를
재사용하므로 축이 어긋날 수 없다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.cross_backend_probe import _값쌍일치  # noqa: E402
from scripts.golden_score_contract import 값필드, 키  # noqa: E402
from scripts.golden_score_matching import 값비교, 매칭하기  # noqa: E402
from scripts.golden_score_workbook import 워크북읽기  # noqa: E402


def _비율(분자: int, 분모: int) -> float | None:
    return round(분자 / 분모, 4) if 분모 else None


def measure(a_path: Path, b_path: Path, golden_path: Path) -> dict[str, Any]:
    a_sheets = 워크북읽기(a_path, golden=False)
    b_sheets = 워크북읽기(b_path, golden=False)
    golden_sheets = 워크북읽기(golden_path, golden=True)

    per_sheet: dict[str, dict[str, Any]] = {}
    total_joined_fields = 0
    total_joined_hits = 0
    for sheet_name in 값필드:
        a_sheet = a_sheets.get(sheet_name)
        b_sheet = b_sheets.get(sheet_name)
        golden_sheet = golden_sheets.get(sheet_name)
        if a_sheet is None or b_sheet is None or golden_sheet is None:
            continue
        a_rows = [row for row in a_sheet.행들 if 키(row, sheet_name, False) or 키(row, sheet_name, True)]
        b_rows = [row for row in b_sheet.행들 if 키(row, sheet_name, False) or 키(row, sheet_name, True)]

        # 1. A↔B 프로브 축(보수 티어) — 값합의쌍
        ab_matches, _, _, semantic, character, observations = 매칭하기(
            a_rows, b_rows, sheet_name, 보수티어만=True
        )
        if semantic or character or observations:
            raise AssertionError("보수 티어 호출에서 후속 유사도 결과가 생성되었습니다")
        comparable = [
            (match, verdict)
            for match in ab_matches
            if (verdict := _값쌍일치(sheet_name, match)) is not None
        ]
        agreed_b_ids = {id(match.출력) for match, verdict in comparable if verdict}

        # 2. 골든↔B 채점기 본축(엄격+완화)
        if golden_sheet.상태 != "정상" or not golden_sheet.행들:
            per_sheet[sheet_name] = {
                "값비교쌍수": len(comparable),
                "값합의쌍수": len(agreed_b_ids),
                "골든상태": golden_sheet.상태,
                "q_필드": None,
            }
            continue
        g_matches, _, _, _, _, _ = 매칭하기(golden_sheet.행들, b_sheet.행들, sheet_name)
        full_stats, _, _, _ = 값비교(sheet_name, g_matches)

        # 3. 교집합: 합의쌍이면서 골든 매칭된 B행
        joined = [m for m in g_matches if id(m.출력) in agreed_b_ids]
        joined_stats, joined_disagreements, joined_by_row, _ = 값비교(sheet_name, joined)
        row_total = sum(1 for stats in joined_by_row.values() if stats.전체 > 0)
        row_hits = sum(
            1 for stats in joined_by_row.values() if stats.전체 > 0 and stats.일치 == stats.전체
        )
        total_joined_fields += joined_stats.전체
        total_joined_hits += joined_stats.일치

        per_sheet[sheet_name] = {
            "값비교쌍수": len(comparable),
            "값합의쌍수": len(agreed_b_ids),
            "값합의율": _비율(len(agreed_b_ids), len(comparable)),
            "골든매칭_합의쌍수": len(joined),
            "골든미판정_합의쌍수": len(agreed_b_ids) - len(joined),
            "q_필드": joined_stats.비율(),
            "q_필드_분자분모": f"{joined_stats.일치}/{joined_stats.전체}",
            "q_행": _비율(row_hits, row_total),
            "q_행_분자분모": f"{row_hits}/{row_total}",
            "전체B_골든값일치율(참고)": full_stats.비율(),
            "합의쌍_골든불일치_상세": joined_disagreements,
        }

    return {
        "A산출물": str(a_path),
        "B산출물": str(b_path),
        "골든셋": str(golden_path),
        "실행시각": time.strftime("%Y-%m-%d %H:%M:%S"),
        "LLM호출수": 0,
        "시트별": per_sheet,
        "종합_q_필드": _비율(total_joined_hits, total_joined_fields),
        "종합_분자분모": f"{total_joined_hits}/{total_joined_fields}",
    }


def _마크다운(result: dict[str, Any], label: str) -> str:
    lines = [
        f"# D3-0 — 값합의쌍 골든 정답률 q 실측 ({label})",
        "",
        f"- A: `{result['A산출물']}`",
        f"- B: `{result['B산출물']}`",
        f"- 골든: `{result['골든셋']}`",
        f"- 실행시각: {result['실행시각']} · LLM 호출: {result['LLM호출수']}",
        "",
        "산식: 합의쌍 = A↔B 보수 티어 매칭 중 값쌍일치(프로브와 동일 코드) / "
        "q_필드 = 합의쌍∩골든매칭의 값비교(채점기와 동일 코드) 일치/전체 / "
        "q_행 = 값 비교 가능한 합의 행 중 전 필드 일치 행 비율.",
        "",
        "| 시트 | 값합의쌍 | 골든매칭 | 미판정 | q_필드 | q_행 | 전체B 값일치율(참고) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sheet_name, stats in result["시트별"].items():
        if stats.get("q_필드") is None and "골든상태" in stats:
            lines.append(
                f"| {sheet_name} | {stats['값합의쌍수']} | - | - | 골든 {stats['골든상태']} | - | - |"
            )
            continue
        lines.append(
            f"| {sheet_name} | {stats['값합의쌍수']} | {stats['골든매칭_합의쌍수']} "
            f"| {stats['골든미판정_합의쌍수']} "
            f"| {stats['q_필드']} ({stats['q_필드_분자분모']}) "
            f"| {stats['q_행']} ({stats['q_행_분자분모']}) "
            f"| {stats['전체B_골든값일치율(참고)']} |"
        )
    lines += ["", f"**종합 q_필드 = {result['종합_q_필드']} ({result['종합_분자분모']})**", ""]
    disagreement_lines = []
    for sheet_name, stats in result["시트별"].items():
        for item in stats.get("합의쌍_골든불일치_상세", []) or []:
            disagreement_lines.append(
                f"- {sheet_name} · `{item['키']}` · {item['필드']}: "
                f"골든={item['골든값']} vs 합의값={item['출력값']} ({item['매칭방식']})"
            )
    if disagreement_lines:
        lines += ["## 합의쌍인데 골든과 다른 값 (공통 오류 후보 전수)", ""]
        lines += disagreement_lines
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="값합의쌍의 골든 대비 정답률 q 측정")
    parser.add_argument("a", help="기준 백엔드 산출물 xlsx")
    parser.add_argument("b", help="대조 백엔드 산출물 xlsx (골든 채점 대상 축)")
    parser.add_argument("golden", help="골든셋 xlsx")
    parser.add_argument("--report-dir", default="output")
    parser.add_argument("--label", default="d30")
    args = parser.parse_args(argv)

    result = measure(Path(args.a), Path(args.b), Path(args.golden))
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"d30_agreed_pair_accuracy_{args.label}.json"
    md_path = report_dir / f"d30_agreed_pair_accuracy_{args.label}.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(_마크다운(result, args.label), encoding="utf-8")
    print(md_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
