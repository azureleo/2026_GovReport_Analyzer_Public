"""
에이전트 2: 텍스트·표 추출 에이전트

변경 사항:
- 5개 시트를 한 번에 추출 → 시트 유형별 개별 호출로 분리
  (출력 토큰 한도 초과로 JSON 잘림 방지)
- 키워드 프리필터: 관련 키워드가 없는 배치는 해당 유형 호출 건너뜀
  (불필요한 API 호출 절감)
"""

import logging
import re

import config
from utils.pdf_reader import PageContent
from utils import llm_client

logger = logging.getLogger(__name__)


EXTRACTION_SYSTEM = """당신은 한국 지자체 탄소중립 녹색성장 기본계획 보고서에서
구조화된 정보를 추출하는 전문 분석가입니다.

규칙:
1. 숫자는 반드시 숫자형(int/float)으로 반환하세요. 단위(tCO2eq, 대 등)는 제외.
2. 값이 없거나 확인 불가인 경우 null로 처리하세요.
3. 연도별 데이터는 {"2018": 값, ...} 형식으로 작성하세요.
4. 모든 텍스트 필드는 한국어로 작성하세요.
5. 반드시 유효한 JSON만 반환하세요.
6. 표 제목, 표 캡션, 그림 제목, 절 제목 등 레이블·헤더 텍스트는 절대 데이터 값으로 사용하지 마세요.
7. 데이터 맥락이 명확하지 않은 숫자(출처 불명, 단위 불일치)는 null로 처리하세요."""


# 시트별 설정: 키워드(프리필터), 프롬프트, JSON 스키마
_SHEET_CONFIGS = {
    "vehicle": {
        "keywords": ["자동차", "차량", "승용", "화물", "버스", "이륜", "등록대수", "주행거리"],
        "prompt": """이 배치에서 '용도별 자동차 현황' 데이터를 추출하세요.

주의사항:
- 용도명은 반드시 다음 중 하나로 통일하세요: 승용, 화물, 버스, 이륜, 특수, 승합
  (승용차→승용, 화물차→화물, 이륜차→이륜 으로 표준화)
- 원문이 용도별이 아니라 연료별 전체 자동차 등록대수만 제시하면 용도는 "전체"로 기록하세요.
- 동일한 용도+차종 조합은 한 번만 기록하세요 (중복 제외).
- 대수와 주행거리는 각각의 단위(대, km)를 제거하고 숫자만 기록하세요.
- 전기차 성장률, 보급 목표, 추진계획, 투자계획, 감축효과 표의 숫자는 자동차 현황 대수로 사용하지 마세요.

JSON 형식: {"vehicle": [{"지자체명": "...", "용도": "전체|승용|화물|버스|이륜|특수|승합", "차종": "경유|휘발유|LPG|전기|수소|하이브리드|내연기관", "대수": 숫자or null, "주행거리": 숫자or null}]}
데이터가 없으면: {"vehicle": []}""",
    },
    "energy": {
        "keywords": ["에너지", "소비량", "석유", "도시가스", "전력", "신재생", "열에너지", "LNG", "TJ", "toe"],
        "prompt": """이 배치에서 '용도별 에너지 소비 현황' 데이터를 추출하세요.

주의사항:
- 용도명은 최소 단위(가정, 상업, 공공, 산업, 수송 등)로 분리하여 기록하세요.
- '가정·상업', '가정/상업' 같은 합산 용도와 '가정', '상업' 개별 용도가 모두 존재하면 개별 세분값만 기록하세요 (합산값 제외).
- '도로수송', '비도로수송'은 '수송'으로 통일하세요.
- 에너지량 단위(TJ, toe, GWh 등)는 제거하고 숫자만 기록하세요.
- 에너지원별 총량 표는 용도를 "전체"로 기록하세요. 총량을 가정/상업/공공 등 특정 용도에 임의 배정하지 마세요.
- 변화율, 비율, 전망, 투자계획, 감축효과 표의 숫자는 에너지 현황 소비량으로 사용하지 마세요.

JSON 형식: {"energy": [{"지자체명": "...", "용도": "가정|상업|공공|산업|수송 등", "석유_에너지유": 숫자or null, "석유_LPG": 숫자or null, "석유_비에너지유": 숫자or null, "가스": 숫자or null, "전력": 숫자or null, "열": 숫자or null, "신재생": 숫자or null}]}
데이터가 없으면: {"energy": []}""",
    },
    "ghg": {
        "keywords": ["온실가스", "배출량", "tCO2", "CO2eq", "탄소", "직접배출", "간접배출", "흡수원", "NDC", "BAU", "감축목표"],
        "prompt": """이 배치에서 '온실가스 배출량(현황·전망·목표)' 데이터를 추출하세요.

주의사항:
- 부문명은 반드시 다음 목록 중 하나만 사용하세요: 건물, 수송, 농축산, 폐기물, 흡수원, 전환, 산업, 수소, 합계, 기타
  표 제목, 캡션, IPCC 코드(예: 1A 연료연소, 4A 폐기물매립) 등 목록 외 값은 절대 부문명으로 사용하지 마세요.
- 연도별 값은 반드시 온실가스 배출량(tCO2eq 단위) 수치만 기록하세요.
  예산(원, 백만원), 면적(m², ha), 개수, 비율(%) 등 다른 단위의 숫자는 절대 포함하지 마세요.
- 같은 표에서 합계 행과 부문별 행이 모두 있을 때는 부문별 행을 우선하고 합계 행도 함께 포함하세요.
- IPCC 분류코드 기반 표는 건너뛰어도 됩니다.

JSON 형식: {"ghg": [{"지자체명": "...", "배출유형": "직접배출|간접배출|흡수원", "종류": "현황|전망|목표", "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계|기타", "연도별": {"2023": 숫자, "2030": 숫자}}]}
※ 연도별에는 실제 숫자값이 있는 연도만 포함하세요. null인 연도는 키 자체를 생략하세요.
데이터가 없으면: {"ghg": []}""",
    },
    "strategy": {
        "keywords": ["감축", "사업", "이행", "계획", "실적", "지표", "예산", "공통", "특화", "ZEB", "전기차", "태양광"],
        "prompt": """이 배치에서 '부문별 감축 사업 계획·실적' 데이터를 추출하세요.

주의사항:
- 감축사업명은 반드시 한글 사업 명칭을 사용하세요.
  영문 코드(B1, M1, E2 등)로만 표기된 경우, 해당 코드의 한글 사업명을 찾아 함께 기록하세요 (예: "B1 기존 건물 ZEB 전환").
  코드 단독('B1')과 코드+한글명('B1 기존 건물 ZEB 전환')이 같은 사업이면 한글명이 포함된 것만 기록하세요.
- 표 제목이나 절 제목은 감축사업명으로 사용하지 마세요.
- 연도별 값의 단위를 종류에 맞게 일관되게 기록하세요:
  계획(감축량): tCO2eq, 계획(예산): 백만원, 계획(지표)/실적(지표): 해당 지표 단위 숫자.
- 연도는 실제로 값이 있는 연도만 포함하세요 (빈 연도는 생략).

JSON 형식: {"strategy": [{"지자체명": "...", "배출유형": "직접배출|간접배출", "감축전략_부문": "전환|산업|건물|수송|농축산|폐기물|흡수원|수소", "감축사업명": "...", "감축사업명_세부": "...", "구분": "공통|특화", "성과지표": "...", "종류": "계획(지표)|계획(감축량)|계획(예산)|실적(지표)|실적(감축량)|실적(예산)", "연도별": {"2023": 값, "2024": 값, "2025": 값, "2030": 값}}]}
※ 연도별에는 실제 숫자가 있는 연도만 포함. null인 연도는 키 자체를 생략.
데이터가 없으면: {"strategy": []}""",
    },
    "summary": {
        "keywords": ["목표", "전략", "핵심", "비전", "방향", "개요", "현황", "배출유형", "탄소중립"],
        "prompt": """이 배치에서 지자체 탄소중립 계획의 핵심 요약 정보를 추출하세요.

추출 대상 항목은 아래 5개뿐입니다. 반드시 항목명을 정확히 사용하세요:
  1. "배출유형" - 직접배출/간접배출/흡수원 등 배출 분류 체계 설명
  2. "감축목표(2030)" - 2030년 온실가스 감축 목표 수치 및 기준연도
  3. "감축목표(2035)" - 2035년 온실가스 감축 목표 수치 및 기준연도
  4. "핵심전략" - 주요 감축 전략 방향 (부문별 전략 요약)
  5. "배출유형-전략 간 연결성" - 배출 부문과 감축 전략의 연계 내용

주의: 항목 필드에는 위 5개 중 하나의 정확한 이름만 넣으세요. 파이프(|)나 다른 내용을 항목 필드에 넣지 마세요.
각 항목은 별도의 JSON 객체로 작성하세요.

JSON 형식:
{"summary": [
  {"지자체명": "서울특별시", "항목": "배출유형", "내용": "직접배출+간접배출로 구성...", "근거": "보고서 p.XX"},
  {"지자체명": "서울특별시", "항목": "감축목표(2030)", "내용": "2018년 대비 40% 감축...", "근거": "보고서 p.XX"}
]}
데이터가 없으면: {"summary": []}""",
    },
}


# 문서 구조 라우팅용 가중 키워드.
# Extractor 호출 전에 페이지 단위로 점수를 매겨 "정말 관련 있는 페이지"만 시트별 배치에 넣는다.
_ROUTE_CONFIGS = {
    "vehicle": {
        "strong": ["자동차 등록", "차량 등록", "등록대수", "용도별 자동차", "차종별", "주행거리"],
        "weak": _SHEET_CONFIGS["vehicle"]["keywords"],
        "negative": ["설문", "자문회의", "해외", "IPCC"],
    },
    "energy": {
        "strong": ["최종에너지", "에너지 소비", "에너지사용량", "에너지원별", "부문별 에너지", "TJ", "toe"],
        "weak": _SHEET_CONFIGS["energy"]["keywords"],
        "negative": ["예산", "재정투자", "설문"],
    },
    "ghg": {
        "strong": [
            "온실가스 배출량", "배출량 현황", "배출량 전망", "감축목표",
            "BAU", "NDC", "인벤토리", "관리권한 배출량", "tCO2", "CO2eq",
        ],
        "weak": _SHEET_CONFIGS["ghg"]["keywords"],
        "negative": ["재정투자", "예산", "설문", "교육 프로그램", "COP28"],
    },
    "strategy": {
        "strong": [
            "부문별 감축", "감축사업", "이행계획", "세부사업", "감축량",
            "연차별", "성과지표", "공통사업", "특화사업", "추진계획",
        ],
        "weak": _SHEET_CONFIGS["strategy"]["keywords"],
        "negative": ["목차", "표 목차", "그림 목차", "설문"],
    },
    "summary": {
        "strong": ["비전", "추진전략", "기본방향", "감축목표", "핵심전략", "계획의 개요"],
        "weak": _SHEET_CONFIGS["summary"]["keywords"],
        "negative": ["표 목차", "그림 목차", "참고문헌", "부록"],
    },
}


_HEADING_PATTERN = re.compile(
    r"^\s*((제\s*\d+\s*[장절])|(\d+(\.\d+){0,3})|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s]))"
)


def _build_page_text(pages: list[PageContent]) -> str:
    parts = []
    for page in pages:
        part = f"=== 페이지 {page.page_number} ===\n{page.text}"
        if page.tables:
            part += "\n\n[표 데이터]\n" + "\n\n".join(page.tables)
        parts.append(part)
    return "\n\n".join(parts)


def _has_keywords(text: str, keywords: list[str]) -> bool:
    """배치 텍스트에 해당 유형의 키워드가 하나라도 있는지 확인"""
    return any(kw in text for kw in keywords)


def _page_title_score(text: str, keywords: list[str]) -> int:
    """페이지 앞부분/제목형 라인에 키워드가 있으면 가중치를 더 준다."""
    score = 0
    head = text[:1200]
    for line in head.splitlines()[:18]:
        line = line.strip()
        if not line:
            continue
        is_heading = bool(_HEADING_PATTERN.match(line)) or len(line) <= 36
        if is_heading and any(kw in line for kw in keywords):
            score += 2
    return score


def _score_page_for_sheet(page: PageContent, sheet_key: str) -> int:
    """페이지가 특정 시트 추출에 얼마나 관련 있는지 결정론적으로 점수화."""
    cfg = _ROUTE_CONFIGS[sheet_key]
    text = page.text or ""
    table_text = "\n".join(page.tables or [])
    combined = f"{text}\n{table_text}"

    score = 0
    score += sum(3 for kw in cfg["strong"] if kw in combined)
    score += sum(1 for kw in cfg["weak"] if kw in combined)
    score += _page_title_score(text, cfg["strong"] + cfg["weak"])

    if page.tables:
        score += 2
    if sheet_key == "ghg" and re.search(r"\b20(1[8-9]|2[0-9]|3[0-4])\b", combined):
        score += 1
    if sheet_key == "strategy" and any(t in combined for t in ["계획(감축량)", "계획(예산)", "계획(지표)", "실적"]):
        score += 2
    if sheet_key == "summary" and page.page_number <= 40:
        score += 1

    score -= sum(2 for kw in cfg["negative"] if kw in combined)
    return score


def _route_pages_by_sheet(
    pages: list[PageContent],
    context_pages: int = config.DOCUMENT_ROUTE_CONTEXT_PAGES,
    min_score: int = config.DOCUMENT_ROUTE_MIN_SCORE,
) -> dict[str, list[PageContent]]:
    """
    전체 문서를 시트별 후보 페이지로 라우팅.

    점수가 높은 페이지와 그 앞뒤 일부 문맥 페이지만 LLM에 전달하여
    토큰 낭비와 관련 없는 숫자 혼입을 줄인다.
    """
    by_num = {p.page_number: p for p in pages}
    routed: dict[str, list[PageContent]] = {}

    max_pages_by_sheet = getattr(config, "DOCUMENT_ROUTE_MAX_PAGES", {})

    for sheet_key in _SHEET_CONFIGS:
        scored_pages: list[tuple[int, int]] = []
        for page in pages:
            score = _score_page_for_sheet(page, sheet_key)
            if score >= min_score:
                scored_pages.append((score, page.page_number))

        # 점수가 높은 핵심 페이지를 먼저 고르고, 이후 앞뒤 문맥 페이지를 붙인다.
        # 단, summary처럼 광범위하게 매칭되는 시트는 상한을 두어 반복 호출을 막는다.
        scored_pages.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        max_pages = max_pages_by_sheet.get(sheet_key)
        anchor_limit = max_pages if isinstance(max_pages, int) and max_pages > 0 else None
        anchor_nums = [page_num for _, page_num in scored_pages[:anchor_limit]]

        selected_nums: set[int] = set()
        for page_num in anchor_nums:
            for n in range(page_num - context_pages, page_num + context_pages + 1):
                if n in by_num:
                    selected_nums.add(n)

        if isinstance(max_pages, int) and max_pages > 0 and len(selected_nums) > max_pages:
            ranked_selected = sorted(
                selected_nums,
                key=lambda n: _score_page_for_sheet(by_num[n], sheet_key),
                reverse=True,
            )
            selected_nums = set(ranked_selected[:max_pages])

        routed[sheet_key] = [by_num[n] for n in sorted(selected_nums)]

    return routed


class ExtractorAgent:
    """에이전트 2: 텍스트·표 추출 에이전트 (시트별 분리 호출)"""

    def __init__(self):
        self._raw_results: dict = {
            "vehicle": [], "energy": [], "ghg": [],
            "strategy": [], "summary": [], "municipality_name": "",
        }

    def _extract_municipality_name(self, full_text: str) -> str:
        prompt = (
            "다음 텍스트는 지자체 탄소중립 기본계획 보고서의 일부입니다.\n"
            "보고서의 지자체명을 추출하세요 (예: '서울특별시', '경기도 수원시').\n"
            "JSON 형식으로만 반환: {\"municipality_name\": \"지자체명\"}\n\n"
            f"텍스트(앞 3000자):\n{full_text[:3000]}"
        )
        resp = llm_client.call_text(prompt, system="당신은 한국 행정구역 명칭 전문가입니다.")
        return llm_client.parse_json(resp).get("municipality_name", "알 수 없음")

    def _extract_sheet(
        self,
        sheet_key: str,
        batch_text: str,
        municipality: str,
        guideline_prompt: str = "",
    ) -> list:
        """단일 시트 유형 추출 (키워드 프리필터 포함)"""
        cfg = _SHEET_CONFIGS[sheet_key]

        # 키워드 없으면 API 호출 건너뜀
        if not _has_keywords(batch_text, cfg["keywords"]):
            return []

        guideline_block = ""
        if guideline_prompt:
            guideline_block = (
                "\n\n[환경부 HWP 가이드라인 기반 보조 지침]\n"
                f"{guideline_prompt}\n"
                "위 지침과 배치 텍스트가 충돌할 경우, 배치 텍스트의 실제 수치와 단위를 우선하되 "
                "필드 구성과 분류 체계는 가이드라인을 따르세요.\n"
            )

        full_prompt = (
            f"지자체명: {municipality}"
            f"{guideline_block}\n\n"
            f"[배치 텍스트]\n{batch_text}\n\n"
            f"{cfg['prompt']}"
        )
        resp = llm_client.call_text(full_prompt, system=EXTRACTION_SYSTEM)
        parsed = llm_client.parse_json(resp)

        if not parsed or not isinstance(parsed, dict):
            return []

        items = parsed.get(sheet_key, [])
        if not isinstance(items, list):
            return []

        for item in items:
            if not isinstance(item, dict):
                continue
            # 지자체명 보정
            if not item.get("지자체명"):
                item["지자체명"] = municipality
            # null 연도 제거 (출력 토큰 절감 + 잘린 JSON 영향 최소화)
            if "연도별" in item and isinstance(item["연도별"], dict):
                item["연도별"] = {
                    k: v for k, v in item["연도별"].items() if v is not None
                }
        return items

    def extract(
        self,
        pages: list[PageContent],
        full_text: str,
        extraction_prompts: dict[str, str],
        batch_size: int = config.BATCH_SIZE,
    ) -> dict:
        print("[에이전트2 텍스트추출] 지자체명 추출 중...")
        municipality = self._extract_municipality_name(full_text)
        self._raw_results["municipality_name"] = municipality
        print(f"[에이전트2 텍스트추출] 지자체명: {municipality}")

        routed_pages = _route_pages_by_sheet(pages)
        route_summary = {k: len(v) for k, v in routed_pages.items()}
        print(f"[에이전트2 텍스트추출] 문서 구조 라우팅 완료(시트별 후보 페이지 수): {route_summary}")

        total_candidate_pages = sum(route_summary.values())
        print(
            f"[에이전트2 텍스트추출] 총 {len(pages)}페이지 → "
            f"시트별 후보 페이지 합계 {total_candidate_pages}개(시트 간 중복 포함) / 시트별 분리 호출"
        )

        for sheet_key in ["vehicle", "energy", "ghg", "strategy", "summary"]:
            sheet_pages = routed_pages.get(sheet_key, [])
            if not sheet_pages:
                print(f"  [{sheet_key}] 후보 페이지 없음, 건너뜀")
                continue

            batches = [sheet_pages[i:i + batch_size] for i in range(0, len(sheet_pages), batch_size)]
            total_batches = len(batches)

            for batch_num, batch in enumerate(batches, start=1):
                batch_text = _build_page_text(batch)
                page_nums = [p.page_number for p in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                guideline_prompt = extraction_prompts.get(sheet_key, "")
                items = self._extract_sheet(sheet_key, batch_text, municipality, guideline_prompt)
                self._raw_results[sheet_key].extend(items)
                status = f"{len(items)}건" if items else "추출 없음"
                print(f"  [{sheet_key}] 배치 {batch_num:>2}/{total_batches} ({page_range}): {status}")

        total = {k: len(self._raw_results[k]) for k in ["vehicle", "energy", "ghg", "strategy", "summary"]}
        print(f"[에이전트2 텍스트추출] 완료. 누적: {total}")
        return self._raw_results

    def report(self) -> str:
        counts = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list)}
        return (
            f"[에이전트2 텍스트추출] 추출 완료\n"
            f"  - 지자체명: {self._raw_results.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
