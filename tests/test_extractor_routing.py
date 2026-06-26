import unittest

import config
from agents.extractor_agent import _route_pages_by_sheet
from utils.pdf_reader import PageContent


class ExtractorRoutingTests(unittest.TestCase):
    def test_route_max_pages_config_contains_current_sheet_keys(self):
        missing = set(config.EXTRACTION_SHEETS) - set(config.DOCUMENT_ROUTE_MAX_PAGES)

        self.assertEqual(missing, set())

    def test_document_meta_route_limit_uses_current_sheet_key(self):
        saved = dict(config.DOCUMENT_ROUTE_MAX_PAGES)
        config.DOCUMENT_ROUTE_MAX_PAGES = {sheet: None for sheet in config.EXTRACTION_SHEETS}
        config.DOCUMENT_ROUTE_MAX_PAGES["document_meta"] = 1
        pages = [
            PageContent(
                page_number=number,
                text="기본계획 탄소중립 기준연도 목표연도",
                tables=[],
                images=[],
            )
            for number in range(1, 5)
        ]

        try:
            routed = _route_pages_by_sheet(pages, context_pages=0, min_score=3)
        finally:
            config.DOCUMENT_ROUTE_MAX_PAGES = saved

        self.assertEqual(len(routed["document_meta"]), 1)


if __name__ == "__main__":
    unittest.main()
