from __future__ import annotations

from openpyxl import load_workbook

from utils import excel_writer


def test_write_excel_sanitizes_dict_list_control_chars_and_formula_text(tmp_path) -> None:
    # Given: LLM 출력 행에 Excel이 직접 저장하지 못하는 값과 수식처럼 보이는 문자열이 섞이면
    output_path = tmp_path / "sanitize.xlsx"
    data = {
        "plan_overview": [
            {
                "지자체명": "서울특별시",
                "개요유형": {"nested": ["값"]},
                "항목명": "제어\x00문자\x07제거",
                "항목값": "=SUM(A1)",
                "일자": 20260705,
                "이해관계자": ["시민", "기업"],
            }
        ]
    }

    # When: workbook을 저장 후 다시 읽으면
    result_path = excel_writer.write_excel(data, output_path)
    workbook = load_workbook(result_path, data_only=False)
    row = workbook["01_계획개요"][2]

    # Then: 크래시 없이 값만 안전하게 정제되고 수식은 텍스트로 보존된다.
    assert row[1].value == '{"nested": ["값"]}'
    assert row[2].value == "제어문자제거"
    assert row[3].value == "=SUM(A1)"
    assert row[3].data_type == "s"
    assert row[4].value == 20260705
    assert row[5].value == '["시민", "기업"]'
