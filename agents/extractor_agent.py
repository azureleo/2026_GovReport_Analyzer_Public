"""
에이전트 2: 텍스트·표 추출 에이전트

carbon_guideline.md 기반 16개 시트 구조에 맞춰 문서를 추출합니다.
시트별로 관련 페이지를 라우팅하고, 배치 단위로 LLM에 전달합니다.
"""
# noqa: SIZE_OK — 16개 시트 추출 프롬프트·라우팅 계약을 보존하는 기존 모놀리식 extractor. WP8은 공개 래퍼만 추가.

import hashlib
import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Sequence

import config
from utils.pdf_reader import PageContent
from utils.document_objects import DocumentObject, build_document_objects, render_table_object_chunks
from utils import llm_client
from utils.parallel import parallel_map, parallel_map_collect
from utils.run_state import RunState, merge_rows_stably
from utils.reading_pipeline import instruction as reading_instruction
from utils.text_optimization import TextPolicy, trace_task, digest, CURRENT_TRACE, policy_snapshot as text_policy_snapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BatchRecord:
    """추출 배치 원장 기록."""

    sheet_key: str
    page_nums: list[int]
    status: str
    rows: int
    error: str = ""
    batch_id: str = ""
    source: str = "live"
    recovered: bool = False


class BatchParseError(llm_client.LLMCallError):
    """응답은 도착했지만 JSON/시트 스키마가 유효하지 않은 배치."""


class ClusterPartialParseError(BatchParseError):
    def __init__(self, result, failed):
        super().__init__("누락/잘못된 시트 응답: " + ", ".join(failed))
        self.result = result
        self.failed = failed


_PROVENANCE_INSTRUCTION = (
    '각 행에 "출처페이지" 필드를 추가하고, 그 행의 근거가 된 페이지 번호'
    '(=== 페이지 N === 마커 기준)를 정수 또는 정수 배열로 기록하세요. 확실하지 않으면 null.'
)
_PAGE_MARKER_RE = re.compile(r"===\s*페이지\s*(\d+)\s*===")


def _page_nums_from_batch_text(batch_text: str) -> list[int]:
    """배치 텍스트의 페이지 마커에서 페이지 번호를 추출한다."""
    return [int(match) for match in _PAGE_MARKER_RE.findall(batch_text or "")]


def _page_range_label(page_nums: list[int]) -> str:
    if not page_nums:
        return "p?"
    return f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"


def _attach_row_context(row: dict, municipality: str, page_nums: list[int]) -> dict:
    """행에 지자체명과 출처페이지 fallback을 결정론적으로 보강한다."""
    if not row.get("지자체명") and not (
        getattr(config, "READING_PIPELINE_ENABLED", True) and "_reading" in row
    ):
        row["지자체명"] = municipality
    if not row.get("출처페이지") and page_nums:
        row["출처페이지"] = list(page_nums)
        row["출처페이지추정"] = True
    return row


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
        "prompt": """이 배치에서 온실가스 감축목표를 추출하세요.

주의:
- 목표수준: 총괄/부문/세부부문/세부사업/연차경로
- 개별 사업·과제 카드(관리번호·성과지표·추진계획 문맥)의 감축 목표는 세부사업. 부문 전체 목표 표(기준배출량·전망·목표배출량 구조)만 부문
- 연도가 연속 나열된 연차별 감축량·감축률 경로표의 각 연도 행은 연차경로(그 연도 시점의 목표가 아님). 5년 단위 이정표 목표 표는 해당 없음
- 사업의 활동량·목표물량·추진일정·예산·일반 이행실적은 감축목표가 아니다. 해당 값은 09_연차별이행계획·10_정량감축량에만 기록하고 06에 중복 생성하지 않는다
- 세부사업·연차경로는 원문이 온실가스 감축량·목표배출량·감축률을 명시할 때만 생성한다
- 표의 행 라벨인 'BAU', '기준배출량', '목표감축량', '목표배출량'을 부문명으로 기록하지 않는다
- 목표수준은 항상 기입
- 목표범위: 관리권한/관리권한+추가감축/지역전체
- 목표범위는 원문 근거가 불확실하면 생략(추측 금지)
- 감축목표 산정의 2018년 기준배출량은 원칙적으로 총배출량(gross), 목표연도 배출량은 순배출량(net) 기준이다
- 다만 원문이 다른 정의를 명시하면 그 정의를 우선하고 reported_other로 기록한다. 정의가 없으면 unknown
- 감축률은 원문에 명시된 값만 감축률에 기록한다. 직접 계산하지 않는다

JSON 형식:
{"reduction_targets": [{"지자체명": "...", "목표수준": "총괄|부문|세부부문|세부사업|연차경로", "목표범위": "관리권한|관리권한+추가감축|지역전체", "부문": "건물|수송|농축산|폐기물|흡수원|전환|산업|수소|합계|null", "기준연도": 숫자, "기준배출량": 숫자or null, "기준배출량기준": "gross|net|reported_other|unknown", "목표연도": 숫자, "배출전망": 숫자or null, "목표감축량": 숫자or null, "목표배출량": 숫자or null, "목표배출량기준": "gross|net|reported_other|unknown", "감축률": 숫자or null, "사업명힌트": "세부사업일 때 해당 사업명, 그 외 생략"}]}
데이터가 없으면: {"reduction_targets": []}""",
    },

    "vision_strategy": {
        "keywords": ["비전", "전략", "추진방향", "핵심과제", "슬로건", "탄소중립 도시"],
        "prompt": """이 배치에서 비전·전략 정보를 추출하세요.

2050 탄소중립 비전·목표는 반드시 탐색하되, 정량값은 원문에 실제로 존재할 때만 추출하세요.

JSON 형식:
{"vision_strategy": [{"지자체명": "...", "비전문구": "2050 탄소중립 ... 등", "전략수준": "비전|추진전략|세부전략", "전략명": "...", "부문": "건물|수송|...|null", "설명": "...", "키워드": "..."}]}
데이터가 없으면: {"vision_strategy": []}""",
    },

    "mitigation_projects": {
        "keywords": ["감축사업", "세부사업", "핵심과제", "추진과제", "관리번호", "주관부서",
                     "성과지표", "공통사업", "특화사업"],
        "prompt": """이 배치에서 감축대책·세부사업 목록을 추출하세요.

주의:
- 사업유형은 신규/계속/확대/변경/기타 중 원문에 명시된 값만 기록
- 한 사업의 개요, 부서, 지표를 한 행에 정리
- 관리번호가 없으면 null
- 사업명이나 교육·캠페인이라는 이유만으로 정량/정성을 판단하지 않는다
- 정량여부는 원문에 감축량 또는 산정 가능한 정량 성과가 명시되면 true, 정량 성과 없이 정책·교육·홍보 성과만 제시되면 false, 판단 불가능하면 null

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
- 재정투자표의 예산액·비예산 표기는 financial_plan에만 기록하며 목표물량/연간계획에 중복 배치하지 않는다
- 금액형 성과지표(예: 투자유치액)는 명시된 성과지표명·원문 근거와 함께 목표로 보존한다

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
- 모니터링인자: 사업량 측정에 사용되는 활동자료
- 부록3 원단위나 유사 사업명을 이용해 감축량·원단위 값을 새로 계산하거나 보완하지 않는다
- 예상감축량은 보고서에 직접 제시된 값만 기록한다. 원문이 계산값이라고 명시하면 감축량유형과 함께 보존한다
- 감축량유형: potential/target/planned/expected/estimated/actual/reported_other
- 시간기준: annual/cumulative/period_total/unknown

JSON 형식:
{"quantitative_reductions": [{"지자체명": "...", "관리번호": "...", "사업명": "...", "연도": 숫자, "모니터링인자": "...", "활동량": 숫자or null, "활동단위": "...", "감축원단위ID": "...", "감축원단위값": 숫자or null, "예상감축량": 숫자or null, "감축량유형": "potential|target|planned|expected|estimated|actual|reported_other", "시간기준": "annual|cumulative|period_total|unknown", "단위": "tCO2eq"}]}
데이터가 없으면: {"quantitative_reductions": []}""",
    },

    "financial_plan": {
        "keywords": ["재정", "투자", "예산", "국비", "시비", "도비", "민간", "백만원", "억원"],
        "prompt": """이 배치에서 재정투자 계획(부문별·재원별·연도별 예산)을 추출하세요.

계획구분은 정책 분류(총계/온실가스감축대책/대응기반강화/기타)입니다. 투자계획·집행실적 여부와 혼동하지 마세요.
분류 근거가 없으면 null로 유지하고, 투자계획 여부는 _reading의 문맥 구분과 근거 문구로 보존하세요.
예산액은 연차별 이행계획의 목표물량에 중복 기록하지 마세요.

JSON 형식:
{"financial_plan": [{"지자체명": "...", "계획구분": "총계|온실가스감축대책|대응기반강화|기타", "부문": "...", "사업명": "...", "재원구분": "합계|국비|도비|시비|민간", "연도": 숫자, "예산액": 숫자or null, "예산단위": "백만원|억원"}]}
데이터가 없으면: {"financial_plan": []}""",
    },

    "foundation_measures": {
        "keywords": ["적응", "공유재산", "국제협력", "교육", "홍보", "녹색성장", "청정에너지",
                     "정의로운 전환", "인력양성", "대응기반", "취약성", "리스크", "위험도",
                     "기후시나리오", "RCP", "SSP", "폭염", "홍수"],
        "prompt": """이 배치에서 기후위기 대응기반 강화대책을 추출하세요.

주의:
- 대응기반영역: 적응대책/공유재산/국제협력/교육소통/녹색성장/청정에너지/정의로운전환/인력양성
- 감시·예측·영향·취약성·리스크·재난 평가 표와 지도를 별도 행으로 구조화한다
- 평가유형: monitoring/projection/impact/vulnerability/risk/disaster
- 값·등급·시나리오·기간은 원문에 보이는 경우에만 기록하고 추정하지 않는다
- 장·절 제목이나 목차 문구만 반복한 행은 만들지 않는다. 정책 과제는 과제명과 정책방향·주요내용·부서·기간 중 실제 내용이 있어야 한다
- 기후위험 행은 평가유형·기후변수·시나리오·리스크항목·취약성지표·값·등급 중 서로 연결되는 근거를 구조화한다. 일반 기후 서술 한 문장만으로 행을 만들지 않는다
- 감축사업 목록과 같은 사업명이 보이더라도 대응기반 영역 또는 기후위험 문맥이 없으면 12에 중복 기록하지 않는다

JSON 형식:
{"foundation_measures": [{"지자체명": "...", "대응기반영역": "적응대책|공유재산|국제협력|교육소통|녹색성장|청정에너지|정의로운전환|인력양성", "과제ID": "...", "과제명": "...", "정책방향": "...", "주요내용": "...", "대상": "...", "주관부서": "...", "기간": "...", "평가유형": "monitoring|projection|impact|vulnerability|risk|disaster|null", "기후변수": "...", "시나리오": "...", "기준기간": "...", "미래기간": "...", "공간단위": "...", "부문": "...", "리스크항목": "...", "취약성지표": "...", "값": 숫자or null, "단위": "...", "리스크등급": "...", "방법론": "...", "자료출처": "...", "연계적응과제": "..."}]}
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
- 소요예산 원문은 그대로 보존하고 계획·소요·확보·배정·집행을 임의로 바꾸지 않는다
- 예산유형: planned/required/secured/allocated/executed/reported_unspecified

JSON 형식:
{"monitoring_performance": [{"지자체명": "...", "점검연도": 숫자, "부문": "...", "관리번호": "...", "사업명": "...", "연간계획": "...", "이행실적": "...", "소요예산": "원문 표기", "예산액": 숫자or null, "예산유형": "planned|required|secured|allocated|executed|reported_unspecified", "예산단위": "원|천원|백만원|억원|null", "예산집행률": 숫자or null, "달성여부": "달성|정상추진|지연|미달성", "사업유형": "기존|변경|신규"}]}
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
    max_chars = max(1000, int(getattr(config, "EXTRACTION_MAX_BATCH_CHARS", 18000)))
    char_bounded: list[list[PageContent]] = []
    for batch in batches:
        current: list[PageContent] = []
        for page in batch:
            candidate = [*current, page]
            if current and len(_build_page_text(candidate)) > max_chars:
                char_bounded.append(current)
                current = [page]
            else:
                current = candidate
        if current:
            char_bounded.append(current)
    return char_bounded


def _split_text_with_page_marker(page: PageContent, max_chars: int) -> list[str]:
    """단일 페이지 본문을 줄 경계에서 나누고 모든 조각에 출처 페이지를 보존한다."""
    marker = f"=== 페이지 {page.page_number} ===\n"
    budget = max(500, max_chars - len(marker))
    lines = (page.text or "").splitlines() or [page.text or ""]
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for line in lines:
        pending = str(line)
        while len(pending) > budget:
            if current:
                chunks.append(marker + "\n".join(current))
                current, current_chars = [], 0
            chunks.append(marker + pending[:budget])
            pending = pending[budget:]
        extra = len(pending) + (1 if current else 0)
        if current and current_chars + extra > budget:
            chunks.append(marker + "\n".join(current))
            current, current_chars = [], 0
        if pending:
            current.append(pending)
            current_chars += extra
    if current or not chunks:
        chunks.append(marker + "\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


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


def _document_frequency(pages: list[PageContent], keywords: set[str]) -> dict[str, int]:
    """각 키워드가 등장하는 페이지 수(문서 빈도)."""
    df: dict[str, int] = {kw: 0 for kw in keywords}
    for page in pages:
        combined = f"{page.text or ''}\n" + "\n".join(page.tables or [])
        for kw in keywords:
            if kw in combined:
                df[kw] += 1
    return df


def _ubiquitous_weak_keywords(
    pages: list[PageContent],
    ratio: float = 0.4,
    min_pages: int = 8,
) -> frozenset[str]:
    """
    문서 전반(>ratio 비율의 페이지)에 등장해 변별력이 사실상 0인 weak 키워드 집합.

    예: '탄소중립', '녹색성장', '에너지'처럼 머리말/공통어로 모든 페이지에 찍히는 단어는
    라우팅 점수를 부풀려 거의 전 문서를 모든 시트에 배정하게 만든다. 이런 키워드의 +1
    가산만 제외한다(샤프닝은 선택만 좁히고 추출 입력 텍스트는 그대로 전체를 보냄).
    """
    n = len(pages)
    if n < min_pages:
        return frozenset()
    weak_all: set[str] = set()
    for cfg in _ROUTE_CONFIGS.values():
        weak_all.update(cfg["weak"])
    df = _document_frequency(pages, weak_all)
    threshold = max(min_pages, int(n * ratio))
    return frozenset(kw for kw, count in df.items() if count >= threshold)


def _ubiquitous_strong_keywords(
    pages: list[PageContent],
    ratio: float = 0.6,
    min_pages: int = 8,
) -> frozenset[str]:
    """
    문서 전반(>ratio 비율)에 편재해 변별력을 잃은 strong 키워드 집합.

    예: 보고서 제목인 '기본계획'은 거의 모든 페이지의 머리말/꼬리말에 찍혀 strong(+3)
    가산을 받는 바람에, document_meta가 문서 전체(서울 기준 500/515p)에 라우팅된다.
    이런 boilerplate strong 신호의 +3 가산만 제외한다. ratio 기준을 weak(0.5)보다
    높게 둬(0.6+) 실제 주제 빈출 키워드는 보존하고, 머리말 boilerplate만 떨군다.
    반드시 scripts/verify_routing_coverage.py로 시트별 정답 리콜 무회귀를 증명한 뒤
    기본 활성화한다.
    """
    n = len(pages)
    if n < min_pages:
        return frozenset()
    strong_all: set[str] = set()
    for cfg in _ROUTE_CONFIGS.values():
        strong_all.update(cfg["strong"])
    df = _document_frequency(pages, strong_all)
    threshold = max(min_pages, int(n * ratio))
    return frozenset(kw for kw, count in df.items() if count >= threshold)


def _score_page_for_sheet(
    page: PageContent,
    sheet_key: str,
    ubiquitous_weak: frozenset[str] = frozenset(),
    ubiquitous_strong: frozenset[str] = frozenset(),
) -> int:
    cfg = _ROUTE_CONFIGS.get(sheet_key)
    if not cfg:
        return 0
    text = page.text or ""
    table_text = "\n".join(page.tables or [])
    combined = f"{text}\n{table_text}"

    weak = [kw for kw in cfg["weak"] if kw not in ubiquitous_weak]
    strong = [kw for kw in cfg["strong"] if kw not in ubiquitous_strong]

    score = 0
    score += sum(3 for kw in strong if kw in combined)
    score += sum(1 for kw in weak if kw in combined)
    score += _page_title_score(text, strong + weak)

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

    # boilerplate strong 키워드(보고서 제목 등 머리말 편재)의 +3 가산도 제외.
    if getattr(config, "ROUTE_DROP_UBIQUITOUS_STRONG", False):
        ubiquitous_strong = _ubiquitous_strong_keywords(
            pages, ratio=getattr(config, "ROUTE_STRONG_UBIQUITY_RATIO", 0.6)
        )
    else:
        ubiquitous_strong = frozenset()

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
            score = _score_page_for_sheet(page, sheet_key, ubiquitous_weak, ubiquitous_strong)
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
                key=lambda n: _score_page_for_sheet(
                    by_num[n], sheet_key, ubiquitous_weak, ubiquitous_strong
                ),
                reverse=True,
            )
            selected_nums = set(ranked_selected[:max_pages])

        routed[sheet_key] = [by_num[n] for n in sorted(selected_nums)]

    return routed


class ExtractorAgent:
    """에이전트 2: 텍스트·표 추출 에이전트 (가이드라인 기반 16개 시트)"""

    def __init__(
        self,
        run_state: RunState | None = None,
        document_objects: Sequence[DocumentObject] | None = None,
    ):
        self._raw_results: dict = {key: [] for key in _SHEET_CONFIGS}
        self._raw_results["municipality_name"] = ""
        # 시트별로 1차 추출에서 실제 LLM에 보낸 페이지번호. 라우팅 통계용으로 유지한다.
        self.routed_page_nums: dict[str, set[int]] = {}
        self.ledger: list[BatchRecord] = []
        self.timeout_splits = 0
        self.failure_splits = 0
        self.preflight_splits = 0
        self.resumed_batches = 0
        self.skipped_batches = 0
        self.enqueued_tasks = 0
        self.deduplicated_tasks = 0
        self.text_policy = None
        self.task_audit = []
        self.call_audit = []
        self.call_plan = []
        self.run_state = run_state
        self._document_objects_by_page: dict[int, tuple[DocumentObject, ...]] = {}
        for obj in document_objects or ():
            current = self._document_objects_by_page.get(obj.page_number, ())
            self._document_objects_by_page[obj.page_number] = (*current, obj)

    def partial_results(self) -> dict:
        return {
            key: list(value) if isinstance(value, list) else value
            for key, value in self._raw_results.items()
        }

    @property
    def extracted_page_nums(self) -> dict[str, set[int]]:
        extracted: dict[str, set[int]] = {}
        for record in self.ledger:
            if record.status != "ok":
                continue
            extracted.setdefault(record.sheet_key, set()).update(record.page_nums)
        return extracted

    def _record_batch(
        self,
        sheet_key: str,
        page_nums: list[int],
        status: str,
        rows: int,
        error: str = "",
        *,
        batch_id: str = "",
        source: str = "live",
        recovered: bool = False,
    ) -> None:
        self.ledger.append(BatchRecord(
            sheet_key,
            list(page_nums),
            status,
            rows,
            error,
            batch_id=batch_id,
            source=source,
            recovered=recovered,
        ))

    @staticmethod
    def _task_sheet_keys(task: dict) -> list[str]:
        if "members" in task:
            return [str(key) for key in task.get("members", [])]
        return [str(task.get("sheet_key", "?"))]

    @classmethod
    def _prompt_contract_fingerprint(
        cls,
        task: dict,
        kind: str,
        extraction_prompts: dict[str, str] | None = None,
    ) -> str:
        sheet_keys = cls._task_sheet_keys(task)
        contracts = []
        for sheet_key in sheet_keys:
            guideline_prompt = (
                extraction_prompts.get(sheet_key, "")
                if extraction_prompts is not None
                else task.get("guideline_prompt", "")
            )
            contracts.append({
                "sheet_key": sheet_key,
                "schema_prompt": str(_SHEET_CONFIGS.get(sheet_key, {}).get("prompt", "")),
                "reading_contract": reading_instruction([sheet_key]) if getattr(config, "READING_PIPELINE_ENABLED", True) else "",
                "guideline_prompt": str(guideline_prompt or ""),
            })
        encoded = json.dumps({
            "kind": kind,
            "system": EXTRACTION_SYSTEM,
            "provenance": _PROVENANCE_INSTRUCTION,
            "contracts": contracts,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _enqueue_task_fingerprint(
        cls,
        task: dict,
        kind: str,
        extraction_prompts: dict[str, str] | None = None,
    ) -> str:
        prompt_fingerprint = str(task.get("prompt_fingerprint") or "")
        if not prompt_fingerprint:
            prompt_fingerprint = cls._prompt_contract_fingerprint(
                task,
                kind,
                extraction_prompts,
            )
            task["prompt_fingerprint"] = prompt_fingerprint
        payload = {
            "kind": kind,
            "sheet_contract": cls._task_sheet_keys(task),
            "page_set": sorted({int(page) for page in task.get("page_nums", [])}),
            "prompt_fingerprint": prompt_fingerprint,
            # 같은 페이지의 표 자식·본문 자식을 잘못 합치지 않는 안전장치다.
            "payload_sha256": hashlib.sha256(
                str(task.get("batch_text", "")).encode("utf-8")
            ).hexdigest(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _ensure_batch_id(self, task: dict, kind: str) -> str:
        batch_id = str(task.get("batch_id") or "")
        if batch_id or self.run_state is None:
            return batch_id
        request_fingerprint = str(task.get("enqueue_fingerprint") or "")
        if not request_fingerprint:
            request_fingerprint = self._enqueue_task_fingerprint(task, kind)
            task["enqueue_fingerprint"] = request_fingerprint
        batch_id = self.run_state.batch_id(
            kind=kind,
            sheet_keys=self._task_sheet_keys(task),
            page_nums=task.get("page_nums", []),
            batch_text=task.get("batch_text", ""),
            request_fingerprint=request_fingerprint,
        )
        task["batch_id"] = batch_id
        if int(task.get("split_depth", 0)) == 0:
            self.run_state.register_expected(batch_id, {
                "kind": kind,
                "sheet_keys": self._task_sheet_keys(task),
                "page_nums": list(task.get("page_nums", [])),
            })
        return batch_id

    def _restore_task(self, task: dict, kind: str) -> tuple[bool, Any]:
        if self.run_state is None:
            return False, None
        batch_id = self._ensure_batch_id(task, kind)
        restored = self.run_state.restored_result(batch_id)
        if restored is not None:
            task["effective_status"] = "restored"
            self.resumed_batches += 1
            page_nums = list(task.get("page_nums", []))
            checkpoint = self.run_state.record_for(batch_id) or {}
            if kind == "sheet":
                rows = [row for row in restored if isinstance(row, dict)] if isinstance(restored, list) else []
                self._record_batch(
                    task.get("sheet_key", "?"),
                    page_nums,
                    "ok",
                    len(rows),
                    batch_id=batch_id,
                    source="checkpoint",
                    recovered=bool(checkpoint.get("recovered", False)),
                )
                return True, rows
            result = restored if isinstance(restored, dict) else {}
            for sheet_key in self._task_sheet_keys(task):
                rows = result.get(sheet_key, []) if isinstance(result, dict) else []
                self._record_batch(
                    sheet_key,
                    page_nums,
                    "ok",
                    len(rows) if isinstance(rows, list) else 0,
                    batch_id=batch_id,
                    source="checkpoint",
                    recovered=bool(checkpoint.get("recovered", False)),
                )
            return True, result
        if not self.run_state.should_execute(
            batch_id,
            is_child=int(task.get("split_depth", 0)) > 0,
        ):
            self.skipped_batches += 1
            task["effective_status"] = "skipped"
            return True, [] if kind == "sheet" else {key: [] for key in self._task_sheet_keys(task)}
        return False, None

    def _persist_task(
        self,
        task: dict,
        kind: str,
        status: str,
        *,
        result: Any = None,
        error: str = "",
        recovered: bool = False,
    ) -> Any:
        task["effective_status"] = status
        if self.run_state is None:
            if result is not None:
                task["preserved_result"] = result
            return task.get("preserved_result")
        batch_id = self._ensure_batch_id(task, kind)
        self.run_state.record_batch(
            batch_id=batch_id,
            kind=kind,
            sheet_keys=self._task_sheet_keys(task),
            page_nums=task.get("page_nums", []),
            status=status,
            result=result,
            error=error,
            split_depth=int(task.get("split_depth", 0)),
            recovered=recovered,
            member_statuses=task.get("member_statuses"),
        )
        persisted = self.run_state.record_for(batch_id) or {}
        return persisted.get("result")

    def _merge_rows(self, sheet_key: str, existing: list[dict], incoming: list[dict]) -> list[dict]:
        rows, conflicts = merge_rows_stably(sheet_key, existing, incoming)
        if self.run_state is not None:
            self.run_state.record_conflicts(conflicts)
        return rows

    def _prepare_tasks(
        self,
        tasks: list[dict],
        kind: str,
        extraction_prompts: dict[str, str] | None = None,
    ) -> list[dict]:
        """실제 요청 계약이 완전히 같은 작업만 enqueue 전에 제거한다."""
        prepared: list[dict] = []
        seen: set[str] = set()
        deduplicate = bool(getattr(config, "TEXT_QUEUE_DEDUP_ENABLED", True))
        for task in tasks:
            fingerprint = self._enqueue_task_fingerprint(
                task,
                kind,
                extraction_prompts,
            )
            task["enqueue_fingerprint"] = fingerprint
            if deduplicate and fingerprint in seen:
                self.deduplicated_tasks += 1
                continue
            seen.add(fingerprint)
            prepared.append(task)
            self.call_plan.append({"fingerprint": fingerprint, "sheets": self._task_sheet_keys(task),
                                   "pages": list(task.get("page_nums", [])),
                                   "input_chars": len(task.get("batch_text", "")),
                                   "input_sha256": digest(task.get("batch_text", "")),
                                   "prompt_fingerprint": task.get("prompt_fingerprint"),
                                   "reason": "routed_sheet_page_assignment"})
        self.enqueued_tasks += len(prepared)
        if self.run_state is not None:
            for task in prepared:
                self._ensure_batch_id(task, kind)
        return prepared

    def _record_call_failure(self, task: dict, exc: Exception) -> Any:
        page_nums = list(task.get("page_nums", []))
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
        status = "parse_fail" if isinstance(exc, BatchParseError) else "call_fail"
        kind = "cluster" if "members" in task else "sheet"
        preserved = self._persist_task(task, kind, status, error=error)
        persisted = (
            self.run_state.record_for(str(task.get("batch_id") or ""))
            if self.run_state is not None else None
        ) or {}
        effective_status = str(persisted.get("status") or status)
        if "members" in task:
            for sheet_key in task["members"]:
                rows = preserved.get(sheet_key, []) if isinstance(preserved, dict) else []
                self._record_batch(
                    sheet_key,
                    page_nums,
                    effective_status,
                    len(rows) if isinstance(rows, list) else 0,
                    error,
                    batch_id=str(task.get("batch_id") or ""),
                )
            label = "/".join(task["members"][:2]) + ("…" if len(task["members"]) > 2 else "")
        else:
            sheet_key = task.get("sheet_key", "?")
            rows = preserved if isinstance(preserved, list) else []
            self._record_batch(
                sheet_key,
                page_nums,
                effective_status,
                len(rows),
                error,
                batch_id=str(task.get("batch_id") or ""),
            )
            label = sheet_key
        failure_label = "파싱 실패" if status == "parse_fail" else "호출 실패"
        print(
            f"  [{label}] 배치 {task.get('batch_num', 0)}/{task.get('batch_total', 0)} "
            f"({task.get('page_range', _page_range_label(page_nums))}): "
            f"{failure_label}({type(exc).__name__}) — 원장 기록"
        )
        return preserved

    @staticmethod
    def _task_label(task: dict) -> str:
        if "members" in task:
            members = list(task.get("members", []))
            return "/".join(members[:2]) + ("…" if len(members) > 2 else "")
        return str(task.get("sheet_key", "?"))

    @staticmethod
    def _split_child_task(task: dict, pages: list[PageContent], child_number: int) -> dict:
        page_nums = [page.page_number for page in pages]
        child = dict(task)
        child.update({
            "pages": pages,
            "batch_text": _build_page_text(pages),
            "page_nums": page_nums,
            "page_range": _page_range_label(page_nums),
            "batch_num": f"{task.get('batch_num', '?')}.{child_number}",
            "batch_total": 2,
            "split_depth": int(task.get("split_depth", 0)) + 1,
        })
        child.pop("batch_id", None)
        child["parent_trace_id"] = task.get("trace_id")
        for key in ("trace_id", "effective_status", "enqueue_fingerprint", "prompt_fingerprint", "preserved_result", "member_statuses"):
            child.pop(key, None)
        return child

    @staticmethod
    def _custom_child_task(
        task: dict,
        *,
        pages: list[PageContent],
        batch_text: str,
        child_number: int,
        object_id: str = "",
    ) -> dict:
        child = ExtractorAgent._split_child_task(task, pages, child_number)
        child["batch_text"] = batch_text
        if object_id:
            child["object_id"] = object_id
        return child

    def _recovery_children(self, task: dict, max_chars: int) -> list[dict]:
        """페이지 → 표 객체/본문 조각 순으로 복구 단위를 만든다."""
        pages = list(task.get("pages", []))
        if not pages:
            return []
        max_chars = max(1000, int(max_chars))
        children: list[dict] = []

        if len(pages) > 1:
            groups: list[list[PageContent]] = []
            current: list[PageContent] = []
            for page in pages:
                candidate = [*current, page]
                if current and len(_build_page_text(candidate)) > max_chars:
                    groups.append(current)
                    current = [page]
                else:
                    current = candidate
            if current:
                groups.append(current)
            if len(groups) == 1:
                midpoint = max(1, len(pages) // 2)
                groups = [pages[:midpoint], pages[midpoint:]]
            for number, group in enumerate((group for group in groups if group), start=1):
                children.append(self._split_child_task(task, group, number))
        else:
            page = pages[0]
            page_objects = self._document_objects_by_page.get(page.page_number)
            if page_objects is None:
                page_objects = tuple(build_document_objects([page]))
            table_objects = [
                obj for obj in page_objects
                if obj.object_type == "table" and obj.rows
            ]
            for table_object in table_objects:
                for rendered in render_table_object_chunks(
                    table_object,
                    rows_per_chunk=max(
                        2,
                        int(getattr(config, "EXTRACTION_TABLE_ROWS_PER_BATCH", 20)),
                    ),
                    max_chars=max_chars,
                ):
                    children.append(self._custom_child_task(
                        task,
                        pages=[page],
                        batch_text=f"=== 페이지 {page.page_number} ===\n{rendered}",
                        child_number=len(children) + 1,
                        object_id=table_object.object_id,
                    ))

            for text_chunk in _split_text_with_page_marker(page, max_chars):
                # 표 원문 HTML은 표 객체 자식에서 처리하고 본문 자식에는 넣지 않는다.
                if text_chunk.strip() == str(task.get("batch_text", "")).strip() and not table_objects:
                    continue
                children.append(self._custom_child_task(
                    task,
                    pages=[page],
                    batch_text=text_chunk,
                    child_number=len(children) + 1,
                    object_id=f"p{page.page_number}_text",
                ))

        for index, child in enumerate(children, start=1):
            child["batch_num"] = f"{task.get('batch_num', '?')}.{index}"
            child["batch_total"] = len(children)
        return children

    def _run_sheet_children(
        self,
        task: dict,
        children: list[dict],
        municipality: str,
        *,
        reason: str,
    ) -> list[dict]:
        label = self._task_label(task)
        page_range = task.get("page_range", _page_range_label(task.get("page_nums", [])))
        child_labels = ", ".join(
            child.get("object_id") or child.get("page_range", "?") for child in children
        )
        print(f"  [{label}] {page_range} {reason} → {len(children)}개 복구 단위: {child_labels}")
        recovered: list[dict] = []
        child_failed = False
        for child in children:
            try:
                child_rows = self._run_sheet_task(child, municipality)
                recovered = self._merge_rows(task["sheet_key"], recovered, child_rows)
                child_failed = child_failed or child.get("effective_status") == "partial"
                if self.run_state is not None:
                    child_record = self.run_state.record_for(str(child.get("batch_id") or "")) or {}
                    child_failed = child_failed or child_record.get("status") not in {None, "ok"}
            except Exception as child_exc:  # 성공한 형제 결과는 보존한다.
                child_failed = True
                preserved = self._record_call_failure(child, child_exc)
                if isinstance(preserved, list):
                    recovered = self._merge_rows(task["sheet_key"], recovered, preserved)
        persisted = self._persist_task(
            task,
            "sheet",
            "partial" if child_failed else "ok",
            result=recovered,
            error=reason if child_failed else "",
            recovered=True,
        )
        if isinstance(persisted, list):
            recovered = persisted
        print(f"  [{label}] {page_range} 분할 복구 완료: {len(recovered)}건")
        return recovered

    def _run_cluster_children(
        self,
        task: dict,
        children: list[dict],
        municipality: str,
        extraction_prompts: dict[str, str],
        *,
        reason: str,
    ) -> dict[str, list]:
        label = self._task_label(task)
        page_range = task.get("page_range", _page_range_label(task.get("page_nums", [])))
        print(f"  [{label}] {page_range} {reason} → {len(children)}개 클러스터 복구 단위")
        recovered = {sheet_key: [] for sheet_key in task["members"]}
        child_failed = False
        for child in children:
            try:
                child_result = self._run_cluster_task(child, municipality, extraction_prompts)
                child_failed = child_failed or child.get("effective_status") == "partial"
                for sheet_key, rows in child_result.items():
                    recovered[sheet_key] = self._merge_rows(
                        sheet_key,
                        recovered.setdefault(sheet_key, []),
                        rows,
                    )
                if self.run_state is not None:
                    child_record = self.run_state.record_for(str(child.get("batch_id") or "")) or {}
                    child_failed = child_failed or child_record.get("status") not in {None, "ok"}
            except Exception as child_exc:
                child_failed = True
                preserved = self._record_call_failure(child, child_exc)
                if isinstance(preserved, dict):
                    for key, rows in preserved.items():
                        recovered[key] = self._merge_rows(key, recovered.get(key, []), rows)
        persisted = self._persist_task(
            task,
            "cluster",
            "partial" if child_failed else "ok",
            result=recovered,
            error=reason if child_failed else "",
            recovered=True,
        )
        if isinstance(persisted, dict):
            recovered = persisted
        total_rows = sum(len(rows) for rows in recovered.values())
        print(f"  [{label}] {page_range} 분할 복구 완료: {total_rows}건")
        return recovered

    @trace_task("sheet")
    def _run_sheet_task(self, task: dict, municipality: str) -> list[dict]:
        """한 추출 태스크를 실행하고, 타임아웃이면 더 작은 페이지 묶음으로 복구한다."""
        restored, restored_rows = self._restore_task(task, "sheet")
        if restored:
            if restored_rows:
                print(
                    f"  [{self._task_label(task)}] {task.get('page_range', '?')} "
                    f"체크포인트 복원: {len(restored_rows)}건"
                )
            return restored_rows
        root_started = float(task.setdefault("timeout_root_started", time.monotonic()))
        recovery_budget = max(
            0,
            int(getattr(config, "EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS", 900)),
        )
        if (
            int(task.get("split_depth", 0)) > 0
            and recovery_budget
            and time.monotonic() - root_started >= recovery_budget
        ):
            raise llm_client.LLMTimeoutError(
                f"타임아웃 분할 복구 시간 상한({recovery_budget}초) 도달"
            )
        split_depth = int(task.get("split_depth", 0))
        max_depth = max(0, int(getattr(config, "EXTRACTION_TIMEOUT_MAX_SPLIT_DEPTH", 6)))
        char_limit = max(
            1000,
            int(getattr(
                config,
                "EXTRACTION_MAX_BATCH_CHARS" if split_depth == 0 else "EXTRACTION_RECOVERY_MAX_BATCH_CHARS",
                18000 if split_depth == 0 else 12000,
            )),
        )
        if not task.get("no_split") and len(str(task.get("batch_text", ""))) > char_limit and split_depth < max_depth:
            children = self._recovery_children(task, char_limit)
            if children:
                self.preflight_splits += 1
                return self._run_sheet_children(
                    task,
                    children,
                    municipality,
                    reason=f"사전 크기 분할({len(str(task.get('batch_text', ''))):,}자>{char_limit:,}자)",
                )
        label = self._task_label(task)
        page_range = task.get("page_range", _page_range_label(task.get("page_nums", [])))
        print(
            f"  [{label}] 배치 {task.get('batch_num', 0)}/{task.get('batch_total', 0)} "
            f"({page_range}) 호출 시작 ({len(task.get('batch_text', '')):,}자)"
        )
        try:
            rows = self._extract_sheet(
                task["sheet_key"],
                task["batch_text"],
                municipality,
                task.get("guideline_prompt", ""),
                fail_fast=True,
                batch_id=str(task.get("batch_id") or ""),
            )
        except llm_client.LLMQuotaExceededError:
            raise
        except (llm_client.LLMTimeoutError, BatchParseError, llm_client.LLMCallError) as exc:
            pages = list(task.get("pages", []))
            split_enabled = (
                getattr(config, "EXTRACTION_SPLIT_ON_TIMEOUT", True)
                if isinstance(exc, llm_client.LLMTimeoutError)
                else getattr(config, "EXTRACTION_SPLIT_ON_FAILURE", True)
            )
            if (
                task.get("no_split") or not split_enabled
                or int(task.get("split_depth", 0)) >= max_depth
                or (
                    recovery_budget
                    and time.monotonic() - root_started >= recovery_budget
                )
            ):
                raise
            if isinstance(exc, llm_client.LLMTimeoutError):
                self.timeout_splits += 1
                failure_label = "타임아웃"
            elif isinstance(exc, BatchParseError):
                self.failure_splits += 1
                failure_label = "파싱 실패"
            else:
                self.failure_splits += 1
                failure_label = "호출 실패"
            children = self._recovery_children(
                task,
                max(1000, int(getattr(config, "EXTRACTION_RECOVERY_MAX_BATCH_CHARS", 12000))),
            )
            if not children:
                raise
            return self._run_sheet_children(
                task,
                children,
                municipality,
                reason=failure_label,
            )

        status = f"{len(rows)}건" if rows else "추출 없음"
        print(
            f"  [{label}] 배치 {task.get('batch_num', 0)}/{task.get('batch_total', 0)} "
            f"({page_range}) 완료: {status}"
        )
        self._persist_task(task, "sheet", "ok", result=rows)
        return rows

    @trace_task("cluster")
    def _run_cluster_task(
        self,
        task: dict,
        municipality: str,
        extraction_prompts: dict[str, str],
    ) -> dict[str, list]:
        """클러스터 태스크에도 동일한 타임아웃 분할 정책을 적용한다."""
        restored, restored_result = self._restore_task(task, "cluster")
        if restored:
            total_restored = sum(
                len(rows) for rows in restored_result.values() if isinstance(rows, list)
            ) if isinstance(restored_result, dict) else 0
            if total_restored:
                print(
                    f"  [{self._task_label(task)}] {task.get('page_range', '?')} "
                    f"체크포인트 복원: {total_restored}건"
                )
            return restored_result
        if self.run_state is not None and self.run_state.resume:
            checkpoint = self.run_state.record_for(str(task.get("batch_id") or "")) or {}
            members = checkpoint.get("member_statuses", {})
            if checkpoint.get("status") == "partial" and set(members) == set(task["members"]):
                missing = [key for key in task["members"] if members[key] != "ok"]
                if missing:
                    self.resumed_batches += 1
                    return self._recover_cluster_members(task, municipality, extraction_prompts,
                        ClusterPartialParseError(deepcopy(checkpoint.get("result") or {}), missing))
        root_started = float(task.setdefault("timeout_root_started", time.monotonic()))
        recovery_budget = max(
            0,
            int(getattr(config, "EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS", 900)),
        )
        if (
            int(task.get("split_depth", 0)) > 0
            and recovery_budget
            and time.monotonic() - root_started >= recovery_budget
        ):
            raise llm_client.LLMTimeoutError(
                f"타임아웃 분할 복구 시간 상한({recovery_budget}초) 도달"
            )
        split_depth = int(task.get("split_depth", 0))
        max_depth = max(0, int(getattr(config, "EXTRACTION_TIMEOUT_MAX_SPLIT_DEPTH", 6)))
        char_limit = max(
            1000,
            int(getattr(
                config,
                "EXTRACTION_MAX_BATCH_CHARS" if split_depth == 0 else "EXTRACTION_RECOVERY_MAX_BATCH_CHARS",
                18000 if split_depth == 0 else 12000,
            )),
        )
        if len(str(task.get("batch_text", ""))) > char_limit and split_depth < max_depth:
            children = self._recovery_children(task, char_limit)
            if children:
                self.preflight_splits += 1
                return self._run_cluster_children(
                    task,
                    children,
                    municipality,
                    extraction_prompts,
                    reason=f"사전 크기 분할({len(str(task.get('batch_text', ''))):,}자>{char_limit:,}자)",
                )
        label = self._task_label(task)
        page_range = task.get("page_range", _page_range_label(task.get("page_nums", [])))
        print(
            f"  [{label}] 배치 {task.get('batch_num', 0)}/{task.get('batch_total', 0)} "
            f"({page_range}) 호출 시작 ({len(task.get('batch_text', '')):,}자)"
        )
        try:
            result = self._extract_cluster(
                task["members"],
                task["batch_text"],
                municipality,
                extraction_prompts,
                fail_fast=True,
                batch_id=str(task.get("batch_id") or ""),
            )
        except ClusterPartialParseError as exc:
            return self._recover_cluster_members(task, municipality, extraction_prompts, exc)
        except llm_client.LLMQuotaExceededError:
            raise
        except (llm_client.LLMTimeoutError, BatchParseError, llm_client.LLMCallError) as exc:
            pages = list(task.get("pages", []))
            split_enabled = (
                getattr(config, "EXTRACTION_SPLIT_ON_TIMEOUT", True)
                if isinstance(exc, llm_client.LLMTimeoutError)
                else getattr(config, "EXTRACTION_SPLIT_ON_FAILURE", True)
            )
            if (
                not split_enabled
                or int(task.get("split_depth", 0)) >= max_depth
                or (
                    recovery_budget
                    and time.monotonic() - root_started >= recovery_budget
                )
            ):
                raise
            if isinstance(exc, llm_client.LLMTimeoutError):
                self.timeout_splits += 1
                failure_label = "타임아웃"
            elif isinstance(exc, BatchParseError):
                self.failure_splits += 1
                failure_label = "파싱 실패"
            else:
                self.failure_splits += 1
                failure_label = "호출 실패"
            children = self._recovery_children(
                task,
                max(1000, int(getattr(config, "EXTRACTION_RECOVERY_MAX_BATCH_CHARS", 12000))),
            )
            if not children:
                raise
            return self._run_cluster_children(
                task,
                children,
                municipality,
                extraction_prompts,
                reason=failure_label,
            )

        counts = [f"{sheet_key}={len(rows)}" for sheet_key, rows in result.items() if rows]
        status = ", ".join(counts) if counts else "추출 없음"
        print(
            f"  [{label}] 배치 {task.get('batch_num', 0)}/{task.get('batch_total', 0)} "
            f"({page_range}) 완료: {status}"
        )
        self._persist_task(task, "cluster", "ok", result=result)
        return result

    def _recover_cluster_members(self, task, municipality, prompts, failure):
        """Never re-request successful members; unresolved keys remain partial."""
        recovered = failure.result
        remaining = list(failure.failed)
        task.setdefault("timeout_root_started", time.monotonic())
        task["member_statuses"] = {key: "partial" if key in remaining else "ok" for key in task["members"]}
        # Persist before the first repair call, including valid empty arrays.
        self._persist_task(task, "cluster", "partial", result=recovered, error="unresolved_members=" + ",".join(remaining))
        attempts = text_policy_snapshot()["cluster_member_recovery_attempts"]
        for attempt in range(attempts):
            for key in list(remaining):
                child = self._custom_child_task(task, pages=task.get("pages", []),
                                                batch_text=task["batch_text"], child_number=attempt + 1)
                child.pop("members", None)
                child.update(sheet_key=key, guideline_prompt=prompts.get(key, ""), no_split=True)
                try:
                    rows = self._run_sheet_task(child, municipality)
                    recovered[key] = self._merge_rows(key, recovered.get(key, []), rows)
                    remaining.remove(key)
                    task["member_statuses"][key] = "ok"
                    self._persist_task(task, "cluster", "partial" if remaining else "ok", result=recovered,
                                       error="unresolved_members=" + ",".join(remaining) if remaining else "", recovered=True)
                except llm_client.LLMQuotaExceededError:
                    self._persist_task(task, "cluster", "partial", result=recovered, error="quota; " + ",".join(remaining))
                    raise
                except Exception as exc:
                    self._record_call_failure(child, exc)
        for key in remaining:
            self._record_batch(key, task.get("page_nums", []), "parse_fail", len(recovered.get(key, [])),
                               "제한 복구 후 미해결", batch_id=str(task.get("batch_id") or ""))
        self._persist_task(task, "cluster", "partial" if remaining else "ok", result=recovered,
                           error="unresolved_members=" + ",".join(remaining) if remaining else "", recovered=True)
        return recovered

    def _call_extraction_json(self, prompt, sheets, batch_id):
        row = {"batch_id": batch_id, "trace_id": CURRENT_TRACE.get(), "sheets": list(sheets), "prompt_chars": len(prompt),
               "prompt_sha256": digest(prompt), "status": "running"}
        self.call_audit.append(row)
        start = time.perf_counter()
        try:
            result = llm_client.call_text_json(prompt, system=EXTRACTION_SYSTEM, stage="extraction")
            row["status"] = "parsed" if result[1] else "parse_fail"
            return result
        except Exception as exc:
            row.update(status="call_fail", error=str(exc)[:300])
            raise
        finally:
            row["elapsed_seconds"] = round(time.perf_counter() - start, 6)

    def optimization_audit(self):
        return {"policy": text_policy_snapshot(), "routing": self.text_policy.audit() if self.text_policy else {},
                "plan": self.call_plan, "tasks": self.task_audit, "calls": self.call_audit,
                "raw_merged_rows": {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list)},
                "note": "Calls count extraction entrypoints, not internal transport retries. Rows are not accuracy. Task times overlap."}

    def ledger_summary(self) -> str:
        total = len(self.ledger)
        ok = sum(1 for record in self.ledger if record.status == "ok")
        call_fail = sum(1 for record in self.ledger if record.status == "call_fail")
        parse_fail = sum(1 for record in self.ledger if record.status == "parse_fail")
        failed_pages = sorted({
            page
            for record in self.ledger
            if record.status != "ok"
            for page in record.page_nums
        })
        page_text = ", ".join(f"p{page}" for page in failed_pages[:12])
        if len(failed_pages) > 12:
            page_text += ", …"
        if not page_text:
            page_text = "없음"
        return (
            f"[감독관] 추출 원장: 배치 {total}건 중 성공 {ok}, "
            f"호출실패 {call_fail}, 파싱실패 {parse_fail}, "
            f"사전 크기 분할 {self.preflight_splits}회, 타임아웃 분할 {self.timeout_splits}회, "
            f"기타 실패 분할 {self.failure_splits}회, "
            f"체크포인트 복원 {self.resumed_batches}회, 선택 건너뜀 {self.skipped_batches}회, "
            f"큐 등록 {self.enqueued_tasks}건, 정확 중복 제거 {self.deduplicated_tasks}건 "
            f"(실패 페이지: {page_text})"
        )

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
        *,
        fail_fast: bool = False,
        batch_id: str = "",
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

        page_nums = _page_nums_from_batch_text(batch_text)
        full_prompt = (
            f"지자체명: {municipality}"
            f"{guideline_block}\n\n"
            f"[배치 텍스트]\n{batch_text}\n\n"
            f"{cfg['prompt']}\n\n{_PROVENANCE_INSTRUCTION}\n"
            f"{reading_instruction([sheet_key]) if getattr(config, 'READING_PIPELINE_ENABLED', True) else ''}"
        )
        parsed, parse_ok = self._call_extraction_json(full_prompt, [sheet_key], batch_id)

        if not parse_ok or not isinstance(parsed, dict) or sheet_key not in parsed:
            if fail_fast:
                raise BatchParseError("JSON 파싱 실패 또는 시트 키 누락")
            self._record_batch(
                sheet_key,
                page_nums,
                "parse_fail",
                0,
                "JSON 파싱 실패 또는 시트 키 누락",
                batch_id=batch_id,
            )
            return []

        items = parsed.get(sheet_key, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            if fail_fast:
                raise BatchParseError("스키마 불일치")
            self._record_batch(
                sheet_key,
                page_nums,
                "parse_fail",
                0,
                "스키마 불일치",
                batch_id=batch_id,
            )
            return []

        rows = [
            _attach_row_context(item, municipality, page_nums)
            for item in items
            if isinstance(item, dict)
        ]
        self._record_batch(sheet_key, page_nums, "ok", len(rows), batch_id=batch_id)
        return rows

    def _extract_cluster(
        self,
        sheet_keys: list[str],
        batch_text: str,
        municipality: str,
        guideline_prompts: dict[str, str],
        *,
        fail_fast: bool = False,
        batch_id: str = "",
    ) -> dict[str, list]:
        """
        여러 시트를 한 번의 호출로 추출한다(클러스터링). 같은 배치 텍스트를 시트마다
        따로 보내던 중복을 없애 호출 수·입력 토큰을 크게 줄인다.

        각 시트의 스키마(cfg['prompt'])와 가이드라인 보조지침을 한 프롬프트에 모아,
        모든 시트 키를 담은 단일 JSON 객체로 반환하도록 요청한다. 반환 JSON에서 시트별
        배열을 분리해 per-sheet 경로와 동일한 형태로 돌려준다.
        """
        schema_blocks: list[str] = []
        for sk in sheet_keys:
            cfg = _SHEET_CONFIGS[sk]
            schema_blocks.append(f"### 추출 항목 [{sk}]\n{cfg['prompt']}")
            gp = guideline_prompts.get(sk, "")
            if gp:
                schema_blocks.append(
                    f"[{sk} 환경부 가이드라인 보조지침]\n{gp}\n"
                    "위 지침과 배치 텍스트가 충돌하면 배치 텍스트의 실제 수치·단위를 우선하되 "
                    "필드 구성·분류 체계는 가이드라인을 따르세요."
                )
        combined_schemas = "\n\n".join(schema_blocks)
        keys_csv = ", ".join(f'"{sk}"' for sk in sheet_keys)
        page_nums = _page_nums_from_batch_text(batch_text)
        full_prompt = (
            f"지자체명: {municipality}\n\n"
            f"[배치 텍스트]\n{batch_text}\n\n"
            "아래 여러 추출 항목을 각각의 스키마에 정확히 맞춰 모두 추출한 뒤, "
            "하나의 JSON 객체로 합쳐 반환하세요. 각 항목 키 아래에 해당 항목의 행 배열을 넣고, "
            "그 항목에 해당하는 데이터가 배치에 없으면 빈 배열([])을 넣으세요. "
            "항목 간 데이터를 섞지 말고, 각 행은 그 항목의 스키마 필드만 사용하세요.\n\n"
            f"{combined_schemas}\n\n"
            f"{_PROVENANCE_INSTRUCTION}\n\n"
            f"{reading_instruction(sheet_keys) if getattr(config, 'READING_PIPELINE_ENABLED', True) else ''}\n\n"
            f"최종 출력은 다음 키를 모두 포함하는 단일 JSON 객체입니다: {{{keys_csv}}}"
        )
        parsed, parse_ok = self._call_extraction_json(full_prompt, sheet_keys, batch_id)

        out: dict[str, list] = {sk: [] for sk in sheet_keys}
        if not parse_ok or not isinstance(parsed, dict):
            if fail_fast:
                raise BatchParseError("JSON 파싱 실패")
            for sk in sheet_keys:
                self._record_batch(
                    sk,
                    page_nums,
                    "parse_fail",
                    0,
                    "JSON 파싱 실패",
                    batch_id=batch_id,
                )
            return out
        invalid_keys = []
        for sk in sheet_keys:
            items = parsed.get(sk)
            invalid = not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
            if invalid:
                invalid_keys.append(sk)
                self._record_batch(
                    sk,
                    page_nums,
                    "parse_fail",
                    0,
                    "스키마 불일치",
                    batch_id=batch_id,
                )
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                out[sk].append(_attach_row_context(item, municipality, page_nums))
            if not invalid:
                self._record_batch(sk, page_nums, "ok", len(out[sk]), batch_id=batch_id)
        if invalid_keys and fail_fast:
            raise ClusterPartialParseError(out, invalid_keys)
        return out

    def _plan_cluster_tasks(
        self,
        routed_pages: dict[str, list[PageContent]],
        municipality: str,
        extraction_prompts: dict[str, str],
        batch_size: int,
    ) -> list[dict]:
        configured_clusters = getattr(config, "EXTRACTION_SHEET_CLUSTERS", [])
        clusters: list[list[str]] = []
        assigned: set[str] = set()
        duplicate_members: list[str] = []
        for configured in configured_clusters:
            members: list[str] = []
            for sheet_key in configured:
                if sheet_key not in _SHEET_CONFIGS:
                    continue
                if sheet_key in assigned:
                    duplicate_members.append(sheet_key)
                    continue
                members.append(sheet_key)
                assigned.add(sheet_key)
            if members:
                clusters.append(members)

        # 사용자 정의 클러스터가 불완전해도 추출 시트를 조용히 누락하지 않는다.
        missing_members = [sheet_key for sheet_key in _SHEET_CONFIGS if sheet_key not in assigned]
        clusters.extend([[sheet_key] for sheet_key in missing_members])

        by_num: dict[int, PageContent] = {}
        for v in routed_pages.values():
            for p in v:
                by_num[p.page_number] = p

        combined_count = sum(len(cluster) > 1 for cluster in clusters)
        singleton_count = sum(len(cluster) == 1 for cluster in clusters)
        print(
            "[에이전트2 텍스트추출] 시트 클러스터링 모드: "
            f"{len(clusters)}개 그룹(결합 {combined_count}, 단독 {singleton_count})"
        )
        if duplicate_members:
            duplicate_label = ", ".join(dict.fromkeys(duplicate_members))
            print(f"  - 중복 시트 정의 제외: {duplicate_label}")
        if missing_members:
            print(f"  - 미정의 시트 단독 보충: {', '.join(missing_members)}")

        tasks: list[dict] = []
        schedule_order = 0
        for members in clusters:
            nums: set[int] = set()
            for sk in members:
                nums |= {p.page_number for p in routed_pages.get(sk, [])}
            if not nums:
                continue
            # Partition by exact membership. Every (sheet, page) pair is
            # identical to per-sheet routing; no union-page over-extraction.
            signatures = {}
            member_nums = {sk: {p.page_number for p in routed_pages.get(sk, [])} for sk in members}
            for n in sorted(nums):
                signature = tuple(sk for sk in members if n in member_nums[sk])
                signatures.setdefault(signature, []).append(by_num[n])
            batches = [(list(signature), batch) for signature, group in signatures.items()
                       for batch in _build_semantic_batches(group, batch_size)]
            for batch_num, (active_members, batch) in enumerate(batches, start=1):
                page_nums = [p.page_number for p in batch]
                page_range = (
                    f"p{page_nums[0]}~{page_nums[-1]}" if len(page_nums) > 1 else f"p{page_nums[0]}"
                )
                task = {
                    "pages": batch,
                    "batch_text": _build_page_text(batch),
                    "batch_num": batch_num,
                    "batch_total": len(batches),
                    "page_range": page_range,
                    "page_nums": page_nums,
                    "schedule_order": schedule_order,
                }
                schedule_order += 1
                if len(active_members) == 1:
                    sheet_key = active_members[0]
                    task.update({
                        "sheet_key": sheet_key,
                        "guideline_prompt": extraction_prompts.get(sheet_key, ""),
                    })
                else:
                    task["members"] = active_members
                tasks.append(task)

        return tasks

    def _extract_clustered(self, routed_pages, municipality, extraction_prompts, batch_size):
        tasks = self._plan_cluster_tasks(routed_pages, municipality, extraction_prompts, batch_size)

        cluster_tasks = self._prepare_tasks(
            [task for task in tasks if "members" in task],
            "cluster",
            extraction_prompts,
        )
        sheet_tasks = self._prepare_tasks(
            [task for task in tasks if "members" not in task],
            "sheet",
            extraction_prompts,
        )
        tasks = sorted(
            [*cluster_tasks, *sheet_tasks],
            key=lambda task: int(task.get("schedule_order", 0)),
        )

        def run_task(task: dict) -> Any:
            if "members" in task:
                return self._run_cluster_task(task, municipality, extraction_prompts)
            return self._run_sheet_task(task, municipality)

        results = parallel_map_collect(
            run_task,
            tasks,
            workers=getattr(config, "TEXT_WORKERS", 4),
            stats_label="text_extraction",
        )
        for task, (res, err) in zip(tasks, results):
            if err is not None:
                preserved = self._record_call_failure(task, err)
                if isinstance(preserved, dict):
                    for sk, items in preserved.items():
                        if isinstance(items, list):
                            self._raw_results[sk] = self._merge_rows(
                                sk, self._raw_results[sk], items,
                            )
                elif isinstance(preserved, list):
                    sheet_key = task["sheet_key"]
                    rows = [row for row in preserved if isinstance(row, dict)]
                    self._raw_results[sheet_key] = self._merge_rows(
                        sheet_key, self._raw_results[sheet_key], rows,
                    )
                continue
            if res is None:
                continue
            if "members" in task:
                for sk, items in res.items():
                    self._raw_results[sk] = self._merge_rows(sk, self._raw_results[sk], items)
            else:
                sheet_key = task["sheet_key"]
                rows = [row for row in res if isinstance(row, dict)]
                self._raw_results[sheet_key] = self._merge_rows(
                    sheet_key, self._raw_results[sheet_key], rows,
                )

    def route_pages(self, pages: list[PageContent]) -> dict[str, list[PageContent]]:
        if getattr(config, "FULL_DOCUMENT_SCAN", False):
            routed = {key: list(pages) for key in _SHEET_CONFIGS}
        else:
            routed = _route_pages_by_sheet(pages)
        self.text_policy = TextPolicy(pages)
        routed = self.text_policy.route(routed)
        source_pages = {p.page_number: p for p in pages}
        for row in self.text_policy.route_audit:
            page = source_pages[row['page']]
            rule = _ROUTE_CONFIGS.get(row['sheet'], {})
            body = (page.text or '') + '\n' + '\n'.join(page.tables or [])
            row['evidence'] = {
                'strong_keywords': [kw for kw in rule.get('strong', []) if kw in body],
                'weak_keywords': [kw for kw in rule.get('weak', []) if kw in body],
                'native_table_count': len(page.tables),
                'context_pages_policy': config.DOCUMENT_ROUTE_CONTEXT_PAGES,
            }
        self.routed_page_nums = {
            key: {page.page_number for page in value}
            for key, value in routed.items()
            if value
        }
        return routed

    @staticmethod
    def _plan_sheet_tasks(routed_pages, extraction_prompts, batch_size):
        tasks = []
        for key, pages in routed_pages.items():
            batches = _build_semantic_batches(pages, batch_size)
            for number, batch in enumerate(batches, start=1):
                nums = [p.page_number for p in batch]
                tasks.append({"sheet_key": key, "pages": batch, "page_nums": nums,
                              "batch_text": _build_page_text(batch), "page_range": _page_range_label(nums),
                              "batch_num": number, "batch_total": len(batches),
                              "guideline_prompt": extraction_prompts.get(key, "")})
        return tasks

    def plan_tasks(self, pages, extraction_prompts=None, batch_size=None):
        """Exact root queue used by extract(), with no LLM or checkpoint writes."""
        prompts = extraction_prompts or {}
        routed = self.route_pages(pages)
        size = config.BATCH_SIZE if batch_size is None else batch_size
        if getattr(config, "EXTRACTION_SHEET_CLUSTERING", False) and not getattr(config, "FULL_DOCUMENT_SCAN", False):
            return self._plan_cluster_tasks(routed, "", prompts, size)
        return self._plan_sheet_tasks(routed, prompts, size)

    def extract_sheet_pages(
        self,
        sheet_key: str,
        sheet_pages: list[PageContent],
        municipality: str,
        extraction_prompts: dict[str, str],
        batch_size: int = config.BATCH_SIZE,
    ) -> list[dict]:
        """
        한 시트의 라우팅 페이지를 배치 단위로 병렬 추출한다.

        기존 extract()의 (시트, 배치) 태스크 의미론을 시트 하나로 좁힌 공개 메서드이며,
        성공·파싱실패 원장은 _extract_sheet, 호출실패 원장은 이 메서드가 기록한다.
        기존 extract()는 이 메서드를 재사용하지 않고 병렬 전체 추출 경로로 분리 유지한다.
        """
        if not sheet_pages:
            return []
        tasks = self._plan_sheet_tasks({sheet_key: sheet_pages}, extraction_prompts, batch_size)

        tasks = self._prepare_tasks(tasks, "sheet", extraction_prompts)
        rows_for_sheet: list[dict] = []
        results = parallel_map_collect(
            lambda task: self._run_sheet_task(task, municipality),
            tasks,
            workers=getattr(config, "TEXT_WORKERS", 4),
            stats_label="text_extraction",
        )
        for task, (items, err) in zip(tasks, results):
            if err is not None:
                preserved = self._record_call_failure(task, err)
                rows = [row for row in (preserved or []) if isinstance(row, dict)]
                rows_for_sheet = self._merge_rows(sheet_key, rows_for_sheet, rows)
                self._raw_results[sheet_key] = self._merge_rows(
                    sheet_key, self._raw_results[sheet_key], rows,
                )
                continue
            rows = [row for row in (items or []) if isinstance(row, dict)]
            rows_for_sheet = self._merge_rows(sheet_key, rows_for_sheet, rows)
            self._raw_results[sheet_key] = self._merge_rows(
                sheet_key,
                self._raw_results[sheet_key],
                rows,
            )
        return rows_for_sheet

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
            tasks = self.plan_tasks(pages, extraction_prompts, batch_size)
            tasks = self._prepare_tasks(tasks, "sheet", extraction_prompts)
            results = parallel_map_collect(
                lambda task: self._run_sheet_task(task, municipality),
                tasks,
                workers=getattr(config, "TEXT_WORKERS", 4),
                stats_label="text_extraction",
            )
            for task, (items, err) in zip(tasks, results):
                if err is not None:
                    preserved = self._record_call_failure(task, err)
                    sheet_key = task["sheet_key"]
                    rows = [row for row in (preserved or []) if isinstance(row, dict)]
                    self._raw_results[sheet_key] = self._merge_rows(
                        sheet_key, self._raw_results[sheet_key], rows,
                    )
                    continue
                sheet_key = task["sheet_key"]
                self._raw_results[sheet_key] = self._merge_rows(
                    sheet_key,
                    self._raw_results[sheet_key],
                    items or [],
                )
            print(f"  [full] 시트별 작업 {len(tasks)}개 추출 완료")

            total = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list)}
            print(f"[에이전트2 텍스트추출] 완료. 누적: {total}")
            return self._raw_results

        routed_pages = self.route_pages(pages)
        route_summary = {k: len(v) for k, v in routed_pages.items() if v}
        print(f"[에이전트2 텍스트추출] 문서 구조 라우팅 완료: {route_summary}")
        self.routed_page_nums = {
            k: {p.page_number for p in v} for k, v in routed_pages.items() if v
        }

        # 시트 클러스터링: 관련 시트를 묶어 페이지 묶음당 1회 호출로 여러 시트를 동시 추출.
        if getattr(config, "EXTRACTION_SHEET_CLUSTERING", False):
            self._extract_clustered(routed_pages, municipality, extraction_prompts, batch_size)
            total = {k: len(v) for k, v in self._raw_results.items() if isinstance(v, list) and v}
            print(f"[에이전트2 텍스트추출] 완료(클러스터). 누적: {total}")
            return self._raw_results

        # (시트, 배치) 조합은 서로 독립적이라 동시에 추출한다. parallel_map이 입력
        # 순서를 보존하므로 누적 순서는 순차 실행과 동일하다(출력 결정성 유지).
        tasks = self._plan_sheet_tasks(routed_pages, extraction_prompts, batch_size)

        tasks = self._prepare_tasks(tasks, "sheet", extraction_prompts)
        results = parallel_map_collect(
            lambda task: self._run_sheet_task(task, municipality),
            tasks,
            workers=getattr(config, "TEXT_WORKERS", 4),
            stats_label="text_extraction",
        )
        for task, (items, err) in zip(tasks, results):
            if err is not None:
                preserved = self._record_call_failure(task, err)
                sheet_key = task["sheet_key"]
                rows = [row for row in (preserved or []) if isinstance(row, dict)]
                self._raw_results[sheet_key] = self._merge_rows(
                    sheet_key, self._raw_results[sheet_key], rows,
                )
                continue
            rows = items or []
            sheet_key = task["sheet_key"]
            self._raw_results[sheet_key] = self._merge_rows(
                sheet_key,
                self._raw_results[sheet_key],
                rows,
            )

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
