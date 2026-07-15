import unittest

import config
from agents.gap_fill_agent import GapFillAgent
from utils.pdf_reader import PageContent


class GapFillTriggerTests(unittest.TestCase):
    def test_emissions_regional_backfills_when_sector_coverage_is_low(self):
        # Given: 배출현황 지역 시트에 부문이 3개뿐이면
        rows = [
            {"부문": "건물", "연도": 2020, "배출량": 1},
            {"부문": "수송", "연도": 2020, "배출량": 2},
            {"부문": "산업", "연도": 2020, "배출량": 3},
        ]

        # When: 커버리지 기준을 평가하면
        needs, reason = GapFillAgent()._needs_backfill("emissions_regional", {"emissions_regional": rows})

        # Then: 표준 부문 커버리지 부족으로 보완한다.
        self.assertTrue(needs)
        self.assertIn("부문 3/", reason)

    def test_emissions_regional_skips_when_sector_and_year_coverage_are_enough(self):
        # Given: 8부문과 3개 연도가 모두 등장하면
        rows = [
            {"부문": sector, "연도": year, "배출량": 1}
            for sector in config.SECTORS
            for year in (2018, 2030, 2050)
        ]

        # When / Then: 추가 보완하지 않는다.
        needs, reason = GapFillAgent()._needs_backfill("emissions_regional", {"emissions_regional": rows})
        self.assertFalse(needs)
        self.assertEqual(reason, "")

    def test_reduction_targets_backfills_when_2050_missing(self):
        # Given: 2030 목표만 존재하면
        cleaned = {"reduction_targets": [{"목표연도": 2030, "목표배출량": 10}]}

        # When: 목표연도 커버리지를 평가하면
        needs, reason = GapFillAgent()._needs_backfill("reduction_targets", cleaned)

        # Then: 2050 누락을 이유로 보완한다.
        self.assertTrue(needs)
        self.assertIn("2050", reason)

    def test_relevant_pages_require_strong_keyword(self):
        # Given: weak 키워드만 있는 페이지와 strong 키워드가 있는 페이지
        weak_only = PageContent(page_number=1, text="재원 사업 연도 계획", tables=[], images=[])
        strong = PageContent(page_number=2, text="재정투자계획 예산액 재원구분", tables=[], images=[])

        # When: 보완 후보 페이지를 고르면
        selected = GapFillAgent()._relevant_pages([weak_only, strong], "financial_plan", 2, 10)

        # Then: strong 키워드가 있는 페이지만 남는다.
        self.assertEqual([page.page_number for page in selected], [2])


if __name__ == "__main__":
    unittest.main()
