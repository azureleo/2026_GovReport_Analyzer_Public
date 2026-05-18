"""
프로젝트 전역 설정
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Gemini API 설정
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash-lite"  # 비용 최적화 (2.5-flash 대비 저렴, 신규 계정 지원)
MAX_TOKENS = 65536  # Gemini 최대값 사용 (GHG/strategy 대용량 출력 대응)

# 연도 범위 (탄소중립 기본계획 기준)
YEARS = list(range(2018, 2035))

# 배출 부문
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

# 엑셀 시트 헤더 정의
EXCEL_HEADERS = {
    "용도별 자동차(현황)": [
        "지자체명", "용도", "차종", "대수(대)", "1일 평균 주행거리(km/대)"
    ],
    "용도별 에너지(현황)": [
        "지자체명", "용도",
        "석유(에너지유)", "석유(LPG)", "석유(비에너지유)",
        "가스", "전력", "열", "신재생"
    ],
    "온실가스(현황전망목표)": (
        ["지자체명", "배출유형", "종류", "부문"] + YEARS
    ),
    "감축전략(계획실적)": (
        ["지자체명", "배출유형", "감축전략_부문",
         "감축사업명", "감축사업명_세부", "구분", "성과지표", "종류"] + YEARS
    ),
    "감축전략(정성사업)": [
        "지자체명", "배출유형", "감축전략_부문",
        "감축사업명", "감축사업명_세부", "구분", "성과지표", "종류"
    ],
    "이미지·그래프 판독결과": [
        "지자체명", "페이지", "대상시트", "그래프유형", "제목", "단위",
        "항목", "연도", "값", "신뢰도", "반영여부", "근거"
    ],
    "지자체별 요약카드": [
        "지자체명", "항목", "내용", "근거"
    ],
}

# 요약카드 항목
SUMMARY_ITEMS = [
    "배출유형",
    "감축목표(2030)",
    "감축목표(2035)",
    "핵심전략",
    "배출유형-전략 간 연결성",
]

# PDF 페이지 배치 처리 크기.
# 너무 크면 출력 JSON이 길어져 파싱 실패가 늘 수 있어 안정성 위주로 둔다.
BATCH_SIZE = 15

# 문서 구조 라우팅 설정
# 관련 페이지 앞뒤 몇 페이지까지 함께 LLM에 전달할지 결정
DOCUMENT_ROUTE_CONTEXT_PAGES = 1
# 시트별 후보 페이지로 선택할 최소 점수
DOCUMENT_ROUTE_MIN_SCORE = 3
# 시트별 라우팅 후보 페이지 상한. 너무 넓게 잡히면 비용과 JSON 파싱 실패가 증가한다.
DOCUMENT_ROUTE_MAX_PAGES = {
    "vehicle": 80,
    "energy": 120,
    "ghg": 220,
    "strategy": 240,
    "summary": 60,
}

# 이미지 분석 최대 개수 (triage 통과 후보 중 상위 N개만 Gemini Vision 분석)
# 테스트 중에는 50 권장. 최종 산출용으로 더 많이 확인할 때만 100~150으로 올린다.
MAX_IMAGES = 50

# ChartQA 스타일 이미지 triage 설정
# True이면 전체 이미지에 대해 로컬 휴리스틱으로 그래프/표/도표 후보를 먼저 선별
IMAGE_TRIAGE_ENABLED = True
# triage 점수 기준. 낮출수록 더 많이 분석하고, 높일수록 더 엄격하게 거른다.
IMAGE_TRIAGE_MIN_SCORE = 5
# 점수가 낮더라도 페이지 전체 렌더링 이미지는 문맥상 중요하면 보존할지 여부
IMAGE_TRIAGE_KEEP_RENDERED_CONTEXT = True
# DePlot 아이디어를 차용해 그래프/차트 이미지를 표 형태 JSON으로 먼저 변환
IMAGE_CHART_TABLE_EXTRACTION = True
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
GAP_FILL_ENABLED = True
GAP_FILL_MAX_TARGETS = {
    "vehicle": 20,
    "energy": 20,
    "ghg": 30,
    "strategy": 60,
}
GAP_FILL_CONTEXT_PAGES = 8
GAP_FILL_TARGET_BATCH_SIZE = 10

# 자동차/에너지처럼 원문 구간이 보고서마다 달라지는 시트는
# 전체 문서에서 관련 구간을 다시 점수화해 집중 재추출한다.
FOCUSED_GAP_FILL_ENABLED = True
FOCUSED_GAP_FILL_CONTEXT_PAGES = 2
FOCUSED_GAP_FILL_MAX_ANCHORS = {
    "vehicle": 10,
    "energy": 10,
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
