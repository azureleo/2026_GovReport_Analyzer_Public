from __future__ import annotations

from pathlib import Path

import openpyxl

import config
from scripts.golden_score_contract import 계약헤더, 레거시계약헤더
from scripts.score_against_golden import score_workbooks
from tests.test_score_against_golden import _계약열, _워크북, _점수


def test_근거_id는_골든셋_내용_계약에서_제외한다() -> None:
    assert "근거ID" in config.EXCEL_HEADERS["03_배출현황_지역"]
    assert "근거ID" not in 계약헤더("03_배출현황_지역")


def test_확장전_골든계약은_레거시로_평가한다(tmp_path: Path) -> None:
    output = _워크북(
        tmp_path / "출력.xlsx",
        {"06_감축목표": [{"목표수준": "총괄", "목표범위": "관리권한", "부문": "합계", "기준연도": 2018, "목표연도": 2030, "목표배출량": 80}]},
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "06_감축목표"
    headers = [
        *레거시계약헤더("06_감축목표"),
        "골든_출처유형", "골든_출처페이지", "골든_채점제외", "골든_비고",
    ]
    ws.append(headers)
    row = {
        "목표수준": "총괄", "목표범위": "관리권한", "부문": "합계",
        "기준연도": 2018, "목표연도": 2030, "목표배출량": 80,
        "골든_출처유형": "텍스트표", "골든_출처페이지": "1",
    }
    ws.append([row.get(header) for header in headers])
    golden = tmp_path / "레거시_골든.xlsx"
    wb.save(golden)

    result = score_workbooks(output, golden, report_dir=tmp_path / "리포트", label="레거시")

    assert result["형식오류"] == []
    assert result["시트별"]["06_감축목표"]["매칭수"] == 1
    assert any("레거시 계약 열" in warning for warning in result["형식경고"])


def test_값일치율은_양쪽_값이_있는_쌍만_분모로_쓴다(tmp_path: Path) -> None:
    result = _점수(
        tmp_path,
        {
            "03_배출현황_지역": [
                {"배출유형": "직접배출", "부문": "건물", "세부부문": "전기", "연도": 2020, "배출량": "", "단위": "톤"},
            ]
        },
        {
            "03_배출현황_지역": [
                {"배출유형": "직접배출", "부문": "건물", "세부부문": "전기", "연도": 2020, "배출량": 100, "단위": "톤", "골든_출처유형": "텍스트표", "골든_출처페이지": "1"},
            ]
        },
    )

    sheet = result["시트별"]["03_배출현황_지역"]
    assert sheet["값일치율"] == 1.0
    assert sheet["골든만있는값"] == 1
    assert result["출처유형별_전체"]["텍스트표"]["값비교수"] == 1
    body = Path(result["리포트"]["md"]).read_text(encoding="utf-8")
    assert "값일치율 정의: 양쪽 값이 모두 존재하는 비교 쌍 중 일치한 값의 비율" in body


def test_골든시트_계약열_불일치_분기를_보고한다(tmp_path: Path) -> None:
    output = _워크북(tmp_path / "출력.xlsx", {"03_배출현황_지역": []})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "03_배출현황_지역"
    ws.append(["엉뚱한열", "부문", "세부부문"])
    golden = tmp_path / "계약열_불일치.xlsx"
    wb.save(golden)

    result = score_workbooks(output, golden, report_dir=tmp_path / "리포트", label="계약열", write_json=True)

    assert result["시트별"]["03_배출현황_지역"]["상태"] == "형식 오류"
    assert "계약 컬럼이 명세와 다릅니다" in result["형식오류"][0]


def test_골든시트_골든열_누락_분기를_보고한다(tmp_path: Path) -> None:
    output = _워크북(tmp_path / "출력.xlsx", {"03_배출현황_지역": []})
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "03_배출현황_지역"
    ws.append(_계약열("03_배출현황_지역"))
    golden = tmp_path / "골든열_누락.xlsx"
    wb.save(golden)

    result = score_workbooks(output, golden, report_dir=tmp_path / "리포트", label="골든열", write_json=True)

    assert result["시트별"]["03_배출현황_지역"]["상태"] == "형식 오류"
    assert "골든_출처유형·골든_출처페이지 컬럼이 계약 컬럼 뒤에 필요합니다" in result["형식오류"][0]


def test_골든_출처페이지_공란은_경고로만_수집한다(tmp_path: Path) -> None:
    result = _점수(
        tmp_path,
        {
            "03_배출현황_지역": [
                {"배출유형": "직접배출", "부문": "건물", "세부부문": "전기", "연도": 2020, "배출량": 10, "단위": "톤"},
            ]
        },
        {
            "03_배출현황_지역": [
                {"배출유형": "직접배출", "부문": "건물", "세부부문": "전기", "연도": 2020, "배출량": 10, "단위": "톤", "골든_출처유형": "본문텍스트", "골든_출처페이지": ""},
            ]
        },
    )

    assert result["시트별"]["03_배출현황_지역"]["상태"] == "정상"
    assert result["형식오류"] == []
    assert "03_배출현황_지역 2행: 골든_출처페이지가 비어 있습니다" in result["형식경고"][0]
    body = Path(result["리포트"]["md"]).read_text(encoding="utf-8")
    assert "## 형식 경고" in body
