"""
에이전트 1: 가이드라인 에이전트

환경부 「지자체 탄소중립 녹색성장 기본계획 수립 및 추진상황 점검 가이드라인」에 따라
추출해야 할 정보의 스키마를 정의하고 반환합니다.

기본 스키마는 코드 내에 내장되어 있으며, 프로젝트 루트의 carbon_guideline.md가 존재하면
이를 시트별 보조 지침으로 우선 활용합니다. carbon_guideline.md가 없고 HWP 파일이
제공된 경우에만 kordoc 기반 HWP 파서를 fallback으로 사용합니다.
"""

import json
import logging
import re
from copy import deepcopy
from pathlib import Path

import config
from utils.hwp_reader import extract_hwp, is_hwp_file
from utils import llm_client

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MARKDOWN_GUIDELINE_PATH = _PROJECT_ROOT / "carbon_guideline.md"


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

    # ── 시트 5: 비전·전략 ────────────────────────────────────
    "summary_card": {
        "sheet_name": "비전·전략",
        "description": "지자체 탄소중립 계획의 비전, 전략, 목표 핵심 요약",
        "fields": {
            "지자체명": "시·군·구 명칭",
            "비전문구": "2050 탄소중립 미래상 등 비전 원문",
            "전략수준": "비전 / 추진전략 / 세부전략",
            "전략명": "전략 명칭",
            "부문": "관련 부문",
            "설명": "전략 설명",
        },
        "extraction_hints": [
            "비전: 2050 탄소중립 관련 미래상 및 슬로건",
            "추진전략: 부문별 핵심 추진 전략",
            "세부전략: 세부 실행 방안",
        ],
    },
}


_EXTRACTOR_KEY_MAP = {
    "document_meta": "vehicle_by_use",
    "plan_overview": "vehicle_by_use",
    "regional_conditions": "energy_by_use",
    "emissions_regional": "greenhouse_gas",
    "emissions_management": "greenhouse_gas",
    "emissions_forecast": "greenhouse_gas",
    "reduction_targets": "greenhouse_gas",
    "vision_strategy": "summary_card",
    "mitigation_projects": "reduction_strategy",
    "annual_implementation": "reduction_strategy",
    "quantitative_reductions": "reduction_strategy",
    "financial_plan": "reduction_strategy",
    "foundation_measures": "summary_card",
    "governance_feedback": "summary_card",
    "monitoring_performance": "reduction_strategy",
    "changes_actions": "reduction_strategy",
}

# 새 16개 시트에서 가이드라인 문맥 검색에 사용하는 키워드
_GUIDELINE_CONTEXT_RULES_V2 = {
    "document_meta": ["기본계획", "계획기간", "기준연도", "목표연도", "수립", "법적 근거"],
    "plan_overview": ["추진체계", "추진절차", "경과", "공청회", "자문", "위원회"],
    "regional_conditions": ["인구", "면적", "GRDP", "에너지", "차량", "전력", "건축물"],
    "emissions_regional": ["온실가스", "배출량", "직접배출", "간접배출", "GIR", "인벤토리", "LULUCF"],
    "emissions_management": ["관리권한", "건물", "수송", "농축산", "폐기물", "흡수원"],
    "emissions_forecast": ["전망", "BAU", "시계열", "LEAP", "증가율"],
    "reduction_targets": ["감축목표", "감축률", "목표배출량", "NDC", "2030", "2018년 대비"],
    "vision_strategy": ["비전", "전략", "추진방향", "핵심", "슬로건"],
    "mitigation_projects": ["감축사업", "세부사업", "핵심과제", "관리번호", "성과지표", "주관부서"],
    "annual_implementation": ["연차별", "이행계획", "단계별", "목표물량"],
    "quantitative_reductions": ["감축량", "감축원단위", "모니터링", "활동량", "배출계수"],
    "financial_plan": ["재정", "투자", "예산", "국비", "시비", "도비"],
    "foundation_measures": ["적응", "공유재산", "국제협력", "교육", "녹색성장", "정의로운 전환"],
    "governance_feedback": ["이행관리", "환류", "점검체계", "탄소중립이행책임관"],
    "monitoring_performance": ["추진상황", "점검", "달성여부", "이행실적"],
    "changes_actions": ["변경과제", "미달성", "조치계획", "변경사유"],
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


_AGENT_SPEC_SYSTEM = """당신은 환경부 지자체 탄소중립 녹색성장 기본계획 가이드라인을
엑셀 추출 요구사항으로 재구성하는 분석가입니다. 반드시 JSON만 반환하세요."""


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


def _format_context_for_agent(context: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for schema_key, snippets in context.items():
        lines.append(f"## {schema_key}")
        for idx, snippet in enumerate(snippets, start=1):
            lines.append(f"{idx}. {snippet}")
    return "\n".join(lines)


def _normalise_agent_spec(parsed: dict) -> dict[str, dict]:
    """에이전트가 재구성한 가이드라인 스펙을 안전한 형태로 정리."""
    if not isinstance(parsed, dict):
        return {}
    spec = parsed.get("sheets", parsed)
    if not isinstance(spec, dict):
        return {}

    normalised: dict[str, dict] = {}
    for schema_key in _EXTRACTOR_KEY_MAP.values():
        raw = spec.get(schema_key)
        if not isinstance(raw, dict):
            continue
        entry: dict[str, list[str]] = {}
        for key in ["required_fields", "extraction_rules", "validation_rules", "source_keywords", "visual_rules"]:
            values = raw.get(key, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                entry[key] = [
                    _compact_text(str(value))[:500]
                    for value in values
                    if _compact_text(str(value))
                ][:8]
        if any(entry.values()):
            normalised[schema_key] = entry
    return normalised


class GuidelineAgent:
    """
    에이전트 1: 가이드라인 분석 에이전트

    추출 스키마를 반환하고, 각 시트의 추출 지침을 제공합니다.

    가이드라인 로드 우선순위:
    1. 프로젝트 루트의 carbon_guideline.md (사전 구조화된 마크다운)
    2. --guideline 옵션으로 전달된 HWP/HWPX 파일 (fallback)
    """

    def __init__(self, hwp_path: str | None = None):
        self.hwp_path = hwp_path
        self.schema = deepcopy(GUIDELINE_SCHEMA)
        self.guideline_text = ""
        self.guideline_context: dict[str, list[str]] = {}
        self.agent_spec: dict[str, dict] = {}
        self.guideline_loaded = False
        self.guideline_error = ""
        self.guideline_source = ""

        # 우선순위 1: carbon_guideline.md
        if _MARKDOWN_GUIDELINE_PATH.exists():
            self._load_markdown_guideline()
        elif hwp_path:
            # 우선순위 2: HWP fallback
            self._load_hwp_guideline(hwp_path)

    def _load_markdown_guideline(self):
        """사전 구조화된 carbon_guideline.md에서 시트별 보조 지침을 로드."""
        try:
            text = _MARKDOWN_GUIDELINE_PATH.read_text(encoding="utf-8")
            self.guideline_text = text
            self.guideline_context = _extract_guideline_context(text)
            self.guideline_loaded = bool(text.strip())
            self.guideline_source = str(_MARKDOWN_GUIDELINE_PATH.name)

            for schema_key, snippets in self.guideline_context.items():
                if schema_key in self.schema and isinstance(self.schema[schema_key], dict):
                    self.schema[schema_key]["guideline_context"] = snippets
        except Exception as exc:
            self.guideline_error = f"carbon_guideline.md 로드 실패: {exc}"
            logger.warning(self.guideline_error)

    def _load_hwp_guideline(self, hwp_path: str):
        """HWP/HWPX 가이드라인을 실제로 파싱해 시트별 보조 지침으로 저장. (fallback)"""
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
            self.guideline_source = str(path.name)
            if self.guideline_loaded and config.GUIDELINE_AGENT_SPEC_ENABLED:
                self.agent_spec = self._build_agent_spec()

            for schema_key, snippets in self.guideline_context.items():
                if schema_key in self.schema and isinstance(self.schema[schema_key], dict):
                    self.schema[schema_key]["guideline_context"] = snippets
            for schema_key, spec in self.agent_spec.items():
                if schema_key in self.schema and isinstance(self.schema[schema_key], dict):
                    self.schema[schema_key]["agent_guideline_spec"] = spec
        except Exception as exc:
            self.guideline_error = str(exc)
            logger.warning(f"가이드라인 HWP 파싱 실패: {exc}")

    def _build_agent_spec(self) -> dict[str, dict]:
        """
        HWP에서 선별한 관련 문맥을 다시 로컬 에이전트에 맡겨 시트별 요구사항으로 재구성.

        코드에 내장된 기본 스키마는 엑셀 컬럼 안정성을 위한 fallback이고, 실제 추출 지침은
        매 실행 시 HWP 내용에서 재구성한 agent_guideline_spec을 프롬프트에 추가한다.
        """
        context_text = _format_context_for_agent(self.guideline_context)
        if not context_text.strip():
            return {}

        prompt = f"""아래는 환경부 HWP 가이드라인에서 시트별로 선별한 원문 조각입니다.
이 조각만 근거로 각 시트에서 반드시 추출해야 하는 필드, 추출 규칙, 검증 규칙,
그래프/표/이미지에서 확인해야 할 비정형 데이터 규칙을 재구성하세요.

반환 JSON 형식:
{{
  "sheets": {{
    "vehicle_by_use": {{
      "required_fields": ["..."],
      "source_keywords": ["..."],
      "extraction_rules": ["..."],
      "visual_rules": ["..."],
      "validation_rules": ["..."]
    }},
    "energy_by_use": {{}},
    "greenhouse_gas": {{}},
    "reduction_strategy": {{}},
    "summary_card": {{}}
  }}
}}

[HWP 가이드라인 관련 문맥]
{context_text[:12000]}
"""
        response = llm_client.call_text(prompt, system=_AGENT_SPEC_SYSTEM, max_retries=1)
        return _normalise_agent_spec(llm_client.parse_json(response))

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
        agent_spec = info.get("agent_guideline_spec", {})
        if agent_spec:
            lines.append("\n## 로컬 에이전트가 HWP에서 재구성한 실행 규칙")
            section_names = {
                "required_fields": "필수 필드",
                "source_keywords": "원문 탐색 키워드",
                "extraction_rules": "추출 규칙",
                "visual_rules": "표·그래프·이미지 판독 규칙",
                "validation_rules": "검증 규칙",
            }
            for key, title in section_names.items():
                values = agent_spec.get(key, [])
                if values:
                    lines.append(f"\n### {title}")
                    for value in values:
                        lines.append(f"- {value}")
        return "\n".join(lines)

    def get_all_prompts(self) -> dict[str, str]:
        """모든 16개 시트의 추출 보조 프롬프트를 시트키 기준으로 반환"""
        prompts: dict[str, str] = {}
        for sheet_key, keywords in _GUIDELINE_CONTEXT_RULES_V2.items():
            snippets = self._get_relevant_snippets(keywords)
            if snippets:
                lines = [f"[가이드라인 보조 지침: {sheet_key}]"]
                for idx, snippet in enumerate(snippets, start=1):
                    lines.append(f"{idx}. {snippet}")
                prompts[sheet_key] = "\n".join(lines)
            else:
                prompts[sheet_key] = ""
        return prompts

    def _get_relevant_snippets(self, keywords: list[str], max_chunks: int = 3) -> list[str]:
        """가이드라인 텍스트에서 키워드 관련 조각을 반환"""
        if not self.guideline_text:
            return []
        chunks = _split_guideline_sentences(self.guideline_text)
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
        return selected

    def report(self) -> str:
        """에이전트 상태 보고"""
        sheets = [v["sheet_name"] for k, v in self.schema.items() if isinstance(v, dict) and "sheet_name" in v]
        return (
            f"[에이전트1 가이드라인] 로드 완료\n"
            f"  - 추출 대상 시트: {len(sheets)}개\n"
            f"  - 시트 목록: {', '.join(sheets)}\n"
            f"  - 가이드라인 출처: {self._guideline_status()}"
        )

    def _guideline_status(self) -> str:
        if self.guideline_loaded:
            total_snippets = sum(len(v) for v in self.guideline_context.values())
            spec_count = len(self.agent_spec)
            spec_text = f", 에이전트 재구성 {spec_count}개 시트" if spec_count else ""
            return f"로드 완료 ({self.guideline_source}, 관련 지침 {total_snippets}개 반영{spec_text})"
        if self.guideline_error:
            return f"로드 실패 ({self.guideline_error})"
        return "없음 (내장 스키마 사용)"
