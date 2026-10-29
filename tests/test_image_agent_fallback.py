import unittest

import config
from agents.image_agent import ImageAgent, _coverage_reduce_images
from agents.image_agent import _fallback_visual_observations
from utils.pdf_reader import PageContent


class ImageFallbackTests(unittest.TestCase):
    def test_extracts_rendered_table_observations_from_page_text(self):
        page = PageContent(
            page_number=137,
            text=(
                "[그림 2-91] 도로 수송 부문의 연도별 연료 사용량 전망\n"
                "[표 2-38] 연도별 통행량 변화율\n"
                "(단위: %)\n"
                "2019~ 2025~ 2030~ 2035~ 2040~ 2045~2050\n"
                "철도 3.30% 0.40% -0.55% -0.82% -1.02% -1.36%\n"
                "※ 주: 수도권 변화율이 서울과 유사하다고 가정"
            ),
            tables=[],
            images=[],
        )

        rows = _fallback_visual_observations(page, "서울특별시")

        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["대상시트"], "energy")
        self.assertEqual(rows[2]["연도"], 2030)
        self.assertEqual(rows[2]["값"], -0.55)
        self.assertEqual(rows[2]["반영여부"], "검토")

    def test_default_image_analysis_has_no_top_n_cap(self):
        saved = {
            "IMAGE_TRIAGE_ENABLED": config.IMAGE_TRIAGE_ENABLED,
            "IMAGE_CHART_TABLE_EXTRACTION": config.IMAGE_CHART_TABLE_EXTRACTION,
            "MAX_IMAGES": config.MAX_IMAGES,
        }
        config.IMAGE_TRIAGE_ENABLED = False
        config.IMAGE_CHART_TABLE_EXTRACTION = False
        config.MAX_IMAGES = None

        class CountingImageAgent(ImageAgent):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def _analyze_image(self, image, page_num, municipality):
                self.calls += 1
                return None

        try:
            pages = [
                PageContent(
                    page_number=i,
                    text="",
                    tables=[],
                    images=[{"base64": f"image-{i}", "width": 300, "height": 200, "caption": ""}],
                )
                for i in range(1, 4)
            ]
            agent = CountingImageAgent()
            agent.extract(pages, {"chart_observations": []}, "서울특별시")
            self.assertEqual(agent.calls, 3)
        finally:
            config.IMAGE_TRIAGE_ENABLED = saved["IMAGE_TRIAGE_ENABLED"]
            config.IMAGE_CHART_TABLE_EXTRACTION = saved["IMAGE_CHART_TABLE_EXTRACTION"]
            config.MAX_IMAGES = saved["MAX_IMAGES"]

    def test_page_render_covers_embedded_images_on_same_page(self):
        page = PageContent(
            page_number=10,
            text="",
            tables=[],
            images=[],
        )
        embedded = {"base64": "embedded", "width": 300, "height": 200, "caption": ""}
        render = {"base64": "render", "width": 600, "height": 800, "caption": "Page 10 full render"}

        reduced = _coverage_reduce_images([(page, embedded), (page, render)])

        self.assertEqual(len(reduced), 1)
        self.assertEqual(reduced[0][1]["caption"], "Page 10 full render")


if __name__ == "__main__":
    unittest.main()
