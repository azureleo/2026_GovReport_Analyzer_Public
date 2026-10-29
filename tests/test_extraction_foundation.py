import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import fitz
from agents.extractor_agent import ExtractorAgent
from utils.document_objects import (DocumentObject, build_document_objects, parse_html_table,
                                    render_table_object_chunks)
from utils.evidence_merge import merge_extraction_rows
from utils.object_evidence_match import descriptor, match_object_evidence
from utils.object_routing import deduplicate_evidence_objects, is_index_page
from utils.pdf_reader import PageContent, _extract_table_records_from_page
from utils.physical_objects import PhysicalObjectIdentity, match_physical_objects
from utils.run_state import RunState


def table(rows=None, **metadata):
    return DocumentObject("p3_table_1", "table", 3, 1,
        rows=rows or [["구분", "연도", "배출량"], ["건물", "2030", "10"]],
        caption="표 1 배출 전망", metadata={"engine": "unlimited_ocr",
        "evidence_id": "ev-source-1", "target_sheets": ["emissions_forecast"], **metadata})


class ExtractionFoundationTests(unittest.TestCase):
    def test_mixed_malformed_response_retains_valid_rows_and_is_not_complete_cached(self):
        agent = ExtractorAgent()
        response = json.dumps({"emissions_forecast": [{"전망값": 10}, None, "invalid", [11]]})
        with patch("utils.llm_client.call_text", return_value=response) as call:
            first = agent.extract_objects([table()], "서울", {})
            second = agent.extract_objects([table()], "서울", {})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(first["emissions_forecast"][0]["전망값"], 10)
        self.assertEqual(first, second)
        self.assertTrue(all(record["status"] == "partial" for record in agent.extraction_records))
        self.assertTrue(all(record["invalid_row_count"] == 3 for record in agent.extraction_records))
        self.assertEqual(agent.extraction_records[0]["invalid_rows"][0]["index"], 2)

    def test_failed_retry_preserves_partial_checkpoint_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ExtractorAgent()
            agent.run_state = RunState(run_id="partial-test", root=Path(tmp), manifest={},
                                       resume=True, retry_failed_only=False)
            with patch("utils.llm_client.call_text", side_effect=[
                    '{"emissions_forecast": [{"전망값": 10}, null]}', "invalid json"]):
                first = agent.extract_objects([table()], "서울", {})
                second = agent.extract_objects([table()], "서울", {})
            self.assertEqual(first, second)
            self.assertEqual(agent.extraction_records[-1]["status"], "partial")
            self.assertTrue(agent.extraction_records[-1]["recovered"])
            self.assertEqual(agent.run_state.summary()["failed"], 1)

    def test_merged_serialized_lineage_contains_both_engines_and_sources(self):
        first = {"전망값": 10, "출처계보": json.dumps([{"engine": "pymupdf", "object_id": "native"}]),
                 "source_engines": ["pymupdf"]}
        second = {"전망값": 10, "_extraction_sources": [{"engine": "ocr", "object_id": "ocr"}],
                  "ocr_source_file": "offline.jsonl", "source_engines": ["ocr"]}
        merged = merge_extraction_rows("emissions_forecast", [first], [second])
        self.assertEqual(len(merged), 1)
        records = json.loads(merged[0]["출처계보"])
        self.assertEqual(records, merged[0]["_extraction_sources"])
        self.assertEqual({record["engine"] for record in records}, {"pymupdf", "ocr"})
        self.assertEqual(set(merged[0]["source_engines"]), {"pymupdf", "ocr"})

    def test_explicit_object_ocr_source_metadata_reaches_row_lineage(self):
        obj = table(source_engines=["pymupdf", "unlimited_ocr"],
                    source_object_ids=["native-id", "original-ocr-id"],
                    source_evidence_id="ev-native", ocr_source_file="offline.jsonl",
                    ocr_source_line=7, ocr_document_trust="document_verified",
                    ocr_selection_method="exact_source_object_id")
        agent = ExtractorAgent()
        with patch("utils.llm_client.call_text", return_value='{"emissions_forecast": [{"전망값": 10}]}'):
            row = agent.extract_objects([obj], "서울", {})["emissions_forecast"][0]
        self.assertEqual(row["ocr_source_file"], "offline.jsonl")
        self.assertIn("native-id", row["source_object_ids"])
        self.assertIn("ev-native", row["source_evidence_ids"])
        self.assertEqual(row["source_engines"], ["pymupdf", "unlimited_ocr"])
        lineage = json.loads(row["출처계보"])[0]
        self.assertEqual(lineage["ocr_source_line"], 7)
        self.assertEqual(lineage["ocr_document_trust"], "document_verified")
        self.assertEqual(lineage["ocr_selection_method"], "exact_source_object_id")

    def test_hwp_kordoc_engine_preserved_for_every_document_object_type(self):
        page = PageContent(1, "표 1 배출량\n그림 1 배출 전망", [
            "<table><tr><th>부문</th><th>값</th></tr><tr><td>건물</td><td>10</td></tr></table>"],
            [{"caption": "도표", "width": 300, "height": 200}], metadata={"engine": "kordoc"})
        objects = build_document_objects([page])
        self.assertTrue(objects)
        self.assertEqual({obj.metadata["engine"] for obj in objects}, {"kordoc"})
        self.assertTrue({"text", "table", "image", "chart"}.issubset({obj.object_type for obj in objects}))

    def test_rotated_native_table_keeps_unrotated_rows_and_restores_rotation(self):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        for y in (100, 130, 160):
            page.draw_line((40, y), (320, y))
        for x in (40, 180, 320):
            page.draw_line((x, 100), (x, 160))
        for x, y, value in ((50, 120, "Sector"), (190, 120, "Value"),
                            (50, 150, "building"), (190, 150, "10")):
            page.insert_text((x, y), value)
        baseline = _extract_table_records_from_page(page)
        page.set_rotation(90)
        rotated = _extract_table_records_from_page(page)
        self.assertEqual(page.rotation, 90)
        self.assertEqual(parse_html_table(rotated[0]["html"]), parse_html_table(baseline[0]["html"]))
        self.assertEqual(rotated[0]["bbox"], baseline[0]["bbox"])
        doc.close()

    def test_chunk_cell_measurement_counts_sent_cells_not_whole_source(self):
        obj = table([["구분", "연도", "배출량"]] + [[f"부문{i}", "2030", str(i)] for i in range(4)],
                    header_row_count=1)
        agent = ExtractorAgent()
        with patch.object(config, "MAX_BATCH_TABLE_CELLS", 6), patch("utils.llm_client.call_text", return_value='{"emissions_forecast": []}'):
            agent.extract_objects([obj], "서울", {})
        self.assertEqual(len(agent.extraction_records), 4)
        self.assertTrue(all(record["table_cells"] == 6 for record in agent.extraction_records))
        self.assertTrue(all(record["source_table_cells"] == 15 for record in agent.extraction_records))

    def test_optional_object_batching_skips_only_table_contained_represented_text(self):
        page = PageContent(3, "온실가스 현황", [], [])
        agent = ExtractorAgent()
        native = table(engine="pymupdf", header_row_count=1)
        native.bbox = (0, 0, 100, 100)
        represented = DocumentObject("represented", "text", 3, 1, text="건물 2030 10", bbox=(10, 10, 90, 30))
        missing = DocumentObject("missing", "text", 3, 2, text="누락값 99", bbox=(10, 40, 90, 50))
        outside = DocumentObject("outside", "text", 3, 3, text="온실가스 현황", bbox=(10, 110, 90, 130))
        agent.document_objects = [native, represented, missing, outside]
        with patch.object(config, "EXTRACTION_OBJECT_BATCHING_ENABLED", True), patch("utils.llm_client.call_text", return_value='{"emissions_regional": []}') as call:
            agent._extract_native_batch("emissions_regional", [page], "서울", "")
        self.assertEqual(call.call_count, 3)
        self.assertNotIn("represented", [record["object_id"] for record in agent.extraction_records])
        self.assertIn("missing", [record["object_id"] for record in agent.extraction_records])

    def test_changed_municipality_does_not_reuse_object_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ExtractorAgent()
            agent.run_state = RunState(run_id="same-input", root=Path(tmp), manifest={},
                                       resume=True, retry_failed_only=False)
            response = '{"emissions_forecast": [{"전망값": 10}]}'
            with patch("utils.llm_client.call_text", return_value=response) as call:
                first = agent.extract_objects([table()], "서울", {})
                second = agent.extract_objects([table()], "경기", {})
            self.assertEqual(call.call_count, 2)
            self.assertEqual(first["emissions_forecast"][0]["지자체명"], "서울")
            self.assertEqual(second["emissions_forecast"][0]["지자체명"], "경기")
            self.assertEqual(agent.run_state.summary()["ok"], 2)

    def test_caption_proxy_consolidates_with_native_table_and_keeps_alias(self):
        native = DocumentObject("native", "table", 3, 1, rows=[["부문", "값"], ["건물", "10"]],
            caption="표 1 배출량", number="표 1", metadata={"evidence_id": "ev-native", "engine": "pymupdf"})
        proxy = DocumentObject("proxy", "table", 3, 2, caption="표 1 배출량", number="표 1",
            metadata={"caption_only": True, "evidence_id": "ev-proxy", "engine": "pymupdf"})
        objects, removed = deduplicate_evidence_objects([native, proxy])
        self.assertEqual(removed, 1)
        self.assertEqual(len(objects), 1)
        self.assertIn("proxy", objects[0].metadata["alias_object_ids"])

    def test_body_row_header_does_not_become_another_column_header(self):
        html = ('<table><tr><th>부문</th><th>값</th></tr>'
                '<tr><th scope="row">건물</th><td>10</td></tr>'
                '<tr><th scope="row">수송</th><td>20</td></tr></table>')
        obj = build_document_objects([PageContent(3, "표 1 배출량", [html], [])])[1]
        self.assertEqual(obj.metadata["header_row_count"], 1)
        self.assertEqual(len(render_table_object_chunks(obj, rows_per_chunk=2)), 2)

    def test_model_provided_source_ids_are_preserved_as_aliases(self):
        agent = ExtractorAgent()
        raw = {"emissions_forecast": [{"전망값": 10, "근거ID": "fixture-evidence", "객체ID": "source-object"}]}
        with patch("utils.llm_client.call_text", return_value=json.dumps(raw)):
            row = agent.extract_objects([table()], "서울", {})["emissions_forecast"][0]
        self.assertIn("fixture-evidence", row["근거ID목록"])
        self.assertIn("fixture-evidence", row["원본근거ID"])
        self.assertIn("source-object", row["source_object_ids"])
        self.assertIn("fixture-evidence", row["출처계보"])

    def test_span_expansion_keeps_multi_header_column_alignment(self):
        html = ('<table><thead><tr><th rowspan="2">부문</th><th colspan="2">전망</th></tr>'
                '<tr><th>2030</th><th>2040</th></tr></thead>'
                '<tbody><tr><td>건물</td><td>10</td><td>20</td></tr></tbody></table>')
        rows = parse_html_table(html)
        self.assertEqual(rows, [["부문", "전망", "전망"], ["부문", "2030", "2040"], ["건물", "10", "20"]])
        obj = build_document_objects([PageContent(3, "표 1 배출 전망", [html], [])])[1]
        self.assertEqual(obj.metadata["header_row_count"], 2)

    def test_all_header_rows_repeat_and_body_is_never_omitted(self):
        obj = table([["구분", "직접", "간접"], ["연도", "2030", "2040"],
                     ["건물", "1", "2"], ["수송", "3", "4"], ["폐기물", "5", "6"]],
                    header_row_count=2)
        chunks = render_table_object_chunks(obj, rows_per_chunk=3, max_cells=9)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all("| 구분 | 직접 | 간접 |" in text and "| 연도 | 2030 | 2040 |" in text for text in chunks))
        self.assertTrue(any("폐기물" in text for text in chunks))

    def test_large_width_not_truncated_at_twelve_columns(self):
        obj = table([["구분"] + [str(2000 + i) for i in range(20)], ["건물"] + [str(i) for i in range(20)]],
                    header_row_count=1)
        text = render_table_object_chunks(obj)[0]
        self.assertIn("2019", text)
        self.assertIn("| 19 |", text)

    def test_single_oversized_row_is_recorded_without_llm_call(self):
        agent = ExtractorAgent()
        with patch.object(config, "MAX_BATCH_CHARS", 600), patch("utils.llm_client.call_text") as call:
            result = agent.extract_objects([table([["구분", "값"], ["건물", "X" * 1000]])], "서울", {})
        call.assert_not_called()
        self.assertEqual(result["emissions_forecast"], [])
        self.assertEqual(agent.extraction_records[0]["status"], "batch_limit")

    def test_targeted_ocr_extraction_lineage_and_reexecution_reuses(self):
        agent = ExtractorAgent()
        raw = {"emissions_forecast": [{"시나리오": "BAU", "전망범위": "지역전체", "부문": "건물", "연도": 2030, "전망값": 10}]}
        with patch("utils.llm_client.call_text", return_value=json.dumps(raw)) as call:
            first = agent.extract_objects([table()], "서울", {})
            second = agent.extract_objects([table()], "서울", {})
        self.assertEqual(call.call_count, 1)
        row = first["emissions_forecast"][0]
        self.assertEqual(row["근거ID"], "ev-source-1")
        self.assertEqual(row["객체ID"], "p3_table_1")
        self.assertTrue(row["OCR보완여부"])
        self.assertEqual(row["출처페이지"], [3])
        self.assertEqual(second["emissions_forecast"], first["emissions_forecast"])

    def test_extraction_failure_not_cached_and_other_sheet_survives(self):
        agent = ExtractorAgent()
        obj = table(target_sheets=["emissions_forecast", "emissions_regional"])
        responses = ["not json", json.dumps({"emissions_regional": [{"부문": "건물", "배출량": 10}]})]
        with patch("utils.llm_client.call_text", side_effect=responses):
            result = agent.extract_objects([obj], "서울", {})
        self.assertEqual(len(result["emissions_regional"]), 1)
        self.assertEqual(result["emissions_forecast"], [])
        self.assertTrue(any(record["status"] in {"parse_fail", "call_fail"} for record in agent.extraction_records))

    def test_exact_fact_merges_sources_and_conflict_retains_both(self):
        base = {"지자체명": "서울", "시나리오": "BAU", "전망범위": "지역전체",
                "부문": "건물", "연도": 2030, "단위": "tCO2eq", "전망값": 10}
        first = {**base, "객체ID": "native", "근거ID": "ev-native", "_extraction_sources": [{"engine": "pymupdf"}]}
        duplicate = {**base, "객체ID": "ocr", "근거ID": "ev-ocr", "_extraction_sources": [{"engine": "ocr"}]}
        conflicts = []
        merged = merge_extraction_rows("emissions_forecast", [first], [duplicate], conflicts)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]["_extraction_sources"]), 2)
        merged = merge_extraction_rows("emissions_forecast", merged, [{**duplicate, "전망값": 11}], conflicts)
        self.assertEqual(len(merged), 2)
        self.assertEqual(conflicts[0]["status"], "value_conflict_retained")

    def test_different_scope_scenario_unit_never_collide(self):
        row = {"지자체명": "서울", "부문": "건물", "연도": 2030, "시나리오": "BAU",
               "전망범위": "지역전체", "단위": "tCO2eq", "전망값": 10}
        incoming = [{**row, "전망범위": "관리권한"}, {**row, "시나리오": "정책반영"}, {**row, "단위": "천톤CO2eq"}]
        conflicts = []
        result = merge_extraction_rows("emissions_forecast", [row], incoming, conflicts)
        self.assertEqual(len(result), 4)
        self.assertEqual(conflicts, [])

    def test_legacy_missing_scope_and_explicit_blank_are_same_fact(self):
        old = {"부문": "건물", "연도": 2030, "전망값": 10}
        self.assertEqual(len(merge_extraction_rows("emissions_forecast", [old], [{**old, "전망범위": ""}])), 1)

    def test_different_table_numbers_block_even_same_bbox(self):
        a = PhysicalObjectIdentity("a", 1, "table", bbox=(0, 0, 100, 100), number="표 1", engine="native")
        b = PhysicalObjectIdentity("b", 1, "table", bbox=(0, 0, 100, 100), number="표 2", engine="ocr")
        self.assertFalse(match_physical_objects(a, b).same)

    def test_content_conflict_objects_not_dropped_by_richer_cells(self):
        left = table(engine="pymupdf")
        right = table([["구분", "연도", "배출량"], ["건물", "2030", "11"]])
        right.object_id = "ocr-1"
        objects, removed = deduplicate_evidence_objects([left, right])
        self.assertEqual(len(objects), 2)
        self.assertEqual(removed, 0)
        self.assertTrue(any(obj.metadata.get("physical_match_status") == "content_conflict_retained" for obj in objects))

    def test_ambiguous_evidence_id_is_not_confirmed(self):
        expected = descriptor(key="source", evidence_ids=["ev-1"], page=3)
        candidates = [descriptor(key="a", evidence_ids=["ev-1"], object_ids=["a"], page=3),
                      descriptor(key="b", evidence_ids=["ev-1"], object_ids=["b"], page=3)]
        self.assertEqual(match_object_evidence(expected, candidates).status, "ambiguous")

    def test_index_page_does_not_generate_real_caption_objects(self):
        text = "표 목차\n표 1 배출 전망 .......... 35\n표 2 감축목표 ........ 40"
        self.assertTrue(is_index_page(text))
        objects = build_document_objects([PageContent(1, text, [], [])])
        self.assertFalse(any(obj.metadata.get("caption_only") for obj in objects))

    def test_default_main_native_batch_text_is_preserved(self):
        agent = ExtractorAgent()
        page = PageContent(2, "온실가스 현황", ["<table><tr><th>연도</th></tr><tr><td>2020</td></tr></table>"], [])
        with patch("utils.llm_client.call_text", return_value='{"emissions_regional": []}') as call:
            agent._extract_native_batch("emissions_regional", [page], "서울", "")
        prompt = call.call_args.args[0]
        self.assertIn("=== 페이지 2 ===\n온실가스 현황\n\n[표 데이터]\n<table>", prompt)

    def test_object_failure_and_success_checkpoint_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState(run_id="test", root=Path(tmp), manifest={}, resume=False, retry_failed_only=False)
            agent = ExtractorAgent()
            agent.run_state = state
            with patch("utils.llm_client.call_text", return_value='{"emissions_forecast": [{"전망값": 10}]}') as call:
                result = agent.extract_objects([table()], "서울", {})
            self.assertEqual(call.call_count, 1)
            resumed = ExtractorAgent()
            resumed.run_state = RunState(run_id="test", root=Path(tmp), manifest={}, resume=True, retry_failed_only=False)
            with patch("utils.llm_client.call_text") as call:
                replay = resumed.extract_objects([table()], "서울", {})
            call.assert_not_called()
            self.assertEqual(replay, result)


if __name__ == "__main__":
    unittest.main()
