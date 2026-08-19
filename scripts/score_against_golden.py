# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# ─── How to run ───
# .venv/bin/python scripts/score_against_golden.py output.xlsx golden.xlsx --report-dir output --json
"""파이프라인 출력 xlsx와 골든셋 xlsx를 LLM 토큰 0으로 대조한다."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from openpyxl.utils.exceptions import InvalidFileException

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.golden_score_contract import (  # noqa: E402
    경로라벨,
    데이터시트,
    페이지집합,
    수치시트,
    산출유형그룹,
)
from scripts.golden_score_matching import (  # noqa: E402
    문자유사포함출처직렬화,
    시트점수,
    의미완화포함출처직렬화,
    없는골든요약,
    출처직렬화,
    출처집계반영,
    출처집계초기화,
)
from scripts.golden_score_report import 마크다운  # noqa: E402
from scripts.golden_score_workbook import 워크북읽기  # noqa: E402


_의미완화표시필드 = {
    "01_계획개요": (("개요유형", "항목명", "항목값"), ("개요유형", "항목명", "항목값")),
    "02_지역여건": (
        ("지표범주", "지표세부범주", "지표명", "연도", "값"),
        ("지표범주", "지표세부범주", "지표명", "값"),
    ),
    "07_비전전략": (
        ("전략수준", "전략명", "비전문구", "설명"),
        ("전략수준", "전략명", "비전문구", "설명"),
    ),
    "13_이행관리환류": (("거버넌스기구", "역할"), ("거버넌스기구", "역할")),
}


def _산출유형집계초기화() -> dict[str, dict[str, int]]:
    return {
        name: {
            "골든행수": 0,
            "매칭수": 0,
            "값비교수": 0,
            "값일치수": 0,
            "산출유형비교수": 0,
            "산출유형일치수": 0,
        }
        for name in ("reported", "normalized", "calculated", "inferred", "external_lookup")
    }


def _산출유형집계반영(
    target: dict[str, dict[str, int]], golden_rows: list[Any], matches: list[Any], row_values: dict[int, Any]
) -> None:
    matched = {match.골든.번호: match for match in matches}
    for row in golden_rows:
        group = 산출유형그룹(row.값.get("골든_산출유형"))
        stat = target[group]
        stat["골든행수"] += 1
        match = matched.get(row.번호)
        if match is None:
            continue
        stat["매칭수"] += 1
        value_stat = row_values.get(row.번호)
        if value_stat is not None:
            stat["값비교수"] += value_stat.전체
            stat["값일치수"] += value_stat.일치
        stat["산출유형비교수"] += 1
        output_group = 산출유형그룹(match.출력.값.get("derivation_type"))
        if output_group == group:
            stat["산출유형일치수"] += 1


def _산출유형직렬화(stats: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, stat in stats.items():
        result[name] = {
            **stat,
            "리콜": (
                round(stat["매칭수"] / stat["골든행수"], 4)
                if stat["골든행수"] else None
            ),
            "값일치율": (
                round(stat["값일치수"] / stat["값비교수"], 4)
                if stat["값비교수"] else None
            ),
            "산출유형일치율": (
                round(stat["산출유형일치수"] / stat["산출유형비교수"], 4)
                if stat["산출유형비교수"] else None
            ),
        }
    return result


def _의미완화매칭직렬화(sheet_name: str, match: Any) -> dict[str, Any]:
    골든필드, 출력필드 = _의미완화표시필드[sheet_name]
    return {
        "시트": sheet_name,
        "매칭방식": match.방식,
        "유사도": match.키,
        "골든": {field: match.골든.값.get(field) for field in 골든필드},
        "출력": {field: match.출력.값.get(field) for field in 출력필드},
    }


def score_workbooks(output_path: str | Path, golden_path: str | Path, *, report_dir: str | Path = "output", label: str | None = None, write_json: bool = False) -> dict[str, Any]:
    output = Path(output_path).expanduser().resolve()
    golden = Path(golden_path).expanduser().resolve()
    report_base = Path(report_dir).expanduser().resolve()
    report_base.mkdir(parents=True, exist_ok=True)
    label_value = 경로라벨(label)

    output_sheets = 워크북읽기(output, golden=False)
    golden_sheets = 워크북읽기(golden, golden=True)
    sheet_results: dict[str, Any] = {}
    all_missing_golden: list[dict[str, Any]] = []
    all_missing_output: list[dict[str, Any]] = []
    all_disagreements: list[dict[str, Any]] = []
    all_source = 출처집계초기화()
    numeric_source = 출처집계초기화()
    derivation_stats = _산출유형집계초기화()
    format_errors: list[str] = []
    format_warnings: list[str] = []
    skipped: list[str] = []
    excluded = 0
    page_pairs = 0
    page_hits = 0
    전체의미완화매칭 = []
    전체의미완화매칭시트 = []
    수치시트의미완화매칭 = []
    전체문자유사매칭 = []
    전체문자유사관찰 = []
    전체스케일동치: list[dict[str, Any]] = []

    for sheet_name in 데이터시트:
        golden_sheet = golden_sheets[sheet_name]
        output_sheet = output_sheets[sheet_name]
        excluded += golden_sheet.제외행수
        format_warnings.extend(golden_sheet.경고)
        if golden_sheet.상태 == "골든 없음":
            sheet_results[sheet_name] = 없는골든요약(output_sheet)
            continue
        if golden_sheet.상태 == "형식 오류":
            skipped.append(sheet_name)
            format_errors.extend(golden_sheet.오류)
        (
            summary,
            missing_golden,
            missing_output,
            disagreements,
            matches,
            row_values,
            의미완화매칭들,
            문자유사매칭들,
            문자유사관찰들,
            스케일동치쌍들,
        ) = 시트점수(sheet_name, golden_sheet, output_sheet)
        sheet_results[sheet_name] = summary
        all_missing_golden.extend(missing_golden)
        all_missing_output.extend(missing_output)
        all_disagreements.extend(disagreements)
        전체의미완화매칭.extend(의미완화매칭들)
        전체의미완화매칭시트.extend((sheet_name, match) for match in 의미완화매칭들)
        if sheet_name in 수치시트:
            수치시트의미완화매칭.extend(의미완화매칭들)
        전체문자유사매칭.extend(문자유사매칭들)
        전체문자유사관찰.extend(문자유사관찰들)
        전체스케일동치.extend(스케일동치쌍들)
        if golden_sheet.상태 == "정상" and sheet_name != "00_문서메타":
            출처집계반영(all_source, golden_sheet.행들, matches, row_values)
            _산출유형집계반영(derivation_stats, golden_sheet.행들, matches, row_values)
            if sheet_name in 수치시트:
                출처집계반영(numeric_source, golden_sheet.행들, matches, row_values)
        for match in matches:
            golden_pages = 페이지집합(match.골든.값.get("골든_출처페이지"))
            if not golden_pages:
                continue
            page_pairs += 1
            output_pages = 페이지집합(match.출력.값.get("출처페이지"))
            if golden_pages & output_pages:
                page_hits += 1

    scored_sheets = [
        sheet for sheet in sheet_results.values()
        if sheet.get("상태") == "정상"
    ]
    golden_rows = sum(int(sheet.get("골든행수", 0) or 0) for sheet in scored_sheets)
    output_rows = sum(int(sheet.get("출력행수", 0) or 0) for sheet in scored_sheets)
    matched_rows = sum(int(sheet.get("매칭수", 0) or 0) for sheet in scored_sheets)
    matched_cells = sum(int(sheet.get("셀일치수", 0) or 0) for sheet in scored_sheets)
    expected_cells = sum(int(sheet.get("셀기대수", 0) or 0) for sheet in scored_sheets)

    result: dict[str, Any] = {
        "라벨": label_value,
        "파이프라인출력": str(output),
        "골든셋": str(golden),
        "실행시각": time.strftime("%Y-%m-%d %H:%M:%S"),
        "채점제외행수": excluded,
        "생략시트": skipped,
        "형식오류": format_errors,
        "형식경고": format_warnings,
        "시트별": sheet_results,
        "출처유형별_전체": 출처직렬화(all_source),
        "출처유형별_수치시트": 출처직렬화(numeric_source),
        "산출유형별_전체": _산출유형직렬화(derivation_stats),
        "출처유형별_전체_의미완화포함": 의미완화포함출처직렬화(all_source, 전체의미완화매칭),
        "출처유형별_수치시트_의미완화포함": 의미완화포함출처직렬화(
            numeric_source, 수치시트의미완화매칭
        ),
        "출처유형별_전체_문자유사포함": 문자유사포함출처직렬화(
            all_source, 전체의미완화매칭, 전체문자유사매칭
        ),
        "출처유형별_수치시트_문자유사포함": 문자유사포함출처직렬화(
            numeric_source, 수치시트의미완화매칭, 전체문자유사매칭
        ),
        "미매칭상세": {"골든": all_missing_golden, "출력": all_missing_output},
        "값불일치상세": all_disagreements,
        "출처페이지통계": {"비교쌍수": page_pairs, "교집합수": page_hits, "교집합비율": round(page_hits / page_pairs, 4) if page_pairs else None},
        "의미완화매칭수": len(전체의미완화매칭),
        "의미완화매칭쌍": [
            _의미완화매칭직렬화(sheet_name, match)
            for sheet_name, match in 전체의미완화매칭시트
        ],
        "문자유사매칭수": len(전체문자유사매칭),
        "문자유사매칭쌍": [
            {
                "시트": "02_지역여건",
                "매칭방식": match.방식,
                "유사도": match.키,
                "골든": {
                    field: match.골든.값.get(field)
                    for field in ("지표범주", "지표세부범주", "지표명", "연도", "값", "단위")
                },
                "출력": {
                    field: match.출력.값.get(field)
                    for field in ("지표범주", "지표세부범주", "지표명", "연도", "값", "단위")
                },
            }
            for match in 전체문자유사매칭
        ],
        "문자유사관찰쌍": [
            {
                "시트": "02_지역여건",
                "매칭방식": match.방식,
                "유사도": match.키,
                "골든": {
                    field: match.골든.값.get(field)
                    for field in ("지표범주", "지표세부범주", "지표명", "연도", "값", "단위")
                },
                "출력": {
                    field: match.출력.값.get(field)
                    for field in ("지표범주", "지표세부범주", "지표명", "연도", "값", "단위")
                },
            }
            for match in 전체문자유사관찰
        ],
        "스케일동치쌍수": len(전체스케일동치),
        "스케일동치쌍": 전체스케일동치,
        "독립평가지표": {
            # 매칭되지 않은 골든 행을 포함한 모든 채점 대상 값 셀을 분모로 삼는다.
            "cell_accuracy": (
                round(matched_cells / expected_cells, 4) if expected_cells else None
            ),
            "cell_matches": matched_cells,
            "cell_expected": expected_cells,
            "row_recall": round(matched_rows / golden_rows, 4) if golden_rows else None,
            "row_precision": round(matched_rows / output_rows, 4) if output_rows else None,
            "row_matches": matched_rows,
            "golden_rows": golden_rows,
            "output_rows": output_rows,
        },
    }
    md_path = report_base / f"golden_score_{label_value}.md"
    md_path.write_text(마크다운(result), encoding="utf-8")
    result["리포트"] = {"md": str(md_path)}
    if write_json:
        json_path = report_base / f"golden_score_{label_value}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result["리포트"]["json"] = str(json_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="파이프라인 출력 xlsx와 골든셋 xlsx를 LLM 토큰 0으로 채점")
    parser.add_argument("output", help="파이프라인 출력 xlsx")
    parser.add_argument("golden", help="골든셋 xlsx")
    parser.add_argument("--report-dir", default="output", help="리포트 저장 디렉터리")
    parser.add_argument("--json", action="store_true", help="동일 구조의 JSON 리포트도 저장")
    parser.add_argument("--label", default=None, help="리포트 파일 라벨")
    args = parser.parse_args()
    try:
        result = score_workbooks(args.output, args.golden, report_dir=args.report_dir, label=args.label, write_json=args.json)
    except (FileNotFoundError, OSError, InvalidFileException) as exc:
        print(f"파일 오류: {exc}", file=sys.stderr)
        return 2
    print(result["리포트"]["md"])
    if result["형식오류"]:
        for error in result["형식오류"]:
            print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
