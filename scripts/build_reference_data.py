#!/usr/bin/env python3
"""가이드라인 HWP 부록3·4를 결정론적으로 CSV 참조 사전으로 변환한다."""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.hwp_reader import extract_hwp  # noqa: E402

GUIDELINE_HWP = ROOT / "docs" / "references" / "가이드라인.hwp"
DATA_DIR = ROOT / "data"
APPENDIX4_CSV = DATA_DIR / "appendix4_projects.csv"
APPENDIX3_CSV = DATA_DIR / "appendix3_reduction_units.csv"

APPENDIX4_BOUNDARIES = [
    ("건물", 1, 143),
    ("농축산", 144, 199),
    ("산업", 200, 239),
    ("수소", 240, 251),
    ("수송", 252, 326),
    ("전환", 327, 375),
    ("폐기물", 376, 438),
    ("흡수원", 439, 467),
    ("이행기반", 468, 507),
]


def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError(f"시작 마커를 찾지 못함: {start_marker}")
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise RuntimeError(f"종료 마커를 찾지 못함: {end_marker}")
    return text[start:end]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _normalise_appendix4_sector(sector: str) -> str:
    text = sector.strip()
    if text.startswith("이행기반"):
        return "이행기반"
    return text


def _parse_appendix4(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if cells and (cells[0] == "연번" or set(cells) <= {"---"}):
            continue
        if len(cells) < 4 or not re.fullmatch(r"\d+", cells[0]):
            print(f"[참조사전] 부록4 행 건너뜀: {line}", file=sys.stderr)
            continue
        rows.append({
            "연번": str(int(cells[0])),
            "부문": _normalise_appendix4_sector(cells[1]),
            "사업명": cells[2],
        })
    return rows


def _parse_appendix3(text: str) -> list[dict[str, str]]:
    grouped: dict[str, dict[str, object]] = {}
    order: list[str] = []
    current_number = ""
    current_sector = ""
    current_project = ""
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) < 6:
            continue
        number, sector, project, monitor, value, unit = cells[:6]
        if number == "번호" or set(cells) <= {"---"}:
            continue
        if number and re.fullmatch(r"\d+-\d+", number):
            current_number = number
            current_sector = sector
            current_project = project
        elif not number and current_number:
            number = current_number
            sector = current_sector
            project = current_project
        else:
            continue
        if not monitor or not value or not unit:
            print(f"[참조사전] 부록3 행 건너뜀: {line}", file=sys.stderr)
            continue
        if number not in grouped:
            order.append(number)
            grouped[number] = {
                "번호": number,
                "부문": sector or current_sector,
                "감축사업명": project or current_project,
                "모니터링인자": [],
                "원단위값": [],
                "단위": [],
            }
        grouped[number]["모니터링인자"].append(monitor)
        grouped[number]["원단위값"].append(value)
        grouped[number]["단위"].append(unit)
    rows: list[dict[str, str]] = []
    for number in order:
        item = grouped[number]
        rows.append({
            "번호": str(item["번호"]),
            "부문": str(item["부문"]),
            "감축사업명": str(item["감축사업명"]),
            "모니터링인자": "; ".join(item["모니터링인자"]),
            "원단위값": "; ".join(item["원단위값"]),
            "단위": "; ".join(item["단위"]),
        })
    return rows


def _assert_appendix4(rows: list[dict[str, str]]) -> None:
    if len(rows) != 507:
        raise AssertionError(f"부록4 행 수 불일치: {len(rows)} != 507")
    by_number = {int(row["연번"]): row for row in rows}
    for sector, start, end in APPENDIX4_BOUNDARIES:
        for number in (start, end):
            if by_number[number]["부문"] != sector:
                raise AssertionError(f"부록4 경계 불일치: {number}번 {by_number[number]['부문']} != {sector}")
        for number in range(start, end + 1):
            if by_number[number]["부문"] != sector:
                raise AssertionError(f"부록4 부문 범위 불일치: {number}번 {by_number[number]['부문']} != {sector}")


def _assert_appendix3(rows: list[dict[str, str]]) -> None:
    if len(rows) != 114:
        raise AssertionError(f"부록3 행 수 불일치: {len(rows)} != 114")
    if rows[0]["번호"] != "1-1" or rows[-1]["번호"] != "8-13":
        raise AssertionError(f"부록3 번호 범위 불일치: {rows[0]['번호']}~{rows[-1]['번호']}")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    content = extract_hwp(GUIDELINE_HWP)
    appendix3_text = _section(content.full_text, "부록3", "부록4")
    appendix4_text = _section(content.full_text, "부록4", "부록5")

    appendix3_rows = _parse_appendix3(appendix3_text)
    appendix4_rows = _parse_appendix4(appendix4_text)
    _assert_appendix3(appendix3_rows)
    _assert_appendix4(appendix4_rows)

    _write_csv(APPENDIX3_CSV, ["번호", "부문", "감축사업명", "모니터링인자", "원단위값", "단위"], appendix3_rows)
    _write_csv(APPENDIX4_CSV, ["연번", "부문", "사업명"], appendix4_rows)
    print(f"[참조사전] {APPENDIX3_CSV.relative_to(ROOT)} {len(appendix3_rows)}행 생성")
    print(f"[참조사전] {APPENDIX4_CSV.relative_to(ROOT)} {len(appendix4_rows)}행 생성")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
