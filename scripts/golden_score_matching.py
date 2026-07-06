"""행 매칭, 값 비교, 출처유형 집계."""
from __future__ import annotations

import re
from typing import Any

from scripts.golden_score_contract import (
    계약헤더,
    값집계,
    값있음,
    값필드,
    키,
    매칭,
    수치시트,
    시각유래,
    시트자료,
    이차키,
    일차키,
    텍스트값필드,
    텍스트유래,
    출처유형목록,
    출처집계,
    dedup_key_text,
    to_float,
)


def _출력인덱스(rows: list, sheet_name: str, relaxed: bool, remaining: set[int]) -> dict[str, list[tuple[int, Any]]]:
    indexed: dict[str, list[tuple[int, Any]]] = {}
    for idx, row in enumerate(rows):
        if idx not in remaining:
            continue
        row_key = 키(row, sheet_name, relaxed)
        if row_key is None:
            continue
        indexed.setdefault(row_key, []).append((idx, row))
    return indexed


def 매칭하기(golden_rows: list, output_rows: list, sheet_name: str) -> tuple[list[매칭], set[int], set[int]]:
    matches: list[매칭] = []
    remaining_golden = set(range(len(golden_rows)))
    remaining_output = set(range(len(output_rows)))
    for relaxed, label in ((False, "엄격"), (True, "완화")):
        output_index = _출력인덱스(output_rows, sheet_name, relaxed, remaining_output)
        for golden_idx, golden_row in enumerate(golden_rows):
            if golden_idx not in remaining_golden:
                continue
            row_key = 키(golden_row, sheet_name, relaxed)
            if row_key is None:
                continue
            candidates = output_index.get(row_key, [])
            while candidates:
                output_idx, candidate = candidates.pop(0)
                if output_idx in remaining_output:
                    matches.append(매칭(golden_row, candidate, label, row_key))
                    remaining_golden.remove(golden_idx)
                    remaining_output.remove(output_idx)
                    break
    return matches, remaining_golden, remaining_output


def _상대오차일치(left: Any, right: Any) -> bool:
    left_num = to_float(left)
    right_num = to_float(right)
    if left_num is None or right_num is None:
        return dedup_key_text(left) == dedup_key_text(right)
    if left_num == right_num:
        return True
    scale = max(abs(left_num), abs(right_num), 1.0)
    return abs(left_num - right_num) / scale <= 0.005


def _다중값집합(value: Any) -> set[str]:
    text = "" if value is None else str(value).strip()
    if not text:
        return set()
    parts = re.split(r"[,;/·]+", text)
    return {dedup_key_text(part) for part in parts if dedup_key_text(part)}


def _필드일치(field_name: str, golden_value: Any, output_value: Any) -> bool:
    if field_name == "목표연도":
        return _다중값집합(golden_value) == _다중값집합(output_value)
    if field_name in 텍스트값필드:
        return dedup_key_text(golden_value) == dedup_key_text(output_value)
    return _상대오차일치(golden_value, output_value)


def 값비교(sheet_name: str, matches: list[매칭]) -> tuple[값집계, list[dict[str, Any]], dict[int, 값집계]]:
    fields = 값필드.get(sheet_name, [])
    if sheet_name == "00_문서메타":
        fields = [field for field in 계약헤더(sheet_name) if field != "지자체명"]
    total = 값집계()
    by_row: dict[int, 값집계] = {}
    disagreements: list[dict[str, Any]] = []
    for match in matches:
        row_stats = 값집계()
        for field_name in fields:
            golden_value = match.골든.값.get(field_name)
            output_value = match.출력.값.get(field_name)
            golden_has = 값있음(golden_value)
            output_has = 값있음(output_value)
            if not golden_has and not output_has:
                continue
            total.전체 += 1
            row_stats.전체 += 1
            if golden_has and not output_has:
                total.골든만 += 1
                row_stats.골든만 += 1
                continue
            if output_has and not golden_has:
                total.출력만 += 1
                row_stats.출력만 += 1
                continue
            if _필드일치(field_name, golden_value, output_value):
                total.일치 += 1
                row_stats.일치 += 1
            else:
                disagreements.append({"시트": sheet_name, "키": match.키, "필드": field_name, "골든값": golden_value, "출력값": output_value, "매칭방식": match.방식})
        by_row[match.골든.번호] = row_stats
    return total, disagreements, by_row


def 출처집계초기화() -> dict[str, 출처집계]:
    return {name: 출처집계() for name in [*출처유형목록, "텍스트 유래", "시각 유래"]}


def _출처키들(source_type: str) -> list[str]:
    keys = [source_type]
    if source_type in 텍스트유래:
        keys.append("텍스트 유래")
    if source_type in 시각유래:
        keys.append("시각 유래")
    return keys


def 출처집계반영(target: dict[str, 출처집계], golden_rows: list, matches: list[매칭], row_values: dict[int, 값집계]) -> None:
    matched_numbers = {match.골든.번호 for match in matches}
    for row in golden_rows:
        for source_key in _출처키들(row.출처유형):
            target[source_key].골든행 += 1
            if row.번호 in matched_numbers:
                target[source_key].매칭행 += 1
            stats = row_values.get(row.번호)
            if stats is not None:
                target[source_key].값전체 += stats.전체
                target[source_key].값일치 += stats.일치


def 출처직렬화(stats: dict[str, 출처집계]) -> dict[str, dict[str, Any]]:
    return {
        name: {"골든행수": stat.골든행, "매칭수": stat.매칭행, "리콜": stat.리콜(), "값비교수": stat.값전체, "값일치율": stat.값일치율()}
        for name, stat in stats.items()
    }


def 시트점수(sheet_name: str, golden: 시트자료, output: 시트자료) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[매칭], dict[int, 값집계]]:
    if golden.상태 != "정상":
        return ({"상태": golden.상태, "골든행수": 0, "출력행수": len(output.행들), "매칭수": 0, "엄격매칭수": 0, "완화매칭수": 0, "리콜": None, "정밀도": None, "값일치율": None, "골든만있는값": 0, "출력만있는값": 0}, [], [], [], [], {})
    if sheet_name == "00_문서메타":
        if golden.행들 and output.행들:
            matches = [매칭(golden.행들[0], output.행들[0], "엄격", "문서메타")]
            remaining_golden: set[int] = set(range(1, len(golden.행들)))
            remaining_output: set[int] = set(range(1, len(output.행들)))
        else:
            matches = []
            remaining_golden = set(range(len(golden.행들)))
            remaining_output = set(range(len(output.행들)))
    else:
        matches, remaining_golden, remaining_output = 매칭하기(golden.행들, output.행들, sheet_name)
    value_stats, disagreements, row_values = 값비교(sheet_name, matches)
    recall = round(len(matches) / len(golden.행들), 4) if golden.행들 else None
    precision = round(len(matches) / len(output.행들), 4) if output.행들 else None
    missing_golden = [{"시트": sheet_name, "행번호": golden.행들[idx].번호, "키": 키(golden.행들[idx], sheet_name, False) or 키(golden.행들[idx], sheet_name, True) or "", "골든_출처유형": golden.행들[idx].출처유형, "골든_출처페이지": golden.행들[idx].출처페이지} for idx in sorted(remaining_golden)]
    missing_output = [{"시트": sheet_name, "행번호": output.행들[idx].번호, "키": 키(output.행들[idx], sheet_name, False) or 키(output.행들[idx], sheet_name, True) or ""} for idx in sorted(remaining_output)]
    summary = {"상태": "정상", "골든행수": len(golden.행들), "출력행수": len(output.행들), "매칭수": len(matches), "엄격매칭수": sum(1 for match in matches if match.방식 == "엄격"), "완화매칭수": sum(1 for match in matches if match.방식 == "완화"), "리콜": recall, "정밀도": precision, "값일치율": value_stats.비율(), "골든만있는값": value_stats.골든만, "출력만있는값": value_stats.출력만}
    return summary, missing_golden, missing_output, disagreements, matches, row_values


def 없는골든요약(output: 시트자료) -> dict[str, Any]:
    return {"상태": "골든 없음", "골든행수": 0, "출력행수": len(output.행들), "매칭수": 0, "엄격매칭수": 0, "완화매칭수": 0, "리콜": None, "정밀도": None, "값일치율": None, "골든만있는값": 0, "출력만있는값": 0}
