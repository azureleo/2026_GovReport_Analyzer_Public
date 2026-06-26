import unittest

from utils.pdf_reader import _has_graph_keywords


class PdfReaderRenderingTests(unittest.TestCase):
    def test_carbon_neutrality_text_alone_does_not_trigger_page_render(self):
        self.assertFalse(_has_graph_keywords("서울특별시 탄소중립 기본계획 목표와 추진 방향"))

    def test_explicit_visual_marker_triggers_page_render(self):
        self.assertTrue(_has_graph_keywords("[그림 2-1] 온실가스 배출량 추이"))


if __name__ == "__main__":
    unittest.main()
