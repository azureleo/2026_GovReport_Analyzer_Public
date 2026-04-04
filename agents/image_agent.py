"""
에이전트 2-b: 이미지·그래프 분석 에이전트

PDF에서 추출한 이미지(그래프, 차트, 도표)를 Gemini Vision으로 분석하여
텍스트 추출로 놓친 수치 데이터를 보완합니다.
"""

import logging

import config
from utils.pdf_reader import PageContent
from utils import llm_client

logger = logging.getLogger(__name__)


IMAGE_SYSTEM = """당신은 한국 지자체 탄소중립 계획 보고서의 그래프와 차트를 분석하는 전문가입니다.

이미지에서 보이는 수치, 연도, 부문, 단위 정보를 최대한 정확하게 읽어내세요.

규칙:
1. 그래프 축의 값을 꼼꼼히 읽어 숫자로 반환하세요.
2. 단위(tCO2eq, 천 tCO2eq 등)를 반드시 기록하세요.
3. 값이 불분명하면 "약 XXX" 형태로 표현하세요.
4. 텍스트가 포함된 표(도표)도 분석하세요.
5. 그래프가 아닌 이미지(사진, 로고 등)는 type을 "해당없음"으로 반환하세요.
6. 반드시 JSON만 반환하세요."""


def _is_relevant_image(image: dict) -> bool:
    w, h = image.get("width", 0), image.get("height", 0)
    if w > 0 and h > 0 and (w / h > 5 or h / w > 5):
        return False
    return w >= 200 and h >= 150


class ImageAgent:
    """에이전트 2-b: 이미지·그래프 분석 에이전트"""

    def __init__(self):
        self._image_results: list[dict] = []

    def _analyze_image(self, image: dict, page_num: int, municipality: str) -> dict | None:
        prompt = f"""이 이미지는 '{municipality}' 탄소중립 기본계획 보고서 {page_num}페이지에서 추출되었습니다.

이미지를 분석하여 다음 JSON 형식으로 반환하세요:
{{
  "type": "그래프유형(막대/꺾은선/파이/표/기타/해당없음)",
  "title": "그래프/표 제목",
  "unit": "단위 (예: tCO2eq, 천tCO2eq, %)",
  "description": "이미지 내용 요약 (1~3문장)",
  "ghg_data": [
    {{
      "label": "부문 또는 계열명",
      "data_type": "현황|전망|목표",
      "values": {{"2018": 숫자, "2019": 숫자, ..., "2030": 숫자}}
    }}
  ],
  "other_data": {{}}
}}"""

        resp = llm_client.call_vision(image["base64"], prompt, system=IMAGE_SYSTEM)
        parsed = llm_client.parse_json(resp)
        # list나 빈 값이 반환되면 건너뜀
        if not parsed or not isinstance(parsed, dict):
            return None
        if parsed.get("type") == "해당없음":
            return None
        parsed["page_number"] = page_num
        parsed["municipality"] = municipality
        return parsed

    def _merge_image_ghg(self, text_results: dict, analyses: list[dict], municipality: str) -> dict:
        existing_ghg = text_results.get("ghg", [])

        for analysis in analyses:
            for series in analysis.get("ghg_data", []):
                label = series.get("label", "")
                data_type = series.get("data_type", "현황")
                values = series.get("values", {})
                if not values:
                    continue

                matched = False
                for ghg_row in existing_ghg:
                    if ghg_row.get("종류") == data_type:
                        row_yearly = ghg_row.setdefault("연도별", {})
                        for year, val in values.items():
                            if str(year) not in row_yearly or row_yearly[str(year)] is None:
                                try:
                                    row_yearly[str(year)] = float(str(val).replace(",", "").replace("약 ", ""))
                                except (ValueError, TypeError):
                                    pass
                        matched = True

                if not matched:
                    new_row = {
                        "지자체명": municipality,
                        "배출유형": "직접배출",
                        "종류": data_type,
                        "부문": label,
                        "연도별": {},
                    }
                    for year, val in values.items():
                        try:
                            new_row["연도별"][str(year)] = float(str(val).replace(",", "").replace("약 ", ""))
                        except (ValueError, TypeError):
                            new_row["연도별"][str(year)] = None
                    existing_ghg.append(new_row)

        text_results["ghg"] = existing_ghg
        return text_results

    def extract(self, pages: list[PageContent], text_results: dict, municipality: str) -> dict:
        pages_with_images = [
            (p, img)
            for p in pages
            for img in p.images
            if _is_relevant_image(img)
        ]

        # 상한 적용 (비용·시간 절감)
        if len(pages_with_images) > config.MAX_IMAGES:
            print(f"[에이전트2b 이미지분석] 이미지 {len(pages_with_images)}개 중 앞 {config.MAX_IMAGES}개만 분석")
            pages_with_images = pages_with_images[:config.MAX_IMAGES]

        total = len(pages_with_images)
        print(f"[에이전트2b 이미지분석] 분석 대상 이미지: {total}개")

        if total == 0:
            print("[에이전트2b 이미지분석] 분석할 이미지 없음.")
            return text_results

        analyses = []
        for idx, (page, image) in enumerate(pages_with_images, start=1):
            print(f"[에이전트2b 이미지분석] 이미지 {idx}/{total} (페이지 {page.page_number}) 분석 중...")
            result = self._analyze_image(image, page.page_number, municipality)
            if result:
                analyses.append(result)
                print(f"  → {result.get('type', '?')}: {result.get('title', '제목없음')}")
            else:
                print(f"  → 관련 없는 이미지, 건너뜀.")

        self._image_results = analyses
        print(f"[에이전트2b 이미지분석] 유효 분석 {len(analyses)}개 완료")

        return self._merge_image_ghg(text_results, analyses, municipality)

    def report(self) -> str:
        types: dict[str, int] = {}
        for a in self._image_results:
            t = a.get("type", "기타")
            types[t] = types.get(t, 0) + 1
        type_str = ", ".join(f"{k}: {v}개" for k, v in types.items()) or "없음"
        return (
            f"[에이전트2b 이미지분석] 완료\n"
            f"  - 분석된 이미지: {len(self._image_results)}개\n"
            f"  - 유형별: {type_str}"
        )
