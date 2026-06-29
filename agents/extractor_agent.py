"""
에이전트 2: 텍스트·표 추출 에이전트

carbon_guideline.md 기반 16개 시트 구조에 맞춰 문서를 추출합니다.
시트별로 관련 페이지를 라우팅하고, 배치 단위로 LLM에 전달합니다.
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
3. 모든 텍스트 필드는 한국어로 작성하세요.
4. 반드시 유효한 JSON만 반환하세요.
5. 표 제목, 표 캡션, 그림 제목, 절 제목 등 레이블·헤더 텍스트는 절대 데이터 값으로 사용하지 마세요.
6. 데이터 맥락이 명확하지 않은 숫자(출처 불명, 단위 불일치)는 null로 처리하세요."""


# ──────────────────────────────────────────────────────────────────────
# 16개 시트별 추출 설정
# ──────────────────────────────────────────────────────────────────────

_SHEET_CONFIGS = {
    "document_meta": {
        "keywords": ["기본계획", "계획기간", "기준연도", "목표연도", "수립", "탄소중립", "녹색성장", "조례"],
        "prompt": """이 배치에서 문서 메타정보를 추출하세요.

JSON 형식:
{"document_meta": [{"지자체명": "...", "지자체유형": "광역|기초|기타", "계획명": "...", "발간일": "YYYY-MM-DD or null", "발간기관": "...", "계획시작연도": 숫자, "계획종료연도": 숫자, "기준연도": 숫자, "목표연도": "2030,2050 등 쉼표 구분", "법적근거": "...", "점검보고서여부": true/false}]}
데이터가 없으면: {"document_meta": []}""",
    },

    "plan_overview": {
        "keywords": ["목적", "필요성", "법적 근거", "추진체계", "추진절차", "경과", "공청회", "위원회", "자문"],
        "prompt": """이 배치에서 계획 수립 개요(목적, 법적근거, 추진경과 등)를 추출하세요.

JSON 형식:
{"plan_overview": [{"지자체명": "...", "개요유형": "목적|법적근거|추진체계|추진경과|의견수렴", "항목명": "...", "항목값": "내용 텍스트", "일자": "YYYY-MM-DD or null", "이해관계자": "...", "관련법령_계획": "..."}]}
데이터가 없으면: {"plan_overview": []}""",
    },

    "regional_conditions": {
        "keywords": ["인구", "면적", "기온", "강수량", "GRDP", "차량등록", "에너지", "소비량", "전력",
                     "도시가스", "가구수", "건축물", "토지이용", "사업체", "종사자"],
        "prompt": """이 배치에서 지역 환경요인(자연, 인문·사회, 경제·산업, 에너지) 지표를 추출하세요.

주의:
- 지표범주는 자연환경/인문사회/경제산업/에너지 중 하나로 분류하세요.
- 연도별 시계열 데이터는 연도마다 별도 행으로 기록하세요.
- 단위는 원문 그대로 기록하세요(명, 대, km, TJ, TOE, GWh 등).

JSON 형식:
{"regional_conditions": [{"지자체명": "...", "지표범주": "자연환경|인문사회|경제산업|에너지", "지표세부범주": "...", "지표명": "...", "연도": 숫자, "값": 숫자or null, "단위": "...", "출처": "..."}]}
데이터가 없으면: {"regional_conditions": []}""",
    },

    "emissions_regional": {
        "keywords": ["온실가스", "배출량", "tCO2", "CO2eq", "직접배출", "간접배출", "흡수원",
                     "인벤토리", "GIR", "LULUCF", "연료연소", "산업공정"],
        "prompt": """이 배치에서 지역 전체 온실가스 배출·흡수 현황(GIR 통계 등)을 추출하세요.

주의:
- 배출범위: 직접배출/간접배출/흡수원 구분
- 부문: 에너지, 산업공정, 농업, LULUCF, 폐기물 등 원문 표기
- 연도별 데이터는 연도마다 별도 행으로 기록

JSON 형식:
{"emissions_regional": [{"지자체명": "...", "인벤토리출처": "GIR|자체산정|기타", "배출범위": "직접배출|간접배출|흡수원", "배출유형": "직접배출|간접배출|흡수원", "부문": "...", "세부부문": "...", "연도": 숫자, "배출량": 숫자or null, "단위": "tCO2eq|천톤CO2eq|백만톤CO2eq", "흡수원여부": true/false}]}
데이터가 없으면: {"emissions_regional": []}""",
    },

    "emissions_management": {
        "keywords": ["관리권한", "관리 권한", "건물", "수송", "농축산", "폐기물", "흡수원",
                     "가정", "상업", "공공", "도로수송"],
        "prompt": """이 배치에서 지자체 관리권한 인벤토리(건물/수송/농축산/폐기물/흡수원) 데이터를 추출하세요.

주의:
- 관리부문은 건물/수송/농축산/폐기물/흡수원/전환/산업/수소/합계 중 하나
- 직간접구분: direct/indirect/sink
- 합계포함여부: 해당 행이 합계에 포함되는지 (흡수원은 보통 제외)

JSON 형식:
{"emissions_management": [{"지자체명": "...", "인벤토리출처": "GIR|자체산정", "관리부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계", "세부부문": "가정|상업/공공|도로수송|...", "직간접구분": "direct|indirect|sink", "연도": 숫자, "배출량": 숫자or null, "단위": "tCO2eq|천톤CO2eq", "합계포함여부": true/false}]}
데이터가 없으면: {"emissions_management": []}""",
    },

    "emissions_forecast": {
        "keywords": ["전망", "BAU", "배출전망", "증가율", "시계열", "LEAP", "추정", "예측"],
        "prompt": """이 배치에서 온실가스 배출 전망(BAU 등) 데이터를 추출하세요.

주의:
- 시나리오: BAU/정책반영/추가조치 등
- 전망방법원문: 보고서가 명시한 전망 방법론 텍스트
- 연도별 데이터는 연도마다 별도 행

JSON 형식:
{"emissions_forecast": [{"지자체명": "...", "시나리오": "BAU|정책반영|추가조치", "전망방법코드": "stat_time_series|stat_regression|stat_growth_rate|bottom_up_accounting_LEAP|기타", "전망방법원문": "...", "부문": "...", "세부부문": "...", "연도": 숫자, "전망값": 숫자or null, "단위": "tCO2eq|천톤CO2eq", "주요가정": "..."}]}
데이터가 없으면: {"emissions_forecast": []}""",
    },

    "reduction_targets": {
        "keywords": ["감축목표", "감축률", "목표배출량", "목표감축량", "2030", "2050",
                     "NDC", "기준연도 대비", "40%", "50%"],
        "prompt": """이 배치에서 총괄·부문별 온실가스 감축목표를 추출하세요.

주의:
- 목표수준: 총괄/부문/세부부문
- 목표범위: 관리권한/관리권한+추가감축/지역전체
- 감축률(%) = (기준배출량 - 목표배출량) / 기준배출량 × 100

JSON 형식:
{"reduction_targets": [{"지자체명": "...", "목표수준": "총괄|부문|세부부문", "목표범위": "관리권한|관리권한+추가감축|지역전체", "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계|null", "기준연도": 숫자, "기준배출량": 숫자or null, "목표연도": 숫자, "배출전망": 숫자or null, "목표감축량": 숫자or null, "목표배출량": 숫자or null, "감축률": 숫자or null}]}
데이터가 없으면: {"reduction_targets": []}""",
    },

    "vision_strategy": {
        "keywords": ["비전", "전략", "추진방향", "핵심과제", "슬로건", "탄소중립 도시"],
        "prompt": """이 배치에서 비전·전략 정보를 추출하세요.

JSON 형식:
{"vision_strategy": [{"지자체명": "...", "비전문구": "2050 탄소중립 ... 등", "전략수준": "비전|추진전략|세부전략", "전략명": "...", "부문": "건물|수송|...|null", "설명": "...", "키워드": "..."}]}
데이터가 없으면: {"vision_strategy": []}""",
    },

    "mitigation_projects": {
        "keywords": ["감축사업", "세부사업", "핵심과제", "추진과제", "관리번호", "주관부서",
                     "성과지표", "공통사업", "특화사업"],
        "prompt": """이 배치에서 감축대책·세부사업 목록을 추출하세요.

주의:
- 사업유형: 정량/정성
- 한 사업의 개요, 부서, 지표를 한 행에 정리
- 관리번호가 없으면 null

JSON 형식:
{"mitigation_projects": [{"지자체명": "...", "관리번호": "...", "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소", "핵심과제": "...", "사업명": "...", "사업유형": "신규|계속|확대|변경|기타", "주관부서": "...", "협조부서": "...", "사업개요": "...", "성과지표명": "...", "성과지표단위": "...", "정량여부": true/false}]}
데이터가 없으면: {"mitigation_projects": []}""",
    },

    "annual_implementation": {
        "keywords": ["연차별", "이행계획", "이행목표", "연도별 목표", "물량", "2024", "2025",
                     "2026", "2027", "2028", "2029", "2030"],
        "prompt": """이 배치에서 연차별 이행계획(연도별 목표물량, 계획 텍스트)을 추출하세요.

주의:
- 초기 5년은 연 단위, 이후는 연 단위 또는 기간 단위
- 기간 표기("2029~2030")는 기간시작/기간종료로 분리

JSON 형식:
{"annual_implementation": [{"지자체명": "...", "관리번호": "...", "사업명": "...", "기간시작": 숫자or null, "기간종료": 숫자or null, "연도": 숫자or null, "연간계획": "...", "목표물량": 숫자or null, "목표단위": "...", "규제혁신계획": "...", "입법계획": "..."}]}
데이터가 없으면: {"annual_implementation": []}""",
    },

    "quantitative_reductions": {
        "keywords": ["감축량", "감축원단위", "모니터링", "활동량", "배출계수", "tCO2eq",
                     "원단위", "전기차", "태양광", "LED"],
        "prompt": """이 배치에서 정량사업 감축량 산정 데이터를 추출하세요.

주의:
- 감축원단위: 활동 1단위당 감축되는 온실가스량
- 예상감축량 = 활동량 × 감축원단위값
- 모니터링인자: 사업량 측정에 사용되는 활동자료

JSON 형식:
{"quantitative_reductions": [{"지자체명": "...", "관리번호": "...", "사업명": "...", "연도": 숫자, "모니터링인자": "...", "활동량": 숫자or null, "활동단위": "...", "감축원단위ID": "...", "감축원단위값": 숫자or null, "예상감축량": 숫자or null, "단위": "tCO2eq"}]}
데이터가 없으면: {"quantitative_reductions": []}""",
    },

    "financial_plan": {
        "keywords": ["재정", "투자", "예산", "국비", "시비", "도비", "민간", "백만원", "억원"],
        "prompt": """이 배치에서 재정투자 계획(부문별·재원별·연도별 예산)을 추출하세요.

JSON 형식:
{"financial_plan": [{"지자체명": "...", "계획구분": "총계|온실가스감축대책|대응기반강화|기타", "부문": "...", "사업명": "...", "재원구분": "합계|국비|도비|시비|민간", "연도": 숫자, "예산액": 숫자or null, "예산단위": "백만원|억원"}]}
데이터가 없으면: {"financial_plan": []}""",
    },

    "foundation_measures": {
        "keywords": ["적응", "공유재산", "국제협력", "교육", "홍보", "녹색성장", "청정에너지",
                     "정의로운 전환", "인력양성", "대응기반"],
        "prompt": """이 배치에서 기후위기 대응기반 강화대책을 추출하세요.

주의:
- 대응기반영역: 적응대책/공유재산/국제협력/교육소통/녹색성장/청정에너지/정의로운전환/인력양성

JSON 형식:
{"foundation_measures": [{"지자체명": "...", "대응기반영역": "적응대책|공유재산|국제협력|교육소통|녹색성장|청정에너지|정의로운전환|인력양성", "과제ID": "...", "과제명": "...", "정책방향": "...", "주요내용": "...", "대상": "...", "주관부서": "...", "기간": "..."}]}
데이터가 없으면: {"foundation_measures": []}""",
    },

    "governance_feedback": {
        "keywords": ["이행관리", "환류", "점검체계", "탄소중립이행책임관", "지방위원회",
                     "지원센터", "점검", "보고"],
        "prompt": """이 배치에서 이행관리·환류체계 정보를 추출하세요.

JSON 형식:
{"governance_feedback": [{"지자체명": "...", "거버넌스기구": "...", "역할": "...", "담당부서": "...", "절차단계": "...", "기한": "...", "산출물": "..."}]}
데이터가 없으면: {"governance_feedback": []}""",
    },

    "monitoring_performance": {
        "keywords": ["추진상황", "점검", "이행실적", "달성여부", "달성", "정상추진",
                     "지연", "미달성", "소요예산"],
        "prompt": """이 배치에서 추진상황 점검 실적 데이터를 추출하세요.

주의:
- 달성여부: 달성/정상추진/지연/미달성 중 하나
- 사업유형: 기존/변경/신규 중 하나

JSON 형식:
{"monitoring_performance": [{"지자체명": "...", "점검연도": 숫자, "부문": "...", "관리번호": "...", "사업명": "...", "연간계획": "...", "이행실적": "...", "소요예산": "...", "달성여부": "달성|정상추진|지연|미달성", "사업유형": "기존|변경|신규"}]}
데이터가 없으면: {"monitoring_performance": []}""",
    },

    "changes_actions": {
        "keywords": ["변경", "신규사업", "미달성", "조치계획", "변경사유", "지연사유", "개선"],
        "prompt": """이 배치에서 변경과제·미달성 조치 정보를 추출하세요.

JSON 형식:
{"changes_actions": [{"지자체명": "...", "점검연도": 숫자or null, "부문": "...", "관리번호": "...", "사업명": "...", "변경전": "...", "변경후": "...", "변경사유": "...", "지연미달성사유": "...", "조치계획": "..."}]}
데이터가 없으면: {"changes_actions": []}""",
    },
}


# ──────────────────────────────────────────────────────────────────────
# 문서 구조 라우팅용 가중 키워드
# ──────────────────────────────────────────────────────────────────────

_ROUTE_CONFIGS = {
    "document_meta": {
        "strong": ["기본계획", "계획기간", "기준연도", "목표연도", "수립 및 추진"],
        "weak": _SHEET_CONFIGS["document_meta"]["keywords"],
        "negative": ["해외", "부록"],
    },
    "plan_overview": {
        "strong": ["수립 배경", "법적 근거", "추진체계", "추진절차", "경과", "공청회"],
        "weak": _SHEET_CONFIGS["plan_overview"]["keywords"],
        "negative": ["해외", "부록3", "부록4"],
    },
    "regional_conditions": {
        "strong": ["지역 현황", "지역현황", "지역 여건", "인구 현황", "에너지 현황",
                   "자동차 등록", "경제 현황", "GRDP"],
        "weak": _SHEET_CONFIGS["regional_conditions"]["keywords"],
        "negative": ["감축사업", "이행계획", "해외"],
    },
    "emissions_regional": {
        "strong": ["온실가스 배출량", "배출량 현황", "지역 온실가스", "GIR", "인벤토리",
                   "직접배출량", "간접배출량", "LULUCF"],
        "weak": _SHEET_CONFIGS["emissions_regional"]["keywords"],
        "negative": ["재정투자", "예산", "설문", "해외"],
    },
    "emissions_management": {
        "strong": ["관리권한", "관리 권한", "관리권한 배출량", "관리권한 인벤토리"],
        "weak": _SHEET_CONFIGS["emissions_management"]["keywords"],
        "negative": ["재정투자", "예산", "설문"],
    },
    "emissions_forecast": {
        "strong": ["배출 전망", "배출전망", "BAU", "전망치", "전망방법"],
        "weak": _SHEET_CONFIGS["emissions_forecast"]["keywords"],
        "negative": ["재정투자", "예산"],
    },
    "reduction_targets": {
        "strong": ["감축목표", "목표배출량", "감축률", "2018년 대비", "NDC"],
        "weak": _SHEET_CONFIGS["reduction_targets"]["keywords"],
        "negative": ["재정투자", "예산", "해외"],
    },
    "vision_strategy": {
        "strong": ["비전", "추진전략", "기본방향", "핵심전략", "비전 체계"],
        "weak": _SHEET_CONFIGS["vision_strategy"]["keywords"],
        "negative": ["표 목차", "그림 목차", "부록"],
    },
    "mitigation_projects": {
        "strong": ["감축사업", "세부사업", "추진과제", "핵심과제", "관리카드", "사업목록"],
        "weak": _SHEET_CONFIGS["mitigation_projects"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "annual_implementation": {
        "strong": ["연차별", "이행계획", "연도별 목표", "단계별 이행"],
        "weak": _SHEET_CONFIGS["annual_implementation"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "quantitative_reductions": {
        "strong": ["감축량", "감축원단위", "모니터링인자", "활동량", "배출계수"],
        "weak": _SHEET_CONFIGS["quantitative_reductions"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "financial_plan": {
        "strong": ["재정투자", "투자계획", "예산", "재원별", "국비", "시비"],
        "weak": _SHEET_CONFIGS["financial_plan"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "foundation_measures": {
        "strong": ["대응기반", "적응대책", "공유재산", "정의로운 전환", "녹색성장 촉진"],
        "weak": _SHEET_CONFIGS["foundation_measures"]["keywords"],
        "negative": ["목차"],
    },
    "governance_feedback": {
        "strong": ["이행관리", "환류", "점검체계", "탄소중립이행책임관"],
        "weak": _SHEET_CONFIGS["governance_feedback"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "monitoring_performance": {
        "strong": ["추진상황 점검", "이행실적", "달성여부", "점검 결과"],
        "weak": _SHEET_CONFIGS["monitoring_performance"]["keywords"],
        "negative": ["목차", "해외"],
    },
    "changes_actions": {
        "strong": ["변경과제", "변경추진사업", "미달성 사유", "조치계획", "개선"],
        "weak": _SHEET_CONFIGS["changes_actions"]["keywords"],
        "negative": ["목차", "해외"],
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
    return any(kw in text for kw in keywords)


# 광역 지자체(특별시/광역시/특별자치시/도/특별자치도)는 형태가 명확해 오탐이 적다.
# 기초 지자체(시/군/구)는 일반어와 충돌이 잦아 광역명 뒤에서만 보조로 본다.
_WIDE_ADMIN_PATTERN = re.compile(
    r"[가-힣]{2,4}(?:특별자치도|특별자치시|특별시|광역시|도)\b"
)
_BASIC_ADMIN_PATTERN = re.compile(r"[가-힣]{2,5}(?:시|군|구)\b")
# '관리시', '도시', '제도' 등 행정구역이 아닌 흔한 오탐 접미 차단용.
_ADMIN_FALSE_POSITIVES = {"관리시", "도시", "제도", "정도", "현도", "보도", "고도", "용도", "강도", "온도", "속도", "각도", "태도", "법도"}


def _municipality_from_text(full_text: str) -> str:
    """
    LLM이 지자체명을 못 잡았을 때의 결정론적 fallback.

    문서 전반에서 가장 자주 등장하는 광역 행정구역명을 채택한다(보고서 본인
    지자체명이 압도적으로 많이 반복된다는 점을 이용). 광역명을 찾으면 바로 인접한
    기초 지자체명(시/군/구)이 함께 자주 나오면 '경기도 수원시' 형태로 결합한다.
    """
    text = full_text or ""
    wide_counts: dict[str, int] = {}
    for match in _WIDE_ADMIN_PATTERN.findall(text):
        if match in _ADMIN_FALSE_POSITIVES:
            continue
        wide_counts[match] = wide_counts.get(match, 0) + 1
    if not wide_counts:
        return ""
    wide = max(wide_counts, key=wide_counts.get)

    # '도'로 끝나는 광역이면 기초 지자체명을 보조로 결합 시도.
    if wide.endswith("도"):
        basic_counts: dict[str, int] = {}
        for match in _BASIC_ADMIN_PATTERN.findall(text):
            if match in _ADMIN_FALSE_POSITIVES or len(match) < 3:
                continue
            basic_counts[match] = basic_counts.get(match, 0) + 1
        if basic_counts:
            basic = max(basic_counts, key=basic_counts.get)
            # 충분히 반복되는 경우에만 결합(우발적 단일 등장 배제).
            if basic_counts[basic] >= 3:
                return f"{wide} {basic}"
    return wide


def _group_contiguous(pages: list[PageContent]) -> list[list[PageContent]]:
    """페이지번호가 연속인 페이지끼리 묶는다(입력은 페이지번호 오름차순 가정)."""
    runs: list[list[PageContent]] = []
    current: list[PageContent] = []
    for page in pages:
        if current and page.page_number == current[-1].page_number + 1:
            current.append(page)
        else:
            if current:
                runs.append(current)
            current = [page]
    if current:
        runs.append(current)
    return runs


def _chunk_run(run: list[PageContent], batch_size: int) -> list[list[PageContent]]:
    """
    하나의 연속 구간을 batch_size 이하로 자른다.
    단, 표가 페이지를 넘어가는 경우(연속 두 페이지가 모두 표를 가짐)에는 그 사이에서
    자르지 않도록 절단 지점을 한 칸 앞으로 당겨 표가 쪼개지는 것을 막는다.
    """
    chunks: list[list[PageContent]] = []
    i, n = 0, len(run)
    while i < n:
        end = min(i + batch_size, n)
        if end < n and run[end - 1].tables and run[end].tables and (end - 1) > i:
            end -= 1
        chunks.append(run[i:end])
        i = end
    return chunks


def _build_semantic_batches(
    pages: list[PageContent],
    batch_size: int,
) -> list[list[PageContent]]:
    """
    페이지를 의미 단위에 가깝게 배치로 묶는다.

    - 연속 구간(run)은 가능하면 통째로 한 배치에 유지(비연속 페이지가 한 배치에
      뒤섞여 LLM 맥락을 흐리는 것을 방지).
    - 작은 구간들은 batch_size 한도 내에서 함께 채운다.
    - batch_size를 넘는 긴 구간은 표 경계를 보호하며 잘게 나눈다.
    """
    if batch_size < 1:
        batch_size = 1
    batches: list[list[PageContent]] = []
    current: list[PageContent] = []
    for run in _group_contiguous(pages):
        if len(run) > batch_size:
            if current:
                batches.append(current)
                current = []
            batches.extend(_chunk_run(run, batch_size))
            continue
        if len(current) + len(run) > batch_size:
            if current:
                batches.append(current)
            current = list(run)
        else:
            current.extend(run)
    if current:
        batches.append(current)
    return batches


def _page_title_score(text: str, keywords: list[str]) -> int:
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


def _ubiquitous_weak_keywords(
    pages: list[PageContent],
    ratio: float = 0.4,
    min_pages: int = 8,
) -> frozenset[str]:
    """
    문서 전반(>ratio 비율의 페이지)에 등장해 변별력이 사실상 0인 weak 키워드 집합.

    예: '탄소중립', '녹색성장', '에너지'처럼 머리말/공통어로 모든 페이지에 찍히는 단어는
    라우팅 점수를 부풀려 거의 전 문서를 모든 시트에 배정하게 만든다. 이런 키워드의 +1
    가산만 제외한다. strong(+3) 신호는 절대 건드리지 않으므로 실제 데이터 페이지는
    여전히 선택된다(샤프닝은 선택만 좁히고 추출 입력 텍스트는 그대로 전체를 보냄).
    """
    n = len(pages)
    if n < min_pages:
        return frozenset()
    weak_all: set[str] = set()
    for cfg in _ROUTE_CONFIGS.values():
        weak_all.update(cfg["weak"])
    df: dict[str, int] = {kw: 0 for kw in weak_all}
    for page in pages:
        combined = f"{page.text or ''}\n" + "\n".join(page.tables or [])
        for kw in weak_all:
            if kw in combined:
                df[kw] += 1
    threshold = max(min_pages, int(n * ratio))
    return frozenset(kw for kw, count in df.items() if count >= threshold)


def _score_page_for_sheet(
    page: PageContent,
    sheet_key: str,
    ubiquitous_weak: frozenset[str] = frozenset(),
) -> int:
    cfg = _ROUTE_CONFIGS.get(sheet_key)
    if not cfg:
        return 0
    text = page.text or ""
    table_text = "\n".join(page.tables or [])
    combined = f"{text}\n{table_text}"

    weak = [kw for kw in cfg["weak"] if kw not in ubiquitous_weak]

    score = 0
    score += sum(3 for kw in cfg["strong"] if kw in combined)
    score += sum(1 for kw in weak if kw in combined)
    score += _page_title_score(text, cfg["strong"] + weak)

    if page.tables:
        score += 2
    score -= sum(2 for kw in cfg["negative"] if kw in combined)
    return score


def _route_pages_by_sheet(
    pages: list[PageContent],
    context_pages: int = config.DOCUMENT_ROUTE_CONTEXT_PAGES,
    min_score: int = config.DOCUMENT_ROUTE_MIN_SCORE,
) -> dict[str, list[PageContent]]:
    by_num = {p.page_number: p for p in pages}
    routed: dict[str, list[PageContent]] = {}
    max_pages_by_sheet = getattr(config, "DOCUMENT_ROUTE_MAX_PAGES", {})
    front_back_by_sheet = getattr(config, "DOCUMENT_ROUTE_FRONT_BACK_PAGES", {})
    max_page_num = max(by_num) if by_num else 0

    # 머리말/공통어로 편재해 변별력이 없는 weak 키워드를 점수에서 제외해 라우팅을 샤프닝.
    if getattr(config, "ROUTE_DROP_UBIQUITOUS_WEAK", True):
        ubiquitous_weak = _ubiquitous_weak_keywords(
            pages, ratio=getattr(config, "ROUTE_UBIQUITY_RATIO", 0.4)
        )
    else:
        ubiquitous_weak = frozenset()

    for sheet_key in _SHEET_CONFIGS:
        # 구조적으로 전면/후면부에만 존재하는 시트는 후보 페이지를 미리 좁힌다.
        front_back = front_back_by_sheet.get(sheet_key)
        if front_back:
            front_n, back_n = front_back
            candidate_pages = [
                page for page in pages
                if page.page_number <= front_n or page.page_number > max_page_num - back_n
            ]
        else:
            candidate_pages = pages

        scored_pages: list[tuple[int, int]] = []
        for page in candidate_pages:
            score = _score_page_for_sheet(page, sheet_key, ubiquitous_weak)
            if score >= min_score:
                scored_pages.append((score, page.page_number))

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
                key=lambda n: _score_page_for_sheet(by_num[n], sheet_key, ubiquitous_weak),
                reverse=True,
            )
            selected_nums = set(ranked_selected[:max_pages])

        routed[sheet_key] = [by_num[n] for n in sorted(selected_nums)]

    return routed


class ExtractorAgent:
    """에이전트 2: 텍스트·표 추출 에이전트 (가이드라인 기반 16개 시트)"""

    def __init__(self):
        self._raw_results: dict = {key: [] for key in _SHEET_CONFIGS}
        self._raw_results["municipality_name"] = ""

    def _extract_municipality_name(self, full_text: str) -> str:
        prompt = (
            "다음 텍스트는 지자체 탄소중립 기본계획 보고서의 일부입니다.\n"
            "보고서의 지자체명을 추출하세요 (예: '서울특별시', '경기도 수원시').\n"
            "JSON 형식으로만 반환: {\"municipality_name\": \"지자체명\"}\n\n"
            f"텍스트(앞 3000자):\n{full_text[:3000]}"
        )
        name = ""
        try:
            resp = llm_client.call_text(prompt, system="당신은 한국 행정구역 명칭 전문가입니다.")
            name = (llm_client.parse_json(resp).get("municipality_name") or "").strip()
        except llm_client.LLMQuotaExceededError:
            raise
        except llm_client.LLMCallError as exc:
            logger.warning("지자체명 LLM 추출 실패, 정규식 fallback 사용: %s", exc)

        if not name or name in {"알 수 없음", "미확인", "null", "None"}:
            fallback = _municipality_from_text(full_text)
            if fallback:
                logger.info("지자체명 정규식 fallback 적용: %s", fallback)
                return fallback
            return "알 수 없음"
        return name

    def _extract_sheet(
        self,
        sheet_key: str,
        batch_text: str,
        municipality: str,
        guideline_prompt: str = "",
    ) -> list:
        cfg = _SHEET_CONFIGS[sheet_key]

        # full-scan 모드에서는 모든 배치에서 모든 시트를 추출하므로, 관련 없는
        # (시트,배치) 조합을 거르기 위해 키워드 게이트를 적용한다.
        # 라우팅 모드에서는 이미 이 시트용으로 선별된 페이지만 들어오므로 게이트를
        # 적용하지 않는다(표만 있고 본문 키워드가 약한 페이지가 탈락하던 이중 필터 제거).
        if getattr(config, "FULL_DOCUMENT_SCAN", False) and not _has_keywords(batch_text, cfg["keywords"]):
            return []

        guideline_block = ""
        if guideline_prompt:
            guideline_block = (
                "\n\n[환경부 가이드라인 기반 보조 지침]\n"
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
            if not item.get("지자체명"):
                item["지자체명"] = municipality
        return [item for item in items if isinstance(item, dict)]

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

        if getattr(config, "FULL_DOCUMENT_SCAN", False):
            print("[에이전트2 텍스트추출] 전체 문서 스캔 모드")
            batches = _build_semantic_batches(pages, batch_size)
            for batch_num, batch in enumerate(batches, start=1):
                batch_text = _build_page_text(batch)
                page_nums = [p.page_number for p in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                for sheet_key in _SHEET_CONFIGS:
                    guideline_prompt = extraction_prompts.get(sheet_key, "")
                    items = self._extract_sheet(sheet_key, batch_text, municipality, guideline_prompt)
                    self._raw_results[sheet_key].extend(items)
                print(f"  [full] 배치 {batch_num:>2}/{len(batches)} ({page_range}) 완료")

            total = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list)}
            print(f"[에이전트2 텍스트추출] 완료. 누적: {total}")
            return self._raw_results

        routed_pages = _route_pages_by_sheet(pages)
        route_summary = {k: len(v) for k, v in routed_pages.items() if v}
        print(f"[에이전트2 텍스트추출] 문서 구조 라우팅 완료: {route_summary}")

        for sheet_key in _SHEET_CONFIGS:
            sheet_pages = routed_pages.get(sheet_key, [])
            if not sheet_pages:
                continue

            batches = _build_semantic_batches(sheet_pages, batch_size)
            for batch_num, batch in enumerate(batches, start=1):
                batch_text = _build_page_text(batch)
                page_nums = [p.page_number for p in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                guideline_prompt = extraction_prompts.get(sheet_key, "")
                items = self._extract_sheet(sheet_key, batch_text, municipality, guideline_prompt)
                self._raw_results[sheet_key].extend(items)
                status = f"{len(items)}건" if items else "추출 없음"
                print(f"  [{sheet_key}] 배치 {batch_num:>2}/{len(batches)} ({page_range}): {status}")

        total = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list) and v}
        print(f"[에이전트2 텍스트추출] 완료. 누적: {total}")
        return self._raw_results

    def report(self) -> str:
        counts = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list) and v}
        return (
            f"[에이전트2 텍스트추출] 추출 완료\n"
            f"  - 지자체명: {self._raw_results.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
