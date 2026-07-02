import os
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import config
from agents.organizer_agent import _normalize_provenance_pages, _merge_provenance
from utils import excel_writer


class ProvenanceTests(unittest.TestCase):
    def test_normalizes_page_values_to_canonical_string(self):
        # Given / When / Then: 숫자·배열·문자열 출처가 같은 문자열 규칙으로 정규화된다.
        self.assertEqual(_normalize_provenance_pages(12), "12")
        self.assertEqual(_normalize_provenance_pages([13, "12", "p12~14"]), "12,13,14")
        self.assertEqual(_normalize_provenance_pages(None), "")

    def test_merges_provenance_page_sets(self):
        # Given / When: 두 행의 출처페이지를 병합하면
        merged = _merge_provenance("12,13", [13, 14])

        # Then: 중복 없는 합집합 문자열이 된다.
        self.assertEqual(merged, "12,13,14")

    def test_excel_header_can_disable_provenance_column(self):
        # Given: provenance가 포함된 데이터와 비활성 플래그
        previous = getattr(config, "PROVENANCE_ENABLED", True)
        config.PROVENANCE_ENABLED = False
        data = {
            "document_meta": [
                {"지자체명": "서울특별시", "계획명": "계획", "출처페이지": "12"}
            ]
        }

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "out.xlsx"
                excel_writer.write_excel(data, path)
                wb = load_workbook(path)
                headers = [cell.value for cell in wb["00_문서메타"][1]]
        finally:
            config.PROVENANCE_ENABLED = previous

        # Then: 출처페이지 컬럼이 출력 스키마에서 제거된다.
        self.assertNotIn("출처페이지", headers)


if __name__ == "__main__":
    unittest.main()
