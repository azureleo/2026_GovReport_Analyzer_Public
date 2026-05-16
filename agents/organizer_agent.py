"""
에이전트 3: 정리·정제 에이전트

에이전트2(텍스트)와 에이전트2b(이미지)의 원시 추출 결과를 받아
최종 엑셀 출력에 맞게 정제·정리합니다.
"""

import logging
import re
from copy import deepcopy
from typing import Any

import config
from utils import llm_client

logger = logging.getLogger(__name__)


ORGANIZER_SYSTEM = """당신은 한국 지자체 탄소중립 계획 데이터를 정제하는 전문 데이터 큐레이터입니다.
반드시 JSON만 반환하세요. 설명 텍스트 없이 JSON만."""


# ── 부문 정규화: IPCC 코드, 복합 표기, 이형어 모두 표준 부문명으로 통일 ──
_SECTOR_MAP = {
    # 기존 표기 변형
    "건물부문": "건물", "수송부문": "수송", "농업부문": "농축산",
    "농업": "농축산", "폐기물부문": "폐기물", "흡수": "흡수원",
    "산업부문": "산업", "에너지전환": "전환", "전환부문": "전환", "수소부문": "수소",
    # 복합·합산 부문 표기 → 표준화
    "상업/공공": "건물", "상업·공공": "건물",
    "가정·상업": "건물", "가정/상업": "건물",
    "도로수송": "수송", "도로": "수송", "비도로수송": "수송", "비도로": "수송",
    "에너지생산": "전환", "에너지 생산": "전환",
    # 세부 용도 → 표준 부문
    "가정": "건물", "상업": "건물", "공공": "건물",
    "에너지": "전환", "전력": "전환", "열": "전환",
    "산업공정": "산업",
    # 관리권한 배출량 → 합계로 통일
    "관리권한 배출량": "합계", "관리 권한 배출량": "합계",
    # 감축전략 시트 비표준 부문명
    "교통": "수송", "교통부문": "수송",
    "탄소흡수": "흡수원", "탄소 흡수": "흡수원",
    "공원녹지": "흡수원", "공원·녹지": "흡수원",
    "녹색성장": "기타", "기후예산": "기타", "재정투자": "기타",
    "협력": "기타", "인력양성": "기타",
    # IPCC 분류 코드 → 표준 부문
    "1A 연료연소": "전환", "1B 탈루배출": "전환",
    "2 산업공정": "산업", "2A 광물산업": "산업", "2B 화학산업": "산업",
    "3A1 장내발효": "농축산", "3A2 가축분뇨": "농축산", "3B 토지이용": "흡수원",
    "4A 폐기물 매립(직접)": "폐기물", "4A 폐기물매립": "폐기물",
    "4B 생물학적처리": "폐기물", "4C 폐수처리": "폐기물", "4D 소각": "폐기물",
}

# GHG 가스 종류 (부문명으로 잘못 들어오면 제거)
_GAS_TYPES = {"CO2", "CH4", "N2O", "HFCs", "PFCs", "SF6", "NF3", "HFC", "PFC"}

# 유효한 GHG 데이터 종류
_VALID_GHG_TYPES = {"현황", "전망", "목표"}

_CHART_STRONG_TYPES = {"표"}
_CHART_ESTIMATED_TYPES = {"막대", "꺾은선", "영역", "복합"}

# ── 배출유형 정규화 ──
_TYPE_MAP = {
    "직접 배출": "직접배출", "직접배출량": "직접배출",
    "간접 배출": "간접배출", "간접배출량": "간접배출",
    "흡수": "흡수원", "흡수량": "흡수원",
    # 총배출량 = 직접+간접 합산, 합계로 처리
    "총배출량": "직접배출", "총 배출량": "직접배출",
    "배출량": "직접배출",
}


def _chart_year_int(year: Any) -> int | None:
    try:
        return int(float(str(year).strip()))
    except (ValueError, TypeError):
        return None


def _is_reference_chart_row(row: dict) -> bool:
    text = " ".join(
        str(row.get(k, "") or "")
        for k in ["제목", "근거", "단위", "그래프유형"]
    ).casefold()
    return any(keyword.casefold() in text for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS)


def _clean_chart_observations(raw_rows: list[dict], municipality: str) -> list[dict]:
    """이미지·그래프 판독 결과를 감사 가능한 후보 목록으로 정제한다."""
    cleaned: list[dict] = []
    seen: set[tuple] = set()

    for r in raw_rows:
        if not isinstance(r, dict):
            continue

        value = _to_float(r.get("값"))
        year = _chart_year_int(r.get("연도"))
        chart_type = str(r.get("그래프유형", "") or "")
        target_sheet = str(r.get("대상시트", "") or "")
        title = str(r.get("제목", "") or "").strip()
        item = str(r.get("항목", "") or "").strip()
        unit = str(r.get("단위", "") or "").strip()
        evidence = str(r.get("근거", "") or "").strip()

        key = (
            r.get("페이지"), target_sheet, chart_type, title,
            unit, item, year, value,
        )
        if key in seen:
            continue
        seen.add(key)

        reasons: list[str] = []
        if value is None:
            reasons.append("값 없음")
        if year is None:
            reasons.append("연도 없음/비숫자")
        if year is not None and year not in config.IMAGE_CHART_MERGE_YEARS:
            reasons.append("연도 범위 외")
        if _is_reference_chart_row(r):
            reasons.append("참고자료/해외사례")
        if target_sheet == "summary":
            reasons.append("요약/참고성 자료")
        if chart_type in _CHART_ESTIMATED_TYPES:
            reasons.append("그래프 추정 후보")
        if "estimated" in evidence.lower():
            reasons.append("축 기반 추정값")

        original_status = str(r.get("반영여부", "") or "")
        can_remain_merged = original_status == "반영" and not reasons and chart_type in _CHART_STRONG_TYPES
        status = "반영" if can_remain_merged else "검토"

        if can_remain_merged:
            confidence = "high"
        elif value is not None and not _is_reference_chart_row(r):
            confidence = "medium"
        else:
            confidence = "low"

        if reasons:
            reason_text = "; ".join(dict.fromkeys(reasons))
            evidence = f"{evidence} | {reason_text}" if evidence else reason_text

        cleaned.append({
            "지자체명": r.get("지자체명") or municipality,
            "페이지": r.get("페이지"),
            "대상시트": target_sheet,
            "그래프유형": chart_type,
            "제목": title,
            "단위": unit,
            "항목": item,
            "연도": year if year is not None else r.get("연도"),
            "값": value,
            "신뢰도": confidence,
            "반영여부": status,
            "근거": evidence,
        })

    return cleaned

# ── GHG 데이터 종류 정규화 ──
_GHG_DATA_TYPE_MAP = {
    "현황(실적)": "현황", "실적": "현황",
    "BAU": "전망", "전망(BAU)": "전망",
    "NDC": "목표", "감축목표": "목표", "계획": "목표",
}

# ── 자동차 용도 정규화 ──
_VEHICLE_USAGE_MAP = {
    "전체": "전체", "총계": "전체", "합계": "전체",
    "승용차": "승용", "승용 차": "승용",
    "화물차": "화물", "화물 차": "화물",
    "이륜차": "이륜", "이륜 차": "이륜",
    "승합차": "승합", "승합 차": "승합",
    "특수차": "특수", "특수 차": "특수",
}

# ── 에너지 합산 용도: 해당 구성 세분 용도가 모두 있으면 합산 행 제거 ──
_ENERGY_AGGREGATE_MAP: dict[str, set[str]] = {
    "가정·상업": {"가정", "상업"},
    "가정/상업": {"가정", "상업"},
    "가정 및 상업": {"가정", "상업"},
    "상업·공공": {"상업", "공공"},
    "공공·기타": {"공공", "기타"},
}


def _normalize(val: str, mapping: dict) -> str:
    if not isinstance(val, str):
        return val
    return mapping.get(val.strip(), val.strip())


def _normalize_sector(val: str) -> str:
    """부문명 정규화: IPCC 코드 prefix 패턴(숫자코드 + 공백)도 제거 후 매핑"""
    if not isinstance(val, str):
        return val
    v = val.strip()
    # 가스 종류 이름은 부문이 아님 → 제거
    if v in _GAS_TYPES:
        return ""
    # 먼저 정확히 매핑되는지 확인
    if v in _SECTOR_MAP:
        return _SECTOR_MAP[v]
    # IPCC 숫자+문자 코드 패턴 제거 후 재시도 (예: "1A2 산업연소" → "산업")
    stripped = re.sub(r'^[\dA-Z]+[A-Za-z\d]*\s+', '', v).strip()
    if stripped in _SECTOR_MAP:
        return _SECTOR_MAP[stripped]
    # 표 제목·절 제목처럼 긴 텍스트는 빈 문자열로 처리
    if len(v) > 25 and re.search(r'[\[\]【】표그림]', v):
        return ""
    if len(v) > 40:  # 너무 긴 텍스트 일반 필터
        return ""
    return v


def _normalize_strategy_name(name: str) -> str:
    """감축사업명에서 선행 영문코드(B1, M1 등) 제거"""
    if not isinstance(name, str):
        return name
    # 예: "B1 기존 건물 ZEB 전환" → "기존 건물 ZEB 전환"
    cleaned = re.sub(r'^[A-Z]\d+\s+', '', name.strip())
    return cleaned if cleaned else name.strip()


def _is_code_only_name(name: str) -> bool:
    """'B1', 'M1' 처럼 코드만으로 된 사업명 여부"""
    return bool(re.fullmatch(r'[A-Z]\d+', name.strip() if isinstance(name, str) else ""))


# 자동차: 합산 차종명 → 세분 차종이 있으면 합산 행 제거
_VEHICLE_AGGREGATE_TYPES = {"내연기관", "내연기관차"}
# 내연기관을 구성하는 세분 차종
_ICE_SUBTYPES = {"경유", "휘발유", "LPG", "CNG"}
_VALID_VEHICLE_USAGES = {"전체", "승용", "화물", "버스", "이륜", "특수", "승합"}
_VALID_VEHICLE_TYPES = {"전체", "경유", "휘발유", "LPG", "전기", "수소", "하이브리드", "CNG", "기타", "내연기관", "내연기관차"}


def _deduplicate_vehicle(rows: list[dict]) -> list[dict]:
    """자동차 데이터: 용도 정규화 + 내연기관 합산 행 제거 + 용도+차종 중복 제거"""
    # 용도 정규화
    for r in rows:
        r["용도"] = _VEHICLE_USAGE_MAP.get(r.get("용도", "").strip(), r.get("용도", ""))

    rows = [
        r for r in rows
        if r.get("용도") in _VALID_VEHICLE_USAGES
        and r.get("차종") in _VALID_VEHICLE_TYPES
        and (r.get("대수") is not None or r.get("주행거리") is not None)
    ]

    # 내연기관(합산) 행 제거: 같은 용도에 경유/휘발유/LPG 세분값이 있으면 제거
    from collections import defaultdict
    by_usage: dict[str, set] = defaultdict(set)
    for r in rows:
        by_usage[r.get("용도", "")].add(r.get("차종", ""))

    filtered = []
    for r in rows:
        차종 = r.get("차종", "")
        if 차종 in _VEHICLE_AGGREGATE_TYPES:
            usage = r.get("용도", "")
            if _ICE_SUBTYPES & by_usage[usage]:  # 세분 차종 존재
                logger.debug(f"자동차 합산 행 제거: {usage}/{차종}")
                continue
        filtered.append(r)

    # 용도+차종 기준 중복 제거 (값 우선순위 병합)
    merged: dict[tuple, dict] = {}
    for r in filtered:
        key = (r.get("지자체명", ""), r.get("용도", ""), r.get("차종", ""))
        if key not in merged:
            merged[key] = dict(r)
        else:
            for field in ["대수", "주행거리"]:
                if merged[key].get(field) is None and r.get(field) is not None:
                    merged[key][field] = r[field]
    return list(merged.values())


def _filter_energy_aggregates(rows: list[dict]) -> list[dict]:
    """에너지 데이터: 세분 용도가 이미 있으면 합산 용도 행 제거"""
    all_usages = {r.get("용도", "") for r in rows}
    result = []
    for r in rows:
        usage = r.get("용도", "")
        if usage in _ENERGY_AGGREGATE_MAP:
            parts = _ENERGY_AGGREGATE_MAP[usage]
            if parts.issubset(all_usages):
                logger.debug(f"에너지 합산 행 제거: {usage} (세분값 존재)")
                continue
        result.append(r)
    return result


def _deduplicate_energy(rows: list[dict]) -> list[dict]:
    """에너지 데이터: 같은 지자체+용도 행을 병합하고 완전 중복을 제거한다."""
    cols = ["석유_에너지유", "석유_LPG", "석유_비에너지유", "가스", "전력", "열", "신재생"]
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = (row.get("지자체명", ""), row.get("용도", ""))
        if key not in merged:
            merged[key] = dict(row)
            continue
        for col in cols:
            if merged[key].get(col) is None and row.get(col) is not None:
                merged[key][col] = row[col]
    return list(merged.values())


def _filter_ghg_outliers(rows: list[dict]) -> list[dict]:
    """
    GHG 연도별 값에서 이상치 제거 (절대값 기준).
    5억 tCO2eq 초과는 어떤 한국 도시에도 불가능.
    """
    for row in rows:
        yearly = row.get("연도별")
        if not isinstance(yearly, dict):
            continue
        for year, val in list(yearly.items()):
            if not isinstance(val, (int, float)):
                continue
            if val > 500_000_000:
                logger.warning(f"GHG 이상치 제거(절대값): 부문={row.get('부문')}, {year}={val}")
                yearly[year] = None
    return rows


def _maybe_rescale_ghg_value(val: float, median: float) -> float | None:
    """
    GHG 값의 천 단위/소수점 파싱 오류를 보정.

    보고서 표에서 25,432.000처럼 표시된 값이 LLM 응답에서 25432000으로
    붙는 경우가 있다. 이 값은 삭제보다 /1000 보정이 더 타당하다.
    """
    if not isinstance(val, (int, float)) or val <= 0:
        return None

    # 가장 흔한 오류는 "천톤CO2eq" 표의 25,432.000을 25,432,000처럼
    # 읽는 경우이므로 /1000을 우선한다.
    preferred = val / 1000
    if 1 <= preferred <= 100_000:
        return preferred

    candidates = [val / 10_000, val / 1_000_000]
    plausible = []
    for scaled in candidates:
        # 지자체 GHG 단위로 자주 등장하는 천톤CO2eq 규모.
        if 1 <= scaled <= 100_000:
            plausible.append(scaled)

    if not plausible:
        return None

    if median > 0:
        # 중앙값과 가장 가까운 스케일을 선택하되, 중앙값이 비율/퍼센트 값으로 오염된
        # 경우에도 천톤CO2eq로 보이는 값은 보존한다.
        return min(plausible, key=lambda x: abs(x - median))
    return plausible[0]


def _filter_ghg_outliers_cross_group(rows: list[dict]) -> list[dict]:
    """
    중복 제거 후 종류(현황/전망/목표) 그룹 내 이상치 탐지.
    그룹 내 모든 연도값의 중앙값 대비 큰 값은 먼저 천 단위 보정을 시도하고,
    보정도 불가능한 경우에만 제거한다.
    """
    import statistics
    from collections import defaultdict

    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[row.get("종류", "")].append(row)

    for kind, group_rows in groups.items():
        all_vals = []
        for row in group_rows:
            yearly = row.get("연도별") or {}
            all_vals.extend(v for v in yearly.values() if isinstance(v, (int, float)) and v > 0)

        if len(all_vals) < 4:
            continue
        try:
            median = statistics.median(all_vals)
        except Exception:
            continue
        if median <= 0:
            continue

        threshold = median * 200

        for row in group_rows:
            yearly = row.get("연도별")
            if not isinstance(yearly, dict):
                continue
            for year, val in list(yearly.items()):
                if isinstance(val, (int, float)) and val > threshold:
                    scaled = _maybe_rescale_ghg_value(val, median)
                    if scaled is not None:
                        logger.warning(
                            f"GHG 그룹이상치 스케일 보정: 종류={kind}, 부문={row.get('부문')}, "
                            f"{year}={val:.0f} → {scaled:g} (그룹중앙값={median:.1f})"
                        )
                        yearly[year] = scaled
                        continue
                    logger.warning(
                        f"GHG 그룹이상치 제거: 종류={kind}, 부문={row.get('부문')}, "
                        f"{year}={val:.0f} (그룹중앙값={median:.1f})"
                    )
                    yearly[year] = None
    return rows


def _filter_strategy_code_duplicates(rows: list[dict]) -> list[dict]:
    """
    감축전략: 코드명('B1')과 코드+한글명('B1 기존 건물 ZEB 전환')이 같은 종류로 중복될 때
    코드명만 있는 행 제거.
    """
    # 코드+한글명 수집 (세부+종류 기준)
    full_names: set[tuple] = set()
    for r in rows:
        name = r.get("감축사업명", "")
        if not _is_code_only_name(name):
            full_names.add((r.get("지자체명", ""), r.get("감축사업명_세부", ""), r.get("종류", "")))

    result = []
    for r in rows:
        name = r.get("감축사업명", "")
        if _is_code_only_name(name):
            key = (r.get("지자체명", ""), r.get("감축사업명_세부", ""), r.get("종류", ""))
            if key in full_names:
                logger.debug(f"코드명 중복 제거: {name}")
                continue
        result.append(r)
    return result


def _split_zero_year_strategy(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """연도값이 없는 감축전략 행을 기존 시트에서 분리해 별도 시트로 보낸다."""
    quantitative: list[dict] = []
    qualitative: list[dict] = []
    marker_patterns = [
        "정성사업/연도값 원문미기재",
        "정성사업/연도값 원문 미기재",
        "정성사업/연도값 미기재",
        "정성사업/원문미기재",
    ]
    for row in rows:
        yearly = row.get("연도별") or {}
        filled = sum(1 for year in config.YEARS if yearly.get(str(year)) is not None)
        if filled == 0:
            row = dict(row)
            current = (row.get("성과지표") or "").strip()
            for marker in marker_patterns:
                current = current.replace(f"({marker})", "")
                current = current.replace(marker, "")
            row["성과지표"] = current.strip()
            row.pop("보완상태", None)
            qualitative.append(row)
        else:
            quantitative.append(row)
    return quantitative, qualitative


def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    # 중첩 dict: {"가정": 13043, "상업": 16251} → 합산
    if isinstance(val, dict):
        try:
            total = sum(
                float(v) for v in val.values()
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(",", "").replace("약 ", "").strip().replace(".", "").lstrip("-").isdigit())
            )
            return total if total != 0 else None
        except Exception:
            return None
    try:
        return float(str(val).replace(",", "").replace("약 ", "").strip())
    except (ValueError, TypeError):
        return None


def _ensure_all_years(yearly: dict) -> dict:
    for year in config.YEARS:
        yearly.setdefault(str(year), None)
    return yearly


def _deduplicate(rows: list[dict], key_fields: list[str]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(f, "") for f in key_fields)
        if key not in merged:
            merged[key] = deepcopy(row)
            if "연도별" in row:
                merged[key]["연도별"] = {}
        if "연도별" in row:
            for year in [str(y) for y in config.YEARS]:
                val = _to_float((row.get("연도별") or {}).get(year))
                if val is not None and merged[key]["연도별"].get(year) is None:
                    merged[key]["연도별"][year] = val
    return list(merged.values())


def _local_clean(raw: dict) -> dict:
    municipality = raw.get("municipality_name", "알 수 없음")

    # ── 자동차: 용도 정규화 + 주행거리 이상값 제거 + 중복 제거 ──
    vehicle_pre = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "용도": r.get("용도", ""),
            "차종": r.get("차종", ""),
            "대수": _to_float(r.get("대수")),
            # 1일 평균 주행거리: 10,000 km/일 초과는 명백한 단위 오류 → None
            "주행거리": _to_float(r.get("주행거리")) if (_to_float(r.get("주행거리")) or 0) <= 10_000 else None,
        }
        for r in raw.get("vehicle", []) if isinstance(r, dict)
    ]
    vehicle = _deduplicate_vehicle(vehicle_pre)

    # ── 에너지: 합산 용도 필터링 ──
    energy_pre = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "용도": r.get("용도", ""),
            "석유_에너지유": _to_float(r.get("석유_에너지유")),
            "석유_LPG": _to_float(r.get("석유_LPG")),
            "석유_비에너지유": _to_float(r.get("석유_비에너지유")),
            "가스": _to_float(r.get("가스")),
            "전력": _to_float(r.get("전력")),
            "열": _to_float(r.get("열")),
            "신재생": _to_float(r.get("신재생")),
        }
        for r in raw.get("energy", []) if isinstance(r, dict)
    ]
    energy = _filter_energy_aggregates(energy_pre)
    # 모든 에너지 수치가 None인 행 제거
    _energy_val_cols = ["석유_에너지유", "석유_LPG", "석유_비에너지유", "가스", "전력", "열", "신재생"]
    energy = [r for r in energy if any(r.get(c) is not None for c in _energy_val_cols)]
    energy = _deduplicate_energy(energy)

    # ── 온실가스: 부문 정규화 + 이상치 제거 + 중복 제거 ──
    ghg_raw = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "배출유형": _normalize(r.get("배출유형", "직접배출"), _TYPE_MAP),
            "종류": _normalize(r.get("종류", "현황"), _GHG_DATA_TYPE_MAP),
            "부문": _normalize_sector(r.get("부문", "")),
            "연도별": _ensure_all_years({
                str(k): _to_float(v) for k, v in (r.get("연도별") or {}).items()
            }),
        }
        for r in raw.get("ghg", []) if isinstance(r, dict)
        # 부문명이 표 제목처럼 긴 경우 제외 (정규화 후 빈 문자열이 될 것이지만 미리 필터)
    ]
    # 부문이 빈 문자열이거나 가스종류인 행 제거
    ghg_raw = [r for r in ghg_raw if r.get("부문", "").strip()]
    # 종류가 유효하지 않은 행 제거 (결과, 지원, 기타 비표준값)
    ghg_raw = [r for r in ghg_raw if r.get("종류", "") in _VALID_GHG_TYPES]
    ghg_raw = _filter_ghg_outliers(ghg_raw)
    ghg = _deduplicate(ghg_raw, ["지자체명", "배출유형", "종류", "부문"])
    # 중복 제거 후 그룹 내 이상치 탐지 (단위 혼재 제거)
    ghg = _filter_ghg_outliers_cross_group(ghg)

    # ── 감축전략: 코드명 중복 제거 + 중복 제거 ──
    strategy_raw = [
        {
            "지자체명": r.get("지자체명") or municipality,
            "배출유형": _normalize(r.get("배출유형", "직접배출"), _TYPE_MAP),
            "감축전략_부문": _normalize_sector(r.get("감축전략_부문", "")),
            "감축사업명": _normalize_strategy_name(r.get("감축사업명", "")),
            "감축사업명_세부": r.get("감축사업명_세부", ""),
            "구분": r.get("구분", "공통"),
            "성과지표": r.get("성과지표", ""),
            "종류": r.get("종류", ""),
            "연도별": _ensure_all_years({
                str(k): _to_float(v) for k, v in (r.get("연도별") or {}).items()
            }),
        }
        for r in raw.get("strategy", []) if isinstance(r, dict)
    ]
    strategy_raw = _filter_strategy_code_duplicates(strategy_raw)
    strategy = _deduplicate(strategy_raw, ["지자체명", "감축사업명", "감축사업명_세부", "종류"])
    strategy, strategy_qualitative = _split_zero_year_strategy(strategy)

    # 요약카드: config.SUMMARY_ITEMS 5개만 유지
    # 파이프 포함 항목 파싱: "부문|내용|근거|전략|연결성" → 첫 부분만 항목으로
    summary_pool: dict[str, dict] = {}
    for r in raw.get("summary", []):
        if not isinstance(r, dict):
            continue
        item = r.get("항목", "")
        content = r.get("내용", "")
        source = r.get("근거", "")
        # 항목이 파이프 포함 시 첫 세그먼트를 항목으로, 나머지는 내용으로 사용
        if isinstance(item, str) and "|" in item:
            parts = [p.strip() for p in item.split("|")]
            item = parts[0]
            if not content and len(parts) > 1:
                content = " | ".join(p for p in parts[1:] if p and p.lower() != "null")
        # config.SUMMARY_ITEMS에 있는 항목만 유지
        if item not in config.SUMMARY_ITEMS:
            continue
        # 이미 내용이 있는 항목은 덮어쓰지 않음
        if item not in summary_pool or not summary_pool[item].get("내용"):
            summary_pool[item] = {
                "지자체명": r.get("지자체명") or municipality,
                "항목": item,
                "내용": content or "",
                "근거": source or "",
            }

    # 누락된 항목 채우기
    for item in config.SUMMARY_ITEMS:
        if item not in summary_pool:
            summary_pool[item] = {"지자체명": municipality, "항목": item, "내용": "", "근거": ""}

    summary = [summary_pool[item] for item in config.SUMMARY_ITEMS]

    chart_observations = _clean_chart_observations(
        raw.get("chart_observations", []),
        municipality,
    )

    return {
        "municipality_name": municipality,
        "vehicle": vehicle, "energy": energy,
        "ghg": ghg, "strategy": strategy,
        "strategy_qualitative": strategy_qualitative,
        "chart_observations": chart_observations,
        "summary": summary,
    }


def _llm_enhance_summary(cleaned: dict, full_text_excerpt: str) -> dict:
    empty_items = [s for s in cleaned["summary"] if not s.get("내용")]
    if not empty_items:
        return cleaned

    import json
    empty_names = [s["항목"] for s in empty_items]
    ghg_sample = json.dumps(cleaned["ghg"][:3], ensure_ascii=False)

    prompt = f"""지자체명: {cleaned['municipality_name']}

다음은 보고서 텍스트 일부와 GHG 데이터 샘플입니다.
아래 요약카드 항목들의 내용을 작성해 주세요.

필요한 항목: {', '.join(empty_names)}

GHG 데이터:
{ghg_sample}

보고서 텍스트:
{full_text_excerpt[:4000]}

다음 JSON 형식으로 반환하세요:
{{
  "summary_updates": [
    {{"항목": "항목명", "내용": "내용", "근거": "보고서 내 근거"}}
  ]
}}"""

    resp = llm_client.call_text(prompt, system=ORGANIZER_SYSTEM)
    parsed = llm_client.parse_json(resp)
    for update in parsed.get("summary_updates", []):
        for row in cleaned["summary"]:
            if row["항목"] == update.get("항목") and not row["내용"]:
                row["내용"] = update.get("내용", "")
                row["근거"] = update.get("근거", "")
    return cleaned


class OrganizerAgent:
    """에이전트 3: 정리·정제 에이전트"""

    def __init__(self):
        self._final_data: dict = {}

    def organize(self, raw_data: dict, full_text_excerpt: str = "") -> dict:
        print("[에이전트3 정리] 로컬 정제 시작...")
        cleaned = _local_clean(raw_data)
        counts = {k: len(v) for k, v in cleaned.items() if isinstance(v, list)}
        print(f"[에이전트3 정리] 로컬 정제 완료: {counts}")

        empty_summary = [s for s in cleaned["summary"] if not s.get("내용")]
        if empty_summary and full_text_excerpt:
            print(f"[에이전트3 정리] 요약카드 빈 항목 {len(empty_summary)}개 LLM 보완 중...")
            cleaned = _llm_enhance_summary(cleaned, full_text_excerpt)
            print("[에이전트3 정리] 요약카드 보완 완료")

        self._final_data = cleaned
        return cleaned

    def get_excel_ready(self) -> dict:
        d = self._final_data
        return {k: d.get(k, []) for k in ["vehicle", "energy", "ghg", "strategy", "strategy_qualitative", "chart_observations", "summary"]}

    def report(self) -> str:
        d = self._final_data
        counts = {k: len(v) for k, v in d.items() if isinstance(v, list)}
        return (
            f"[에이전트3 정리] 정제 완료\n"
            f"  - 지자체명: {d.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
