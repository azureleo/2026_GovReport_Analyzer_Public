import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from agents.extractor_agent import BatchParseError, BatchRecord, ExtractorAgent
from utils.pdf_reader import PageContent
from utils import llm_client
from utils.run_state import RunState


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

    def test_bare_empty_object_is_not_accepted_as_successful_sheet_schema(self):
        agent = ExtractorAgent()
        batch_text = "=== 페이지 12 ===\n배출량 표"

        with patch.object(llm_client, "call_text_json", return_value=({}, True)):
            rows = agent._extract_sheet("emissions_regional", batch_text, "서울특별시")

        self.assertEqual(rows, [])
        self.assertEqual(agent.ledger[0].status, "parse_fail")
        self.assertIn("시트 키 누락", agent.ledger[0].error)

    def test_call_fail_batch_is_collected_and_other_rows_are_kept(self):
        # Given: 두 라우팅 배치 중 하나만 호출 실패하는 추출기
        agent = ExtractorAgent()
        pages = [
            PageContent(page_number=1, text="온실가스 배출량 부문별 현황", tables=[], images=[]),
            PageContent(page_number=2, text="온실가스 배출량 부문별 현황", tables=[], images=[]),
        ]

        def fake_extract(sheet_key, batch_text, municipality, guideline_prompt="", **kwargs):
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

    def test_timeout_batch_is_split_and_partial_results_are_preserved(self):
        # Given: 4페이지 호출만 타임아웃되고 2페이지 호출은 성공하는 추출기
        agent = ExtractorAgent()
        pages = [
            PageContent(page_number=number, text=f"배출량 표 {number}", tables=[], images=[])
            for number in range(1, 5)
        ]

        def fake_extract(sheet_key, batch_text, municipality, guideline_prompt="", **kwargs):
            page_nums = [int(value) for value in re.findall(r"페이지 (\d+)", batch_text)]
            if len(page_nums) > 2:
                raise llm_client.LLMTimeoutError("timeout")
            agent._record_batch(sheet_key, page_nums, "ok", 1)
            return [{"지자체명": municipality, "부문": "합계", "출처페이지": page_nums}]

        with patch.object(config, "EXTRACTION_SPLIT_ON_TIMEOUT", True), \
             patch.object(config, "EXTRACTION_TIMEOUT_MIN_BATCH_PAGES", 2), \
             patch.object(agent, "_extract_sheet", side_effect=fake_extract):
            rows = agent.extract_sheet_pages(
                "emissions_regional",
                pages,
                "서울특별시",
                {"emissions_regional": ""},
                batch_size=4,
            )

        # Then: p1~2, p3~4로 나눠 두 결과를 모두 보존하고 실패 원장은 남기지 않는다.
        self.assertEqual(len(rows), 2)
        self.assertEqual(agent.timeout_splits, 1)
        self.assertEqual([record.status for record in agent.ledger], ["ok", "ok"])
        self.assertEqual([record.page_nums for record in agent.ledger], [[1, 2], [3, 4]])

    def test_parse_failure_batch_is_split_and_recovered(self):
        agent = ExtractorAgent()
        pages = [
            PageContent(page_number=number, text=f"배출량 표 {number}", tables=[], images=[])
            for number in range(1, 5)
        ]

        def fake_extract(sheet_key, batch_text, municipality, guideline_prompt="", **kwargs):
            page_nums = [int(value) for value in re.findall(r"페이지 (\d+)", batch_text)]
            if len(page_nums) > 2:
                raise BatchParseError("truncated json")
            agent._record_batch(sheet_key, page_nums, "ok", 1)
            return [{"지자체명": municipality, "부문": "합계", "출처페이지": page_nums}]

        with patch.object(config, "EXTRACTION_SPLIT_ON_FAILURE", True), \
             patch.object(config, "EXTRACTION_TIMEOUT_MIN_BATCH_PAGES", 2), \
             patch.object(agent, "_extract_sheet", side_effect=fake_extract):
            rows = agent.extract_sheet_pages(
                "emissions_regional",
                pages,
                "서울특별시",
                {"emissions_regional": ""},
                batch_size=4,
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual(agent.failure_splits, 1)
        self.assertEqual([record.page_nums for record in agent.ledger], [[1, 2], [3, 4]])

    def test_successful_extractor_batches_resume_without_llm_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "source.pdf"
            source.write_bytes(b"input")
            pages = [
                PageContent(page_number=number, text="배출량 표", tables=[], images=[])
                for number in (1, 2)
            ]

            def new_state(resume=False):
                return RunState.create(
                    input_path=source,
                    guideline_path=None,
                    extraction_prompts={"emissions_regional": "prompt"},
                    execution_info={"text_backend": "test", "text_model": "model"},
                    output_path=tmp_path / "result.xlsx",
                    resume=resume,
                )

            def fake_extract(sheet_key, batch_text, municipality, guideline_prompt="", **kwargs):
                page_nums = [int(value) for value in re.findall(r"페이지 (\d+)", batch_text)]
                return [{"지자체명": municipality, "부문": "합계", "출처페이지": page_nums}]

            with patch.object(config, "RUN_STATE_DIR", str(tmp_path / "runs")):
                first = ExtractorAgent(run_state=new_state())
                with patch.object(first, "_extract_sheet", side_effect=fake_extract):
                    expected = first.extract_sheet_pages(
                        "emissions_regional", pages, "서울특별시", {}, batch_size=1,
                    )

                resumed = ExtractorAgent(run_state=new_state(resume=True))
                with patch.object(
                    resumed,
                    "_extract_sheet",
                    side_effect=AssertionError("LLM 호출이 발생하면 안 됨"),
                ):
                    actual = resumed.extract_sheet_pages(
                        "emissions_regional", pages, "서울특별시", {}, batch_size=1,
                    )

            self.assertEqual(actual, expected)
            self.assertEqual(resumed.resumed_batches, 2)

    def test_timeout_recovery_budget_stops_recursive_split(self):
        agent = ExtractorAgent()
        pages = [
            PageContent(page_number=number, text="배출량 표", tables=[], images=[])
            for number in range(1, 5)
        ]
        task = {
            "sheet_key": "emissions_regional",
            "pages": pages,
            "batch_text": "\n".join(f"=== 페이지 {number} ===" for number in range(1, 5)),
            "page_nums": [1, 2, 3, 4],
            "page_range": "p1~p4",
            "batch_num": 1,
            "batch_total": 1,
        }

        with patch.object(config, "EXTRACTION_SPLIT_ON_TIMEOUT", True), \
             patch.object(config, "EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS", 10), \
             patch.object(agent, "_extract_sheet", side_effect=llm_client.LLMTimeoutError("timeout")), \
             patch("agents.extractor_agent.time.monotonic", side_effect=[0.0, 11.0]):
            with self.assertRaises(llm_client.LLMTimeoutError):
                agent._run_sheet_task(task, "서울특별시")

        self.assertEqual(agent.timeout_splits, 0)

    def test_oversized_single_page_is_split_before_first_llm_call(self):
        agent = ExtractorAgent()
        page = PageContent(
            page_number=75,
            text="\n".join(f"지역여건 지표 {index} 값 {index * 10}" for index in range(500)),
            tables=[],
            images=[],
        )
        call_sizes = []

        def fake_extract(sheet_key, batch_text, municipality, guideline_prompt="", **kwargs):
            call_sizes.append(len(batch_text))
            return [{
                "지자체명": municipality,
                "지표명": f"조각 {len(call_sizes)}",
                "연도": 2021,
                "값": len(call_sizes),
                "출처페이지": 75,
            }]

        with patch.object(config, "EXTRACTION_MAX_BATCH_CHARS", 1000), \
             patch.object(config, "EXTRACTION_RECOVERY_MAX_BATCH_CHARS", 1000), \
             patch.object(agent, "_extract_sheet", side_effect=fake_extract):
            rows = agent.extract_sheet_pages(
                "regional_conditions", [page], "서울특별시", {}, batch_size=1,
            )

        self.assertGreater(len(call_sizes), 1)
        self.assertTrue(all(size <= 1000 for size in call_sizes))
        self.assertEqual(agent.preflight_splits, 1)
        self.assertEqual(len(rows), len(call_sizes))

    def test_large_table_recovery_uses_header_repeated_object_chunks(self):
        agent = ExtractorAgent()
        body = "".join(
            f"<tr><td>{year}</td><td>{year * 10}</td></tr>"
            for year in range(2000, 2045)
        )
        page = PageContent(
            page_number=81,
            text="표 2-12 연도별 에너지 소비량",
            tables=[f"<table><tr><th>연도</th><th>전력</th></tr>{body}</table>"],
            images=[],
        )
        task = {
            "sheet_key": "regional_conditions",
            "pages": [page],
            "batch_text": "x" * 5000,
            "page_nums": [81],
            "page_range": "p81",
            "batch_num": 1,
            "batch_total": 1,
        }

        with patch.object(config, "EXTRACTION_TABLE_ROWS_PER_BATCH", 10):
            children = agent._recovery_children(task, 1000)

        table_children = [child for child in children if child.get("object_id") == "p81_table_1"]
        self.assertGreater(len(table_children), 1)
        self.assertTrue(all("| 연도 | 전력 |" in child["batch_text"] for child in table_children))


if __name__ == "__main__":
    unittest.main()
