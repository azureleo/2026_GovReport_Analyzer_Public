import unittest

from utils import pdf_reader
from utils.pdf_reader import _has_graph_keywords


class PdfReaderRenderingTests(unittest.TestCase):
    def test_carbon_neutrality_text_alone_does_not_trigger_page_render(self):
        self.assertFalse(_has_graph_keywords("서울특별시 탄소중립 기본계획 목표와 추진 방향"))

    def test_explicit_visual_marker_triggers_page_render(self):
        self.assertTrue(_has_graph_keywords("[그림 2-1] 온실가스 배출량 추이"))

    def test_password_protected_pdf_closes_handle_and_explains_fix(self):
        # Given: PyMuPDF가 암호 PDF로 연 문서 핸들
        closed = {"value": False}

        class FakeDocument:
            needs_pass = True

            def close(self):
                closed["value"] = True

        original_open = pdf_reader.fitz.open

        try:
            pdf_reader.fitz.open = lambda path: FakeDocument()

            # When / Then: 즉시 한국어 안내를 내고 핸들을 닫는다.
            with self.assertRaisesRegex(RuntimeError, "암호로 보호된 PDF입니다"):
                pdf_reader.extract_pdf("locked.pdf")
        finally:
            pdf_reader.fitz.open = original_open

        self.assertTrue(closed["value"])


if __name__ == "__main__":
    unittest.main()
