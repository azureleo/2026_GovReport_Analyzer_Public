"""
에이전트 2: 텍스트·표 추출 에이전트

carbon_guideline.md 기반 16개 시트 구조에 맞춰 문서를 추출합니다.
시트별로 관련 페이지를 라우팅하고, 배치 단위로 LLM에 전달합니다.
"""

import logging
import hashlib
import json
import re
import time

import config
from utils.pdf_reader import PageContent
from utils import llm_client
from utils.document_objects import (DocumentObject, build_document_objects, parse_html_table,
                                    render_table_object_chunks)
from utils.evidence_merge import merge_extraction_rows, normalize_evidence_ids
from utils.physical_objects import normalize_object_ids
from utils.object_routing import deduplicate_evidence_objects, target_sheets_for_object

logger = logging.getLogger(__name__)


class PartialExtractionError(ValueError):
    """Keep valid rows while recording malformed siblings as an incomplete batch."""

    def __init__(self, rows: list[dict], invalid_rows: list[dict]):
        super().__init__(f"extraction response contains {len(invalid_rows)} invalid row(s)")
        self.rows = rows
        self.invalid_rows = invalid_rows


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
- 전망범위: 지역전체/관리권한. 원문이 해당 전망의 범위를 명시할 때만 기록하고, 근거가 부족하면 null. 지자체명이나 페이지 번호로 추측하지 않는다.
- 전망방법원문: 보고서가 명시한 전망 방법론 텍스트
- 연도별 데이터는 연도마다 별도 행

JSON 형식:
{"emissions_forecast": [{"지자체명": "...", "시나리오": "BAU|정책반영|추가조치", "전망범위": "지역전체|관리권한|null", "전망방법코드": "stat_time_series|stat_regression|stat_growth_rate|bottom_up_accounting_LEAP|기타", "전망방법원문": "...", "부문": "...", "세부부문": "...", "연도": 숫자, "전망값": 숫자or null, "단위": "tCO2eq|천톤CO2eq", "주요가정": "..."}]}
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


def _score_page_for_sheet(page: PageContent, sheet_key: str) -> int:
    cfg = _ROUTE_CONFIGS.get(sheet_key)
    if not cfg:
        return 0
    text = page.text or ""
    table_text = "\n".join(page.tables or [])
    combined = f"{text}\n{table_text}"

    score = 0
    score += sum(3 for kw in cfg["strong"] if kw in combined)
    score += sum(1 for kw in cfg["weak"] if kw in combined)
    score += _page_title_score(text, cfg["strong"] + cfg["weak"])

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

    for sheet_key in _SHEET_CONFIGS:
        scored_pages: list[tuple[int, int]] = []
        for page in pages:
            score = _score_page_for_sheet(page, sheet_key)
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
                key=lambda n: _score_page_for_sheet(by_num[n], sheet_key),
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
        self.document_objects: list[DocumentObject] = []
        self.extraction_records: list[dict] = []
        self.merge_conflicts: list[dict] = []
        self._completed_object_chunks: set[str] = set()
        self._object_chunk_results: dict[str, list[dict]] = {}
        self.coverage_routes: list[dict] = []
        self._coverage_current_record = None

    def _record_extraction(self, record):
        if config.SOURCE_COVERAGE_MODE == "audit":
            record.setdefault("model_requested", False)
            record.setdefault("no_call_reason", "" if record["model_requested"] else
                              "success_restored" if record.get("reused") or record.get("status") == "reused" else
                              "retry_failed_only_not_selected" if record.get("status") == "unresolved" else
                              record.get("reason", record.get("status", "boundary_not_observed")))
        self.extraction_records.append(record)

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
        *,
        strict: bool = False,
        force: bool = False,
        reader=None,
    ) -> list:
        cfg = _SHEET_CONFIGS[sheet_key]

        if not force and not getattr(config, "FULL_DOCUMENT_SCAN", False) and not _has_keywords(batch_text, cfg["keywords"]):
            if self._coverage_current_record is not None:
                self._coverage_current_record.update(model_requested=False, no_call_reason="keyword_gate")
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
            '각 행에 출처페이지를 정수 또는 정수 배열로 기록하세요. 명시된 object_id/evidence_id가 있으면 객체ID/근거ID로 보존하세요.\n'
            f"{cfg['prompt']}"
        )
        if self._coverage_current_record is not None:
            self._coverage_current_record.update(model_requested=True, no_call_reason="")
        # The optional transport boundary is used by saved-response recovery;
        # the prompt, parser and strict response contract remain identical.
        resp = (reader or llm_client.call_text)(full_prompt, system=EXTRACTION_SYSTEM)
        parsed = llm_client.parse_json(resp)

        if not parsed or not isinstance(parsed, dict):
            if strict:
                raise ValueError("extraction response is not a JSON object")
            return []

        items = parsed.get(sheet_key, [])
        if not isinstance(items, list):
            if strict:
                raise ValueError(f"extraction response {sheet_key} is not an array")
            return []
        if strict and sheet_key not in parsed:
            raise ValueError(f"extraction response missing {sheet_key}")

        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("지자체명"):
                item["지자체명"] = municipality
        rows = [item for item in items if isinstance(item, dict)]
        invalid_rows = [{"index": index, "value": item} for index, item in enumerate(items, 1)
                        if not isinstance(item, dict)]
        if strict and invalid_rows:
            raise PartialExtractionError(rows, invalid_rows)
        return rows


    def _merge_rows(self, sheet_key: str, existing: list, incoming: list) -> list:
        start = len(self.merge_conflicts)
        merged = merge_extraction_rows(sheet_key, existing, incoming, self.merge_conflicts)
        state = getattr(self, "run_state", None)
        if state is not None and len(self.merge_conflicts) > start:
            state.record_conflicts(self.merge_conflicts[start:])
        return merged

    def _attach_sources(self, rows: list[dict], obj: DocumentObject, source: str, pages: list[int]):
        engine = str(obj.metadata.get("engine") or "pymupdf")
        is_ocr = source.startswith("ocr") or bool(obj.metadata.get("ocr_supplement"))
        eid = str(obj.metadata.get("evidence_id") or f"ev-{obj.object_id}")
        object_source_ids = normalize_object_ids(obj.object_id, obj.metadata.get("source_object_ids"),
            obj.metadata.get("alias_object_ids"), obj.metadata.get("ocr_original_object_id"))
        object_evidence_ids = normalize_evidence_ids(eid, obj.metadata.get("source_evidence_id"),
            obj.metadata.get("source_evidence_ids"))
        source_engines = list(normalize_object_ids(obj.metadata.get("source_engines"), engine))
        lineage = {"object_id": obj.object_id, "evidence_id": eid, "page_numbers": pages,
                   "bbox": list(obj.bbox) if obj.bbox else None, "engine": engine, "source": source,
                   "source_engines": source_engines}
        for field in ("ocr_source_file", "ocr_source_line", "ocr_document_trust", "ocr_selection_method",
                      "ocr_status", "physical_object_id", "canonical_object_id", "ocr_backend"):
            if obj.metadata.get(field) not in (None, ""):
                lineage[field] = obj.metadata[field]
        for row in rows:
            original_eids = normalize_evidence_ids(row.get("근거ID"), row.get("근거ID목록"),
                row.get("evidence_id"), row.get("evidence_ids"), row.get("source_evidence_ids"))
            original_oids = normalize_object_ids(row.get("객체ID"), row.get("근거객체ID"),
                row.get("object_id"), row.get("source_object_ids"))
            if original_eids:
                row.setdefault("원본근거ID", list(original_eids))
            if original_oids:
                row.setdefault("원본객체ID", list(original_oids))
            if not row.get("출처페이지"):
                row["출처페이지"] = pages
            row["객체ID"] = obj.object_id
            row["근거ID"] = eid
            row["추출원천"] = source
            row["추출엔진"] = engine
            row["_추출원천"] = "표객체" if obj.object_type == "table" else "본문청크"
            row["source_object_ids"] = list(normalize_object_ids(object_source_ids, original_oids))
            row["evidence_ids"] = normalize_evidence_ids(object_evidence_ids, original_eids)
            row["근거ID목록"] = row["evidence_ids"]
            row["source_evidence_ids"] = row["evidence_ids"]
            row["source_engines"] = list(normalize_object_ids(row.get("source_engines"), source_engines))
            for field in ("ocr_source_file", "ocr_source_line"):
                if field in lineage:
                    row[field] = lineage[field]
            row_lineage = {**lineage, "source_evidence_ids": row["evidence_ids"],
                           "source_object_ids": row["source_object_ids"]}
            row.setdefault("_extraction_sources", []).append(row_lineage)
            row["출처계보"] = json.dumps(row["_extraction_sources"], ensure_ascii=False, sort_keys=True)
            row["OCR보완여부"] = is_ocr
        return rows

    def _run_extraction_batch(self, sheet_key: str, text: str, municipality: str,
                              guideline_prompt: str, obj: DocumentObject, source: str,
                              pages: list[int]) -> list[dict]:
        """Persist complete/failed batches independently; a failure preserves other rows."""
        state = getattr(self, "run_state", None)
        request = json.dumps({"prompt": guideline_prompt, "source": source,
                              "municipality": municipality,
                              "schema_prompt": _SHEET_CONFIGS[sheet_key]["prompt"]},
                             ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256((sheet_key + text + request).encode()).hexdigest()
        batch_id = state.batch_id(kind="sheet", sheet_keys=[sheet_key], page_nums=pages,
                                  batch_text=text, request_fingerprint=fingerprint) if state else fingerprint[:32]
        source_table_cells = (max((len(row) for row in obj.rows), default=0) * len(obj.rows)
                              if obj.rows else int(obj.metadata.get("source_table_cells", 0)))
        if obj.object_type == "table" and "table_markdown:\n" in text:
            table_text = text.rsplit("table_markdown:\n", 1)[1]
            table_cells = sum(line.count("|") - 1 for line in table_text.splitlines()
                              if line.startswith("|") and not re.fullmatch(r"[|\s:\-]+", line))
        else:
            table_cells = source_table_cells
        record = {"batch_id": batch_id, "kind": "sheet", "sheet_key": sheet_key,
                  "page_nums": pages, "object_id": obj.object_id,
                  "evidence_id": obj.metadata.get("evidence_id"), "source": source,
                  "engine": obj.metadata.get("engine"), "input_chars": len(text),
                  "table_cells": table_cells, "source_table_cells": source_table_cells, "status": "pending"}
        if obj.metadata.get("table_measurement_errors"):
            record["table_measurement_errors"] = obj.metadata["table_measurement_errors"]
        if state:
            state.register_expected(batch_id, {"kind": "sheet", "sheet_keys": [sheet_key], "page_nums": pages})
            restored = state.restored_result(batch_id)
            if isinstance(restored, list):
                record.update(status="ok", rows=len(restored), reused=True)
                self._record_extraction(record)
                return restored
            if not state.should_execute(batch_id):
                record.update(status="unresolved", rows=0, error="batch was not attempted in retry-failed-only mode")
                self._record_extraction(record)
                return []
        started = time.perf_counter()
        if config.SOURCE_COVERAGE_MODE == "audit":
            record["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            self._coverage_current_record = record
        try:
            rows = self._extract_sheet(sheet_key, text, municipality, guideline_prompt,
                                       strict=True, force=obj.metadata.get("force_extract", False))
            rows = self._attach_sources(rows, obj, source, pages)
            record.update(status="ok", rows=len(rows), elapsed_seconds=time.perf_counter() - started)
            if state:
                state.record_batch(batch_id=batch_id, kind="sheet", sheet_keys=[sheet_key],
                                   page_nums=pages, status="ok", result=rows)
        except PartialExtractionError as exc:
            rows = self._attach_sources(exc.rows, obj, source, pages)
            record.update(status="partial" if rows else "parse_fail", rows=len(rows),
                          invalid_row_count=len(exc.invalid_rows), invalid_rows=exc.invalid_rows,
                          error=str(exc), elapsed_seconds=time.perf_counter() - started)
            if state:
                state.record_batch(batch_id=batch_id, kind="sheet", sheet_keys=[sheet_key],
                                   page_nums=pages, status=record["status"], result=rows, error=str(exc))
                saved = state.record_for(batch_id)
                if saved and isinstance(saved.get("result"), list):
                    rows = self._merge_rows(sheet_key, rows, saved["result"])
                    record.update(rows=len(rows), status=saved["status"], recovered=bool(saved.get("recovered")))
        except Exception as exc:
            logger.warning("Extraction batch %s failed: %s", batch_id, exc)
            record.update(status="parse_fail" if isinstance(exc, ValueError) else "call_fail",
                          rows=0, error=str(exc), elapsed_seconds=time.perf_counter() - started)
            rows = []
            if state:
                state.record_batch(batch_id=batch_id, kind="sheet", sheet_keys=[sheet_key],
                                   page_nums=pages, status=record["status"], result=[], error=str(exc))
                saved = state.record_for(batch_id)
                if saved and isinstance(saved.get("result"), list) and saved["result"]:
                    rows = saved["result"]
                    record.update(rows=len(rows), status=saved["status"], recovered=True)
        self._coverage_current_record = None
        self._record_extraction(record)
        return rows

    def extract_objects(self, objects: list[DocumentObject], municipality: str,
                        extraction_prompts: dict[str, str], *, sheet_keys=None) -> dict:
        """Re-extract selected changed objects, never a whole OCR page or document."""
        output = {key: [] for key in _SHEET_CONFIGS}
        for obj in sorted(objects, key=lambda item: (item.page_number, item.object_id)):
            if obj.metadata.get("index_reference") or obj.metadata.get("is_index_reference"):
                continue
            if obj.object_type not in {"table", "text"} or not (obj.rows or obj.text):
                self._record_extraction({"object_id": obj.object_id, "status": "no_content",
                                                "reason": "changed object has no extractable text/table"})
                continue
            targets = list(sheet_keys) if sheet_keys is not None else target_sheets_for_object(obj)
            targets = [key for key in targets if key in _SHEET_CONFIGS]
            if not targets:
                self._record_extraction({"object_id": obj.object_id, "status": "unrouted",
                                                "reason": "no sheet supported by object evidence"})
                continue
            try:
                if obj.object_type == "table":
                    chunks = render_table_object_chunks(obj,
                        rows_per_chunk=getattr(config, "TABLE_OBJECT_ROWS_PER_CHUNK", 20),
                        max_chars=getattr(config, "MAX_BATCH_CHARS", None),
                        max_cells=getattr(config, "MAX_BATCH_TABLE_CELLS", None))
                else:
                    cap = getattr(config, "MAX_BATCH_CHARS", None)
                    prefix = f"=== 페이지 {obj.page_number} ===\nobject_id: {obj.object_id}\nevidence_id: {obj.metadata.get('evidence_id', '')}\n"
                    width = max(1, cap - len(prefix)) if cap else max(1, len(obj.text))
                    if cap and len(prefix) >= cap:
                        raise ValueError("text object metadata exceeds configured character limit")
                    chunks = [prefix + obj.text[offset:offset + width] for offset in range(0, len(obj.text), width)]
            except ValueError as exc:
                self._record_extraction({"object_id": obj.object_id, "status": "batch_limit",
                                                "error": str(exc), "reason": "object retained without truncation"})
                state = getattr(self, "run_state", None)
                if state:
                    batch_id = state.batch_id(kind="sheet", sheet_keys=targets,
                                              page_nums=[obj.page_number], batch_text=obj.searchable_text(),
                                              request_fingerprint="batch_limit")
                    state.register_expected(batch_id, {"kind": "sheet", "sheet_keys": targets})
                    state.record_batch(batch_id=batch_id, kind="sheet", sheet_keys=targets,
                                       page_nums=[obj.page_number], status="unresolved", error=str(exc))
                continue
            engine = str(obj.metadata.get("engine") or "pymupdf")
            is_ocr = bool(obj.metadata.get("ocr_supplement") or obj.metadata.get("ocr_backend")) or engine not in {"pymupdf", "native", "kordoc"}
            source = ("ocr_" if is_ocr else "") + ("table_object" if obj.object_type == "table" else "text_chunk")
            obj.metadata["force_extract"] = True
            for sheet_key in targets:
                for chunk_index, text in enumerate(chunks, 1):
                    key = hashlib.sha256(json.dumps(
                        {"sheet": sheet_key, "text": text, "object_id": obj.object_id,
                         "municipality": municipality,
                         "prompt": extraction_prompts.get(sheet_key, ""),
                         "schema_prompt": _SHEET_CONFIGS[sheet_key]["prompt"]},
                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    if key in self._completed_object_chunks:
                        self._record_extraction({"object_id": obj.object_id, "sheet_key": sheet_key,
                                                        "chunk": chunk_index, "status": "reused",
                                                        "reason": "identical object/chunk/sheet request already completed"})
                        rows = self._object_chunk_results.get(key, [])
                    else:
                        rows = self._run_extraction_batch(sheet_key, text, municipality,
                            extraction_prompts.get(sheet_key, ""), obj, source, [obj.page_number])
                        if self.extraction_records[-1]["status"] == "ok":
                            self._completed_object_chunks.add(key)
                            self._object_chunk_results[key] = rows
                    output[sheet_key] = self._merge_rows(sheet_key, output[sheet_key], rows)
        return output

    def _extract_native_batch(self, sheet_key, batch, municipality, guideline_prompt):
        """Keep main batches by default; apply requested text/cell bounds when configured."""
        cap = getattr(config, "MAX_BATCH_CHARS", None)
        cell_cap = getattr(config, "MAX_BATCH_TABLE_CELLS", None)
        if getattr(config, "EXTRACTION_OBJECT_BATCHING_ENABLED", False) or cap or cell_cap:
            selected = [obj for obj in self.document_objects
                        if obj.page_number in {page.page_number for page in batch}
                        and obj.object_type in {"text", "table"} and (obj.rows or obj.text)]
            tables = [obj for obj in selected if obj.object_type == "table" and obj.rows and obj.bbox]
            def represented_by_table(obj):
                if obj.object_type != "text" or not obj.bbox or not obj.text.strip():
                    return False
                text = re.sub(r"\s+", " ", obj.text).strip()
                for table in tables:
                    if table.page_number != obj.page_number:
                        continue
                    inside = (table.bbox[0] <= obj.bbox[0] and table.bbox[1] <= obj.bbox[1]
                              and table.bbox[2] >= obj.bbox[2] and table.bbox[3] >= obj.bbox[3])
                    cells = re.sub(r"\s+", " ", " ".join(str(cell) for row in table.rows for cell in row)).strip()
                    if inside and text in cells:
                        return True
                return False
            selected = [obj for obj in selected if not represented_by_table(obj)]
            return self.extract_objects(selected, municipality, {sheet_key: guideline_prompt},
                                        sheet_keys=[sheet_key])[sheet_key]
        text = _build_page_text(batch)
        pages = [page.page_number for page in batch]
        oid = "batch-" + hashlib.sha256(text.encode()).hexdigest()[:20]
        table_cells = 0
        table_measurement_errors = []
        for page in batch:
            for html in page.tables:
                try:
                    table_cells += sum(len(row) for row in parse_html_table(html))
                except ValueError as exc:
                    table_measurement_errors.append({"page": page.page_number, "error": str(exc)})
        obj = DocumentObject(oid, "text", pages[0], 1, text=text,
                             metadata={"engine": "native", "evidence_id": f"ev-{oid}", "page_numbers": pages,
                                       "source_table_cells": table_cells,
                                       "table_measurement_errors": table_measurement_errors})
        return self._run_extraction_batch(sheet_key, text, municipality, guideline_prompt,
                                          obj, "text_chunk", pages)

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
        self.document_objects, _removed = deduplicate_evidence_objects(build_document_objects(pages))
        batch_size = max(1, int(batch_size))

        if getattr(config, "FULL_DOCUMENT_SCAN", False):
            if config.SOURCE_COVERAGE_MODE == "audit":
                self.coverage_routes = [{"sheet_key": key, "selected_pages": [p.page_number for p in pages],
                                         "not_selected_pages": [], "policy": "full_scan"} for key in _SHEET_CONFIGS]
            print("[에이전트2 텍스트추출] 전체 문서 스캔 모드")
            batches = [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]
            for batch_num, batch in enumerate(batches, start=1):
                batch_text = _build_page_text(batch)
                page_nums = [p.page_number for p in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                for sheet_key in _SHEET_CONFIGS:
                    guideline_prompt = extraction_prompts.get(sheet_key, "")
                    items = self._extract_native_batch(sheet_key, batch, municipality, guideline_prompt)
                    self._raw_results[sheet_key] = self._merge_rows(sheet_key, self._raw_results[sheet_key], items)
                print(f"  [full] 배치 {batch_num:>2}/{len(batches)} ({page_range}) 완료")

            total = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list)}
            print(f"[에이전트2 텍스트추출] 완료. 누적: {total}")
            return self._raw_results

        routed_pages = _route_pages_by_sheet(pages)
        if config.SOURCE_COVERAGE_MODE == "audit":
            selected_numbers = {key: {p.page_number for p in routed_pages.get(key, [])} for key in _SHEET_CONFIGS}
            self.coverage_routes = [{"sheet_key": key, "policy": "existing_page_routing",
                "selected_pages": [p.page_number for p in routed_pages.get(key, [])],
                "not_selected_pages": [p.page_number for p in pages if p.page_number not in selected_numbers[key]],
                "no_call_reason": "not_selected_by_existing_route_threshold_context_or_cap"} for key in _SHEET_CONFIGS]
        route_summary = {k: len(v) for k, v in routed_pages.items() if v}
        print(f"[에이전트2 텍스트추출] 문서 구조 라우팅 완료: {route_summary}")

        for sheet_key in _SHEET_CONFIGS:
            sheet_pages = routed_pages.get(sheet_key, [])
            if not sheet_pages:
                continue

            batches = [sheet_pages[i:i + batch_size] for i in range(0, len(sheet_pages), batch_size)]
            for batch_num, batch in enumerate(batches, start=1):
                batch_text = _build_page_text(batch)
                page_nums = [p.page_number for p in batch]
                page_range = f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                guideline_prompt = extraction_prompts.get(sheet_key, "")
                items = self._extract_native_batch(sheet_key, batch, municipality, guideline_prompt)
                self._raw_results[sheet_key] = self._merge_rows(sheet_key, self._raw_results[sheet_key], items)
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
