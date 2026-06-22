import unittest

import config
from agents.organizer_agent import _apply_context_summary_evidence
from agents.supervisor import _build_summary_context


class SummaryContextTests(unittest.TestCase):
    def test_selects_goal_page_beyond_front_matter(self):
        full_text = "\n".join(
            [
                "[페이지 1]\n목차\n" + ("서론 " * 400),
                "[페이지 25]\n2030·2033년 온실가스 감축목표 제시\n"
                "(2005년 대비 2033년 50% 감축, 2018년 대비 2030년 30% 감축)\n",
                "[페이지 182]\n표 5-1 서울시 중장기 감축률\n2050 탄소중립 경로\n",
            ]
        )

        context = _build_summary_context(full_text)

        self.assertIn("[페이지 25]", context)
        self.assertIn("2030년 30% 감축", context)
        self.assertIn("2033년 50% 감축", context)
        self.assertIn("[페이지 182]", context)

    def test_summary_targets_are_filled_from_context_evidence(self):
        cleaned = {
            "summary": [
                {"지자체명": "서울특별시", "항목": item, "내용": "", "근거": ""}
                for item in config.SUMMARY_ITEMS
            ]
        }
        context = (
            "[페이지 25]\n2030·2033년 온실가스 감축목표 제시\n"
            "(2005년 대비 2033년 50% 감축, 2018년 대비 2030년 30% 감축)\n"
            "[페이지 182]\n2030년 배출목표는 2005년 배출량(52,342천 톤CO₂eq.) 대비 약 40% 줄어든 "
            "31,530천 톤CO₂eq.으로 설정함. 2033년 목표배출량은 2005년 대비 51% 줄어든 25,671천 톤CO₂eq.\n"
            "[페이지 185]\n2018년 대비 2030년 배출량을 37.8% 감축하는 것을 목표로 함\n"
            "2033년 목표배출량은 23,225천 톤CO₂eq.(2018년 대비 49.6% 감축)임"
        )

        _apply_context_summary_evidence(cleaned, context)
        summary = {row["항목"]: row for row in cleaned["summary"]}

        self.assertIn("30% 감축", summary["감축목표(2030)"]["내용"])
        self.assertIn("31,530천 톤CO₂eq", summary["감축목표(2030)"]["내용"])
        self.assertIn("2035년 별도 목표는 확인되지 않는다", summary["감축목표(2035)"]["내용"])
        self.assertIn("2033년 목표", summary["감축목표(2035)"]["내용"])


if __name__ == "__main__":
    unittest.main()
