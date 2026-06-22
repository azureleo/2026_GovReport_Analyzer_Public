"""
에이전트 3: 정리·정제 에이전트

16개 시트 구조에 맞춰 원시 추출 결과를 정제합니다.
가이드라인(carbon_guideline.md) 기준 표준 코드에 맞춰 정규화하고,
중복 제거, 이상치 처리, 단위 검증을 수행합니다.
"""

import logging
import re
from copy import deepcopy
from typing import Any

import config

logger = logging.getLogger(__name__)


# ── 부문 정규화 매핑 ──
_SECTOR_MAP = {
    "건물부문": "건물", "수송부문": "수송", "농업부문": "농축산",
    "농업": "농축산", "폐기물부문": "폐기물", "흡수": "흡수원",
    "산업부문": "산업", "에너지전환": "전환", "전환부문": "전환", "수소부문": "수소",
    "상업/공공": "건물", "상업·공공": "건물",
    "가정·상업": "건물", "가정/상업": "건물",
    "도로수송": "수송", "도로": "수송", "비도로수송": "수송",
    "에너지생산": "전환", "에너지 생산": "전환",
    "가정": "건물", "상업": "건물", "공공": "건물",
    "에너지": "전환", "전력": "전환", "열": "전환",
    "산업공정": "산업",
    "관리권한 배출량": "합계", "관리 권한 배출량": "합계",
    "교통": "수송", "교통부문": "수송",
    "탄소흡수": "흡수원", "탄소 흡수": "흡수원",
    "공원녹지": "흡수원", "공원·녹지": "흡수원",
    "순환경제": "폐기물", "자원순환": "폐기물",
    "도시건물": "건물", "수송교통": "수송",
}

# 배출유형 정규화
_TYPE_MAP = {
    "직접 배출": "직접배출", "직접배출량": "직접배출",
    "간접 배출": "간접배출", "간접배출량": "간접배출",
    "흡수": "흡수원", "흡수량": "흡수원",
    "총배출량": "직접배출", "총 배출량": "직접배출",
    "배출량": "직접배출",
}

# 달성여부 정규화
_ACHIEVEMENT_MAP = {
    "달성": "달성", "완료": "달성", "목표달성": "달성",
    "정상추진": "정상추진", "정상 추진": "정상추진", "추진중": "정상추진",
    "지연": "지연", "일부지연": "지연",
    "미달성": "미달성", "미이행": "미달성", "중단": "미달성",
}

# 사업유형 정규화
_BUSINESS_TYPE_MAP = {
    "기존": "기존", "계속": "기존", "유지": "기존",
    "변경": "변경", "수정": "변경", "폐지": "변경",
    "신규": "신규", "추가": "신규", "새로운": "신규",
}

# 전망방법 코드 정규화
_FORECAST_METHOD_MAP = {
    "시계열": "stat_time_series", "시계열분석": "stat_time_series",
    "회귀분석": "stat_regression", "회귀": "stat_regression",
    "증가율": "stat_growth_rate", "증가율분석": "stat_growth_rate",
    "LEAP": "bottom_up_accounting_LEAP",
    "MARKAL": "bottom_up_optimization_MARKAL",
}


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        try:
            total = sum(float(v) for v in val.values() if isinstance(v, (int, float)))
            return total if total != 0 else None
        except Exception:
            return None
    try:
        return float(str(val).replace(",", "").replace("약 ", "").strip())
    except (ValueError, TypeError):
        return None


def _to_int(val: Any) -> int | None:
    f = _to_float(val)
    return int(f) if f is not None else None


def _normalize(val: str, mapping: dict) -> str:
    if not isinstance(val, str):
        return str(val) if val is not None else ""
    return mapping.get(val.strip(), val.strip())


def _normalize_sector(val: str) -> str:
    if not isinstance(val, str):
        return ""
    v = val.strip()
    if v in _SECTOR_MAP:
        return _SECTOR_MAP[v]
    stripped = re.sub(r'^[\dA-Z]+[A-Za-z\d]*\s+', '', v).strip()
    if stripped in _SECTOR_MAP:
        return _SECTOR_MAP[stripped]
    if len(v) > 40:
        return ""
    return v


def _deduplicate_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(str(row.get(f, "")).strip() for f in key_fields)
        if key not in seen:
            seen[key] = row
        else:
            for k, v in row.items():
                if v is not None and seen[key].get(k) is None:
                    seen[key][k] = v
    return list(seen.values())


def _filter_empty_rows(rows: list[dict], required_fields: list[str]) -> list[dict]:
    """required_fields 중 하나라도 값이 있는 행만 유지"""
    return [
        row for row in rows
        if any(row.get(f) is not None and str(row.get(f, "")).strip() for f in required_fields)
    ]


# ──────────────────────────────────────────────────────────────────────
# 시트별 정제 함수
# ──────────────────────────────────────────────────────────────────────

def _clean_document_meta(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["계획시작연도"] = _to_int(row.get("계획시작연도"))
        row["계획종료연도"] = _to_int(row.get("계획종료연도"))
        row["기준연도"] = _to_int(row.get("기준연도"))
    return _deduplicate_rows(rows, ["지자체명", "계획명"])


def _clean_plan_overview(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return rows


def _clean_regional_conditions(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["값"] = _to_float(row.get("값"))
    rows = _filter_empty_rows(rows, ["값", "지표명"])
    return _deduplicate_rows(rows, ["지자체명", "지표범주", "지표명", "연도"])


def _clean_emissions_regional(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["배출유형"] = _normalize(row.get("배출유형", ""), _TYPE_MAP)
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, ["지자체명", "배출유형", "부문", "세부부문", "연도"])


def _clean_emissions_management(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["관리부문"] = _normalize_sector(row.get("관리부문", ""))
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
    rows = [r for r in rows if r.get("관리부문")]
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, ["지자체명", "관리부문", "세부부문", "직간접구분", "연도"])


def _clean_emissions_forecast(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["전망값"] = _to_float(row.get("전망값"))
        method_raw = row.get("전망방법원문", "")
        if not row.get("전망방법코드") and method_raw:
            for keyword, code in _FORECAST_METHOD_MAP.items():
                if keyword in method_raw:
                    row["전망방법코드"] = code
                    break
    rows = _filter_empty_rows(rows, ["전망값"])
    return _deduplicate_rows(rows, ["지자체명", "시나리오", "부문", "연도"])


def _clean_reduction_targets(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalize_sector(row.get("부문", "") or "")
        row["기준연도"] = _to_int(row.get("기준연도"))
        row["목표연도"] = _to_int(row.get("목표연도"))
        row["기준배출량"] = _to_float(row.get("기준배출량"))
        row["배출전망"] = _to_float(row.get("배출전망"))
        row["목표감축량"] = _to_float(row.get("목표감축량"))
        row["목표배출량"] = _to_float(row.get("목표배출량"))
        row["감축률"] = _to_float(row.get("감축률"))
    return _deduplicate_rows(rows, ["지자체명", "목표수준", "부문", "목표연도"])


def _clean_vision_strategy(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return _filter_empty_rows(rows, ["비전문구", "전략명"])


def _clean_mitigation_projects(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalize_sector(row.get("부문", ""))
    return _filter_empty_rows(rows, ["사업명"])


def _clean_annual_implementation(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["기간시작"] = _to_int(row.get("기간시작"))
        row["기간종료"] = _to_int(row.get("기간종료"))
        row["연도"] = _to_int(row.get("연도"))
        row["목표물량"] = _to_float(row.get("목표물량"))
    return _filter_empty_rows(rows, ["사업명", "연간계획", "목표물량"])


def _clean_quantitative_reductions(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["활동량"] = _to_float(row.get("활동량"))
        row["감축원단위값"] = _to_float(row.get("감축원단위값"))
        row["예상감축량"] = _to_float(row.get("예상감축량"))
    return _filter_empty_rows(rows, ["예상감축량", "활동량"])


def _clean_financial_plan(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["예산액"] = _to_float(row.get("예산액"))
    return _filter_empty_rows(rows, ["예산액"])


def _clean_foundation_measures(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return _filter_empty_rows(rows, ["과제명", "주요내용"])


def _clean_governance_feedback(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return _filter_empty_rows(rows, ["거버넌스기구", "역할", "담당부서"])


def _clean_monitoring_performance(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["점검연도"] = _to_int(row.get("점검연도"))
        row["달성여부"] = _normalize(row.get("달성여부", ""), _ACHIEVEMENT_MAP)
        row["사업유형"] = _normalize(row.get("사업유형", ""), _BUSINESS_TYPE_MAP)
    return _filter_empty_rows(rows, ["사업명", "이행실적"])


def _clean_changes_actions(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["점검연도"] = _to_int(row.get("점검연도"))
    return _filter_empty_rows(rows, ["사업명", "변경사유", "조치계획"])


# 시트키 → 정제함수 매핑
_CLEANERS = {
    "document_meta": _clean_document_meta,
    "plan_overview": _clean_plan_overview,
    "regional_conditions": _clean_regional_conditions,
    "emissions_regional": _clean_emissions_regional,
    "emissions_management": _clean_emissions_management,
    "emissions_forecast": _clean_emissions_forecast,
    "reduction_targets": _clean_reduction_targets,
    "vision_strategy": _clean_vision_strategy,
    "mitigation_projects": _clean_mitigation_projects,
    "annual_implementation": _clean_annual_implementation,
    "quantitative_reductions": _clean_quantitative_reductions,
    "financial_plan": _clean_financial_plan,
    "foundation_measures": _clean_foundation_measures,
    "governance_feedback": _clean_governance_feedback,
    "monitoring_performance": _clean_monitoring_performance,
    "changes_actions": _clean_changes_actions,
}


class OrganizerAgent:
    """에이전트 3: 정리·정제 에이전트 (가이드라인 기반 16개 시트)"""

    def __init__(self):
        self._final_data: dict = {}

    def organize(self, raw_data: dict, full_text_excerpt: str = "") -> dict:
        print("[에이전트3 정리] 정제 시작...")
        municipality = raw_data.get("municipality_name", "알 수 없음")

        cleaned: dict = {"municipality_name": municipality}
        for sheet_key, cleaner in _CLEANERS.items():
            rows = raw_data.get(sheet_key, [])
            if not isinstance(rows, list):
                rows = []
            rows = [r for r in rows if isinstance(r, dict)]
            cleaned[sheet_key] = cleaner(rows, municipality)

        counts = {k: len(v) for k, v in cleaned.items() if isinstance(v, list) and v}
        print(f"[에이전트3 정리] 정제 완료: {counts}")

        self._final_data = cleaned
        return cleaned

    def get_excel_ready(self) -> dict:
        return {k: v for k, v in self._final_data.items() if isinstance(v, list)}

    def report(self) -> str:
        d = self._final_data
        counts = {k: len(v) for k, v in d.items() if isinstance(v, list) and v}
        return (
            f"[에이전트3 정리] 정제 완료\n"
            f"  - 지자체명: {d.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
