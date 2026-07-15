import unittest

from agents.extractor_agent import BatchRecord
from agents.organizer_agent import _validate_final_data


class ValidationReportTests(unittest.TestCase):
    def test_cross_sheet_base_emissions_mismatch_is_reported(self):
        # Given: 감축목표 기준배출량과 배출현황 합계가 5% 넘게 다르면
        cleaned = {
            "emissions_regional": [
                {"지자체명": "서울", "부문": "합계", "연도": 2018, "배출량": 100.0, "단위": "천톤CO2eq"},
            ],
            "reduction_targets": [
                {"지자체명": "서울", "목표수준": "총괄", "기준연도": 2018, "기준배출량": 130.0, "목표연도": 2030, "목표감축량": 10.0},
                {"지자체명": "서울", "목표수준": "총괄", "기준연도": 2018, "기준배출량": 130.0, "목표연도": 2050, "목표감축량": 20.0},
            ],
            "quantitative_reductions": [],
            "financial_plan": [],
        }

        # When: 검증 리포트를 계산하면
        issues = _validate_final_data(cleaned, "서울")

        # Then: 교차시트 경고가 포함된다.
        self.assertTrue(any(issue["항목"].startswith("기준배출량≠배출현황") for issue in issues))

    def test_ledger_failures_are_reported(self):
        # Given: 원장에 파싱 실패가 있으면
        cleaned = {"emissions_regional": []}
        ledger = [BatchRecord("emissions_regional", [12, 13], "parse_fail", 0, "JSON 파싱 실패")]

        # When: 원장을 포함해 검증하면
        issues = _validate_final_data(cleaned, "서울", ledger=ledger, raw_counts={"emissions_regional": 0})

        # Then: 실패 배치와 빈 시트 원인이 함께 보인다.
        self.assertTrue(any(issue["항목"] == "원장 파싱실패" for issue in issues))
        self.assertTrue(any(issue["항목"] == "빈 시트 원인" and issue["문제내용"] == "호출·파싱 실패" for issue in issues))


if __name__ == "__main__":
    unittest.main()
