"""
프로젝트 전역 설정
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    """환경변수 정수. 0/음수/all/none/unlimited는 '상한 없음'으로 해석."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"0", "-1", "none", "all", "unlimited", "false", "off"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# LLM/로컬 에이전트 실행 설정
#
# 기본값은 Gemini API가 아니라 로컬 Codex CLI입니다. 필요하면 환경변수 또는
# main.py --agent 옵션으로 codex / claude / auto / gemini 중 선택합니다.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
LOCAL_AGENT_MODEL = os.environ.get("LOCAL_AGENT_MODEL", "").strip()
LOCAL_AGENT_TIMEOUT = _env_int("LOCAL_AGENT_TIMEOUT", 900)
CODEX_COMMAND = os.environ.get("CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("CLAUDE_COMMAND", "claude").strip()
GUIDELINE_AGENT_SPEC_ENABLED = _env_bool("GUIDELINE_AGENT_SPEC_ENABLED", True)

# Gemini는 명시적으로 LLM_PROVIDER=gemini를 선택한 경우에만 쓰는 레거시 백엔드입니다.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_TOKENS = 65536  # 레거시 Gemini 백엔드 사용 시 최대 출력 토큰

# 연도 범위 (탄소중립 기본계획 기준)
YEARS = list(range(2018, 2051))

# 배출 부문 (가이드라인 2.6.3절 표준 부문)
SECTORS = ["건물", "수송", "농축산", "폐기물", "흡수원", "전환", "산업", "수소"]

# 배출유형
EMISSION_TYPES = ["직접배출", "간접배출", "흡수원"]

# 온실가스 데이터 종류
GHG_TYPES = ["현황", "전망", "목표"]

# 감축사업 종류
STRATEGY_TYPES = [
    "계획(지표)", "계획(감축량)", "계획(예산)",
    "실적(지표)", "실적(감축량)", "실적(예산)"
]

# 달성여부 코드 (가이드라인 3.3절)
ACHIEVEMENT_STATUS = ["달성", "정상추진", "지연", "미달성"]

# 사업유형 코드 (가이드라인 3.4절)
BUSINESS_TYPES = ["기존", "변경", "신규"]

# ──────────────────────────────────────────────────────────────────────
# 엑셀 시트 헤더 정의 (carbon_guideline.md 5.2절 16개 시트 + 보조 시트)
# ──────────────────────────────────────────────────────────────────────
EXCEL_HEADERS = {
    "00_문서메타": [
        "지자체명", "지자체유형", "계획명", "발간일", "발간기관",
        "계획시작연도", "계획종료연도", "기준연도", "목표연도",
        "법적근거", "점검보고서여부",
    ],
    "01_계획개요": [
        "지자체명", "개요유형", "항목명", "항목값",
        "일자", "이해관계자", "관련법령_계획",
    ],
    "02_지역여건": [
        "지자체명", "지표범주", "지표세부범주", "지표명",
        "연도", "값", "단위", "출처",
    ],
    "03_배출현황_지역": [
        "지자체명", "인벤토리출처", "배출범위", "배출유형",
        "부문", "세부부문", "연도", "배출량", "단위", "흡수원여부",
    ],
    "04_배출현황_관리권한": [
        "지자체명", "인벤토리출처", "관리부문", "세부부문",
        "직간접구분", "연도", "배출량", "단위", "합계포함여부",
    ],
    "05_배출전망": [
        "지자체명", "전망범위", "시나리오", "전망방법코드", "전망방법원문",
        "부문", "세부부문", "연도", "전망값", "단위", "주요가정",
    ],
    "06_감축목표": [
        "지자체명", "목표수준", "목표범위", "부문",
        "기준연도", "기준배출량", "목표연도", "배출전망",
        "목표감축량", "목표배출량", "감축률(%)",
    ],
    "07_비전전략": [
        "지자체명", "비전문구", "전략수준", "전략명",
        "부문", "설명", "키워드",
    ],
    "08_감축사업목록": [
        "지자체명", "관리번호", "부문", "핵심과제",
        "사업명", "사업유형", "주관부서", "협조부서",
        "사업개요", "성과지표명", "성과지표단위", "정량여부",
    ],
    "09_연차별이행계획": [
        "지자체명", "관리번호", "사업명",
        "기간시작", "기간종료", "연도", "연간계획",
        "목표물량", "목표단위", "규제혁신계획", "입법계획",
    ],
    "10_정량감축량": [
        "지자체명", "관리번호", "사업명", "연도",
        "모니터링인자", "활동량", "활동단위",
        "감축원단위ID", "감축원단위값", "예상감축량", "단위",
    ],
    "11_재정투자계획": [
        "지자체명", "계획구분", "부문", "사업명",
        "재원구분", "연도", "예산액", "예산단위",
    ],
    "12_대응기반강화": [
        "지자체명", "대응기반영역", "과제ID", "과제명",
        "정책방향", "주요내용", "대상", "주관부서", "기간",
    ],
    "13_이행관리환류": [
        "지자체명", "거버넌스기구", "역할", "담당부서",
        "절차단계", "기한", "산출물",
    ],
    "14_점검실적": [
        "지자체명", "점검연도", "부문", "관리번호", "사업명",
        "연간계획", "이행실적", "소요예산", "달성여부", "사업유형",
    ],
    "15_변경과제_조치": [
        "지자체명", "점검연도", "부문", "관리번호", "사업명",
        "변경전", "변경후", "변경사유",
        "지연미달성사유", "조치계획",
    ],
    "16_시각자료목록": [
        "지자체명", "시각자료ID", "캡션", "유형",
        "데이터포함여부", "추출값요약", "디지타이징필요", "관련시트",
    ],
}

# 추출 대상 시트 키 목록 (파이프라인에서 LLM으로 추출하는 시트)
EXTRACTION_SHEETS = [
    "document_meta", "plan_overview", "regional_conditions",
    "emissions_regional", "emissions_management",
    "emissions_forecast", "reduction_targets",
    "vision_strategy", "mitigation_projects",
    "annual_implementation", "quantitative_reductions",
    "financial_plan", "foundation_measures",
    "governance_feedback", "monitoring_performance",
    "changes_actions",
]

# 시트 내부 키 → 엑셀 시트명 매핑
SHEET_KEY_TO_NAME = {
    "document_meta": "00_문서메타",
    "plan_overview": "01_계획개요",
    "regional_conditions": "02_지역여건",
    "emissions_regional": "03_배출현황_지역",
    "emissions_management": "04_배출현황_관리권한",
    "emissions_forecast": "05_배출전망",
    "reduction_targets": "06_감축목표",
    "vision_strategy": "07_비전전략",
    "mitigation_projects": "08_감축사업목록",
    "annual_implementation": "09_연차별이행계획",
    "quantitative_reductions": "10_정량감축량",
    "financial_plan": "11_재정투자계획",
    "foundation_measures": "12_대응기반강화",
    "governance_feedback": "13_이행관리환류",
    "monitoring_performance": "14_점검실적",
    "changes_actions": "15_변경과제_조치",
    "visual_inventory": "16_시각자료목록",
}

# PDF 페이지 배치 처리 크기.
# 너무 크면 출력 JSON이 길어져 파싱 실패가 늘 수 있어 안정성 위주로 둔다.
BATCH_SIZE = _env_int("BATCH_SIZE", 15)

# 문서 구조 라우팅 설정
# 관련 페이지 앞뒤 몇 페이지까지 함께 LLM에 전달할지 결정
DOCUMENT_ROUTE_CONTEXT_PAGES = 1
# 시트별 후보 페이지로 선택할 최소 점수.
# FULL_DOCUMENT_SCAN=1이면 모든 페이지를 후보로 넘긴다(느리지만 누락 방지).
FULL_DOCUMENT_SCAN = _env_bool("FULL_DOCUMENT_SCAN", False)
DOCUMENT_ROUTE_MIN_SCORE = _env_int("DOCUMENT_ROUTE_MIN_SCORE", -9999 if FULL_DOCUMENT_SCAN else 3)
# 시트별 라우팅 후보 페이지 상한. 기본값은 없음.
# 필요한 경우에만 DOCUMENT_ROUTE_MAX_PAGES_* 환경변수로 명시적으로 샘플링한다.
DOCUMENT_ROUTE_MAX_PAGES = {
    "vehicle": _env_optional_int("DOCUMENT_ROUTE_MAX_PAGES_VEHICLE", None),
    "energy": _env_optional_int("DOCUMENT_ROUTE_MAX_PAGES_ENERGY", None),
    "ghg": _env_optional_int("DOCUMENT_ROUTE_MAX_PAGES_GHG", None),
    "strategy": _env_optional_int("DOCUMENT_ROUTE_MAX_PAGES_STRATEGY", None),
    "summary": _env_optional_int("DOCUMENT_ROUTE_MAX_PAGES_SUMMARY", None),
}

# 이미지 분석 최대 개수. 기본값은 없음(=triage 통과 후보 전부 분석).
# 테스트/디버그 때만 MAX_IMAGES=30처럼 명시적으로 제한한다.
MAX_IMAGES = _env_optional_int("MAX_IMAGES", None)

# ChartQA 스타일 이미지 triage 설정
# True이면 전체 이미지에 대해 로컬 휴리스틱으로 그래프/표/도표 후보를 먼저 선별
IMAGE_TRIAGE_ENABLED = True
# triage 점수 기준. 낮출수록 더 많이 분석하고, 높일수록 더 엄격하게 거른다.
IMAGE_TRIAGE_MIN_SCORE = 5
# 점수가 낮더라도 페이지 전체 렌더링 이미지는 문맥상 중요하면 보존할지 여부
IMAGE_TRIAGE_KEEP_RENDERED_CONTEXT = True
# DePlot 아이디어를 차용해 그래프/차트 이미지를 표 형태 JSON으로 먼저 변환
IMAGE_CHART_TABLE_EXTRACTION = True
# 전수 이미지 분석 시 여러 이미지를 한 번의 로컬 에이전트 호출로 묶는다.
IMAGE_ANALYSIS_BATCH_SIZE = _env_int("IMAGE_ANALYSIS_BATCH_SIZE", 8)
# 그래프 판독값을 본 시트에 자동 병합할 최소 신뢰도.
# low는 별도 판독결과 시트에만 남기고 본 데이터에는 병합하지 않는다.
IMAGE_CHART_MERGE_MIN_CONFIDENCE = "medium"
# 그래프 판독값 자동 반영 시 허용할 연도. 기준연도 2005 등은 판독결과 시트에만 남긴다.
IMAGE_CHART_MERGE_YEARS = YEARS
# 참고자료/해외사례/목차성 이미지는 판독결과에는 남기되 본 시트에는 자동 반영하지 않는다.
IMAGE_CHART_REFERENCE_KEYWORDS = [
    "뉴욕시", "런던", "파리", "도쿄", "세계도시", "주요국", "국내외",
    "COP", "IPCC", "UN", "EU", "OECD", "사례", "동향", "목차",
    "우리나라", "국가 온실가스", "국가 감축목표", "중앙정부", "NDC",
]
# True이면 참고자료/해외사례/목차성 페이지 이미지를 Vision 호출 전에 제외한다.
IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT = True

# 1차 추출 후 빈칸이 큰 행만 좁은 문맥으로 다시 보완
GAP_FILL_ENABLED = _env_bool("GAP_FILL_ENABLED", True)
GAP_FILL_MAX_TARGETS = {
    "vehicle": _env_optional_int("GAP_FILL_MAX_TARGETS_VEHICLE", None),
    "energy": _env_optional_int("GAP_FILL_MAX_TARGETS_ENERGY", None),
    "ghg": _env_optional_int("GAP_FILL_MAX_TARGETS_GHG", None),
    "strategy": _env_optional_int("GAP_FILL_MAX_TARGETS_STRATEGY", None),
}
GAP_FILL_CONTEXT_PAGES = 8
GAP_FILL_TARGET_BATCH_SIZE = 10

# 자동차/에너지처럼 원문 구간이 보고서마다 달라지는 시트는
# 전체 문서에서 관련 구간을 다시 점수화해 집중 재추출한다.
FOCUSED_GAP_FILL_ENABLED = True
FOCUSED_GAP_FILL_CONTEXT_PAGES = 2
FOCUSED_GAP_FILL_MAX_ANCHORS = {
    "vehicle": _env_optional_int("FOCUSED_GAP_FILL_MAX_ANCHORS_VEHICLE", None),
    "energy": _env_optional_int("FOCUSED_GAP_FILL_MAX_ANCHORS_ENERGY", None),
}
FOCUSED_GAP_FILL_MIN_SCORE = {
    "vehicle": 5,
    "energy": 5,
}
FOCUSED_GAP_FILL_BATCH_PAGES = 6

# 이미지 DPI (PDF → 이미지 변환 시)
IMAGE_DPI = 150

# 최대 재시도 횟수
MAX_RETRIES = 3

# 이미지 최대 크기 (픽셀, 긴 변 기준)
MAX_IMAGE_SIZE = 1568

# Vision이 유효 차트로 반환하지 못한 이미지 페이지에서 텍스트/표 기반 검토 후보를
# 남기는 fallback 상한. 기본값은 없음(=발견한 후보 전부 기록).
IMAGE_FALLBACK_MAX_OBSERVATIONS = _env_optional_int("IMAGE_FALLBACK_MAX_OBSERVATIONS", None)

# main 기반 선별 통합. 모델/provider/reasoning 설정은 위의 기존 계약을 유지한다.
# 객체 추출·OCR은 명시적으로 켜기 전에는 추가 모델 호출을 만들지 않는다.
EXTRACTION_OBJECT_BATCHING_ENABLED = _env_bool("EXTRACTION_OBJECT_BATCHING_ENABLED", False)
MAX_BATCH_CHARS = _env_optional_int("MAX_BATCH_CHARS", None)
MAX_BATCH_TABLE_CELLS = _env_optional_int("MAX_BATCH_TABLE_CELLS", None)
TABLE_OBJECT_ROWS_PER_CHUNK = _env_int("TABLE_OBJECT_ROWS_PER_CHUNK", 20)
SELECTIVE_OCR_ENABLED = _env_bool("SELECTIVE_OCR_ENABLED", False)
PHYSICAL_OBJECT_MERGE_ENABLED = _env_bool("PHYSICAL_OBJECT_MERGE_ENABLED", True)
OCR_BACKEND = os.environ.get("OCR_BACKEND", "directory").strip().lower()
OCR_RESULTS_DIR = os.environ.get("OCR_RESULTS_DIR", "").strip()
OCR_NATIVE_CONFIDENCE_THRESHOLD = _env_float("OCR_NATIVE_CONFIDENCE_THRESHOLD", 0.78)
OCR_OBJECT_MATCH_MIN_IOU = _env_float("OCR_OBJECT_MATCH_MIN_IOU", 0.55)
OCR_ALLOW_LEGACY_UNLOCATED = _env_bool("OCR_ALLOW_LEGACY_UNLOCATED", False)
OCR_REPLACE_MIN_CELLS = _env_int("OCR_REPLACE_MIN_CELLS", 2)
OCR_REPLACE_CELL_RATIO = _env_float("OCR_REPLACE_CELL_RATIO", 1.10)
OCR_COMPLEX_TABLE_ROWS = _env_int("OCR_COMPLEX_TABLE_ROWS", 45)
OCR_COMPLEX_TABLE_COLUMNS = _env_int("OCR_COMPLEX_TABLE_COLUMNS", 12)
OCR_RENDER_DPI = _env_int("OCR_RENDER_DPI", 200)
OCR_RENDER_CACHE_ENABLED = _env_bool("OCR_RENDER_CACHE_ENABLED", True)
OCR_SPATIAL_RECONSTRUCTION_ENABLED = _env_bool("OCR_SPATIAL_RECONSTRUCTION_ENABLED", True)
OCR_FULL_PAGE_CONTEXT_ENABLED = _env_bool("OCR_FULL_PAGE_CONTEXT_ENABLED", False)
OCR_FULL_PAGE_CONTEXT_MAX_OBJECT_RATIO = 0.15
OCR_INDEX_CAPTION_MIN_COUNT = 5
OCR_MULTI_PANEL_GAP = 24
OCR_CAPTION_REGION_MAX_HEIGHT_RATIO = 0.20
SEMANTIC_ROUTING_ENABLED = _env_bool("SEMANTIC_ROUTING_ENABLED", False)
SEMANTIC_AUTO_RECLASSIFY = _env_bool("SEMANTIC_AUTO_RECLASSIFY", False)
SEMANTIC_CONTRACT_GUARDS_ENABLED = _env_bool("SEMANTIC_CONTRACT_GUARDS_ENABLED", False)
RUN_STATE_ENABLED = _env_bool("RUN_STATE_ENABLED", False)
RUN_STATE_DIR = os.environ.get("RUN_STATE_DIR", ".cache/run_state").strip()
EXTRACTION_RESUME = _env_bool("EXTRACTION_RESUME", False)
EXTRACTION_RETRY_FAILED_ONLY = _env_bool("EXTRACTION_RETRY_FAILED_ONLY", False)
# 원문 누락 후보 기록만 수행. 추출/라우팅/모델/업무 값은 변경하지 않는다.
SOURCE_COVERAGE_MODE = os.environ.get("SOURCE_COVERAGE_MODE", "off").strip().lower()
if SOURCE_COVERAGE_MODE not in {"off", "audit"}:
    raise ValueError("SOURCE_COVERAGE_MODE must be off or audit")
# 저장 후 진단만 수행한다. 기존 최소 구조 재시도/모델 설정은 바꾸지 않는다.
QUALITY_REPORT_ENABLED = _env_bool("QUALITY_REPORT_ENABLED", True)
EVALUATION_MODE = os.environ.get("EVALUATION_MODE", "auto").strip().lower()
if EVALUATION_MODE not in {"auto", "off", "required"}:
    raise ValueError("EVALUATION_MODE must be auto, off or required")
EVALUATION_MANIFEST_PATH = os.environ.get("EVALUATION_MANIFEST_PATH", "").strip()
EVALUATION_DATASET_ID = os.environ.get("EVALUATION_DATASET_ID", "").strip()
ALLOW_DRAFT_EVALUATION = _env_bool("ALLOW_DRAFT_EVALUATION", False)
EVALUATE_HOLDOUT = _env_bool("EVALUATE_HOLDOUT", False)
PROVENANCE_ENABLED = _env_bool("PROVENANCE_ENABLED", True)
INTERNAL_OBJECT_KEYS = {
    "document_objects", "object_processing_states", "extraction_records", "merge_conflicts",
    "ocr_import_audit", "ocr_reextraction_audit", "semantic_contract_review",
    "_semantic_contract_quarantine", "_key_blank_stats", "reduction_target_context",
    "source_validation", "vision_execution", "semantic_routing_report", "object_evidence_audit",
    "source_coverage_snapshot",
}
PROVENANCE_HEADERS = ["출처페이지", "근거ID", "객체ID", "추출원천", "검수상태", "추출엔진", "OCR보완여부", "출처계보"]
if PROVENANCE_ENABLED:
    for _sheet_key in EXTRACTION_SHEETS:
        for _header in PROVENANCE_HEADERS:
            if _header not in EXCEL_HEADERS[SHEET_KEY_TO_NAME[_sheet_key]]:
                EXCEL_HEADERS[SHEET_KEY_TO_NAME[_sheet_key]].append(_header)
EXCEL_HEADERS["17_검증리포트"] = ["지자체명", "대상시트", "항목", "사유", "출처페이지", "감사파일"]
SHEET_KEY_TO_NAME["validation_report"] = "17_검증리포트"
for _header in ("객체ID", "근거ID", "출처페이지", "엔진", "처리상태", "본문반영행수"):
    if _header not in EXCEL_HEADERS["16_시각자료목록"]:
        EXCEL_HEADERS["16_시각자료목록"].append(_header)
