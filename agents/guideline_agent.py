"""
에이전트 1: 가이드라인 에이전트

환경부 「지자체 탄소중립 녹색성장 기본계획 수립 및 추진상황 점검 가이드라인」에 따라
추출해야 할 정보의 스키마를 정의하고 반환합니다.

기본 스키마는 코드 내에 내장되어 있으며, HWP 가이드라인 파일이 제공되면
kordoc 기반 HWP 파서를 통해 문서 내용을 읽고 시트별 보조 지침으로 반영합니다.
"""

import json
import logging
import re
from copy import deepcopy
from pathlib import Path

import config
from utils.hwp_reader import extract_hwp, is_hwp_file


logger = logging.getLogger(__name__)


GUIDELINE_SCHEMA = {
    "description": (
        "환경부 지자체 탄소중립 녹색성장 기본계획 수립 가이드라인에 따른 "
        "정보 추출 스키마 (v2024.09)"
    ),

    # ── 시트 1: 용도별 자동차(현황) ──────────────────────────────────
    "vehicle_by_use": {
        "sheet_name": "용도별 자동차(현황)",
        "description": "지자체 내 등록 차량을 용도·차종별로 집계한 현황 데이터",
        "fields": {
            "지자체명": "시·군·구 명칭 (예: 서울특별시 관악구)",
            "용도": "승용, 화물, 승합, 특수, 이륜 등",
            "차종": "경유, 휘발유, LPG, 전기, 수소, CNG, 하이브리드 등 연료 유형",
            "대수(대)": "등록 차량 대수 (정수)",
            "1일 평균 주행거리(km/대)": "차종별 1일 평균 주행 거리 (실수)",
        },
        "extraction_hints": [
            "차량 등록 현황, 등록 대수, 용도별 자동차 등의 표에서 추출",
            "연료 유형(경유/휘발유/LPG/전기/수소)으로 행을 구분",
            "주행거리가 없으면 None으로 처리",
        ],
    },

    # ── 시트 2: 용도별 에너지(현황) ──────────────────────────────────
    "energy_by_use": {
        "sheet_name": "용도별 에너지(현황)",
        "description": "지자체 내 용도별 최종 에너지 소비 현황 (단위: TJ 또는 toe)",
        "fields": {
            "지자체명": "시·군·구 명칭",
            "용도": "건물(가정·상업·공공), 수송, 산업, 농림어업 등",
            "석유(에너지유)": "등유, 경유, 휘발유 등 에너지용 석유 소비량",
            "석유(LPG)": "LPG 소비량",
            "석유(비에너지유)": "납사, 아스팔트 등 비에너지용 석유 소비량",
            "가스": "도시가스(LNG), 천연가스 소비량",
            "전력": "전력 소비량",
            "열": "지역난방 등 열에너지 소비량",
            "신재생": "태양광·풍력·바이오 등 신재생에너지 소비량",
        },
        "extraction_hints": [
            "에너지 소비 현황, 최종 에너지 소비, 부문별 에너지 등의 표에서 추출",
            "단위를 통일하여 기재 (TJ 또는 toe)",
            "값이 없으면 None 처리",
        ],
    },

    # ── 시트 3: 온실가스(현황전망목표) ───────────────────────────────
    "greenhouse_gas": {
        "sheet_name": "온실가스(현황전망목표)",
        "description": (
            "온실가스 배출·흡수량의 연도별 현황(실적), 전망(BAU), 목표(NDC) 데이터. "
            "단위: tCO2eq (또는 천 tCO2eq, 백만 tCO2eq — 보고서 단위 확인 필수)"
        ),
        "fields": {
            "지자체명": "시·군·구 명칭",
            "배출유형": "직접배출 / 간접배출 / 흡수원 (가이드라인 7p 참고)",
            "종류": "현황(실적) / 전망(BAU) / 목표(NDC·계획)",
            "부문": "건물 / 수송 / 농축산 / 폐기물 / 흡수원 / 전환 / 산업 / 수소",
            "연도별": f"2018~2034년 연도별 배출·흡수량 숫자값",
        },
        "extraction_hints": [
            "온실가스 배출량, GHG 인벤토리, 부문별 배출, 감축 목표 등의 표·그래프에서 추출",
            "현황(과거 실적), 전망(BAU 시나리오), 목표(감축 목표) 를 종류로 구분",
            "흡수원은 음수(-)로 표기되는 경우가 많음",
            "그래프에 수치가 없으면 그래프 이미지 분석으로 대략적 값 추정",
            "단위가 천 tCO2eq 이면 곱하기 1000 하여 tCO2eq 로 환산 또는 단위 명시",
        ],
    },

    # ── 시트 4: 감축전략(계획실적) ───────────────────────────────────
    "reduction_strategy": {
        "sheet_name": "감축전략(계획실적)",
        "description": "부문별 감축 사업의 계획 및 실적 데이터",
        "fields": {
            "지자체명": "시·군·구 명칭",
            "배출유형": "직접배출 / 간접배출",
            "감축전략_부문": "전환 / 산업 / 건물 / 수송 / 농축산 / 폐기물 / 흡수원 / 수소",
            "감축사업명": "가이드라인 부록3·4의 공통 감축사업명",
            "감축사업명_세부": "지자체가 자체적으로 추진하는 구체적 사업명",
            "구분": "공통(가이드라인 공통 사업) / 특화(지자체 자체 사업)",
            "성과지표": "사업 성과를 측정하는 지표 (예: ZEB 면적(㎡), 전기차 보급 대수)",
            "종류": (
                "계획(지표) / 계획(감축량, tCO2eq) / 계획(예산, 백만원) / "
                "실적(지표) / 실적(감축량) / 실적(예산)"
            ),
            "연도별": "2018~2034년 연도별 계획·실적 값",
        },
        "extraction_hints": [
            "감축 계획, 감축 사업, 부문별 이행계획, 감축 실적 표에서 추출",
            "하나의 사업에 대해 종류(계획/실적) × 지표/감축량/예산 = 6개 행 생성",
            "예산이 '비예산'인 경우 문자열 '비예산'으로 입력",
            "미래 연도 실적은 None으로 처리",
        ],
    },

    # ── 시트 5: 지자체별 요약카드 ────────────────────────────────────
    "summary_card": {
        "sheet_name": "지자체별 요약카드",
        "description": "지자체 탄소중립 계획의 핵심 내용 요약",
        "fields": {
            "지자체명": "시·군·구 명칭",
            "항목": " / ".join(config.SUMMARY_ITEMS),
            "내용": "해당 항목의 핵심 내용 (2~5문장 이내로 간결하게)",
            "근거": "보고서 내 근거 위치 (예: 보고서 p.45, 3장 온실가스 배출 현황)",
        },
        "items": config.SUMMARY_ITEMS,
        "extraction_hints": [
            "배출유형: 직접/간접/흡수원 배출 구성 및 주요 배출원 설명",
            "감축목표(2030): 2030년 온실가스 감축 목표량 및 감축률",
            "감축목표(2035): 2035년 목표 (있는 경우)",
            "핵심전략: 지자체의 핵심 감축 전략 3~5가지",
            "배출유형-전략 간 연결성: 주요 배출 부문과 대응 감축 전략의 연결 관계",
        ],
    },
}


_EXTRACTOR_KEY_MAP = {
    "vehicle": "vehicle_by_use",
    "energy": "energy_by_use",
    "ghg": "greenhouse_gas",
    "strategy": "reduction_strategy",
    "summary": "summary_card",
}


_GUIDELINE_CONTEXT_RULES = {
    "vehicle_by_use": [
        "자동차", "차량", "등록대수", "주행거리", "수송", "활동자료",
    ],
    "energy_by_use": [
        "에너지", "최종에너지", "석유", "도시가스", "전력", "열", "신재생", "toe", "TJ",
    ],
    "greenhouse_gas": [
        "온실가스", "배출량", "흡수", "직접배출", "간접배출", "관리권한", "BAU", "NDC", "목표",
    ],
    "reduction_strategy": [
        "감축", "감축사업", "이행", "추진상황", "실적", "성과지표", "공통", "특화", "부록",
    ],
    "summary_card": [
        "비전", "목표", "전략", "기본계획", "추진방향", "배출유형", "녹색성장",
    ],
}


def _compact_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def _split_guideline_sentences(text: str) -> list[str]:
    """가이드라인 전문을 검색 가능한 짧은 조각으로 분할."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for raw in text.splitlines():
        line = _compact_text(raw)
        if not line:
            continue

        # 제목/표 캡션 후보는 단독 조각으로도 보존
        if re.match(r"^(\d+(\.\d+)*|제\s*\d+\s*[장절]|부록|표\s*\d+|그림\s*\d+)", line):
            if current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            chunks.append(line)
            continue

        current.append(line)
        current_len += len(line)
        if current_len >= 550:
            chunks.append(" ".join(current))
            current = []
            current_len = 0

    if current:
        chunks.append(" ".join(current))
    return chunks


def _score_guideline_chunk(chunk: str, keywords: list[str]) -> int:
    score = 0
    score += sum(3 for kw in keywords if kw in chunk)
    if any(unit in chunk for unit in ["tCO2", "CO2eq", "천톤", "백만원", "대", "km", "%"]):
        score += 1
    if any(term in chunk for term in ["작성", "산정", "기준", "부문", "양식", "항목", "점검"]):
        score += 1
    return score


def _extract_guideline_context(full_text: str, max_chunks: int = 4) -> dict[str, list[str]]:
    """시트별로 관련성이 높은 HWP 가이드라인 문맥을 추출."""
    chunks = _split_guideline_sentences(full_text)
    result: dict[str, list[str]] = {}

    for schema_key, keywords in _GUIDELINE_CONTEXT_RULES.items():
        ranked = sorted(
            ((_score_guideline_chunk(chunk, keywords), chunk) for chunk in chunks),
            key=lambda item: item[0],
            reverse=True,
        )
        selected = []
        seen = set()
        for score, chunk in ranked:
            if score <= 0:
                break
            compact = _compact_text(chunk)
            if not compact or compact in seen:
                continue
            seen.add(compact)
            selected.append(compact[:500])
            if len(selected) >= max_chunks:
                break
        result[schema_key] = selected

    return result


class GuidelineAgent:
    """
    에이전트 1: 가이드라인 분석 에이전트

    추출 스키마를 반환하고, 각 시트의 추출 지침을 제공합니다.
    HWP 파일이 제공된 경우 추가 컨텍스트를 읽어 보완합니다.
    """

    def __init__(self, hwp_path: str | None = None):
        self.hwp_path = hwp_path
        self.schema = deepcopy(GUIDELINE_SCHEMA)
        self.guideline_text = ""
        self.guideline_context: dict[str, list[str]] = {}
        self.guideline_loaded = False
        self.guideline_error = ""

        if hwp_path:
            self._load_hwp_guideline(hwp_path)

    def _load_hwp_guideline(self, hwp_path: str):
        """HWP/HWPX 가이드라인을 실제로 파싱해 시트별 보조 지침으로 저장."""
        path = Path(hwp_path)
        if not path.exists():
            self.guideline_error = f"파일 없음: {path}"
            logger.warning(self.guideline_error)
            return
        if not is_hwp_file(path):
            self.guideline_error = f"지원하지 않는 가이드라인 형식: {path.suffix}"
            logger.warning(self.guideline_error)
            return

        try:
            content = extract_hwp(path)
            self.guideline_text = content.full_text
            self.guideline_context = _extract_guideline_context(content.full_text)
            self.guideline_loaded = bool(self.guideline_text.strip())

            for schema_key, snippets in self.guideline_context.items():
                if schema_key in self.schema and isinstance(self.schema[schema_key], dict):
                    self.schema[schema_key]["guideline_context"] = snippets
        except Exception as exc:
            self.guideline_error = str(exc)
            logger.warning(f"가이드라인 HWP 파싱 실패: {exc}")

    def get_schema(self) -> dict:
        """추출 스키마 반환"""
        return self.schema

    def get_extraction_prompt(self, sheet_key: str) -> str:
        """특정 시트에 대한 추출 프롬프트 생성"""
        if sheet_key not in self.schema:
            return ""
        info = self.schema[sheet_key]
        lines = [
            f"# {info['sheet_name']} 추출 지침",
            f"\n{info['description']}",
            "\n## 추출할 필드",
        ]
        for field_name, field_desc in info["fields"].items():
            lines.append(f"- **{field_name}**: {field_desc}")
        lines.append("\n## 추출 힌트")
        for hint in info.get("extraction_hints", []):
            lines.append(f"- {hint}")
        guideline_context = info.get("guideline_context", [])
        if guideline_context:
            lines.append("\n## HWP 가이드라인에서 추출한 관련 지침")
            for idx, snippet in enumerate(guideline_context, start=1):
                lines.append(f"{idx}. {snippet}")
        return "\n".join(lines)

    def get_all_prompts(self) -> dict[str, str]:
        """모든 시트의 추출 프롬프트를 extractor 키 기준으로 반환"""
        return {
            extractor_key: self.get_extraction_prompt(schema_key)
            for extractor_key, schema_key in _EXTRACTOR_KEY_MAP.items()
        }

    def report(self) -> str:
        """에이전트 상태 보고"""
        sheets = [v["sheet_name"] for k, v in self.schema.items() if isinstance(v, dict) and "sheet_name" in v]
        return (
            f"[에이전트1 가이드라인] 로드 완료\n"
            f"  - 추출 대상 시트: {len(sheets)}개\n"
            f"  - 시트 목록: {', '.join(sheets)}\n"
            f"  - HWP 가이드라인 파일: {self._guideline_status()}"
        )

    def _guideline_status(self) -> str:
        if not self.hwp_path:
            return "없음 (내장 스키마 사용)"
        if self.guideline_loaded:
            total_snippets = sum(len(v) for v in self.guideline_context.values())
            return f"파싱 완료 ({self.hwp_path}, 관련 지침 {total_snippets}개 반영)"
        return f"파싱 실패/미반영 ({self.hwp_path}; {self.guideline_error or '원인 미상'})"
