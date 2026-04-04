"""
구조화된 데이터를 엑셀 파일로 작성하는 유틸리티

탄소_출력파일.xlsx 와 동일한 5-시트 구조를 생성합니다.
"""

from pathlib import Path

import openpyxl
from openpyxl.styles import (
    Alignment, Font, PatternFill, Border, Side, numbers
)
from openpyxl.utils import get_column_letter

import config


# ──────────────────────────────────────────────
#  색상 팔레트
# ──────────────────────────────────────────────
COLOR_HEADER_BG   = "2E75B6"   # 헤더 배경 (진파랑)
COLOR_HEADER_FONT = "FFFFFF"   # 헤더 글꼴 (흰색)
COLOR_YEAR_BG     = "D9E1F2"   # 연도 행 배경 (연파랑)
COLOR_ALT_ROW     = "F2F2F2"   # 짝수 행 배경 (연회색)
COLOR_NOTE_BG     = "FFF2CC"   # 가이드라인 안내 셀 배경 (연노랑)


def _thin_border() -> Border:
    side = Side(style="thin", color="AAAAAA")
    return Border(left=side, right=side, top=side, bottom=side)


def _header_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_HEADER_BG)


def _year_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_YEAR_BG)


def _alt_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_ALT_ROW)


def _note_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_NOTE_BG)


def _apply_header_row(ws, headers: list, row: int = 1):
    """헤더 행 스타일 적용"""
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=header)
        cell.font = Font(bold=True, color=COLOR_HEADER_FONT, name="맑은 고딕", size=10)
        cell.fill = _header_fill()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _thin_border()


def _set_column_widths(ws, widths: dict[int, float]):
    """열 너비 설정 (열 인덱스 → 너비)"""
    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _auto_column_widths(ws):
    """내용 기준으로 열 너비 자동 설정 (최대 40)"""
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                cell_len = len(str(cell.value)) if cell.value is not None else 0
                max_len = max(max_len, cell_len)
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len * 1.4 + 2, 40)


def _freeze_panes(ws, cell: str = "E2"):
    """틀 고정"""
    ws.freeze_panes = cell


# ──────────────────────────────────────────────
#  시트별 작성 함수
# ──────────────────────────────────────────────

def _write_vehicle_sheet(wb: openpyxl.Workbook, data: list[dict]):
    """
    시트: 용도별 자동차(현황)
    data 항목 키: 지자체명, 용도, 차종, 대수, 주행거리
    """
    ws = wb["용도별 자동차(현황)"]
    headers = config.EXCEL_HEADERS["용도별 자동차(현황)"]
    _apply_header_row(ws, headers)
    _freeze_panes(ws, "A2")

    for row_idx, row_data in enumerate(data, start=2):
        values = [
            row_data.get("지자체명", ""),
            row_data.get("용도", ""),
            row_data.get("차종", ""),
            row_data.get("대수", None),
            row_data.get("주행거리", None),
        ]
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()

    _set_column_widths(ws, {1: 20, 2: 12, 3: 12, 4: 14, 5: 22})


def _write_energy_sheet(wb: openpyxl.Workbook, data: list[dict]):
    """
    시트: 용도별 에너지(현황)
    data 항목 키: 지자체명, 용도, 석유_에너지유, 석유_LPG, 석유_비에너지유,
                  가스, 전력, 열, 신재생
    """
    ws = wb["용도별 에너지(현황)"]
    headers = config.EXCEL_HEADERS["용도별 에너지(현황)"]
    _apply_header_row(ws, headers)
    _freeze_panes(ws, "C2")

    key_map = [
        "지자체명", "용도",
        "석유_에너지유", "석유_LPG", "석유_비에너지유",
        "가스", "전력", "열", "신재생",
    ]

    for row_idx, row_data in enumerate(data, start=2):
        for col_idx, key in enumerate(key_map, start=1):
            val = row_data.get(key, None)
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()

    _set_column_widths(ws, {1: 20, 2: 12, **{i: 14 for i in range(3, 10)}})


def _write_ghg_sheet(wb: openpyxl.Workbook, data: list[dict]):
    """
    시트: 온실가스(현황전망목표)
    data 항목 키: 지자체명, 배출유형, 종류, 부문, {연도: 값, ...}
    """
    ws = wb["온실가스(현황전망목표)"]
    headers = config.EXCEL_HEADERS["온실가스(현황전망목표)"]
    _apply_header_row(ws, headers)
    _freeze_panes(ws, "E2")

    # 연도 헤더 셀 스타일
    for col_idx in range(5, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _year_fill()
        cell.font = Font(bold=True, name="맑은 고딕", size=9)

    for row_idx, row_data in enumerate(data, start=2):
        base_vals = [
            row_data.get("지자체명", ""),
            row_data.get("배출유형", ""),
            row_data.get("종류", ""),
            row_data.get("부문", ""),
        ]
        for col_idx, val in enumerate(base_vals, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()

        for year_offset, year in enumerate(config.YEARS):
            col_idx = 5 + year_offset
            val = row_data.get("연도별", {}).get(str(year), None)
            # 중첩 dict 방어: {"가정": 13043, ...} → 합산 또는 None
            if isinstance(val, dict):
                try:
                    val = float(sum(v for v in val.values() if isinstance(v, (int, float))))
                except Exception:
                    val = None
            if val is not None:
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    val = None
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="right", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()
            if isinstance(val, (int, float)):
                cell.number_format = "#,##0.00"

    _set_column_widths(ws, {1: 20, 2: 12, 3: 8, 4: 10, **{i: 10 for i in range(5, 5 + len(config.YEARS))}})


def _write_strategy_sheet(wb: openpyxl.Workbook, data: list[dict]):
    """
    시트: 감축전략(계획실적)
    data 항목 키: 지자체명, 배출유형, 감축전략_부문, 감축사업명, 감축사업명_세부,
                  구분, 성과지표, 종류, 연도별: {연도: 값}
    """
    ws = wb["감축전략(계획실적)"]
    headers = config.EXCEL_HEADERS["감축전략(계획실적)"]
    _apply_header_row(ws, headers)
    _freeze_panes(ws, "I2")

    for col_idx in range(9, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _year_fill()
        cell.font = Font(bold=True, name="맑은 고딕", size=9)

    base_keys = [
        "지자체명", "배출유형", "감축전략_부문",
        "감축사업명", "감축사업명_세부", "구분", "성과지표", "종류",
    ]

    for row_idx, row_data in enumerate(data, start=2):
        for col_idx, key in enumerate(base_keys, start=1):
            val = row_data.get(key, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()

        for year_offset, year in enumerate(config.YEARS):
            col_idx = 9 + year_offset
            val = row_data.get("연도별", {}).get(str(year), None)
            # 중첩 dict 방어: {"계획": 100, ...} → 합산 또는 None
            if isinstance(val, dict):
                try:
                    val = float(sum(v for v in val.values() if isinstance(v, (int, float))))
                except Exception:
                    val = None
            if val is not None and val != "비예산":
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    pass
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="right", vertical="center")
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()
            if isinstance(val, (int, float)):
                cell.number_format = "#,##0.00"

    _set_column_widths(ws, {
        1: 20, 2: 10, 3: 10, 4: 20, 5: 25, 6: 8, 7: 20, 8: 14,
        **{i: 10 for i in range(9, 9 + len(config.YEARS))}
    })


def _write_summary_sheet(wb: openpyxl.Workbook, data: list[dict]):
    """
    시트: 지자체별 요약카드
    data 항목 키: 지자체명, 항목, 내용, 근거
    """
    ws = wb["지자체별 요약카드"]
    headers = config.EXCEL_HEADERS["지자체별 요약카드"]
    _apply_header_row(ws, headers)
    _freeze_panes(ws, "B2")

    for row_idx, row_data in enumerate(data, start=2):
        values = [
            row_data.get("지자체명", ""),
            row_data.get("항목", ""),
            row_data.get("내용", ""),
            row_data.get("근거", ""),
        ]
        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = _thin_border()
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            if row_idx % 2 == 0:
                cell.fill = _alt_fill()
        ws.row_dimensions[row_idx].height = 40

    _set_column_widths(ws, {1: 20, 2: 22, 3: 60, 4: 20})


# ──────────────────────────────────────────────
#  메인 작성 함수
# ──────────────────────────────────────────────

def write_excel(extracted_data: dict, output_path: str | Path) -> Path:
    """
    추출된 데이터 딕셔너리를 받아 엑셀 파일을 생성합니다.

    Args:
        extracted_data: {
            "vehicle": [...],       # 용도별 자동차
            "energy": [...],        # 용도별 에너지
            "ghg": [...],           # 온실가스 현황전망목표
            "strategy": [...],      # 감축전략 계획실적
            "summary": [...],       # 지자체별 요약카드
        }
        output_path: 저장할 엑셀 파일 경로

    Returns:
        저장된 파일의 Path 객체
    """
    output_path = Path(output_path)

    wb = openpyxl.Workbook()
    # 기본 시트 제거 후 5개 시트 생성
    del wb[wb.sheetnames[0]]
    for sheet_name in config.EXCEL_HEADERS:
        wb.create_sheet(sheet_name)

    _write_vehicle_sheet(wb, extracted_data.get("vehicle", []))
    _write_energy_sheet(wb, extracted_data.get("energy", []))
    _write_ghg_sheet(wb, extracted_data.get("ghg", []))
    _write_strategy_sheet(wb, extracted_data.get("strategy", []))
    _write_summary_sheet(wb, extracted_data.get("summary", []))

    wb.save(str(output_path))
    return output_path
