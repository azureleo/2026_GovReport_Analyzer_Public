"""행 매칭, 값 비교, 출처유형 집계."""
from __future__ import annotations

import re
from typing import Any

from scripts.benchmark_golden_contract import (
    CHAR_SIMILARITY_MIN_JACCARD,
    계약헤더,
    값집계,
    값있음,
    값필드,
    키,
    키값,
    매칭,
    수치시트,
    시각유래,
    시트자료,
    이차키,
    일차키,
    의미완화적용시트,
    SEMANTIC_MATCH_MIN_JACCARD,
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


def _세부부문완화후보허용(sheet_name: str, golden_row: Any, output_row: Any) -> bool:
    if sheet_name not in {"03_배출현황_지역", "04_배출현황_관리권한"}:
        return True
    golden_subsector = dedup_key_text(golden_row.값.get("세부부문"))
    output_subsector = dedup_key_text(output_row.값.get("세부부문"))
    if not golden_subsector or not output_subsector or golden_subsector == output_subsector:
        return True
    golden_value = golden_row.값.get("배출량")
    output_value = output_row.값.get("배출량")
    return (
        값있음(golden_value)
        and 값있음(output_value)
        and _상대오차일치(golden_value, output_value)
    )


def _감축목표완화후보허용(
    sheet_name: str,
    golden_row: Any,
    output_row: Any,
) -> bool:
    if sheet_name != "06_감축목표":
        return True
    golden_level = dedup_key_text(golden_row.값.get("목표수준"))
    output_level = dedup_key_text(output_row.값.get("목표수준"))
    golden_base_year = 키값("기준연도", golden_row.값.get("기준연도"))
    output_base_year = 키값("기준연도", output_row.값.get("기준연도"))
    level_diverged = bool(
        golden_level and output_level and golden_level != output_level
    )
    base_year_diverged = bool(
        golden_base_year
        and output_base_year
        and golden_base_year != output_base_year
    )
    if not level_diverged and not base_year_diverged:
        return True
    common_fields = [
        field_name
        for field_name in (
            "목표감축량",
            "목표배출량",
            "감축률(%)",
            "기준배출량",
            "배출전망",
        )
        if 값있음(golden_row.값.get(field_name))
        and 값있음(output_row.값.get(field_name))
    ]
    return bool(common_fields) and all(
        _상대오차일치(
            golden_row.값.get(field_name),
            output_row.값.get(field_name),
        )
        for field_name in common_fields
    )


_의미완화토큰필드 = {
    "01_계획개요": ("항목명", "항목값"),
    "02_지역여건": ("지표명", "지표세부범주"),
    "07_비전전략": ("전략명", "비전문구", "설명"),
    "13_이행관리환류": ("거버넌스기구", "역할"),
}


def _지표토큰(row: Any, sheet_name: str = "02_지역여건") -> set[str]:
    text = " ".join(
        str(row.값.get(field_name) or "")
        for field_name in _의미완화토큰필드[sheet_name]
    )
    parts = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split()
    return {정규화토큰 for 토큰 in parts if (정규화토큰 := dedup_key_text(토큰))}


def _자카드(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _의미완화전제충족(
    golden_row: Any,
    output_row: Any,
    sheet_name: str = "02_지역여건",
) -> bool:
    if sheet_name == "01_계획개요":
        golden_type = dedup_key_text(golden_row.값.get("개요유형"))
        output_type = dedup_key_text(output_row.값.get("개요유형"))
        if not golden_type and not output_type:
            return False
        return not (
            golden_type
            and output_type
            and golden_type != output_type
        )
    if sheet_name == "07_비전전략":
        golden_level = dedup_key_text(golden_row.값.get("전략수준"))
        output_level = dedup_key_text(output_row.값.get("전략수준"))
        return not (
            golden_level
            and output_level
            and golden_level != output_level
        )
    if sheet_name == "13_이행관리환류":
        return True
    if sheet_name != "02_지역여건":
        return False
    골든연도 = 키값("연도", golden_row.값.get("연도"))
    출력연도 = 키값("연도", output_row.값.get("연도"))
    if not 골든연도 or not 출력연도 or 골든연도 != 출력연도:
        return False
    골든단위 = golden_row.값.get("단위")
    출력단위 = output_row.값.get("단위")
    if 값있음(골든단위) and 값있음(출력단위) and dedup_key_text(골든단위) != dedup_key_text(출력단위):
        return False
    골든값 = golden_row.값.get("값")
    출력값 = output_row.값.get("값")
    if not 값있음(골든값) or not 값있음(출력값) or not _상대오차일치(골든값, 출력값):
        return False
    return True


def _의미완화후보(
    golden_row: Any,
    output_row: Any,
    sheet_name: str,
) -> float | None:
    if not _의미완화전제충족(golden_row, output_row, sheet_name):
        return None
    유사도 = _자카드(
        _지표토큰(golden_row, sheet_name),
        _지표토큰(output_row, sheet_name),
    )
    return 유사도 if 유사도 >= SEMANTIC_MATCH_MIN_JACCARD else None


def _의미완화매칭하기(
    golden_rows: list,
    output_rows: list,
    remaining_golden: set[int],
    remaining_output: set[int],
    sheet_name: str,
) -> list[매칭]:
    의미완화매칭들: list[매칭] = []
    의미완화골든잔여 = set(remaining_golden)
    의미완화출력잔여 = set(remaining_output)
    골든별후보: dict[int, list[tuple[float, int, Any]]] = {}
    출력별골든: dict[int, list[tuple[int, float]]] = {}
    for golden_idx in sorted(remaining_golden):
        candidates: list[tuple[float, int, Any]] = []
        for output_idx in sorted(remaining_output):
            유사도 = _의미완화후보(
                golden_rows[golden_idx],
                output_rows[output_idx],
                sheet_name,
            )
            if 유사도 is None:
                continue
            candidates.append((유사도, output_idx, output_rows[output_idx]))
            출력별골든.setdefault(output_idx, []).append((golden_idx, 유사도))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        골든별후보[golden_idx] = candidates
    for golden_idx, golden_row in enumerate(golden_rows):
        if golden_idx not in 의미완화골든잔여:
            continue
        candidates = [item for item in 골든별후보[golden_idx] if item[1] in 의미완화출력잔여]
        if not candidates:
            continue
        최고유사도 = candidates[0][0]
        if sum(1 for 유사도, _, _ in candidates if 유사도 == 최고유사도) != 1:
            continue
        _, output_idx, output_row = candidates[0]
        골든동률 = False
        for other_golden_idx, 선택유사도 in 출력별골든.get(output_idx, []):
            if other_golden_idx == golden_idx or other_golden_idx not in 의미완화골든잔여:
                continue
            다른후보 = [
                item for item in 골든별후보[other_golden_idx] if item[1] in 의미완화출력잔여
            ]
            if 다른후보 and 선택유사도 == 다른후보[0][0] == 최고유사도:
                골든동률 = True
                break
        if 골든동률:
            continue
        의미완화매칭들.append(매칭(golden_row, output_row, "의미완화", f"자카드={최고유사도:.4f}"))
        의미완화골든잔여.remove(golden_idx)
        의미완화출력잔여.remove(output_idx)
    return 의미완화매칭들


def _지표문자그램(row: Any) -> set[str]:
    text = f"{row.값.get('지표명') or ''}{row.값.get('지표세부범주') or ''}"
    normalized = dedup_key_text(re.sub(r"[^\w]", "", text, flags=re.UNICODE))
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {normalized[idx : idx + 2] for idx in range(len(normalized) - 1)}


def _문자유사도(golden_row: Any, output_row: Any) -> float | None:
    if not _의미완화전제충족(golden_row, output_row):
        return None
    골든그램 = _지표문자그램(golden_row)
    출력그램 = _지표문자그램(output_row)
    if not 골든그램 or not 출력그램:
        return None
    return _자카드(골든그램, 출력그램)


def _문자유사매칭하기(
    golden_rows: list, output_rows: list, remaining_golden: set[int], remaining_output: set[int]
) -> tuple[list[매칭], list[매칭]]:
    문자유사매칭들: list[매칭] = []
    문자유사관찰들: list[매칭] = []
    문자유사골든잔여 = set(remaining_golden)
    문자유사출력잔여 = set(remaining_output)
    골든별후보: dict[int, list[tuple[float, int, Any]]] = {}
    출력별골든: dict[int, list[tuple[int, float]]] = {}
    for golden_idx in sorted(remaining_golden):
        candidates: list[tuple[float, int, Any]] = []
        for output_idx in sorted(remaining_output):
            유사도 = _문자유사도(golden_rows[golden_idx], output_rows[output_idx])
            if 유사도 is None:
                continue
            if 0.20 <= 유사도 < CHAR_SIMILARITY_MIN_JACCARD:
                문자유사관찰들.append(
                    매칭(
                        golden_rows[golden_idx],
                        output_rows[output_idx],
                        "문자유사 관찰(비매칭)",
                        f"자카드={유사도:.4f}",
                    )
                )
            if 유사도 < CHAR_SIMILARITY_MIN_JACCARD:
                continue
            candidates.append((유사도, output_idx, output_rows[output_idx]))
            출력별골든.setdefault(output_idx, []).append((golden_idx, 유사도))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        골든별후보[golden_idx] = candidates
    for golden_idx, golden_row in enumerate(golden_rows):
        if golden_idx not in 문자유사골든잔여:
            continue
        candidates = [item for item in 골든별후보[golden_idx] if item[1] in 문자유사출력잔여]
        if not candidates:
            continue
        최고유사도 = candidates[0][0]
        if sum(1 for 유사도, _, _ in candidates if 유사도 == 최고유사도) != 1:
            continue
        _, output_idx, output_row = candidates[0]
        골든동률 = False
        for other_golden_idx, 선택유사도 in 출력별골든.get(output_idx, []):
            if other_golden_idx == golden_idx or other_golden_idx not in 문자유사골든잔여:
                continue
            다른후보 = [
                item for item in 골든별후보[other_golden_idx] if item[1] in 문자유사출력잔여
            ]
            if 다른후보 and 선택유사도 == 다른후보[0][0] == 최고유사도:
                골든동률 = True
                break
        if 골든동률:
            continue
        문자유사매칭들.append(매칭(golden_row, output_row, "문자유사", f"자카드={최고유사도:.4f}"))
        문자유사골든잔여.remove(golden_idx)
        문자유사출력잔여.remove(output_idx)
    return 문자유사매칭들, 문자유사관찰들


def 매칭하기(
    golden_rows: list,
    output_rows: list,
    sheet_name: str,
    보수티어만: bool = False,
) -> tuple[list[매칭], set[int], set[int], list[매칭], list[매칭], list[매칭]]:
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
                if output_idx not in remaining_output:
                    continue
                if relaxed and not _세부부문완화후보허용(sheet_name, golden_row, candidate):
                    continue
                if relaxed and not _감축목표완화후보허용(
                    sheet_name,
                    golden_row,
                    candidate,
                ):
                    continue
                matches.append(매칭(golden_row, candidate, label, row_key))
                remaining_golden.remove(golden_idx)
                remaining_output.remove(output_idx)
                break
    의미완화매칭들 = []
    문자유사매칭들 = []
    문자유사관찰들 = []
    if not 보수티어만 and sheet_name in 의미완화적용시트:
        의미완화매칭들 = _의미완화매칭하기(
            golden_rows,
            output_rows,
            remaining_golden,
            remaining_output,
            sheet_name,
        )
        if sheet_name == "02_지역여건":
            소비된골든 = {id(match.골든) for match in 의미완화매칭들}
            소비된출력 = {id(match.출력) for match in 의미완화매칭들}
            문자유사골든잔여 = {
                idx
                for idx in remaining_golden
                if id(golden_rows[idx]) not in 소비된골든
            }
            문자유사출력잔여 = {
                idx
                for idx in remaining_output
                if id(output_rows[idx]) not in 소비된출력
            }
            문자유사매칭들, 문자유사관찰들 = _문자유사매칭하기(
                golden_rows,
                output_rows,
                문자유사골든잔여,
                문자유사출력잔여,
            )
    return (
        matches,
        remaining_golden,
        remaining_output,
        의미완화매칭들,
        문자유사매칭들,
        문자유사관찰들,
    )


def _상대오차일치(left: Any, right: Any) -> bool:
    left_num = to_float(left)
    right_num = to_float(right)
    if left_num is None or right_num is None:
        return dedup_key_text(left) == dedup_key_text(right)
    if left_num == right_num:
        return True
    scale = max(abs(left_num), abs(right_num), 1.0)
    return abs(left_num - right_num) / scale <= 0.005


def _스케일동치배율(left: Any, right: Any) -> int | None:
    left_num = to_float(left)
    right_num = to_float(right)
    if left_num in (None, 0) or right_num in (None, 0):
        return None
    ratio = max(abs(left_num), abs(right_num)) / min(abs(left_num), abs(right_num))
    for multiplier in (10**3, 10**6):
        if abs(ratio - multiplier) / multiplier <= 0.005:
            return multiplier
    return None


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


def 값비교(
    sheet_name: str, matches: list[매칭]
) -> tuple[값집계, list[dict[str, Any]], dict[int, 값집계], list[dict[str, Any]]]:
    fields = 값필드.get(sheet_name, [])
    if sheet_name == "00_문서메타":
        fields = [field for field in 계약헤더(sheet_name) if field != "지자체명"]
    total = 값집계()
    by_row: dict[int, 값집계] = {}
    disagreements: list[dict[str, Any]] = []
    scale_equivalents: list[dict[str, Any]] = []
    for match in matches:
        row_stats = 값집계()
        for field_name in fields:
            golden_value = match.골든.값.get(field_name)
            output_value = match.출력.값.get(field_name)
            golden_has = 값있음(golden_value)
            output_has = 값있음(output_value)
            if not golden_has and not output_has:
                continue
            if golden_has and not output_has:
                total.골든만 += 1
                row_stats.골든만 += 1
                continue
            if output_has and not golden_has:
                total.출력만 += 1
                row_stats.출력만 += 1
                continue
            total.전체 += 1
            row_stats.전체 += 1
            if _필드일치(field_name, golden_value, output_value):
                total.일치 += 1
                row_stats.일치 += 1
            else:
                disagreements.append({"시트": sheet_name, "키": match.키, "필드": field_name, "골든값": golden_value, "출력값": output_value, "매칭방식": match.방식})
                if sheet_name == "06_감축목표" and field_name in {
                    "기준배출량", "배출전망", "목표감축량", "목표배출량"
                }:
                    multiplier = _스케일동치배율(golden_value, output_value)
                    if multiplier is not None:
                        scale_equivalents.append({
                            "시트": sheet_name,
                            "키": match.키,
                            "필드": field_name,
                            "골든값": golden_value,
                            "출력값": output_value,
                            "배율": multiplier,
                        })
        by_row[match.골든.번호] = row_stats
    return total, disagreements, by_row, scale_equivalents


def 셀정확도집계(sheet_name: str, golden_rows: list, matches: list[매칭]) -> tuple[int, int]:
    """모든 의미 셀을 분모로 삼고 매칭 행의 정확한 셀을 센다."""
    excluded = {"지자체명", "출처페이지", "데이터상태", "derivation_type", "근거ID"}
    fields = [
        field_name
        for field_name in 계약헤더(sheet_name)
        if field_name not in excluded
    ]
    matched_by_row = {match.골든.번호: match for match in matches}
    expected = 0
    correct = 0
    for golden_row in golden_rows:
        match = matched_by_row.get(golden_row.번호)
        for field_name in fields:
            golden_value = golden_row.값.get(field_name)
            if not 값있음(golden_value):
                continue
            expected += 1
            if (
                match is not None
                and 값있음(match.출력.값.get(field_name))
                and _필드일치(
                    field_name,
                    golden_value,
                    match.출력.값.get(field_name),
                )
            ):
                correct += 1
    return correct, expected


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


def 의미완화포함출처직렬화(stats: dict[str, 출처집계], 의미완화매칭들: list[매칭]) -> dict[str, dict[str, Any]]:
    serialized = 출처직렬화(stats)
    additions = {"텍스트 유래": 0, "시각 유래": 0}
    for match in 의미완화매칭들:
        for source_key in _출처키들(match.골든.출처유형):
            if source_key in additions:
                additions[source_key] += 1
    result: dict[str, dict[str, Any]] = {}
    for source_key, added in additions.items():
        row = dict(serialized[source_key])
        row["매칭수"] += added
        row["리콜"] = round(row["매칭수"] / row["골든행수"], 4) if row["골든행수"] else None
        result[source_key] = row
    return result


def 문자유사포함출처직렬화(
    stats: dict[str, 출처집계], 의미완화매칭들: list[매칭], 문자유사매칭들: list[매칭]
) -> dict[str, dict[str, Any]]:
    serialized = 의미완화포함출처직렬화(stats, 의미완화매칭들)
    row = dict(serialized["시각 유래"])
    row["매칭수"] += sum(
        1 for match in 문자유사매칭들 if match.골든.출처유형 in 시각유래
    )
    row["리콜"] = round(row["매칭수"] / row["골든행수"], 4) if row["골든행수"] else None
    return {"시각 유래": row}


def 시트점수(sheet_name: str, golden: 시트자료, output: 시트자료) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[매칭], dict[int, 값집계], list[매칭], list[매칭], list[매칭], list[dict[str, Any]]]:
    if golden.상태 != "정상":
        return ({"상태": golden.상태, "골든행수": 0, "출력행수": len(output.행들), "매칭수": 0, "엄격매칭수": 0, "완화매칭수": 0, "리콜": None, "정밀도": None, "셀기대수": 0, "셀일치수": 0, "값비교수": 0, "값일치수": 0, "값일치율": None, "값일치율(스케일동치 포함)": None, "스케일동치쌍수": 0, "골든만있는값": 0, "출력만있는값": 0, "의미완화매칭수": 0, "문자유사매칭수": 0}, [], [], [], [], {}, [], [], [], [])
    if sheet_name == "00_문서메타":
        if golden.행들 and output.행들:
            matches = [매칭(golden.행들[0], output.행들[0], "엄격", "문서메타")]
            remaining_golden: set[int] = set(range(1, len(golden.행들)))
            remaining_output: set[int] = set(range(1, len(output.행들)))
            의미완화매칭들: list[매칭] = []
            문자유사매칭들: list[매칭] = []
            문자유사관찰들: list[매칭] = []
        else:
            matches = []
            remaining_golden = set(range(len(golden.행들)))
            remaining_output = set(range(len(output.행들)))
            의미완화매칭들 = []
            문자유사매칭들 = []
            문자유사관찰들 = []
    else:
        (
            matches,
            remaining_golden,
            remaining_output,
            의미완화매칭들,
            문자유사매칭들,
            문자유사관찰들,
        ) = 매칭하기(golden.행들, output.행들, sheet_name)
    value_stats, disagreements, row_values, scale_equivalents = 값비교(sheet_name, matches)
    correct_cells, expected_cells = 셀정확도집계(sheet_name, golden.행들, matches)
    recall = round(len(matches) / len(golden.행들), 4) if golden.행들 else None
    precision = round(len(matches) / len(output.행들), 4) if output.행들 else None
    missing_golden = [{"시트": sheet_name, "행번호": golden.행들[idx].번호, "키": 키(golden.행들[idx], sheet_name, False) or 키(golden.행들[idx], sheet_name, True) or "", "골든_출처유형": golden.행들[idx].출처유형, "골든_출처페이지": golden.행들[idx].출처페이지} for idx in sorted(remaining_golden)]
    missing_output = [{"시트": sheet_name, "행번호": output.행들[idx].번호, "키": 키(output.행들[idx], sheet_name, False) or 키(output.행들[idx], sheet_name, True) or ""} for idx in sorted(remaining_output)]
    inclusive_value_rate = (
        round((value_stats.일치 + len(scale_equivalents)) / value_stats.전체, 4)
        if value_stats.전체 else None
    )
    summary = {"상태": "정상", "골든행수": len(golden.행들), "출력행수": len(output.행들), "매칭수": len(matches), "엄격매칭수": sum(1 for match in matches if match.방식 == "엄격"), "완화매칭수": sum(1 for match in matches if match.방식 == "완화"), "리콜": recall, "정밀도": precision, "셀기대수": expected_cells, "셀일치수": correct_cells, "값비교수": value_stats.전체, "값일치수": value_stats.일치, "값일치율": value_stats.비율(), "값일치율(스케일동치 포함)": inclusive_value_rate, "스케일동치쌍수": len(scale_equivalents), "골든만있는값": value_stats.골든만, "출력만있는값": value_stats.출력만, "의미완화매칭수": len(의미완화매칭들), "문자유사매칭수": len(문자유사매칭들)}
    return summary, missing_golden, missing_output, disagreements, matches, row_values, 의미완화매칭들, 문자유사매칭들, 문자유사관찰들, scale_equivalents


def 없는골든요약(output: 시트자료) -> dict[str, Any]:
    return {"상태": "골든 없음", "골든행수": 0, "출력행수": len(output.행들), "매칭수": 0, "엄격매칭수": 0, "완화매칭수": 0, "리콜": None, "정밀도": None, "셀기대수": 0, "셀일치수": 0, "값비교수": 0, "값일치수": 0, "값일치율": None, "값일치율(스케일동치 포함)": None, "스케일동치쌍수": 0, "골든만있는값": 0, "출력만있는값": 0, "의미완화매칭수": 0, "문자유사매칭수": 0}
