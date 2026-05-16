"""
에이전트 2-b: 이미지·그래프 분석 에이전트

PDF에서 추출한 이미지(그래프, 차트, 도표)를 Gemini Vision으로 분석하여
텍스트 추출로 놓친 수치 데이터를 보완합니다.
"""

import logging
import base64
import io
import json

import config
from utils.pdf_reader import PageContent
from utils import llm_client
from PIL import Image, ImageFilter, ImageStat

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


CHART_TABLE_SYSTEM = """당신은 한국 지자체 탄소중립 계획 보고서의 그래프를 표로 변환하는 전문가입니다.

DePlot 방식처럼 그래프 이미지를 먼저 선형화된 표 데이터로 바꾸세요.
추론이나 보간보다 이미지에 보이는 축, 범례, 라벨, 수치만 우선합니다.

규칙:
1. 막대그래프, 꺾은선그래프, 영역그래프, 원그래프, 데이터 표만 변환하세요.
2. 지도, 사진, 포스터, 목차, 설명용 삽화는 type을 "해당없음"으로 반환하세요.
3. target_sheet는 이미지 내용에 따라 vehicle, energy, ghg, strategy, summary 중 하나로 분류하세요.
4. 단위가 "천 tCO2eq", "천톤CO2eq"이면 unit에 그대로 적고 값은 이미지에 보이는 숫자 그대로 반환하세요.
5. 25,432.000처럼 천 단위/소수 표기가 있으면 25432000으로 붙이지 말고 25432 또는 25432.0으로 반환하세요.
6. 수치가 불분명하면 null로 두고 confidence를 낮추세요.
7. 막대/선의 값이 축 눈금만으로 추정된 값이면 fields에 {"estimated": true}를 넣고 confidence는 medium 이하로 두세요.
8. 반드시 JSON만 반환하세요.

시트별 fields 예시:
- vehicle: {"용도": "승용", "차종": "전기", "대수": 123, "주행거리": 45.1}
- energy: {"용도": "가정", "전력": 123, "가스": 456, "석유_에너지유": 12}
- ghg: {"연도": 2030, "항목": "건물", "종류": "목표", "값": 12345}
- strategy: {"감축전략_부문": "건물", "감축사업명": "공공건물 그린리모델링", "종류": "계획(감축량)", "연도": 2030, "값": 123}
"""


def _is_relevant_image(image: dict) -> bool:
    w, h = image.get("width", 0), image.get("height", 0)
    if w > 0 and h > 0 and (w / h > 5 or h / w > 5):
        return False
    return w >= 200 and h >= 150


_CHART_CONTEXT_KEYWORDS = [
    "그림", "그래프", "차트", "도표", "표", "추이", "전망", "현황", "목표",
    "배출량", "온실가스", "감축", "BAU", "NDC", "tCO2", "CO2eq", "비율", "%",
]

_NEGATIVE_CONTEXT_KEYWORDS = [
    "목차", "표 목차", "그림 목차", "사진", "행사", "공모전", "모집", "설문", "자문회의",
]

_LOCAL_DIRECT_DATA_KEYWORDS = [
    "배출량", "배출 현황", "배출 전망", "감축목표", "감축사업", "추진계획",
    "최종에너지", "에너지 소비", "자동차 등록", "주행거리",
]


def _decode_image(image: dict) -> Image.Image | None:
    try:
        raw = base64.b64decode(image.get("base64", ""))
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def _image_feature_score(image: dict) -> tuple[int, list[str]]:
    """
    ChartQA식 triage의 로컬 근사.

    ChartQA가 강조하는 차트의 핵심 단서(축/격자/범례/데이터 마크/텍스트-숫자 구조)를
    비용 없는 이미지 특징으로 근사한다. 이 단계는 정답 추출이 아니라 Vision 분석 대상
    선별만 담당한다.
    """
    pil = _decode_image(image)
    if pil is None:
        return 0, ["decode_failed"]

    score = 0
    reasons: list[str] = []
    w, h = pil.size
    area = w * h
    if area >= 120_000:
        score += 1
        reasons.append("large")

    small = pil.copy()
    small.thumbnail((256, 256))
    gray = small.convert("L")

    stat = ImageStat.Stat(small)
    color_std = sum(stat.stddev) / max(len(stat.stddev), 1)
    gray_stat = ImageStat.Stat(gray)
    gray_std = gray_stat.stddev[0] if gray_stat.stddev else 0

    hist = gray.histogram()
    total = sum(hist) or 1
    white_ratio = sum(hist[235:]) / total
    dark_ratio = sum(hist[:65]) / total

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_hist = edges.histogram()
    edge_total = sum(edge_hist) or 1
    edge_ratio = sum(edge_hist[80:]) / edge_total

    # 차트/표는 대개 흰 배경 + 선/글자/격자 조합을 가진다.
    if white_ratio >= 0.35:
        score += 2
        reasons.append("white_background")
    if 0.02 <= dark_ratio <= 0.45:
        score += 1
        reasons.append("text_or_axis_pixels")
    if edge_ratio >= 0.025:
        score += 2
        reasons.append("line_grid_density")
    if gray_std >= 25:
        score += 1
        reasons.append("contrast")

    # 사진/삽화는 색 분산이 크고 흰 배경 비율이 낮은 경우가 많다.
    if color_std >= 70 and white_ratio < 0.25:
        score -= 3
        reasons.append("photo_like_color_variance")

    caption = image.get("caption", "")
    if "full render" in caption.lower():
        score += 2
        reasons.append("page_render")

    return score, reasons


def _context_score(page: PageContent) -> tuple[int, list[str]]:
    text = page.text or ""
    score = 0
    reasons: list[str] = []
    hits = [kw for kw in _CHART_CONTEXT_KEYWORDS if kw in text]
    if hits:
        score += min(len(hits), 5)
        reasons.append("context:" + ",".join(hits[:4]))

    negatives = [kw for kw in _NEGATIVE_CONTEXT_KEYWORDS if kw in text]
    if negatives:
        score -= min(len(negatives) * 2, 6)
        reasons.append("negative_context:" + ",".join(negatives[:3]))

    if page.tables:
        score += 2
        reasons.append("page_has_tables")
    return score, reasons


def _has_reference_context(page: PageContent, image: dict, municipality: str) -> tuple[bool, list[str]]:
    """
    보고서 작성 지자체의 직접 데이터가 아니라 참고자료/해외사례/목차성 페이지인지 판별한다.

    이 단계는 Vision 호출 전 비용 절감용 필터다. 지자체명과 직접 데이터 키워드가 함께 있으면
    참고 키워드가 일부 있어도 보존한다.
    """
    text = " ".join([page.text or "", image.get("caption", "") or ""])
    lowered = text.casefold()
    hits = [
        keyword
        for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS
        if keyword.casefold() in lowered
    ]
    if not hits:
        return False, []

    local_names = {municipality, municipality.replace("특별시", ""), municipality.replace("광역시", "")}
    has_local_name = any(name and name in text for name in local_names)
    has_direct_data = any(keyword in text for keyword in _LOCAL_DIRECT_DATA_KEYWORDS)

    if has_local_name and has_direct_data:
        return False, hits
    return True, hits


def _triage_image(page: PageContent, image: dict) -> dict:
    image_score, image_reasons = _image_feature_score(image)
    context_score, context_reasons = _context_score(page)
    score = image_score + context_score

    caption = image.get("caption", "")
    keep_rendered = (
        config.IMAGE_TRIAGE_KEEP_RENDERED_CONTEXT
        and "full render" in caption.lower()
        and context_score >= 3
    )
    passed = score >= config.IMAGE_TRIAGE_MIN_SCORE or keep_rendered

    return {
        "page": page,
        "image": image,
        "score": score,
        "passed": passed,
        "reasons": image_reasons + context_reasons,
    }


def _parse_chart_value(value, unit: str | None = None) -> float | None:
    """차트 표 변환 결과의 값을 숫자로 정규화."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"null", "none", "nan", "-"}:
            return None
        text = text.replace("약", "").replace(",", "").replace(" ", "")
        try:
            numeric = float(text)
        except ValueError:
            return None

    # Gemini가 25,432.000 같은 표기를 25432000으로 붙여 반환하는 경우가 있어
    # 차트 단위가 천톤 계열이면 Organizer에서 한 번 더 스케일 검증하도록 원값을 유지한다.
    return numeric


def _normalize_chart_year(year) -> str | None:
    """차트 표 변환 결과의 연도를 config.YEARS 문자열 키로 정규화."""
    try:
        year_int = int(float(str(year).strip()))
    except (ValueError, TypeError):
        return None
    if year_int not in config.YEARS:
        return None
    return str(year_int)


def _chart_year_int(year) -> int | None:
    try:
        return int(float(str(year).strip()))
    except (ValueError, TypeError):
        return None


def _is_reference_chart(analysis: dict) -> bool:
    """해외사례/참고자료성 이미지는 본 데이터 자동 반영에서 제외."""
    text = " ".join(
        str(analysis.get(k, "") or "")
        for k in ["title", "summary", "unit", "chart_type"]
    ).casefold()
    return any(keyword.casefold() in text for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS)


def _is_table_like_chart(analysis: dict) -> bool:
    return str(analysis.get("chart_type", "")).strip() == "표"


def _has_chart_value(item: dict) -> bool:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
    if item.get("값") is not None:
        return True
    return any(
        fields.get(k) is not None
        for k in [
            "대수", "주행거리", "석유_에너지유", "석유_LPG",
            "석유_비에너지유", "가스", "전력", "열", "신재생",
        ]
    )


def _infer_chart_kind(item: dict, analysis: dict) -> str:
    """차트 행의 종류(현황/전망/목표)를 제목·요약·행 텍스트에서 보강 추론."""
    kind = item.get("종류")
    if kind in ("현황", "전망", "목표"):
        return kind
    text = " ".join(
        str(v or "") for v in [
            item.get("항목"),
            analysis.get("title"),
            analysis.get("summary"),
        ]
    )
    if any(token in text for token in ("목표", "감축목표", "NDC")):
        return "목표"
    if any(token in text for token in ("전망", "BAU", "추정")):
        return "전망"
    return "현황"


class ImageAgent:
    """에이전트 2-b: 이미지·그래프 분석 에이전트"""

    def __init__(self):
        self._image_results: list[dict] = []
        self._triage_stats: dict = {}

    def _analyze_image(self, image: dict, page_num: int, municipality: str) -> dict | None:
        if config.IMAGE_CHART_TABLE_EXTRACTION:
            return self._chart_to_table(image, page_num, municipality)

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

    def _chart_to_table(self, image: dict, page_num: int, municipality: str) -> dict | None:
        prompt = f"""이 이미지는 '{municipality}' 탄소중립 기본계획 보고서 {page_num}페이지에서 추출되었습니다.

이미지가 그래프/차트/표라면 DePlot 방식으로 다음 JSON 형식의 표 데이터로 변환하세요:
{{
  "type": "chart_table|해당없음",
  "target_sheet": "vehicle|energy|ghg|strategy|summary",
  "chart_type": "막대|꺾은선|영역|원|표|복합|기타",
  "title": "그래프/표 제목",
  "unit": "단위 원문",
  "page_number": {page_num},
  "table": [
    {{
      "연도": 2030,
      "항목": "계열명 또는 부문명",
      "종류": "현황|전망|목표|기타",
      "값": 12345,
      "단위": "단위 원문",
      "fields": {{"시트별 추가 필드": "값"}}
    }}
  ],
  "summary": "이미지 내용 요약 1문장",
  "confidence": "high|medium|low"
}}

온실가스 배출량/전망/목표 그래프라면 target_sheet는 ghg, 항목에는 건물, 수송, 폐기물, 기타, 합계 같은 부문명을 넣으세요.
감축사업별 계획/실적/예산/감축량 표라면 target_sheet는 strategy로 두고 fields에 사업명과 부문을 넣으세요.
차량 등록대수/주행거리 표라면 target_sheet는 vehicle, 에너지 소비량 표라면 target_sheet는 energy로 두세요.
이미지에 숫자축만 있고 정확한 값을 읽기 어려우면 대략값을 만들지 말고 null로 반환하세요."""

        resp = llm_client.call_vision(image["base64"], prompt, system=CHART_TABLE_SYSTEM)
        parsed = llm_client.parse_json(resp)
        if not parsed or not isinstance(parsed, dict):
            return None
        if parsed.get("type") == "해당없음":
            return None
        table = parsed.get("table", [])
        if not isinstance(table, list) or not table:
            return None
        parsed["type"] = "chart_table"
        parsed["page_number"] = page_num
        parsed["municipality"] = municipality
        return parsed

    def _infer_target_sheet(self, analysis: dict) -> str:
        target = analysis.get("target_sheet")
        if target in {"vehicle", "energy", "ghg", "strategy", "summary"}:
            return target
        text = " ".join(str(analysis.get(k, "")) for k in ["title", "summary", "unit"])
        if any(k in text for k in ["자동차", "차량", "등록대수", "주행거리"]):
            return "vehicle"
        if any(k in text for k in ["에너지", "전력", "도시가스", "석유", "신재생", "TJ", "toe"]):
            return "energy"
        if any(k in text for k in ["감축사업", "성과지표", "예산", "이행", "계획(감축량)"]):
            return "strategy"
        return "ghg"

    def _confidence_rank(self, confidence: str | None) -> int:
        return {"low": 1, "medium": 2, "high": 3}.get(str(confidence or "").lower(), 1)

    def _chart_merge_decision(self, analysis: dict, item: dict, target_sheet: str) -> tuple[bool, str, list[str]]:
        """
        그래프 판독값의 본 시트 자동 반영 여부를 보수적으로 결정한다.

        Gemini가 confidence를 과하게 high로 주는 경우가 있어, 프로젝트 범위/참고자료/표 여부를
        코드에서 한 번 더 확인한다.
        """
        reasons: list[str] = []
        min_conf = getattr(config, "IMAGE_CHART_MERGE_MIN_CONFIDENCE", "medium")
        confidence = analysis.get("confidence", "low")
        if self._confidence_rank(confidence) < self._confidence_rank(min_conf):
            reasons.append("신뢰도 기준 미달")

        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        if fields.get("estimated") is True:
            reasons.append("축 기반 추정값")
        if target_sheet == "summary":
            reasons.append("요약/참고성 자료")
        if _is_reference_chart(analysis):
            reasons.append("참고자료/해외사례")
        if not _has_chart_value(item):
            reasons.append("값 없음")

        year_int = _chart_year_int(item.get("연도"))
        if year_int is None:
            reasons.append("연도 없음/비숫자")
        if year_int is not None and year_int not in config.IMAGE_CHART_MERGE_YEARS:
            reasons.append("연도 범위 외")

        chart_type = str(analysis.get("chart_type", "") or "")
        if chart_type in {"꺾은선", "막대", "영역", "복합"} and not _is_table_like_chart(analysis):
            # 그래프 축에서 읽은 값은 감사 시트에는 남기되, 본 데이터 자동 병합은 표보다 훨씬 보수적으로 한다.
            reasons.append("그래프 추정 후보")

        can_merge = not reasons
        if can_merge:
            return True, "high", []
        if _has_chart_value(item) and not any(r in reasons for r in ["참고자료/해외사례", "연도 범위 외"]):
            return False, "medium", reasons
        return False, "low", reasons

    def _can_merge_chart(self, analysis: dict, item: dict, target_sheet: str) -> bool:
        can_merge, _, _ = self._chart_merge_decision(analysis, item, target_sheet)
        return can_merge

    def _append_chart_observation(
        self,
        text_results: dict,
        analysis: dict,
        item: dict,
        target_sheet: str,
        merged: bool,
        confidence: str | None = None,
        reasons: list[str] | None = None,
    ):
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        reason_text = "; ".join(reasons or [])
        base_evidence = json.dumps(fields, ensure_ascii=False) if fields else analysis.get("summary", "")
        evidence_text = f"{base_evidence} | {reason_text}" if reason_text else base_evidence
        evidence = {
            "지자체명": analysis.get("municipality", ""),
            "페이지": analysis.get("page_number"),
            "대상시트": target_sheet,
            "그래프유형": analysis.get("chart_type", ""),
            "제목": analysis.get("title", ""),
            "단위": item.get("단위") or analysis.get("unit", ""),
            "항목": item.get("항목") or fields.get("용도") or fields.get("감축사업명") or "",
            "연도": item.get("연도"),
            "값": item.get("값"),
            "신뢰도": confidence or analysis.get("confidence", "low"),
            "반영여부": "반영" if merged else "검토",
            "근거": evidence_text,
        }
        text_results.setdefault("chart_observations", []).append(evidence)

    def _merge_image_results(self, text_results: dict, analyses: list[dict], municipality: str) -> dict:
        existing_ghg = text_results.get("ghg", [])
        existing_vehicle = text_results.setdefault("vehicle", [])
        existing_energy = text_results.setdefault("energy", [])
        existing_strategy = text_results.setdefault("strategy", [])

        for analysis in analyses:
            chart_rows = analysis.get("table", []) if analysis.get("type") == "chart_table" else []
            target_sheet = self._infer_target_sheet(analysis)
            if isinstance(chart_rows, list):
                for item in chart_rows:
                    if not isinstance(item, dict):
                        continue
                    fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                    merged_item = {**fields, **item}
                    can_merge, final_confidence, merge_reasons = self._chart_merge_decision(analysis, item, target_sheet)
                    self._append_chart_observation(
                        text_results, analysis, item, target_sheet,
                        can_merge, final_confidence, merge_reasons
                    )

                    if not can_merge:
                        continue
                    if target_sheet == "summary":
                        continue

                    if target_sheet == "vehicle":
                        existing_vehicle.append({
                            "지자체명": municipality,
                            "용도": merged_item.get("용도") or merged_item.get("항목") or "",
                            "차종": merged_item.get("차종") or "",
                            "대수": _parse_chart_value(merged_item.get("대수") or merged_item.get("값")),
                            "주행거리": _parse_chart_value(merged_item.get("주행거리")),
                        })
                        continue

                    if target_sheet == "energy":
                        existing_energy.append({
                            "지자체명": municipality,
                            "용도": merged_item.get("용도") or merged_item.get("항목") or "",
                            "석유_에너지유": _parse_chart_value(merged_item.get("석유_에너지유")),
                            "석유_LPG": _parse_chart_value(merged_item.get("석유_LPG")),
                            "석유_비에너지유": _parse_chart_value(merged_item.get("석유_비에너지유")),
                            "가스": _parse_chart_value(merged_item.get("가스")),
                            "전력": _parse_chart_value(
                                merged_item.get("전력")
                                if merged_item.get("전력") is not None
                                else (merged_item.get("값") if "전력" in str(merged_item.get("항목", "")) else None)
                            ),
                            "열": _parse_chart_value(merged_item.get("열")),
                            "신재생": _parse_chart_value(merged_item.get("신재생")),
                        })
                        continue

                    if target_sheet == "strategy":
                        year_key = _normalize_chart_year(merged_item.get("연도"))
                        yearly = {}
                        if year_key:
                            val = _parse_chart_value(merged_item.get("값"))
                            if val is not None:
                                yearly[year_key] = val
                        existing_strategy.append({
                            "지자체명": municipality,
                            "배출유형": merged_item.get("배출유형") or "직접배출",
                            "감축전략_부문": merged_item.get("감축전략_부문") or merged_item.get("항목") or "",
                            "감축사업명": merged_item.get("감축사업명") or analysis.get("title") or "",
                            "감축사업명_세부": merged_item.get("감축사업명_세부") or "",
                            "구분": merged_item.get("구분") or "공통",
                            "성과지표": merged_item.get("성과지표") or "",
                            "종류": merged_item.get("종류") if merged_item.get("종류") in config.STRATEGY_TYPES else "계획(지표)",
                            "연도별": yearly,
                            "근거": f"이미지 p.{analysis.get('page_number')}",
                        })
                        continue

                    year = item.get("연도")
                    value = item.get("값")
                    if year is None or value is None:
                        continue
                    year_key = _normalize_chart_year(year)
                    if year_key is None:
                        continue
                    kind = _infer_chart_kind(item, analysis)
                    label = item.get("항목") or analysis.get("title") or "기타"
                    unit = item.get("단위") or analysis.get("unit")
                    numeric = _parse_chart_value(value, unit)
                    if numeric is None:
                        continue

                    existing_ghg.append({
                        "지자체명": municipality,
                        "배출유형": "직접배출",
                        "종류": kind,
                        "부문": label,
                        "연도별": {year_key: numeric},
                        "근거": f"이미지 p.{analysis.get('page_number')}",
                    })

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
        text_results["vehicle"] = existing_vehicle
        text_results["energy"] = existing_energy
        text_results["strategy"] = existing_strategy
        return text_results

    def extract(self, pages: list[PageContent], text_results: dict, municipality: str) -> dict:
        raw_images = [
            (p, img)
            for p in pages
            for img in p.images
            if _is_relevant_image(img)
        ]

        if config.IMAGE_TRIAGE_ENABLED:
            triaged = []
            reference_filtered = 0
            for page, image in raw_images:
                item = _triage_image(page, image)
                if config.IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT:
                    is_reference, ref_hits = _has_reference_context(page, image, municipality)
                    if is_reference:
                        item["passed"] = False
                        item["score"] -= 20
                        item["reasons"].append("reference_context:" + ",".join(ref_hits[:4]))
                        reference_filtered += 1
                triaged.append(item)
            passed = [item for item in triaged if item["passed"]]
            passed.sort(key=lambda item: item["score"], reverse=True)

            self._triage_stats = {
                "raw": len(raw_images),
                "passed": len(passed),
                "filtered": len(raw_images) - len(passed),
                "min_score": config.IMAGE_TRIAGE_MIN_SCORE,
                "reference_filtered": reference_filtered,
            }

            pages_with_images = [(item["page"], item["image"]) for item in passed]
            print(
                "[에이전트2b 이미지분석] ChartQA-style triage: "
                f"후보 {len(raw_images)}개 → 통과 {len(pages_with_images)}개 "
                f"(필터 {len(raw_images) - len(pages_with_images)}개, "
                f"참고자료 {reference_filtered}개, 기준 {config.IMAGE_TRIAGE_MIN_SCORE})"
            )
            for item in passed[:5]:
                print(
                    f"  - triage 상위 후보: p{item['page'].page_number}, "
                    f"score={item['score']}, {', '.join(item['reasons'][:4])}"
                )
        else:
            pages_with_images = raw_images
            self._triage_stats = {
                "raw": len(raw_images),
                "passed": len(raw_images),
                "filtered": 0,
                "min_score": None,
            }

        # 상한 적용 (비용·시간 절감)
        if len(pages_with_images) > config.MAX_IMAGES:
            print(
                f"[에이전트2b 이미지분석] triage 통과 이미지 {len(pages_with_images)}개 중 "
                f"상위 {config.MAX_IMAGES}개만 분석"
            )
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

        return self._merge_image_results(text_results, analyses, municipality)

    def report(self) -> str:
        types: dict[str, int] = {}
        for a in self._image_results:
            t = a.get("type", "기타")
            types[t] = types.get(t, 0) + 1
        type_str = ", ".join(f"{k}: {v}개" for k, v in types.items()) or "없음"
        triage = self._triage_stats or {}
        triage_line = ""
        if triage:
            triage_line = (
                f"\n  - triage: 원본 {triage.get('raw', 0)}개 → "
                f"통과 {triage.get('passed', 0)}개 / 필터 {triage.get('filtered', 0)}개"
                f" / 참고자료 제외 {triage.get('reference_filtered', 0)}개"
            )
        return (
            f"[에이전트2b 이미지분석] 완료\n"
            f"  - 분석된 이미지: {len(self._image_results)}개\n"
            f"  - 유형별: {type_str}"
            f"{triage_line}"
        )
