import unittest
from unittest.mock import patch

from agents.extractor_agent import BatchRecord, ExtractorAgent
from utils.pdf_reader import PageContent
from utils import llm_client


class ExtractionLedgerTests(unittest.TestCase):
    def test_extracted_page_nums_excludes_failed_batches(self):
        # Given: 성공/실패가 섞인 추출 원장
        agent = ExtractorAgent()
        agent.ledger = [
            BatchRecord(sheet_key="emissions_regional", page_nums=[1, 2], status="ok", rows=3),
            BatchRecord(sheet_key="emissions_regional", page_nums=[3, 4], status="parse_fail", rows=0),
            BatchRecord(sheet_key="financial_plan", page_nums=[9], status="call_fail", rows=0),
        ]

        # When: 성공 추출 페이지를 계산하면
        extracted = agent.extracted_page_nums

        # Then: 실패 배치 페이지는 제외된다.
        self.assertEqual(extracted["emissions_regional"], {1, 2})
        self.assertNotIn("financial_plan", extracted)

    def test_parse_fail_records_ledger_and_returns_no_rows(self):
        # Given: JSON이 아닌 응답으로 끝나는 추출 호출
        agent = ExtractorAgent()
        batch_text = "=== 페이지 12 ===\n배출량 표"

        with patch.object(llm_client, "call_text_json", return_value=({}, False)):
            # When: 시트 추출을 수행하면
            rows = agent._extract_sheet("emissions_regional", batch_text, "서울특별시")

        # Then: 조용히 유실하지 않고 parse_fail 원장에 남긴다.
        self.assertEqual(rows, [])
        self.assertEqual(len(agent.ledger), 1)
        self.assertEqual(agent.ledger[0].sheet_key, "emissions_regional")
        self.assertEqual(agent.ledger[0].page_nums, [12])
        self.assertEqual(agent.ledger[0].status, "parse_fail")

    def test_call_fail_batch_is_collected_and_other_rows_are_kept(self):
        # Given: 두 라우팅 배치 중 하나만 호출 실패하는 추출기
        agent = ExtractorAgent()
        pages = [
            PageContent(page_number=1, text="온실가스 배출량 부문별 현황", tables=[], images=[]),
            PageContent(page_number=2, text="온실가스 배출량 부문별 현황", tables=[], images=[]),
        ]

        def fake_extract(sheet_key, batch_text, municipality, guideline_prompt=""):
            if "페이지 1" in batch_text:
                raise llm_client.LLMCallError("boom")
            return [{"지자체명": municipality, "부문": "수송", "연도": 2030, "배출량": 1}]

        with patch("agents.extractor_agent._route_pages_by_sheet", return_value={"emissions_regional": pages}), \
             patch("agents.extractor_agent._SHEET_CONFIGS", {"emissions_regional": {"keywords": [], "prompt": ""}}), \
             patch.object(agent, "_extract_municipality_name", return_value="서울특별시"), \
             patch.object(agent, "_extract_sheet", side_effect=fake_extract):
            # When: 배치 크기 1로 추출하면
            raw = agent.extract(pages, "", {"emissions_regional": ""}, batch_size=1)

        # Then: 성공 배치 행은 보존되고 실패 배치는 원장에 남는다.
        self.assertEqual(len(raw["emissions_regional"]), 1)
        failed = [record for record in agent.ledger if record.status == "call_fail"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].page_nums, [1])


if __name__ == "__main__":
    unittest.main()
