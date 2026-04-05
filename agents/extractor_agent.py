"""
에이전트 2: 텍스트·표 추출 에이전트

변경 사항:
- 5개 시트를 한 번에 추출 → 시트 유형별 개별 호출로 분리
  (출력 토큰 한도 초과로 JSON 잘림 방지)
- 키워드 프리필터: 관련 키워드가 없는 배치는 해당 유형 호출 건너뜀
  (불필요한 API 호출 절감)
"""

import logging

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
- 동일한 용도+차종 조합은 한 번만 기록하세요 (중복 제외).
- 대수와 주행거리는 각각의 단위(대, km)를 제거하고 숫자만 기록하세요.

JSON 형식: {"vehicle": [{"지자체명": "...", "용도": "승용|화물|버스|이륜|특수|승합", "차종": "경유|휘발유|LPG|전기|수소|하이브리드|내연기관", "대수": 숫자or null, "주행거리": 숫자or null}]}
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

    def _extract_sheet(self, sheet_key: str, batch_text: str, municipality: str) -> list:
        """단일 시트 유형 추출 (키워드 프리필터 포함)"""
        cfg = _SHEET_CONFIGS[sheet_key]

        # 키워드 없으면 API 호출 건너뜀
        if not _has_keywords(batch_text, cfg["keywords"]):
            return []

        full_prompt = f"지자체명: {municipality}\n\n[배치 텍스트]\n{batch_text}\n\n{cfg['prompt']}"
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
        extraction_prompts: dict[str, str],   # guideline_agent 호환용 (미사용)
        batch_size: int = config.BATCH_SIZE,
    ) -> dict:
        print("[에이전트2 텍스트추출] 지자체명 추출 중...")
        municipality = self._extract_municipality_name(full_text)
        self._raw_results["municipality_name"] = municipality
        print(f"[에이전트2 텍스트추출] 지자체명: {municipality}")

        batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
        total_batches = len(batches)
        print(f"[에이전트2 텍스트추출] 총 {len(pages)}페이지 / {total_batches}배치 / 시트별 분리 호출")

        for batch_num, batch in enumerate(batches, start=1):
            batch_text = _build_page_text(batch)
            page_range = f"p{batch[0].page_number}~{batch[-1].page_number}"
            batch_counts = {}

            for sheet_key in ["vehicle", "energy", "ghg", "strategy", "summary"]:
                items = self._extract_sheet(sheet_key, batch_text, municipality)
                self._raw_results[sheet_key].extend(items)
                batch_counts[sheet_key] = len(items)

            # 하나라도 추출된 경우만 출력 (빈 배치는 생략)
            non_zero = {k: v for k, v in batch_counts.items() if v > 0}
            status = str(non_zero) if non_zero else "키워드 없음, 건너뜀"
            print(f"  배치 {batch_num:>2}/{total_batches} ({page_range}): {status}")

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
