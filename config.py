"""
프로젝트 전역 설정
"""
# noqa: SIZE_OK — 엑셀 헤더·시트 계약과 환경변수 기본값을 한 곳에 고정하는 순수 설정 파일.
import json
import os
from dotenv import load_dotenv

load_dotenv()

PIPELINE_VERSION = "v5.3"


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


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_pattern_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return list(default)
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return list(default)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return list(default)
    return [item.strip() for item in stripped.split(";;") if item.strip()]


# LLM/로컬 에이전트 실행 설정
#
# 기본값은 Gemini API입니다. 필요하면 환경변수 또는 main.py --agent 옵션으로
# gemini / openai / codex / claude / auto 중 선택합니다.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
LOCAL_AGENT_MODEL = os.environ.get("LOCAL_AGENT_MODEL", "").strip()
# 정상 추출 호출은 보통 1~2분 내 끝난다. 900초 기본값은 hang을 15분씩 방치해
# 로컬 에이전트 실행을 수 시간 지연시켰으므로, 필요 시 env로만 되돌린다.
LOCAL_AGENT_TIMEOUT = _env_int("LOCAL_AGENT_TIMEOUT", 300)
# vision 호출은 이미지 페이로드·프롬프트가 커 텍스트보다 오래 걸린다. 로컬 에이전트의
# vision 단계(stage="vision")에만 별도 타임아웃을 준다(v8-1 S3-3).
LOCAL_AGENT_VISION_TIMEOUT = _env_int("LOCAL_AGENT_VISION_TIMEOUT", 600)
# 타임아웃은 quota로 추정하지 않는다. 최초 호출 뒤 허용할 추가 재시도 횟수와
# 재시도 간격을 별도로 제한해 한 배치가 수십 분씩 점유하지 않게 한다.
LOCAL_AGENT_TIMEOUT_RETRIES = _env_int("LOCAL_AGENT_TIMEOUT_RETRIES", 1)
LOCAL_AGENT_TIMEOUT_RETRY_DELAY_SECONDS = _env_int(
    "LOCAL_AGENT_TIMEOUT_RETRY_DELAY_SECONDS", 5
)
# 텍스트 추출 배치가 끝내 타임아웃되면 페이지 묶음을 더 작게 나눠 다시 시도한다.
# 모델 호출 전에도 문자 수가 큰 배치를 미리 분할해, 300초 타임아웃을 먼저 소비하지 않는다.
EXTRACTION_SPLIT_ON_TIMEOUT = _env_bool("EXTRACTION_SPLIT_ON_TIMEOUT", True)
EXTRACTION_TIMEOUT_MIN_BATCH_PAGES = _env_int("EXTRACTION_TIMEOUT_MIN_BATCH_PAGES", 2)
EXTRACTION_TIMEOUT_MAX_SPLIT_DEPTH = _env_int("EXTRACTION_TIMEOUT_MAX_SPLIT_DEPTH", 6)
EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS = _env_int(
    "EXTRACTION_TIMEOUT_RECOVERY_BUDGET_SECONDS", 900
)
# 타임아웃뿐 아니라 최종 호출 실패·JSON 파싱 실패도 작은 배치에서 복구한다.
EXTRACTION_SPLIT_ON_FAILURE = _env_bool("EXTRACTION_SPLIT_ON_FAILURE", True)
# 일반 호출의 최대 배치 문자 수. 초과 배치는 호출 전에 페이지/문서객체 단위로 분할한다.
# 이 상한은 로컬 에이전트(codex/claude) 타임아웃 완화용이다. API 백엔드(gemini/openai)는
# 별도 상한 EXTRACTION_MAX_BATCH_CHARS_API를 쓰며 기본 0(=상한 없음, BATCH_SIZE 페이지
# 묶음 그대로) — 짧은 배치가 요약표·본문표·부록표의 같은 사실을 표기 변형으로 중복
# 산출하는 과잉 추출을 막는다(v8-1 S2-3). 실패 복구 자식 상한은 양쪽 공통이다.
EXTRACTION_MAX_BATCH_CHARS = _env_int("EXTRACTION_MAX_BATCH_CHARS", 18000)
EXTRACTION_MAX_BATCH_CHARS_API = _env_int("EXTRACTION_MAX_BATCH_CHARS_API", 0)
# 배치당 표 셀 수 상한(0 = 비활성). 행렬형 표(연도×부문)는 셀 하나가 출력 행 하나로
# 펼쳐져, 한 배치에 표 셀이 몰리면 로컬 에이전트가 출력 예산 때문에 표를 요약해 버린다
# (v8-1 서울 실측: 셀 ~640 배치는 완전 전사, ~1,400+ 배치는 20~50행으로 요약).
# 문서의 표 구조만 사용하므로 지자체 불변. 문자 상한과 독립적으로 배치를 닫는다.
EXTRACTION_MAX_BATCH_TABLE_CELLS = _env_int("EXTRACTION_MAX_BATCH_TABLE_CELLS", 0)
# 실패 복구 자식은 일반 배치보다 작게 유지한다.
EXTRACTION_RECOVERY_MAX_BATCH_CHARS = _env_int("EXTRACTION_RECOVERY_MAX_BATCH_CHARS", 12000)
# 단일 페이지의 큰 표는 헤더를 반복하면서 이 행 수 단위로 잘라 정규화한다.
EXTRACTION_TABLE_ROWS_PER_BATCH = _env_int("EXTRACTION_TABLE_ROWS_PER_BATCH", 20)

# 성공 배치 결과와 실패 상태를 입력/프롬프트/모델 해시별로 영속화한다.
RUN_STATE_ENABLED = _env_bool("RUN_STATE_ENABLED", True)
RUN_STATE_DIR = os.environ.get("RUN_STATE_DIR", ".cache/runs").strip()
EXTRACTION_RESUME = _env_bool("EXTRACTION_RESUME", False)
EXTRACTION_RETRY_FAILED_ONLY = _env_bool("EXTRACTION_RETRY_FAILED_ONLY", False)
CODEX_COMMAND = os.environ.get("CODEX_COMMAND", "codex").strip()
CLAUDE_COMMAND = os.environ.get("CLAUDE_COMMAND", "claude").strip()
GUIDELINE_STRUCTURED_INJECTION = _env_bool("GUIDELINE_STRUCTURED_INJECTION", True)
GUIDELINE_PROMPT_MAX_CHARS = _env_int("GUIDELINE_PROMPT_MAX_CHARS", 3000)
APPENDIX4_MATCH_THRESHOLD = _env_float("APPENDIX4_MATCH_THRESHOLD", 0.55)
APPENDIX3_MATCH_THRESHOLD = _env_float("APPENDIX3_MATCH_THRESHOLD", APPENDIX4_MATCH_THRESHOLD)
CODEBOOK_SHEET_ENABLED = _env_bool("CODEBOOK_SHEET_ENABLED", True)
DATA_STATUS_ENABLED = _env_bool("DATA_STATUS_ENABLED", True)
MIN_DOCUMENT_TEXT_CHARS = _env_int("MIN_DOCUMENT_TEXT_CHARS", 500)

# 최종 데이터의 원문 근거를 LLM 없이 행 단위로 대조한다. 원문 표·그래프
# 인벤토리가 비활성 또는 미연결인 실행에만 보수적인 품질 점수 상한을 적용한다.
SOURCE_VERIFICATION_ENABLED = _env_bool("SOURCE_VERIFICATION_ENABLED", True)
SOURCE_VERIFICATION_PAGE_RADIUS = _env_int("SOURCE_VERIFICATION_PAGE_RADIUS", 1)
SOURCE_VERIFICATION_MAX_TERMS = _env_int("SOURCE_VERIFICATION_MAX_TERMS", 12)
SOURCE_VERIFICATION_GLOBAL_SEARCH = _env_bool("SOURCE_VERIFICATION_GLOBAL_SEARCH", True)
SOURCE_VERIFICATION_MARK_PDF = _env_bool("SOURCE_VERIFICATION_MARK_PDF", True)
SOURCE_VERIFICATION_MAX_MARKS = _env_int("SOURCE_VERIFICATION_MAX_MARKS", 10000)
SOURCE_OBJECT_INVENTORY_ENABLED = _env_bool("SOURCE_OBJECT_INVENTORY_ENABLED", True)
# 표·그림 객체 완전성에서 같은 페이지 행만 있는 경우는 부분 확인으로 계산한다.
SOURCE_OBJECT_PARTIAL_WEIGHT = _env_float("SOURCE_OBJECT_PARTIAL_WEIGHT", 0.5)
# 표 번호·캡션·섹션을 이용해 결과 행이 올바른 시트에 배치됐는지 별도로 검증한다.
# 자동 이동은 계획 시작 이후의 배출현황 행이 명시적인 전망표에 놓인 경우로 제한한다.
# 자동 이동은 축 필드(03 배출유형·04 직간접구분)가 빈 전망 연도행에만 적용한다(v8-1 S2-5):
# 축이 채워진 행은 인벤토리 축을 가진 표(예: '관리 권한 내 배출량 전망')에서 온 것이라
# 골든 계약(v7-4 — 05에 지역전체/관리권한 축 필드 없음)대로 03/04에 남긴다.
SEMANTIC_ROUTING_ENABLED = _env_bool("SEMANTIC_ROUTING_ENABLED", True)
SEMANTIC_ROUTING_AUTO_RECLASSIFY = _env_bool("SEMANTIC_ROUTING_AUTO_RECLASSIFY", True)
SEMANTIC_ROUTING_MIN_SCORE = _env_float("SEMANTIC_ROUTING_MIN_SCORE", 5.0)
SEMANTIC_ROUTING_MIN_MARGIN = _env_float("SEMANTIC_ROUTING_MIN_MARGIN", 1.5)
# 한 표가 계획개요와 감축목표처럼 둘 이상의 본문 시트에 합법적으로 대응할 때,
# 최고 점수와 가까운 후보를 허용 집합으로 보존한다. 단일 행 자동 이동에는 사용하지 않는다.
SEMANTIC_ROUTING_ALLOWED_SCORE_DELTA = _env_float(
    "SEMANTIC_ROUTING_ALLOWED_SCORE_DELTA", 1.5
)
SEMANTIC_ROUTING_MAX_ALLOWED_TARGETS = _env_int(
    "SEMANTIC_ROUTING_MAX_ALLOWED_TARGETS", 3
)
# 06_감축목표의 세부사업·연차경로를 페이지 겹침만으로 재태깅하지 않고,
# 근거 객체/캡션/섹션/사업 식별자/연도 구조를 함께 평가한다.
# context: 새 분류기, legacy: v7-1 페이지·연속연도 규칙, off: 재태깅 없음.
REDUCTION_TARGET_CONTEXT_MODE = os.environ.get(
    "REDUCTION_TARGET_CONTEXT_MODE", "context"
).strip().lower()
if REDUCTION_TARGET_CONTEXT_MODE not in {"context", "legacy", "off"}:
    REDUCTION_TARGET_CONTEXT_MODE = "context"
REDUCTION_TARGET_CONTEXT_MIN_SCORE = _env_float(
    "REDUCTION_TARGET_CONTEXT_MIN_SCORE", 6.0
)
REDUCTION_TARGET_CONTEXT_MIN_MARGIN = _env_float(
    "REDUCTION_TARGET_CONTEXT_MIN_MARGIN", 2.0
)
REDUCTION_TARGET_CONTEXT_REVIEW_SCORE = _env_float(
    "REDUCTION_TARGET_CONTEXT_REVIEW_SCORE", 4.0
)
REDUCTION_TARGET_CONTEXT_MIN_ANNUAL_RUN = _env_int(
    "REDUCTION_TARGET_CONTEXT_MIN_ANNUAL_RUN", 4
)
# 06·12 시트의 행 증식을 보수적으로 억제한다. 고정 페이지나 지자체명 대신
# 시트 의미 계약과 08·10 시트의 동일 근거를 사용하며, 제외 행은 내부 원장에 남긴다.
# 12a 독립 시트 승격은 여러 지자체 문서에서 필드 밀도를 검증한 뒤 별도로 진행한다.
SEMANTIC_OVEREXTRACTION_GUARD_ENABLED = _env_bool(
    "SEMANTIC_OVEREXTRACTION_GUARD_ENABLED", True
)
QUALITY_THRESHOLD = _env_float("QUALITY_THRESHOLD", 70.0)
QUALITY_MAX_WITHOUT_SOURCE_INVENTORY = _env_float("QUALITY_MAX_WITHOUT_SOURCE_INVENTORY", 95.0)

# 고정 골든셋/홀드아웃 평가는 일반 실행과 분리된 명시적 테스트 모드다.
EVALUATION_MANIFEST_PATH = os.environ.get(
    "EVALUATION_MANIFEST_PATH",
    "data/evaluation/benchmark_manifest.json",
).strip()

# 추출은 단발 JSON 작업이라 레포 파일·MCP 서버·스킬·프로젝트 메모리(CLAUDE.md)가 불필요하다.
# True이면 claude/codex를 중립 임시 디렉터리에서 실행하고, claude는 MCP/스킬/설정/동적
# 시스템 프롬프트 섹션을 끈 채 호출해 호출당 세션 오버헤드를 제거한다. 추출 출력에는 영향 없음.
CLAUDE_MINIMAL_SESSION = _env_bool("CLAUDE_MINIMAL_SESSION", True)

# Gemini API 설정
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
# vision(이미지·차트 판독) 단계의 Gemini 기본 모델.
# 서울 시각요소 벤치마크(docs/benchmark/서울시각요소_VLM벤치마크_v1.md) 실측에서
# flash-lite 리콜 0.707 vs 2.5-pro 0.976으로 격차가 커, API 모드의 vision 기본만
# pro로 올린다(텍스트 추출 기본은 비용 때문에 flash-lite 유지). 채택 승인 2026-07-07.
# 우선순위: STAGE_MODEL_VISION > GEMINI_VISION_MODEL > MODEL. claude 에이전트
# 모드의 vision은 이 값과 무관하게 CLI 기본 모델을 쓴다(구독 quota, 비용 0).
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-2.5-pro").strip()
# 에이전트(codex) 모드 vision 기본 모델.
# 동일 통제 벤치마크(docs/benchmark/서울시각요소_VLM벤치마크_v1.md §3, 2026-07-10
# 추가 측정)에서 gpt-5.6-luna(reasoning medium)가 gpt-5.5(xhigh)를 리콜
# (0.963 vs 0.951)·속도(1,300초 vs 2,869초) 모두 상회해 CLI 기본 모델 대신
# 명시 지정한다. 채택 승인 2026-07-10.
# 우선순위: STAGE_MODEL_VISION > CODEX_VISION_MODEL. 빈 값이면 CLI 기본 모델.
CODEX_VISION_MODEL = os.environ.get("CODEX_VISION_MODEL", "gpt-5.6-luna").strip()
# 에이전트(codex) 모드 텍스트 기본 모델.
# 우선순위: STAGE_MODEL_* > LOCAL_AGENT_MODEL > CODEX_TEXT_MODEL.
# 빈 값이면 Codex CLI 기본 모델을 사용한다.
CODEX_TEXT_MODEL = os.environ.get("CODEX_TEXT_MODEL", "gpt-5.6-luna").strip()
MAX_TOKENS = 65536  # Gemini 백엔드 사용 시 최대 출력 토큰

# Gemini 503/일시 과부하 대응 설정.
# 429/ResourceExhausted는 계정·quota 문제로 보고 중단하지만, 503은 해당 호출만
# 빈 JSON으로 처리해 전체 파이프라인이 중간에 죽지 않도록 한다.
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 4)
GEMINI_RETRY_BASE_SECONDS = _env_int("GEMINI_RETRY_BASE_SECONDS", 20)
GEMINI_RETRY_MAX_SECONDS = _env_int("GEMINI_RETRY_MAX_SECONDS", 90)
GEMINI_FAIL_SOFT_ON_TRANSIENT = _env_bool("GEMINI_FAIL_SOFT_ON_TRANSIENT", True)
GEMINI_REQUEST_TIMEOUT_SECONDS = _env_int("GEMINI_REQUEST_TIMEOUT_SECONDS", 180)
# 추출 재현성을 우선한다. 일부 추론 모델/CLI는 이 값을 지원하지 않으므로 Gemini
# GenerateContent 요청에만 직접 적용하고, 나머지는 캐시·체크포인트로 고정한다.
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.0)

# OpenAI API 설정.
# Gemini Flash 계열과 비교 테스트하기 위한 기본 모델은 사용자가 지정한 gpt-5.4-mini로 둔다.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
OPENAI_MAX_OUTPUT_TOKENS = _env_int("OPENAI_MAX_OUTPUT_TOKENS", 32768)
OPENAI_MAX_RETRIES = _env_int("OPENAI_MAX_RETRIES", 4)
OPENAI_RETRY_BASE_SECONDS = _env_int("OPENAI_RETRY_BASE_SECONDS", 10)
OPENAI_RETRY_MAX_SECONDS = _env_int("OPENAI_RETRY_MAX_SECONDS", 60)
OPENAI_FAIL_SOFT_ON_TRANSIENT = _env_bool("OPENAI_FAIL_SOFT_ON_TRANSIENT", True)

STAGE_PROVIDERS = {
    "extraction": os.environ.get("STAGE_PROVIDER_EXTRACTION", "").strip().lower(),
    "vision": os.environ.get("STAGE_PROVIDER_VISION", "").strip().lower(),
    "gap_fill": os.environ.get("STAGE_PROVIDER_GAP_FILL", "").strip().lower(),
    "review": os.environ.get("STAGE_PROVIDER_REVIEW", "").strip().lower(),
}
STAGE_MODELS = {
    "extraction": os.environ.get("STAGE_MODEL_EXTRACTION", "").strip(),
    "vision": os.environ.get("STAGE_MODEL_VISION", "").strip(),
    "gap_fill": os.environ.get("STAGE_MODEL_GAP_FILL", "").strip(),
    "review": os.environ.get("STAGE_MODEL_REVIEW", "").strip(),
}

# 로컬 에이전트가 명시적으로 "model at capacity"를 반환하면 같은 모델에 대한
# 후속 호출을 잠시 차단한다. 대체 모델은 단계별로 명시한 경우에만 사용해, 기본
# 실행의 모델·정확도 계약이 조용히 바뀌지 않게 한다.
LLM_CAPACITY_CIRCUIT_ENABLED = _env_bool("LLM_CAPACITY_CIRCUIT_ENABLED", True)
LLM_CAPACITY_FAILURE_THRESHOLD = _env_int("LLM_CAPACITY_FAILURE_THRESHOLD", 1)
LLM_CAPACITY_COOLDOWN_SECONDS = _env_int("LLM_CAPACITY_COOLDOWN_SECONDS", 120)
LLM_CAPACITY_FALLBACK_ENABLED = _env_bool("LLM_CAPACITY_FALLBACK_ENABLED", True)
LLM_CAPACITY_FALLBACK_MODEL = os.environ.get("LLM_CAPACITY_FALLBACK_MODEL", "").strip()
STAGE_FALLBACK_MODELS = {
    "extraction": os.environ.get("STAGE_FALLBACK_MODEL_EXTRACTION", "").strip(),
    "vision": os.environ.get("STAGE_FALLBACK_MODEL_VISION", "").strip(),
    "gap_fill": os.environ.get("STAGE_FALLBACK_MODEL_GAP_FILL", "").strip(),
    "review": os.environ.get("STAGE_FALLBACK_MODEL_REVIEW", "").strip(),
}

# GPT-Mini/OpenAI 기본본 + Gemini 타깃 검수 구조.
# 기본값은 비용/시간 보호를 위해 비활성이고, 테스트 시 --hybrid-review 또는
# HYBRID_REVIEW_ENABLED=1로 켠다. 검수 결과는 자동 병합하지 않고 별도 후보 시트에 남긴다.
HYBRID_REVIEW_ENABLED = _env_bool("HYBRID_REVIEW_ENABLED", False)
SHEET_CLOSED_LOOP_ENABLED = _env_bool("SHEET_CLOSED_LOOP_ENABLED", False)
HYBRID_REVIEW_PROVIDER = os.environ.get("HYBRID_REVIEW_PROVIDER", "gemini").strip().lower()
HYBRID_REVIEW_MODEL = os.environ.get("HYBRID_REVIEW_MODEL", MODEL).strip()
HYBRID_REVIEW_TARGETED = _env_bool("HYBRID_REVIEW_TARGETED", True)
HYBRID_REVIEW_INCLUDE_FAILED_PAGES = _env_bool("HYBRID_REVIEW_INCLUDE_FAILED_PAGES", False)
HYBRID_REVIEW_TARGET_MAX_ROWS = _env_int("HYBRID_REVIEW_TARGET_MAX_ROWS", 50)
HYBRID_REVIEW_SHEETS = _env_list(
    "HYBRID_REVIEW_SHEETS",
    [
        "emissions_management",
        "reduction_targets",
        "mitigation_projects",
        "quantitative_reductions",
        "financial_plan",
    ],
)
HYBRID_REVIEW_BATCH_SIZE = _env_int("HYBRID_REVIEW_BATCH_SIZE", 8)
HYBRID_REVIEW_MAX_BATCHES_PER_SHEET = _env_int("HYBRID_REVIEW_MAX_BATCHES_PER_SHEET", 2)
HYBRID_REVIEW_BASE_ROWS_PER_SHEET = _env_int("HYBRID_REVIEW_BASE_ROWS_PER_SHEET", 80)
HYBRID_SHEETWISE_FLOW_ENABLED = _env_bool("HYBRID_SHEETWISE_FLOW_ENABLED", True)
HYBRID_PROGRESS_LOG_ENABLED = _env_bool("HYBRID_PROGRESS_LOG_ENABLED", True)

# Gemini Flash가 찾은 후보를 더 강한 모델(Gemini Pro 등)이 원문 근거 기준으로
# 재판정한다. 기본값은 판정 로그만 남기고 자동 병합은 하지 않는다.
HYBRID_ADJUDICATION_ENABLED = _env_bool("HYBRID_ADJUDICATION_ENABLED", True)
HYBRID_ADJUDICATION_PROVIDER = os.environ.get("HYBRID_ADJUDICATION_PROVIDER", "gemini").strip().lower()
HYBRID_ADJUDICATION_MODEL = os.environ.get("HYBRID_ADJUDICATION_MODEL", "gemini-2.5-pro").strip()
HYBRID_ADJUDICATION_MAX_CANDIDATES = _env_int("HYBRID_ADJUDICATION_MAX_CANDIDATES", 0)
HYBRID_ADJUDICATION_BATCH_SIZE = _env_int("HYBRID_ADJUDICATION_BATCH_SIZE", 6)
HYBRID_ADJUDICATION_CONTEXT_CHARS = _env_int("HYBRID_ADJUDICATION_CONTEXT_CHARS", 8000)
HYBRID_AUTO_MERGE_ENABLED = _env_bool("HYBRID_AUTO_MERGE_ENABLED", False)
HYBRID_AUTO_MERGE_MIN_CONFIDENCE = os.environ.get("HYBRID_AUTO_MERGE_MIN_CONFIDENCE", "high").strip().lower()

# 라벨 기반 시각 판독값을 Organizer에서 본 시트로 병합한다.
# 기본 활성화하되 환경변수를 0으로 지정하면 기존 비활성 경로를 유지한다.
VISUAL_MERGE_LABELED_ENABLED = _env_bool("VISUAL_MERGE_LABELED_ENABLED", True)
# 객체 인벤토리가 연결된 운영 경로에서는 단일 근거 ID가 단일 extracted 객체와
# 정확히 일치한 시각 후보만 본문 시트에 병합한다. 나머지는 16번 시트에 격리한다.
VISUAL_EVIDENCE_MERGE_ENABLED = _env_bool("VISUAL_EVIDENCE_MERGE_ENABLED", True)
# 운영 Vision 결과를 모델 호출 없이 병합 정책 A/B에 재사용할 수 있도록
# 정제 직전의 최소 입력을 gzip JSON 스냅샷으로 보존한다.
VISUAL_MERGE_SNAPSHOT_ENABLED = _env_bool("VISUAL_MERGE_SNAPSHOT_ENABLED", True)

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
        "지자체명", "시나리오", "전망방법코드", "전망방법원문",
        "부문", "세부부문", "연도", "전망값", "단위", "주요가정",
    ],
    "06_감축목표": [
        "지자체명", "목표수준", "목표범위", "부문",
        "기준연도", "기준배출량", "기준배출량기준",
        "목표연도", "배출전망", "목표감축량", "목표배출량",
        "목표배출량기준", "감축률(%)", "감축률계산값",
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
        "감축원단위ID", "감축원단위값", "예상감축량",
        "감축량유형", "시간기준", "단위",
    ],
    "11_재정투자계획": [
        "지자체명", "계획구분", "부문", "사업명",
        "재원구분", "연도", "예산액", "예산단위",
    ],
    "12_대응기반강화": [
        "지자체명", "대응기반영역", "과제ID", "과제명",
        "정책방향", "주요내용", "대상", "주관부서", "기간",
        "평가유형", "기후변수", "시나리오", "기준기간", "미래기간",
        "공간단위", "부문", "리스크항목", "취약성지표", "값", "단위",
        "리스크등급", "방법론", "자료출처", "연계적응과제",
    ],
    "13_이행관리환류": [
        "지자체명", "거버넌스기구", "역할", "담당부서",
        "절차단계", "기한", "산출물",
    ],
    "14_점검실적": [
        "지자체명", "점검연도", "부문", "관리번호", "사업명",
        "연간계획", "이행실적", "소요예산", "예산액", "예산유형",
        "예산단위", "예산집행률", "달성여부", "사업유형",
    ],
    "15_변경과제_조치": [
        "지자체명", "점검연도", "부문", "관리번호", "사업명",
        "변경전", "변경후", "변경사유",
        "지연미달성사유", "조치계획",
    ],
    "16_시각자료목록": [
        "지자체명", "시각자료ID", "캡션", "유형",
        "데이터포함여부", "추출값요약", "원문값", "원문단위",
        "정규화값", "정규화단위", "정규화배율",
        "값근거", "값검증상태", "계약버전",
        "디지타이징필요", "관련시트",
        "참고자료여부", "참고자료근거", "시각구조유형",
        "음성재검증상태", "음성재검증근거", "자동병합정책",
        "근거ID", "근거매칭상태", "물리객체ID", "렌더변형",
        "렌더변형목록", "물리중복통합수", "병합상태", "병합차단사유",
    ],
    "17_보조검수후보": [
        "지자체명", "대상시트", "후보유형", "신뢰도", "근거페이지",
        "후보행JSON", "기본본유사행JSON", "검수사유", "병합권장", "검수상태",
    ],
    "18_보조병합로그": [
        "지자체명", "대상시트", "판정", "신뢰도", "최종반영여부", "근거페이지",
        "근거문구", "위험플래그", "후보행JSON", "정규화행JSON",
        "판정사유", "병합차단사유",
    ],
    "19_검증리포트": [
        "지자체명", "심각도", "영역", "항목", "문제내용", "권장조치",
    ],
    "20_원문대조": [
        "지자체명", "대상시트", "원본행번호", "상태", "신뢰도",
        "출처페이지", "확인페이지", "점수", "검색어", "일치검색어",
        "행요약", "검수메시지",
    ],
    "21_원문객체인벤토리": [
        "지자체명", "객체ID", "객체유형", "출처페이지", "번호", "캡션",
        "섹션", "행수", "열수", "연결상태", "완전성점수", "연결시트",
        "연결행수", "검수메시지", "예상시트", "시트정합상태", "좌표",
        "원본신뢰도", "Triage판정", "Triage사유", "보완백엔드", "보완상태",
        "최종상태", "시도횟수", "종결사유", "근거ID",
        "근거매칭단계", "일치근거ID", "평가분모상태", "Triage평가상태",
        "허용시트", "보조연결시트", "목차참조페이지", "중복객체ID",
        "물리객체ID", "대표객체ID", "별칭객체ID", "물리통합상태",
        "물리통합방식", "물리통합신뢰도", "물리통합좌표",
    ],
    "90_코드북": [
        "코드유형", "코드", "라벨", "정의", "비고",
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

# ──────────────────────────────────────────────────────────────────────
# 시트 클러스터링 추출 (agent 모드 핵심 최적화)
# ──────────────────────────────────────────────────────────────────────
# 같은 페이지가 시트마다 따로 호출되는 중복을 관련 시트끼리만 묶어 줄인다.
# 단독 그룹은 기존 per-sheet 경로를 그대로 사용하고, 결합 그룹만 한 번의 호출에서
# 여러 시트를 추출한다. codex/claude처럼 호출당 오버헤드가 큰 agent 모드에서
# 시간·토큰 절감 효과를 기대할 수 있다.
#
# 품질 주의(기본 비활성): 한 번에 여러 시트 스키마를 추출하면 출력 JSON이 길어져
# 파싱 실패·시트별 정확도 저하 위험이 있다. 그래서 기본은 안전한 per-sheet(False)이며,
# 실제 추출 A/B로 시트별 행 수·정확도 무회귀를 증명한 뒤 1로 켠다.
EXTRACTION_SHEET_CLUSTERING = _env_bool("EXTRACTION_SHEET_CLUSTERING", False)
# 클러스터는 원문 구간과 스키마 의미가 가까운 시트만 보수적으로 묶는다.
# 행 수가 많거나 의미 경계가 중요한 시트는 단독으로 유지하고 그룹 크기는 최대 3개다.
EXTRACTION_SHEET_CLUSTERS = [
    ["document_meta", "plan_overview"],
    ["regional_conditions"],
    ["emissions_regional", "emissions_management"],
    ["emissions_forecast"],
    ["reduction_targets"],
    ["vision_strategy"],
    ["mitigation_projects", "annual_implementation"],
    ["quantitative_reductions", "financial_plan"],
    ["foundation_measures"],
    ["governance_feedback", "monitoring_performance", "changes_actions"],
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
    "hybrid_review_candidates": "17_보조검수후보",
    "hybrid_merge_log": "18_보조병합로그",
    "validation_report": "19_검증리포트",
    "source_verification": "20_원문대조",
    "source_object_inventory": "21_원문객체인벤토리",
    "codebook": "90_코드북",
}

# 데이터가 있을 때만 생성하는 선택 시트
OPTIONAL_EXCEL_SHEETS = {
    "17_보조검수후보", "18_보조병합로그", "19_검증리포트",
    "20_원문대조", "21_원문객체인벤토리", "90_코드북",
}

# 원문 객체 검증 단계에서만 사용하는 내부 원장. Excel 시트 행 수, 최종 LLM 요약,
# 사용자용 작성 데이터 집계에는 포함하지 않는다.
INTERNAL_OBJECT_KEYS = {
    "object_triage", "ocr_document_objects", "document_objects",
    "reduction_target_context", "semantic_contract_review",
}

# 행 단위 원문 대조를 위한 페이지 근거. 헤더에는 항상 맨 뒤에 추가하되,
# 실제 엑셀 출력에서만 PROVENANCE_ENABLED=0으로 v3 스키마를 복원할 수 있다.
PROVENANCE_ENABLED = _env_bool("PROVENANCE_ENABLED", True)
_PROVENANCE_DATA_SHEETS = [name for name in EXCEL_HEADERS if name[:2].isdigit() and int(name[:2]) <= 15]
for _sheet_name in _PROVENANCE_DATA_SHEETS:
    if "derivation_type" not in EXCEL_HEADERS[_sheet_name]:
        EXCEL_HEADERS[_sheet_name].append("derivation_type")
    if "근거ID" not in EXCEL_HEADERS[_sheet_name]:
        EXCEL_HEADERS[_sheet_name].append("근거ID")
    if "출처페이지" not in EXCEL_HEADERS[_sheet_name]:
        EXCEL_HEADERS[_sheet_name].append("출처페이지")
    if "데이터상태" not in EXCEL_HEADERS[_sheet_name]:
        EXCEL_HEADERS[_sheet_name].append("데이터상태")

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
# 정규식은 "{0,6}"처럼 콤마를 포함할 수 있으므로 이 키는 ";;" 구분 또는 JSON 배열만 쓴다.
PRIOR_PLAN_HEADING_PATTERNS = _env_pattern_list(
    "PRIOR_PLAN_HEADING_PATTERNS",
    [
        r"기존\s*계획.{0,6}(평가|분석)",
        r"기존\s*(시책|사업).{0,6}평가",
        r"이전\s*계획.{0,6}평가",
    ],
)
# 다음 장 제목을 찾지 못했을 때 기존계획 구간을 문서 끝까지 확장하지 않는 안전 상한.
# 상한을 넘으면 탐지를 무효화하고 오탐 경고를 만들지 않는다.
PRIOR_PLAN_MAX_PAGES = _env_int("PRIOR_PLAN_MAX_PAGES", 60)
# 시트별 라우팅 후보 페이지 상한. 기본값은 없음.
# 테스트/최적화가 필요할 때만 DOCUMENT_ROUTE_MAX_PAGES_* 환경변수로 명시적으로 샘플링한다.
_DOCUMENT_ROUTE_DEFAULT_MAX_PAGES: dict[str, int | None] = {}

_DOCUMENT_ROUTE_LEGACY_ALIASES = {
    "regional_conditions": ("DOCUMENT_ROUTE_MAX_PAGES_VEHICLE", "DOCUMENT_ROUTE_MAX_PAGES_ENERGY"),
    "emissions_regional": ("DOCUMENT_ROUTE_MAX_PAGES_GHG",),
    "emissions_management": ("DOCUMENT_ROUTE_MAX_PAGES_GHG",),
    "mitigation_projects": ("DOCUMENT_ROUTE_MAX_PAGES_STRATEGY",),
    "annual_implementation": ("DOCUMENT_ROUTE_MAX_PAGES_STRATEGY",),
    "quantitative_reductions": ("DOCUMENT_ROUTE_MAX_PAGES_STRATEGY",),
    "vision_strategy": ("DOCUMENT_ROUTE_MAX_PAGES_SUMMARY",),
}


def _env_optional_int_first(names: tuple[str, ...], default: int | None = None) -> int | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return _env_optional_int(name, default)
    return default


def _route_max_page_env_names(sheet_key: str) -> tuple[str, ...]:
    current = f"DOCUMENT_ROUTE_MAX_PAGES_{sheet_key.upper()}"
    return (current, *_DOCUMENT_ROUTE_LEGACY_ALIASES.get(sheet_key, ()))


DOCUMENT_ROUTE_MAX_PAGES = {
    sheet_key: _env_optional_int_first(
        _route_max_page_env_names(sheet_key),
        _DOCUMENT_ROUTE_DEFAULT_MAX_PAGES.get(sheet_key),
    )
    for sheet_key in EXTRACTION_SHEETS
}

# (실험적, 기본 비활성) 구조적으로 문서 앞/뒤에만 존재하는 시트를 전면부 N + 후면부 M
# 페이지로 제한하는 메커니즘. 시트키→(front_n, back_n).
#
# 주의: document_meta에 (20,5)를 적용했더니 scripts/verify_routing_coverage.py가
# 안전하지 않음을 잡아냈다 — 서울 보고서는 앞쪽 목차가 길어 법적근거/계획기간/기준연도
# (2018, p.161) 등 메타 필드가 20p 밖에 있었다. 그래서 기본은 비활성({}).
# 적용하려면 반드시 검증기로 시트별 커버리지가 베이스라인 이상인지 먼저 증명할 것.
DOCUMENT_ROUTE_FRONT_BACK_PAGES: dict[str, tuple[int, int]] = {}

# 라우팅 샤프닝: 문서 전반(ROUTE_UBIQUITY_RATIO 비율 이상 페이지)에 편재해 변별력이
# 없는 weak 키워드의 점수 가산을 제외한다. strong 신호는 보존하므로 실제 데이터 페이지는
# 그대로 선택되고, 머리말/공통어로 전 문서가 모든 시트에 배정되던 중복만 줄인다.
# scripts/verify_routing_coverage.py로 시트별 커버리지가 베이스라인 이상인지 증명됨.
# ratio=0.5가 검증기에서 무회귀 최대치(서울 기준 ~8% 페이지 축소). 더 낮추면(0.4)
# regional_conditions의 '에너지/전력' 같은 실신호 weak를 떨궈 리콜이 회귀한다.
ROUTE_DROP_UBIQUITOUS_WEAK = _env_bool("ROUTE_DROP_UBIQUITOUS_WEAK", True)
ROUTE_UBIQUITY_RATIO = _env_float("ROUTE_UBIQUITY_RATIO", 0.5)

# 라우팅 샤프닝(strong): 보고서 제목('기본계획')처럼 머리말/꼬리말로 거의 모든
# 페이지에 편재해 strong(+3) 가산을 부당하게 받는 boilerplate 키워드를 제외한다.
# 이게 없으면 document_meta가 문서 전체(서울 기준 500/515p)에 라우팅돼 큰 중복이 발생한다.
#
# 주의(기본 비활성): 서울 문서 측정 결과, 이 샤프닝은 document_meta를 500→180p로 줄여
# 총 page-send를 7.1% 절감하지만, 떨군 320p 중 법적근거(p252)·조례(p14,429)·발간(p45,515)
# 등 실제 메타 필드를 가진 페이지가 포함되고, 기준연도 2018이 있는 p161도 떨어진다.
# 이 필드들이 보존 페이지로 충분히 커버되는지는 정답지 기반 Check A
# (scripts/verify_routing_coverage.py)로만 증명할 수 있는데, 검증을 통과하기 전에는
# document_meta 리콜 회귀 위험이 있어 기본 비활성으로 둔다.
# 활성화 전: `python scripts/verify_routing_coverage.py <golden.xlsx> <source.pdf>`로
# document_meta 커버리지 무회귀를 먼저 증명할 것. 증명되면 ROUTE_DROP_UBIQUITOUS_STRONG=1.
ROUTE_DROP_UBIQUITOUS_STRONG = _env_bool("ROUTE_DROP_UBIQUITOUS_STRONG", False)
ROUTE_STRONG_UBIQUITY_RATIO = _env_float("ROUTE_STRONG_UBIQUITY_RATIO", 0.6)

# ──────────────────────────────────────────────────────────────────────
# 병렬 실행 설정
# ──────────────────────────────────────────────────────────────────────
# 추출(시트×배치)·이미지 vision 호출은 서로 완전히 독립적이라 동시에 실행해도
# 보내는 프롬프트·받는 응답이 동일하다 → 추출 결과 불변, 기존 LLM 캐시와도 호환.
# 순수하게 벽시계 시간만 줄인다(품질·토큰 변화 없음).
# Gemini API/로컬 에이전트 동시성 한도를 고려해 보수적 기본값을 둔다.
PARALLEL_PROCESSING_ENABLED = _env_bool("PARALLEL_PROCESSING_ENABLED", True)
# 같은 시트 계약·페이지 집합·주입 프롬프트·payload인 텍스트 작업은 enqueue 전에 제거한다.
# 서로 다른 시트나 복구 청크는 fingerprint가 달라 합쳐지지 않는다.
TEXT_QUEUE_DEDUP_ENABLED = _env_bool("TEXT_QUEUE_DEDUP_ENABLED", True)
# 텍스트 추출(extractor/gap_fill) 동시 호출 수.
TEXT_WORKERS = _env_int("TEXT_WORKERS", 4)
# 이미지 vision 동시 호출 수. vision은 호출당 페이로드가 커 보수적으로 둔다.
VISION_WORKERS = _env_int("VISION_WORKERS", 2)

LLM_CACHE_ENABLED = _env_bool("LLM_CACHE_ENABLED", True)
LLM_CACHE_DIR = os.environ.get("LLM_CACHE_DIR", ".cache/llm_responses").strip()
LLM_CACHE_VERSION = os.environ.get("LLM_CACHE_VERSION", "carbon-report-llm-cache-v1").strip()
# 병렬 작업에서 동일한 요청이 동시에 캐시 미스가 나도 실제 LLM 호출은 한 번만 수행한다.
# 디스크 캐시를 끈 무캐시 실행에서도 동시에 겹친 요청만 공유하며 순차 요청은 재호출한다.
LLM_SINGLEFLIGHT_ENABLED = _env_bool("LLM_SINGLEFLIGHT_ENABLED", True)

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
# 전수 이미지 분석 시 여러 이미지를 한 번의 호출로 묶는다. API 백엔드 기본 8,
# 로컬 에이전트(codex/claude) 기본 4 — 무거운 vision 프롬프트로 배치당 타임아웃이
# 잦아 완주가 불가했던 문제의 완화(v8-1 S3-3).
IMAGE_ANALYSIS_BATCH_SIZE = _env_int("IMAGE_ANALYSIS_BATCH_SIZE", 8)
IMAGE_ANALYSIS_BATCH_SIZE_LOCAL_AGENT = _env_int("IMAGE_ANALYSIS_BATCH_SIZE_LOCAL_AGENT", 4)
# 비전 배치도 텍스트 배치와 같은 실행 원장에 저장하고, 실패 시 이미지 단위로 분할한다.
VISION_CHECKPOINT_ENABLED = _env_bool("VISION_CHECKPOINT_ENABLED", True)
VISION_SPLIT_ON_FAILURE = _env_bool("VISION_SPLIT_ON_FAILURE", True)
VISION_RECOVERY_MAX_SPLIT_DEPTH = _env_int("VISION_RECOVERY_MAX_SPLIT_DEPTH", 6)
# 루트 호출도 1회로 계산한다. 기본 3회 — 계속 실패하는 객체 하나가 최대 7회×타임아웃을
# 점유하던 것을 막는다(v8-1 S3-3). 상한 도달 객체는 needs_review로 원장에 남는다.
VISION_RECOVERY_MAX_OBJECT_ATTEMPTS = _env_int(
    "VISION_RECOVERY_MAX_OBJECT_ATTEMPTS", 3
)
# 강한 캡션·수치·단위 신호가 있는데도 비데이터로 판정된 객체만 전용 프롬프트로
# 근거 ID당 한 번 재확인한다. 0은 실행 전체 객체 수 상한 없음이다.
VISION_NEGATIVE_REVALIDATION_ENABLED = _env_bool(
    "VISION_NEGATIVE_REVALIDATION_ENABLED", True
)
VISION_NEGATIVE_REVALIDATION_MAX_OBJECTS = _env_int(
    "VISION_NEGATIVE_REVALIDATION_MAX_OBJECTS", 0
)
# 재개 실행에서 성공 체크포인트 전체를 버리지 않고, 지정한 근거 객체나
# 페이지가 포함된 Vision 배치만 다시 판독한다. 새 결과는 같은 배치의 기존
# 비대상 결과와 병합되어 체크포인트에 다시 저장된다.
VISION_RETRY_EVIDENCE_IDS = _env_list("VISION_RETRY_EVIDENCE_IDS", [])
VISION_RETRY_PAGES = {
    page
    for value in _env_list("VISION_RETRY_PAGES", [])
    if str(value).strip().isdigit() and (page := int(value)) > 0
}
# 그래프 판독값을 본 시트에 자동 병합할 최소 신뢰도.
# low는 별도 판독결과 시트에만 남기고 본 데이터에는 병합하지 않는다.
IMAGE_CHART_MERGE_MIN_CONFIDENCE = "medium"
# 그래프 판독값 자동 반영 시 허용할 연도. 기준연도 2005 등은 판독결과 시트에만 남긴다.
IMAGE_CHART_MERGE_YEARS = YEARS
# 지역여건·배출현황은 과거 시계열이 본질이라 병합 연도 하한을 넓힌다.
IMAGE_CHART_MERGE_YEARS_BY_SHEET = {
    "regional_conditions": list(range(1990, 2051)),
    "emissions_regional": list(range(1990, 2051)),
}
# 참고자료/해외사례/목차성 이미지는 판독결과에는 남기되 본 시트에는 자동 반영하지 않는다.
IMAGE_CHART_REFERENCE_KEYWORDS = [
    "뉴욕시", "런던", "파리", "도쿄", "세계도시", "주요국", "국내외",
    "COP", "IPCC", "UN", "EU", "OECD", "사례", "동향", "목차",
    "우리나라", "국가 온실가스", "국가 감축목표", "중앙정부", "NDC",
]
# 참고자료도 먼저 판독해 감사 가능한 관찰값을 남기고, 본문 시트 자동 병합만 차단한다.
# False로 바꾸고 아래 레거시 제외를 켜면 P3 이전의 호출 전 필터 동작을 재현할 수 있다.
IMAGE_REFERENCE_ANALYZE_THEN_BLOCK = _env_bool("IMAGE_REFERENCE_ANALYZE_THEN_BLOCK", True)
# P3 이전 비교 실험용 레거시 설정. ocr_required 객체에는 적용되지 않는다.
IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT = _env_bool(
    "IMAGE_TRIAGE_EXCLUDE_REFERENCE_CONTEXT", False
)

# ──────────────────────────────────────────────────────────────────────
# 문서 객체 기반 선택적 OCR/VLM
# ──────────────────────────────────────────────────────────────────────
# PyMuPDF가 충분히 읽은 표·텍스트는 재호출하지 않고, 누락·부분·복잡 객체와
# 데이터 차트만 보완한다. 백엔드는 vlm(기존 이미지 에이전트),
# unlimited_ocr(사전 생성 Markdown/JSONL 디렉터리), none 중 하나다.
SELECTIVE_OCR_ENABLED = _env_bool("SELECTIVE_OCR_ENABLED", True)
# 동일 표·차트의 native/캡션/OCR/VLM 표현을 Triage 전에 하나의 물리 객체로
# 통합한다. 원래 객체 ID는 alias로 보존하고 서로 다른 패널은 합치지 않는다.
PHYSICAL_OBJECT_MERGE_ENABLED = _env_bool("PHYSICAL_OBJECT_MERGE_ENABLED", True)
OCR_BACKEND = os.environ.get("OCR_BACKEND", "vlm").strip().lower() or "vlm"
OCR_RESULTS_DIR = os.environ.get("OCR_RESULTS_DIR", "").strip()
OCR_NATIVE_CONFIDENCE_THRESHOLD = _env_float("OCR_NATIVE_CONFIDENCE_THRESHOLD", 0.78)
OCR_COMPLEX_TABLE_ROWS = _env_int("OCR_COMPLEX_TABLE_ROWS", 45)
OCR_COMPLEX_TABLE_COLUMNS = _env_int("OCR_COMPLEX_TABLE_COLUMNS", 12)
OCR_RENDER_DPI = _env_int("OCR_RENDER_DPI", 200)
# 한 실행 안에서 같은 페이지·좌표를 요구하는 물리 객체는 동일 PNG를 재사용한다.
# 후보·프롬프트·판독 순서는 바뀌지 않고 PDF rasterization 비용만 제거한다.
OCR_RENDER_CACHE_ENABLED = _env_bool("OCR_RENDER_CACHE_ENABLED", True)
# 캡션만 검출된 객체는 페이지 전체 대신 인접 이미지·표·벡터 영역을 먼저 복원한다.
OCR_SPATIAL_RECONSTRUCTION_ENABLED = _env_bool("OCR_SPATIAL_RECONSTRUCTION_ENABLED", True)
# 전체 페이지 문맥 렌더는 (a) 패널이 2개 이상이거나 (b) 객체 영역이 페이지 면적의
# OCR_FULL_PAGE_CONTEXT_MAX_OBJECT_RATIO 미만일 때만 추가한다(v8-1 S3-2 — 프록시마다
# 문맥 렌더가 붙어 vision 후보가 두 배로 늘던 문제).
OCR_FULL_PAGE_CONTEXT_ENABLED = _env_bool("OCR_FULL_PAGE_CONTEXT_ENABLED", True)
OCR_FULL_PAGE_CONTEXT_MAX_OBJECT_RATIO = _env_float("OCR_FULL_PAGE_CONTEXT_MAX_OBJECT_RATIO", 0.15)
# 목차·표목차·그림목차 페이지의 캡션 참조는 판독 대상에서 제외한다(v8-1 S3-1).
# 같은 페이지에 캡션 프록시가 이 수 이상이고 native 표·이미지·차트가 없으면 목록 페이지로 본다.
OCR_INDEX_CAPTION_MIN_COUNT = _env_int("OCR_INDEX_CAPTION_MIN_COUNT", 5)
OCR_MULTI_PANEL_GAP = _env_int("OCR_MULTI_PANEL_GAP", 24)
OCR_CAPTION_REGION_MAX_HEIGHT_RATIO = _env_float(
    "OCR_CAPTION_REGION_MAX_HEIGHT_RATIO", 0.45
)

# 1차 추출 후 빈칸이 큰 행만 좁은 문맥으로 다시 보완
GAP_FILL_ENABLED = _env_bool("GAP_FILL_ENABLED", True)
# 시트별 채움률이 이 값 미만이면 보완 재추출 대상으로 본다.
GAP_FILL_MIN_FILL_RATIO = _env_float("GAP_FILL_MIN_FILL_RATIO", 0.6)
# 보완 재추출 시 페이지 라우팅 점수 임계값. 1차(DOCUMENT_ROUTE_MIN_SCORE=3)보다 낮춰
# 1차 패스가 놓친 페이지를 다시 잡는다(recall 우선).
GAP_FILL_REEXTRACT_MIN_SCORE = _env_int("GAP_FILL_REEXTRACT_MIN_SCORE", 2)
# 시트당 보완 재추출에 사용할 최대 후보 페이지 수(비용 가드).
GAP_FILL_REEXTRACT_MAX_PAGES = _env_int("GAP_FILL_REEXTRACT_MAX_PAGES", 24)
GAP_FILL_MAX_TARGETS = {
    "vehicle": _env_optional_int("GAP_FILL_MAX_TARGETS_VEHICLE", None),
    "energy": _env_optional_int("GAP_FILL_MAX_TARGETS_ENERGY", None),
    "ghg": _env_optional_int("GAP_FILL_MAX_TARGETS_GHG", None),
    "strategy": _env_optional_int("GAP_FILL_MAX_TARGETS_STRATEGY", None),
}
GAP_FILL_CONTEXT_PAGES = 8
GAP_FILL_TARGET_BATCH_SIZE = 10
# True이면 빈칸보완 재추출에서 1차 추출이 이미 성공한 페이지를 제외한다.
# "보냈다"가 아니라 원장 status==ok 기준이므로 호출/파싱 실패 페이지는 다시 후보가 된다.
GAP_FILL_SKIP_ALREADY_ROUTED = _env_bool("GAP_FILL_SKIP_ALREADY_ROUTED", True)
GAP_FILL_COVERAGE_THRESHOLDS = {
    "emissions_regional_min_sectors": _env_int("GAP_FILL_EMISSIONS_REGIONAL_MIN_SECTORS", 4),
    "emissions_regional_min_years": _env_int("GAP_FILL_EMISSIONS_REGIONAL_MIN_YEARS", 2),
    "emissions_management_min_sectors": _env_int("GAP_FILL_EMISSIONS_MANAGEMENT_MIN_SECTORS", 2),
    "emissions_forecast_min_years": _env_int("GAP_FILL_EMISSIONS_FORECAST_MIN_YEARS", 2),
    "financial_plan_min_years": _env_int("GAP_FILL_FINANCIAL_PLAN_MIN_YEARS", 2),
    "financial_plan_min_sectors": _env_int("GAP_FILL_FINANCIAL_PLAN_MIN_SECTORS", 2),
    "project_ratio": _env_float("GAP_FILL_PROJECT_RATIO", 0.3),
}

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

# 벡터로 그려진 차트(축·막대·격자가 벡터 path) 누락 방지.
# 이런 차트는 page.get_images()에 안 잡혀 통째로 누락된다. 표·임베드이미지가 없는데
# 벡터 path가 충분히 많은 페이지는 벡터 차트로 보고 전체 렌더링한다.
# (정밀도는 이후 이미지 triage가 담당하므로 과렌더링은 허용된다.)
VECTOR_RENDER_ENABLED = _env_bool("VECTOR_RENDER_ENABLED", True)
VECTOR_RENDER_MIN_DRAWINGS = _env_int("VECTOR_RENDER_MIN_DRAWINGS", 60)

# 최대 재시도 횟수
MAX_RETRIES = 3

# ──────────────────────────────────────────────────────────────────────
# 할당량(quota/세션 한도) 회복 대기-재개 설정
# ──────────────────────────────────────────────────────────────────────
# 명시적인 quota/session-limit 오류에만 회복 대기를 적용한다. 일반 타임아웃은
# LOCAL_AGENT_TIMEOUT_RETRIES만큼만 재시도한 뒤 배치 분할/실패 원장으로 넘긴다.
LLM_QUOTA_WAIT_ENABLED = _env_bool("LLM_QUOTA_WAIT_ENABLED", True)
# 명시적 quota 회복을 확인할 폴링 간격(초).
LLM_QUOTA_WAIT_POLL_SECONDS = _env_int("LLM_QUOTA_WAIT_POLL_SECONDS", 120)
# 누적 대기 상한(초). 6시간은 밤샘 완주용으로만 env에서 선택하고 기본은 30분으로 제한한다.
LLM_QUOTA_WAIT_MAX_SECONDS = _env_int("LLM_QUOTA_WAIT_MAX_SECONDS", 1800)
# 대기 중 "아직 살아 있음"을 알리는 하트비트 로그 간격(초). 기본 5분.
LLM_QUOTA_WAIT_HEARTBEAT_SECONDS = _env_int("LLM_QUOTA_WAIT_HEARTBEAT_SECONDS", 300)

# 이미지 최대 크기 (픽셀, 긴 변 기준)
MAX_IMAGE_SIZE = 1568

# Vision이 유효 차트로 반환하지 못한 이미지 페이지에서 텍스트/표 기반 검토 후보를
# 남기는 fallback 상한. 기본값은 없음(=발견한 후보 전부 기록).
IMAGE_FALLBACK_MAX_OBSERVATIONS = _env_optional_int("IMAGE_FALLBACK_MAX_OBSERVATIONS", None)
