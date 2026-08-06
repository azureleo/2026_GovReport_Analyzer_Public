"""
에이전트 3: 정리·정제 에이전트

16개 시트 구조에 맞춰 원시 추출 결과를 정제합니다.
가이드라인(carbon_guideline.md) 기준 표준 코드에 맞춰 정규화하고,
중복 제거, 이상치 처리, 단위 검증을 수행합니다.
"""
# noqa: SIZE_OK — 16개 시트 정제 계약을 보존하는 기존 모놀리식 cleaner. WP별로만 국소 수정.

import json
import logging
import math
import re
import unicodedata
from copy import deepcopy
from typing import Any

import config
from utils.evidence_merge import build_evidence_catalog, match_evidence, normalize_evidence_ids
from utils.visual_contract import (
    normalize_comparison_text,
    normalize_quantity,
    normalize_value_source,
    quantities_conflict,
)
from utils.reference_data import (
    build_codebook_rows,
    find_appendix3_unit_by_id,
    match_appendix3_unit,
    match_appendix4_project,
    normalise_key_text,
)

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

_DIRECT_INDIRECT_TYPE_MAP = {
    "direct": "직접",
    "직접배출": "직접",
    "indirect": "간접",
    "간접배출": "간접",
    "sink": "흡수",
    "absorption": "흡수",
    "direct+indirect": "직접+간접",
    "direct+indirect emissions": "직접+간접",
    "direct + indirect": "직접+간접",
    "direct + indirect emissions": "직접+간접",
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
    "MARKAL-MACRO": "bottom_up_hybrid_MARKAL_MACRO",
    "MARKAL": "bottom_up_optimization_MARKAL",
    "ENPEP": "bottom_up_simulation_ENPEP",
}

_FORECAST_SCENARIO_MAP = {
    "추가조치": "추가조치",
    "추가 조치": "추가조치",
    "추가대책": "추가조치",
    "추가 대책": "추가조치",
    "BAU 대비": "정책반영",
    "BAU대비": "정책반영",
    "정책반영": "정책반영",
    "정책 반영": "정책반영",
    "현행추세": "BAU",
    "현행 추세": "BAU",
    "감축": "정책반영",
    "목표": "정책반영",
    "정책": "정책반영",
    "전망": "BAU",
    "BAU": "BAU",
}

_MANAGEMENT_ENERGY_SOURCE_TERMS = {"전력", "열", "에너지"}
_GUIDELINE_INVENTORY_SECTORS = (
    "에너지",
    "산업공정 및 제품 생산",
    "농업",
    "LULUCF",
    "전력",
    "열",
    "폐기물",
)
_IPCC_INVENTORY_SECTOR_MAP = {
    "1": "에너지",
    "2": "산업공정 및 제품 생산",
    "3": "농업",
    "4": "폐기물",
}
_SECTOR_QUALIFIER_RE = re.compile(r"^\s*(?P<sector>[^()（）]+?)\s*[（(](?P<qualifier>[^()（）]+)[）)]\s*$")
_IPCC_SECTOR_PREFIX_RE = re.compile(r"^\s*(?P<code>[1-4](?:[A-D]\d*)?)\s*(?P<label>[가-힣A-Za-z].*)$")
_TABLE_MARKER_RE = re.compile(r"(?:\[\s*)?표\s*\d+\s*[-–—.]\s*\d+(?:\s*\])?")
_DEDUP_TABLE_MARKER_FIELD = "__dedup_표마커"
_INTERNAL_DEDUP_FIELDS = {_DEDUP_TABLE_MARKER_FIELD}


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
    text = val.strip()
    if text in mapping:
        return mapping[text]
    casefold_mapping = {str(key).casefold(): value for key, value in mapping.items()}
    return casefold_mapping.get(text.casefold(), text)


def _normalize_direct_indirect_type(value: Any) -> str:
    """관리권한 직간접구분 표기 차이를 키 비교 전용 표준값으로 정규화한다."""
    if value is None:
        return ""
    return _normalize(str(value), _DIRECT_INDIRECT_TYPE_MAP)


def _normalize_co2_unit(unit: Any) -> str:
    """
    온실가스 단위 표기를 표준형으로 정규화한다.

    같은 단위가 '천톤CO2eq', '천 톤CO2eq.', '천톤CO₂eq'처럼 여러 표기로 갈려
    집계·비교가 불가능해지던 문제를 해결한다. CO2 단위가 아니면(명·대·TJ·원 등)
    원문을 그대로 둔다(스케일 환산은 하지 않음 — 표기만 통일).
    """
    if not isinstance(unit, str):
        return unit if unit is None else str(unit)
    raw = unit.strip()
    if not raw:
        return raw
    key = (
        raw.replace(" ", "")
        .replace(".", "")
        .replace("₂", "2")
        .replace("CO₂", "CO2")
        .lower()
    )
    if key in {"백만톤co2eq", "백만톤co2", "백만톤co2e", "백만톤이산화탄소"}:
        return "백만톤CO2eq"
    if key in {"천톤co2eq", "천톤co2", "천톤co2e", "천toco2eq", "천톤이산화탄소"}:
        return "천톤CO2eq"
    if key in {"tco2eq", "톤co2eq", "tco2e", "톤co2", "tco2", "톤co2e"}:
        return "tCO2eq"
    return raw


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


def _inventory_sector_key(value: str) -> str:
    return normalise_key_text(value).replace(" ", "")


def _standard_inventory_sector(value: str) -> str:
    sectors = list(_GUIDELINE_INVENTORY_SECTORS) + list(getattr(config, "SECTORS", []))
    by_key = {_inventory_sector_key(sector): sector for sector in sectors}
    return by_key.get(_inventory_sector_key(value), "")


def _compact_qualifier(value: str) -> str:
    return normalise_key_text(value).replace(" ", "")


def _apply_inventory_sector_normalization(row: dict, field: str, raw_field: str) -> None:
    raw_text = str(row.get(field, "") or "").strip()
    if not raw_text:
        return

    ipcc_match = _IPCC_SECTOR_PREFIX_RE.match(raw_text)
    if ipcc_match is not None:
        mapped = _IPCC_INVENTORY_SECTOR_MAP.get(ipcc_match.group("code")[0])
        if mapped:
            row[field] = mapped
            row.setdefault(raw_field, raw_text)
            if not _has_cell_value(row.get("세부부문")):
                row["세부부문"] = raw_text
            return

    qualifier_match = _SECTOR_QUALIFIER_RE.match(raw_text)
    if qualifier_match is None:
        return
    sector = _standard_inventory_sector(qualifier_match.group("sector"))
    if not sector:
        return
    row[field] = sector
    row.setdefault(raw_field, raw_text)
    if not _has_cell_value(row.get("세부부문")):
        row["세부부문"] = _compact_qualifier(qualifier_match.group("qualifier"))


_MAJOR_CHAPTER_LINE_PATTERNS = (
    re.compile(r"^\s*제\s*0*(\d{1,2})\s*장(?:\s|[:：.]|$)"),
    re.compile(r"^\s*0*(\d{1,2})\s*장(?:\s|[:：.]|$)"),
    # 실제 지자체 보고서에서 쓰는 '03 기존 계획의 평가', '04 비전 및 전략' 형식.
    # 한 자리 절 번호와 혼동하지 않도록 이 형식은 두 자리 번호만 허용한다.
    re.compile(r"^\s*(\d{2})\s+(?=\S)"),
)


def _chapter_heading_ids(text: str) -> list[str]:
    ids: list[str] = []
    lines = [line.strip() for line in (text or "").splitlines()[:16]]
    for index, compact in enumerate(lines):
        if not compact:
            continue
        for pattern in _MAJOR_CHAPTER_LINE_PATTERNS:
            match = pattern.match(compact)
            if match is not None:
                ids.append(str(int(match.group(1))))
                break
        else:
            # PDF 레이아웃 추출에서 '04'와 '비전 및 전략'이 서로 다른 줄로 분리되는 경우.
            # 다음 줄이 짧은 제목일 때만 장 후보로 인정한다.
            number_only = re.fullmatch(r"(\d{2})", compact)
            following = next((line for line in lines[index + 1:] if line), "")
            if (
                number_only is not None
                and following
                and len(following) <= 80
                and not re.fullmatch(r"[\d\s.,%-]+", following)
            ):
                ids.append(str(int(number_only.group(1))))
    return ids


def _chapter_id(text: str) -> str:
    ids = _chapter_heading_ids(text)
    return ids[0] if ids else ""


def _has_chapter_heading(text: str) -> bool:
    return bool(_chapter_heading_ids(text))


def _is_prior_plan_heading(text: str) -> bool:
    patterns = getattr(config, "PRIOR_PLAN_HEADING_PATTERNS", [])
    for line in (text or "").splitlines()[:16]:
        compact = line.strip()
        if compact and len(compact) <= 120 and any(re.search(pattern, compact) for pattern in patterns):
            return True
    return False


def detect_prior_plan_pages(pages: list) -> set[int]:
    sorted_pages = sorted(pages, key=lambda page: getattr(page, "page_number", 0))
    start_page = None
    start_chapter = ""
    for page in sorted_pages:
        text = getattr(page, "text", "")
        if not _is_prior_plan_heading(text):
            continue
        chapter_ids = set(_chapter_heading_ids(text))
        # 목차 페이지는 여러 장 제목이 한꺼번에 등장한다. 시작점으로 쓰지 않는다.
        if len(chapter_ids) >= 3 or "목차" in "\n".join((text or "").splitlines()[:8]):
            continue
        start_page = int(getattr(page, "page_number", 0))
        start_chapter = _chapter_id(text)
        break
    if start_page is None:
        return set()

    detected: set[int] = set()
    active = False
    max_pages = max(1, int(getattr(config, "PRIOR_PLAN_MAX_PAGES", 60)))
    for page in sorted_pages:
        page_number = int(getattr(page, "page_number", 0))
        text = getattr(page, "text", "")
        if page_number == start_page:
            active = True
        if not active:
            continue
        current_chapter = _chapter_id(text)
        if page_number != start_page and _has_chapter_heading(text):
            # 장 내부의 '01 개요', '02 성과 평가' 절 번호는 시작 장 번호보다 작거나
            # 같을 수 있다. 시작 장보다 큰 번호만 다음 장 경계로 본다.
            is_later_chapter = bool(
                start_chapter
                and current_chapter
                and int(current_chapter) > int(start_chapter)
            )
            if not start_chapter or is_later_chapter:
                break
        detected.add(page_number)
        if len(detected) > max_pages:
            logger.warning(
                "기존계획 평가 장 종료 경계를 %s페이지 안에 찾지 못해 탐지를 무효화합니다: p%s 이후",
                max_pages,
                start_page,
            )
            return set()
    return detected


_DEDUP_CONFLICTS: list[dict] = []
_RECORDED_VALIDATION_ISSUES: list[dict] = []
_NUMERIC_CONFLICT_FIELDS = {
    "배출량", "전망값", "예산액", "예상감축량", "목표배출량", "목표감축량",
    "기준배출량", "배출전망", "값", "활동량", "목표물량", "감축률",
}
_PLAN_CONTEXT_FIELD = "계획구분출처"
_DATA_STATUS_PRIORITY = {
    "reported": 0,
    "gap_fill": 1,
    "visual_only": 2,
    "calculated": 3,
    "conflicting": 4,
}
_REGIONAL_EMISSIONS_KEY_FIELDS = ["지자체명", "배출유형", "부문", "세부부문", "연도"]
_MANAGEMENT_EMISSIONS_KEY_FIELDS = ["지자체명", "관리부문", "세부부문", "직간접구분", "연도"]
_REDUCTION_TARGET_KEY_FIELDS = ["지자체명", "목표수준", "목표범위", "부문", "기준연도", "목표연도"]
_REDUCTION_SCALE_FIELDS = ("기준배출량", "배출전망", "목표감축량", "목표배출량")
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_VISUAL_MERGE_KEY_FIELDS = {
    "regional_conditions": ["지자체명", "지표범주", "지표세부범주", "지표명", "연도"],
    "emissions_regional": _REGIONAL_EMISSIONS_KEY_FIELDS,
    "emissions_management": _MANAGEMENT_EMISSIONS_KEY_FIELDS,
    "emissions_forecast": ["지자체명", "시나리오", "부문", "세부부문", "연도"],
    "reduction_targets": _REDUCTION_TARGET_KEY_FIELDS,
    "mitigation_projects": ["지자체명", "관리번호", "사업명"],
    "financial_plan": ["지자체명", "계획구분", "부문", "사업명", "재원구분", "연도"],
}
_VISUAL_MERGE_VALUE_FIELDS = {
    "regional_conditions": "값",
    "emissions_regional": "배출량",
    "emissions_management": "배출량",
    "emissions_forecast": "전망값",
    "reduction_targets": "목표배출량",
    "mitigation_projects": "사업명",
    "financial_plan": "예산액",
}
_BLANK_ABSORB_FIELDS_BY_KEY = {
    tuple(_REGIONAL_EMISSIONS_KEY_FIELDS): ("세부부문", "배출유형"),
    tuple(_MANAGEMENT_EMISSIONS_KEY_FIELDS): ("세부부문", "직간접구분"),
    tuple(_REDUCTION_TARGET_KEY_FIELDS): ("기준연도",),
}
_VISUAL_OPTIONAL_KEY_FIELDS = {"세부부문", "지표세부범주", "관리번호", "기준연도"}
_VISUAL_QUALITATIVE_SHEETS = {"mitigation_projects"}
_REDUCTION_VISUAL_VALUE_FIELDS = {
    "기준배출량", "배출전망", "목표감축량", "목표배출량", "감축률",
}
_IPCC_GAS_NAMES = {
    "이산화탄소", "메탄", "아산화질소", "수소불화탄소", "과불화탄소", "육불화황",
    "CO2", "CH4", "N2O", "HFCs", "PFCs", "SF6",
}
_GAS_SECTOR_NOISE_TOKENS = {"총", "배출량", "흡수량", "배출", "부문"}


def _normalize_provenance_pages(value: Any) -> str:
    """출처페이지 값을 엑셀에 쓰기 쉬운 오름차순 문자열로 정규화한다."""
    pages: set[int] = set()

    def add_one(item: Any) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            pages.add(item)
            return
        if isinstance(item, float):
            if item.is_integer():
                pages.add(int(item))
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                add_one(nested)
            return
        text = str(item)
        for start, end in re.findall(r"(\d+)\s*(?:~|-|–|—)\s*(\d+)", text):
            a = int(start)
            b = int(end)
            lo, hi = (a, b) if a <= b else (b, a)
            pages.update(range(lo, hi + 1))
        for number in re.findall(r"\d+", text):
            pages.add(int(number))

    add_one(value)
    return ",".join(str(page) for page in sorted(page for page in pages if page > 0))


def _merge_provenance(left: Any, right: Any) -> str:
    """두 출처페이지 값을 중복 없는 합집합 문자열로 병합한다."""
    return _normalize_provenance_pages([left, right])


def _normalize_row_provenance(row: dict) -> dict:
    if "출처페이지" in row or "출처페이지추정" in row:
        page_value = row.get("출처페이지") or row.get("출처페이지추정")
        normalized = _normalize_provenance_pages(page_value)
        if normalized:
            row["출처페이지"] = normalized
    return row


def _row_source_pages(row: dict) -> set[int]:
    normalized = _normalize_provenance_pages(row.get("출처페이지"))
    if not normalized:
        return set()
    return {int(part) for part in normalized.split(",") if part.isdigit()}


def _raw_row_source_pages(row: dict) -> set[int]:
    normalized = _normalize_provenance_pages([
        row.get("출처페이지"),
        row.get("출처페이지추정"),
    ])
    if not normalized:
        return set()
    return {int(part) for part in normalized.split(",") if part.isdigit()}


def _card_context_pages(raw_data: dict) -> set[int]:
    pages: set[int] = set()
    for sheet_key in (
        "mitigation_projects",
        "annual_implementation",
        "quantitative_reductions",
    ):
        rows = raw_data.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                pages.update(_raw_row_source_pages(row))
    return pages


def _years_in_consecutive_runs(
    years: set[int],
    minimum_length: int = 4,
) -> set[int]:
    qualifying: set[int] = set()
    run: list[int] = []
    for year in sorted(years):
        if run and year != run[-1] + 1:
            if len(run) >= minimum_length:
                qualifying.update(run)
            run = []
        run.append(year)
    if len(run) >= minimum_length:
        qualifying.update(run)
    return qualifying


def _retag_reduction_target_rows(
    rows: list[dict],
    municipality: str,
    card_pages: set[int],
) -> list[dict]:
    """06 원행을 dedup 전에 사업카드·연차경로 키로 분리한다."""
    for row in rows:
        pages = _raw_row_source_pages(row)
        target_level = str(row.get("목표수준", "") or "").strip()
        if pages and pages.issubset(card_pages) and target_level in {"총괄", "부문"}:
            row.setdefault("_재태깅이전목표수준", target_level)
            row["목표수준"] = "세부사업"
            row["_재태깅사유"] = "사업문맥페이지"

    groups: dict[tuple[str, str, str, str], list[tuple[dict, int]]] = {}
    for row in rows:
        year = _to_int(row.get("목표연도"))
        if year is None:
            continue
        page_key = _normalize_provenance_pages([
            row.get("출처페이지"),
            row.get("출처페이지추정"),
        ])
        key = (
            page_key,
            _dedup_key_text(row.get("목표수준")),
            _dedup_key_text(row.get("목표범위")),
            _dedup_key_text(row.get("부문")),
        )
        groups.setdefault(key, []).append((row, year))

    for group_rows in groups.values():
        run_years = _years_in_consecutive_runs({year for _row, year in group_rows})
        if not run_years:
            continue
        for row, year in group_rows:
            if year not in run_years:
                continue
            row.setdefault("_재태깅이전목표수준", row.get("목표수준"))
            row["목표수준"] = "연차경로"
            row["_재태깅사유"] = "연차시퀀스"

    retagged_count = 0
    for index, row in enumerate(rows, start=1):
        reason = str(row.get("_재태깅사유", "") or "").strip()
        if not reason:
            continue
        retagged_count += 1
        pages = _normalize_provenance_pages([
            row.get("출처페이지"),
            row.get("출처페이지추정"),
        ]) or "미상"
        _remember_validation_issue(
            municipality,
            "정보",
            _sheet_area("reduction_targets"),
            f"목표수준 재태깅(06 원행 {index})",
            f"출처페이지 {pages}; "
            f"{row.get('_재태깅이전목표수준') or '빈 값'}→{row.get('목표수준')}; "
            f"사유 {reason}",
            "원문 표가 사업 카드 또는 연차별 감축 경로인지 확인",
            target_sheet_key="reduction_targets",
        )
    print(f"[에이전트3 정리] 06_감축목표 재태깅 {retagged_count}건")
    return rows


def _dedup_key_text(val: Any) -> str:
    """dedup 키 생성 전용 텍스트 정규화. 원본 셀 값은 바꾸지 않는다."""
    return normalise_key_text(val).replace(" ", "")


def _reduction_target_project_hint_key(row: dict) -> str:
    if _dedup_key_text(row.get("목표수준")) != "세부사업":
        return ""
    return _dedup_key_text(row.get("_사업명힌트"))


def _dedup_key(row: dict, key_fields: list[str]) -> tuple:
    key = tuple(_dedup_key_text(row.get(field)) for field in key_fields)
    if tuple(key_fields) != tuple(_REDUCTION_TARGET_KEY_FIELDS):
        return key
    project_hint = _reduction_target_project_hint_key(row)
    return (*key, project_hint) if project_hint else key


def _visual_key_text(val: Any) -> str:
    """시각 병합 키에서만 밑줄과 공백 표기를 같은 값으로 접는다."""
    return _dedup_key_text(val).replace("_", "")


def _gas_sector_key(value: Any) -> str:
    text = normalise_key_text(value)
    tokens = text.split()
    while tokens and tokens[0] in _GAS_SECTOR_NOISE_TOKENS:
        tokens.pop(0)
    while tokens and tokens[-1] in _GAS_SECTOR_NOISE_TOKENS:
        tokens.pop()
    compact = "".join(tokens)
    noise_by_length = sorted(_GAS_SECTOR_NOISE_TOKENS, key=len, reverse=True)
    changed = True
    while compact and changed:
        changed = False
        for noise in noise_by_length:
            if compact.startswith(noise):
                compact = compact[len(noise):]
                changed = True
                break
        for noise in noise_by_length:
            if compact.endswith(noise):
                compact = compact[:-len(noise)]
                changed = True
                break
    return compact


def _normalize_project_id(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not text:
        return ""
    parts = re.split(r"([-_])", text)
    normalized: list[str] = []
    for part in parts:
        if part in {"-", "_"}:
            normalized.append("-")
            continue
        match = re.fullmatch(r"([A-Z]+)(0*\d+)", part)
        if match is not None:
            normalized.append(f"{match.group(1)}{int(match.group(2))}")
            continue
        if re.fullmatch(r"0*\d+", part):
            normalized.append(str(int(part)))
            continue
        normalized.append(part)
    return "".join(normalized)


def _has_cell_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _set_data_status(row: dict, status: Any) -> None:
    status_text = str(status or "").strip()
    if status_text not in _DATA_STATUS_PRIORITY:
        return
    current = str(row.get("데이터상태", "") or "").strip()
    if _DATA_STATUS_PRIORITY.get(status_text, -1) > _DATA_STATUS_PRIORITY.get(current, -1):
        row["데이터상태"] = status_text


def _is_visual_row(row: dict) -> bool:
    source_values = [
        row.get("출처"),
        row.get("인벤토리출처"),
        row.get("전망방법원문"),
        row.get("주요가정"),
    ]
    return any("이미지" in str(value or "") for value in source_values)


def _apply_row_data_status(row: dict) -> None:
    _set_data_status(row, "reported")
    if row.get("보완출처") == "gap_fill":
        _set_data_status(row, "gap_fill")
    if _is_visual_row(row):
        _set_data_status(row, "visual_only")


def _apply_default_data_status(cleaned: dict) -> None:
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = cleaned.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                _apply_row_data_status(row)


def _remember_validation_issue(
    municipality: str,
    severity: str,
    area: str,
    item: str,
    detail: str,
    action: str,
    *,
    target_sheet_key: str | None = None,
    target_row_number: int | None = None,
) -> None:
    _RECORDED_VALIDATION_ISSUES.append(
        _issue(
            municipality,
            severity,
            area,
            item,
            detail,
            action,
            target_sheet_key=target_sheet_key,
            target_row_number=target_row_number,
        )
    )


def _normalise_sector_with_raw(row: dict, field: str, raw_field: str) -> str:
    raw = row.get(field, "")
    normalized = _normalize_sector(raw)
    raw_text = str(raw).strip() if raw is not None else ""
    if raw_text and normalized and normalized != raw_text:
        row.setdefault(raw_field, raw_text)
    return normalized


def _apply_appendix4_project_match(row: dict, municipality: str, row_index: int) -> None:
    match = match_appendix4_project(row.get("사업명"))
    if match is None:
        return
    reference = match["row"]
    row["표준사업연번"] = reference.get("연번")
    row["표준사업명"] = reference.get("사업명")
    row["매칭신뢰도"] = match["confidence"]

    reference_sector = str(reference.get("부문", "") or "").strip()
    current_sector = str(row.get("부문", "") or "").strip()
    if reference_sector and not current_sector:
        row["부문"] = reference_sector
        return
    if reference_sector and current_sector and current_sector != reference_sector:
        _remember_validation_issue(
            municipality,
            "정보",
            _sheet_area("mitigation_projects"),
            f"부문 불일치(08 행 {row_index})",
            f"행 부문 '{current_sector}' vs 부록4 '{reference_sector}'",
            "지자체 신규/혼합 사업인지 확인하고 필요 시 부문을 수동 보정",
        )


def _appendix3_reference_value(reference: dict[str, str] | None) -> float | None:
    if reference is None:
        return None
    return _to_float(reference.get("원단위값"))


def _apply_appendix3_unit_match(row: dict, municipality: str, row_index: int) -> None:
    unit_id = str(row.get("감축원단위ID", "") or "").strip()
    reference = find_appendix3_unit_by_id(unit_id, row.get("모니터링인자")) if unit_id else None
    match = None
    if reference is None:
        match = match_appendix3_unit(row.get("사업명"), row.get("모니터링인자"))
        reference = match["row"] if match is not None else None
    if reference is None:
        return

    if not unit_id:
        row["감축원단위ID"] = reference.get("번호")
        if match is not None:
            row["감축원단위매칭신뢰도"] = match["confidence"]
    reference_value = _appendix3_reference_value(reference)
    current_value = row.get("감축원단위값")
    if current_value is None and reference_value is not None:
        row["감축원단위값"] = reference_value
        return
    if not isinstance(current_value, (int, float)) or reference_value in (None, 0):
        return
    if abs(current_value - reference_value) / abs(reference_value) > 0.05:
        _remember_validation_issue(
            municipality,
            "정보",
            _sheet_area("quantitative_reductions"),
            f"감축원단위값 차이(10 행 {row_index})",
            f"행 값 {current_value} vs 부록3 {reference.get('번호')} 값 {reference_value}",
            "원문 산식·단위와 부록3 적용 가능성을 확인",
        )


def _merge_target_year_values(values: list[Any]) -> str:
    years: set[int] = set()
    for value in values:
        if isinstance(value, int):
            years.add(value)
            continue
        for year in re.findall(r"\d{4}", str(value)):
            years.add(int(year))
    if years:
        return ",".join(str(year) for year in sorted(years))
    return max((str(value).strip() for value in values), key=len)


def _most_frequent_value(values: list[Any]) -> Any:
    counts: dict[str, int] = {}
    first_by_key: dict[str, Any] = {}
    for value in values:
        key = str(value).strip()
        counts[key] = counts.get(key, 0) + 1
        first_by_key.setdefault(key, value)
    winner = max(counts, key=lambda key: (counts[key], len(key)))
    return first_by_key[winner]


def _choose_document_meta_value(field: str, values: list[Any]) -> Any:
    if field == "목표연도":
        return _merge_target_year_values(values)
    if all(isinstance(value, (int, float)) for value in values):
        return _most_frequent_value(values)
    text_values = [str(value).strip() for value in values]
    return max(text_values, key=len)


def _merge_document_meta_rows(rows: list[dict], municipality: str) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("지자체명") or municipality), []).append(row)

    merged_rows: list[dict] = []
    for group_municipality, group_rows in grouped.items():
        merged: dict = {"지자체명": group_municipality}
        fields = sorted({field for row in group_rows for field in row})
        for field in fields:
            if field == "지자체명":
                continue
            values = [row.get(field) for row in group_rows if _has_cell_value(row.get(field))]
            if not values:
                continue
            distinct = {str(value).strip() for value in values}
            merged[field] = _choose_document_meta_value(field, values)
            if len(distinct) > 1 and field != "목표연도":
                _remember_validation_issue(
                    group_municipality,
                    "정보",
                    "문서메타",
                    f"문서메타 병합({field})",
                    f"{field} 후보 {sorted(distinct)} 중 '{merged[field]}' 채택",
                    "원문 표지·개요의 문서 메타를 확인",
                )
        merged_rows.append(merged)
    return merged_rows


def _values_conflict(left: Any, right: Any) -> bool:
    if not _has_cell_value(left) or not _has_cell_value(right):
        return False
    left_num = _to_float(left)
    right_num = _to_float(right)
    if left_num is None or right_num is None:
        return False
    if left_num == right_num:
        return False
    scale = max(abs(left_num), abs(right_num), 1.0)
    return abs(left_num - right_num) / scale > 0.005


def _row_conflicts(kept: dict, discarded: dict) -> list[str]:
    conflicts: list[str] = []
    for field in _NUMERIC_CONFLICT_FIELDS:
        if field in kept or field in discarded:
            if _values_conflict(kept.get(field), discarded.get(field)):
                conflicts.append(field)
    return conflicts


def _remember_dedup_conflict(
    key_fields: list[str],
    kept: dict,
    discarded: dict,
    fields: list[str],
    reason: str = "기존 순서 유지",
) -> None:
    resolved = bool(reason and reason != "기존 순서 유지")
    _set_data_status(kept, "conflicting")
    key_summary = ", ".join(f"{field}={kept.get(field) or discarded.get(field) or ''}" for field in key_fields)
    detail = "; ".join(
        f"{field}: 유지 {kept.get(field)} vs 폐기 {discarded.get(field)}"
        for field in fields
    )
    if reason:
        detail = f"{detail}; 채택근거: {reason}"
    _DEDUP_CONFLICTS.append({
        "key": key_summary,
        "detail": detail,
        "resolved": resolved,
        "reason": reason,
    })


def _merge_missing_values(target: dict, source: dict) -> None:
    for key, value in source.items():
        if key in _INTERNAL_DEDUP_FIELDS:
            continue
        if key == "출처페이지":
            merged = _merge_provenance(target.get("출처페이지"), value)
            if merged:
                target["출처페이지"] = merged
            continue
        if key == "데이터상태":
            _set_data_status(target, value)
            continue
        if _has_cell_value(value) and not _has_cell_value(target.get(key)):
            target[key] = value


def _scale_ratio(left: Any, right: Any) -> float | None:
    left_num = _to_float(left)
    right_num = _to_float(right)
    if left_num in (None, 0) or right_num in (None, 0):
        return None
    smaller = min(abs(left_num), abs(right_num))
    return max(abs(left_num), abs(right_num)) / smaller if smaller else None


def _reduction_scale_guard_kind(left: dict, right: dict) -> str | None:
    common_fields = [
        field for field in _REDUCTION_SCALE_FIELDS
        if _has_cell_value(left.get(field)) and _has_cell_value(right.get(field))
    ]
    for field in common_fields:
        ratio = _scale_ratio(left.get(field), right.get(field))
        if ratio is not None and abs(ratio - 1000.0) / 1000.0 <= 0.005:
            return "공통 필드"
    if common_fields:
        return None

    left_values = [abs(value) for field in _REDUCTION_SCALE_FIELDS if (value := _to_float(left.get(field))) is not None]
    right_values = [abs(value) for field in _REDUCTION_SCALE_FIELDS if (value := _to_float(right.get(field))) is not None]
    if not left_values or not right_values:
        return None
    ratio = _scale_ratio(max(left_values), max(right_values))
    return "스케일 서명" if ratio is not None and ratio >= 1000.0 else None


def _remember_reduction_scale_warning(left: dict, right: dict, kind: str) -> None:
    _remember_validation_issue(
        str(left.get("지자체명") or right.get("지자체명") or ""),
        "경고",
        _sheet_area("reduction_targets"),
        "스케일 표기 차 의심(톤↔천톤)",
        f"{kind} 기준으로 06 감축목표 중복 후보의 1,000배 스케일 차이를 감지",
        "자동 환산·교차 채움 없이 원문 단위를 수동 확인",
        target_sheet_key="reduction_targets",
    )


def _source_has_table_marker(row: dict) -> bool:
    return bool(_TABLE_MARKER_RE.search(str(row.get("출처페이지", "") or "")))


def _filled_cell_count(row: dict) -> int:
    return sum(
        1 for key, value in row.items()
        if key not in _INTERNAL_DEDUP_FIELDS and _has_cell_value(value)
    )


def _choose_conflict_row(kept: dict, incoming: dict) -> tuple[dict, dict, str]:
    kept_table = bool(kept.get(_DEDUP_TABLE_MARKER_FIELD))
    incoming_table = bool(incoming.get(_DEDUP_TABLE_MARKER_FIELD))
    if incoming_table and not kept_table:
        return incoming, kept, "표 마커 출처 우선"
    if kept_table and not incoming_table:
        return kept, incoming, "표 마커 출처 우선"

    # 본문/파싱 표의 reported 값을 이미지 판독값보다 우선한다. 상태가 같을 때만
    # 채움 필드 수를 비교해 값이 많은 행을 선택한다.
    source_rank = {
        "reported": 4,
        "gap_fill": 3,
        "calculated": 3,
        "visual_only": 2,
        "conflicting": 1,
    }
    kept_status = str(kept.get("데이터상태", "") or "").strip()
    incoming_status = str(incoming.get("데이터상태", "") or "").strip()
    kept_rank = source_rank.get(kept_status, 0)
    incoming_rank = source_rank.get(incoming_status, 0)
    if incoming_rank > kept_rank:
        return incoming, kept, "데이터 출처 우선순위"
    if kept_rank > incoming_rank:
        return kept, incoming, "데이터 출처 우선순위"

    kept_count = _filled_cell_count(kept)
    incoming_count = _filled_cell_count(incoming)
    if incoming_count > kept_count:
        return incoming, kept, "값 채움 필드 수 우선"
    if kept_count > incoming_count:
        return kept, incoming, "값 채움 필드 수 우선"

    return kept, incoming, "기존 순서 유지"


def _strip_dedup_internal_fields(rows: list[dict]) -> list[dict]:
    for row in rows:
        for field in _INTERNAL_DEDUP_FIELDS:
            row.pop(field, None)
    return rows


def _same_except_field(left: dict, right: dict, key_fields: list[str], blank_field: str) -> bool:
    same_base_key = all(
        field == blank_field or _dedup_key_text(left.get(field)) == _dedup_key_text(right.get(field))
        for field in key_fields
    )
    if not same_base_key:
        return False
    if tuple(key_fields) != tuple(_REDUCTION_TARGET_KEY_FIELDS):
        return True
    return (
        _reduction_target_project_hint_key(left)
        == _reduction_target_project_hint_key(right)
    )


def _absorb_blank_field_rows(rows: list[dict], key_fields: list[str], blank_field: str) -> list[dict]:
    if blank_field not in key_fields:
        return rows
    retained: list[dict] = [row for row in rows if _dedup_key_text(row.get(blank_field))]
    blanks = [row for row in rows if not _dedup_key_text(row.get(blank_field))]
    for blank in blanks:
        absorbed = False
        for candidate in retained:
            if not _same_except_field(blank, candidate, key_fields, blank_field):
                continue
            if tuple(key_fields) == tuple(_REDUCTION_TARGET_KEY_FIELDS):
                scale_guard_kind = _reduction_scale_guard_kind(candidate, blank)
                if scale_guard_kind is not None:
                    _remember_reduction_scale_warning(candidate, blank, scale_guard_kind)
                    continue
            conflicts = _row_conflicts(candidate, blank)
            if conflicts:
                _remember_dedup_conflict(key_fields, candidate, blank, conflicts)
                continue
            _merge_missing_values(candidate, blank)
            absorbed = True
            break
        if not absorbed:
            retained.append(blank)
    return retained


def _absorb_blank_key_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
    absorbed = rows
    for blank_field in _BLANK_ABSORB_FIELDS_BY_KEY.get(tuple(key_fields), ()):
        absorbed = _absorb_blank_field_rows(absorbed, key_fields, blank_field)
    return absorbed


def _deduplicate_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    scale_guarded_rows: list[dict] = []
    for source_row in rows:
        row = _normalize_row_provenance(dict(source_row))
        row[_DEDUP_TABLE_MARKER_FIELD] = _source_has_table_marker(source_row)
        _apply_row_data_status(row)
        key = _dedup_key(row, key_fields)
        if key not in seen:
            seen[key] = row
            continue
        kept = seen[key]
        if tuple(key_fields) == tuple(_REDUCTION_TARGET_KEY_FIELDS):
            scale_guard_kind = _reduction_scale_guard_kind(kept, row)
            if scale_guard_kind is not None:
                _remember_reduction_scale_warning(kept, row, scale_guard_kind)
                if scale_guard_kind == "스케일 서명":
                    scale_guarded_rows.append(row)
                continue
        conflicts = _row_conflicts(kept, row)
        if conflicts:
            winner, loser, reason = _choose_conflict_row(kept, row)
            seen[key] = winner
            _remember_dedup_conflict(key_fields, winner, loser, conflicts, reason)
            _merge_missing_values(winner, loser)
            continue
        _merge_missing_values(kept, row)
    deduped = _absorb_blank_key_rows([*seen.values(), *scale_guarded_rows], key_fields)
    return _strip_dedup_internal_fields(deduped)


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
    return _merge_document_meta_rows(rows, municipality)


def _clean_plan_overview(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return rows


def _clean_regional_conditions(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["값"] = _to_float(row.get("값"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
    rows = _filter_empty_rows(rows, ["값", "지표명"])
    return _deduplicate_rows(rows, ["지자체명", "지표범주", "지표명", "연도"])


def _clean_emissions_regional(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        _apply_inventory_sector_normalization(row, "부문", "부문원문")
        row["배출유형"] = _normalize(row.get("배출유형", ""), _TYPE_MAP)
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, _REGIONAL_EMISSIONS_KEY_FIELDS)


def _clean_emissions_management(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        _apply_inventory_sector_normalization(row, "관리부문", "관리부문원문")
        raw_sector = str(row.get("관리부문", "") or "").strip()
        normalized_sector = _normalise_sector_with_raw(row, "관리부문", "관리부문원문")
        if normalized_sector == "전환" and raw_sector in _MANAGEMENT_ENERGY_SOURCE_TERMS:
            row["관리부문"] = raw_sector
            row.setdefault("관리부문원문", raw_sector)
            if not _has_cell_value(row.get("직간접구분")):
                row["직간접구분"] = "간접"
            _remember_validation_issue(
                municipality,
                "정보",
                "배출현황_관리권한",
                f"관리부문에 에너지원 표기 '{raw_sector}'",
                f"관리권한 인벤토리의 '{raw_sector}' 표기는 전환 부문으로 자동 변환하지 않음",
                "세부부문·직간접구분 원문 확인",
            )
        else:
            row["관리부문"] = normalized_sector
        if _has_cell_value(row.get("직간접구분")):
            row["직간접구분"] = _normalize_direct_indirect_type(row.get("직간접구분"))
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
    rows = [r for r in rows if r.get("관리부문")]
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, _MANAGEMENT_EMISSIONS_KEY_FIELDS)


def _clean_emissions_forecast(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        scenario_raw = str(row.get("시나리오", "") or "").strip()
        normalized_scenario = scenario_raw
        for keyword, scenario in sorted(
            _FORECAST_SCENARIO_MAP.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if keyword.casefold() in scenario_raw.casefold():
                normalized_scenario = scenario
                break
        row["시나리오"] = normalized_scenario
        if (
            normalized_scenario != scenario_raw
            and not _has_cell_value(row.get("전망방법원문"))
        ):
            row["전망방법원문"] = scenario_raw
        row["연도"] = _to_int(row.get("연도"))
        row["전망값"] = _to_float(row.get("전망값"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
        method_raw = row.get("전망방법원문", "")
        if not row.get("전망방법코드") and method_raw:
            for keyword, code in sorted(_FORECAST_METHOD_MAP.items(), key=lambda item: len(item[0]), reverse=True):
                if keyword in method_raw:
                    row["전망방법코드"] = code
                    break
    rows = _filter_empty_rows(rows, ["전망값"])
    return _deduplicate_rows(rows, ["지자체명", "시나리오", "부문", "연도"])


def _clean_reduction_targets(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        if "사업명힌트" in row:
            project_hint = row.pop("사업명힌트")
            if _has_cell_value(project_hint):
                row["_사업명힌트"] = str(project_hint).strip()
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalise_sector_with_raw(row, "부문", "부문원문")
        row["기준연도"] = _to_int(row.get("기준연도"))
        row["목표연도"] = _to_int(row.get("목표연도"))
        row["기준배출량"] = _to_float(row.get("기준배출량"))
        row["배출전망"] = _to_float(row.get("배출전망"))
        row["목표감축량"] = _to_float(row.get("목표감축량"))
        row["목표배출량"] = _to_float(row.get("목표배출량"))
        row["감축률"] = _to_float(row.get("감축률"))
    return _deduplicate_rows(rows, _REDUCTION_TARGET_KEY_FIELDS)


def _target_value_fields_within_tolerance(left: dict, right: dict) -> list[str]:
    matching: list[str] = []
    for field in ("목표감축량", "목표배출량"):
        left_value = _to_float(left.get(field))
        right_value = _to_float(right.get(field))
        if left_value is None or right_value is None:
            continue
        scale = max(abs(left_value), abs(right_value), 1.0)
        if abs(left_value - right_value) / scale <= 0.005:
            matching.append(field)
    return matching


def _is_overall_reduction_target(row: dict) -> bool:
    return (
        str(row.get("목표수준", "") or "").strip() == "총괄"
        and str(row.get("부문", "") or "").strip() in {"", "합계", "전체"}
    )


def _absorb_suspected_target_scope_mixture(
    rows: list[dict],
    municipality: str,
) -> list[dict]:
    """값이 같은 지역전체 총괄 오분류를 관리권한 총괄 행에 흡수한다."""
    management_rows = [
        row
        for row in rows
        if _is_overall_reduction_target(row)
        and str(row.get("목표범위", "") or "").strip() == "관리권한"
        and (
            _has_cell_value(row.get("감축률"))
            or _has_cell_value(row.get("목표배출량"))
        )
    ]
    absorbed_ids: set[int] = set()
    events: list[tuple[dict, dict, list[str]]] = []
    for regional in rows:
        if not _is_overall_reduction_target(regional):
            continue
        if str(regional.get("목표범위", "") or "").strip() != "지역전체":
            continue
        for management in management_rows:
            if regional.get("목표연도") != management.get("목표연도"):
                continue
            matching_fields = _target_value_fields_within_tolerance(
                regional,
                management,
            )
            if not matching_fields:
                continue
            management["출처페이지"] = _merge_provenance(
                management.get("출처페이지"),
                regional.get("출처페이지"),
            )
            absorbed_ids.add(id(regional))
            events.append((regional, management, matching_fields))
            break

    retained = [row for row in rows if id(row) not in absorbed_ids]
    for regional, management, matching_fields in events:
        target_row_number = retained.index(management) + 1
        regional_pages = (
            _normalize_provenance_pages(regional.get("출처페이지")) or "미상"
        )
        _remember_validation_issue(
            municipality,
            "경고",
            _sheet_area("reduction_targets"),
            f"목표범위혼입의심(06 행 {target_row_number})",
            f"목표연도 {management.get('목표연도')}; "
            f"지역전체 출처페이지 {regional_pages}; 관리권한 행과 "
            f"{', '.join(matching_fields)} 값이 0.5% 이내여서 흡수",
            "지역전체 목표가 별도로 존재하는지 원문 구조표를 확인",
            target_sheet_key="reduction_targets",
            target_row_number=target_row_number,
        )
    return retained


def _clean_vision_strategy(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return _filter_empty_rows(rows, ["비전문구", "전략명"])


def _clean_mitigation_projects(rows: list[dict], municipality: str) -> list[dict]:
    for index, row in enumerate(rows, start=1):
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalise_sector_with_raw(row, "부문", "부문원문")
        _apply_appendix4_project_match(row, municipality, index)
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
    for index, row in enumerate(rows, start=1):
        row["지자체명"] = row.get("지자체명") or municipality
        row["연도"] = _to_int(row.get("연도"))
        row["활동량"] = _to_float(row.get("활동량"))
        row["감축원단위값"] = _to_float(row.get("감축원단위값"))
        row["예상감축량"] = _to_float(row.get("예상감축량"))
        _apply_appendix3_unit_match(row, municipality, index)
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


def _build_visual_inventory(observations: list[dict], municipality: str) -> list[dict]:
    """
    이미지 에이전트의 chart_observations(감사 가능한 판독 관찰값)를
    16_시각자료목록 시트 헤더에 맞춰 변환한다.

    config.EXCEL_HEADERS["16_시각자료목록"]에 맞춰 근거·병합 상태까지 보존한다.
    """
    sheet_key_to_name = getattr(config, "SHEET_KEY_TO_NAME", {})
    inventory: list[dict] = []
    for idx, obs in enumerate(observations, start=1):
        if not isinstance(obs, dict):
            continue
        page = obs.get("페이지")
        value = obs.get("값")
        item = str(obs.get("항목", "") or "").strip()
        year = obs.get("연도")
        unit = str(obs.get("단위", "") or "").strip()
        evidence = str(obs.get("근거", "") or "").strip()
        fields = _visual_fields(obs)
        if fields:
            canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
            suffix = evidence.split(" | ", 1)[1] if evidence.startswith("{") and " | " in evidence else ""
            evidence = f"{canonical} | {suffix}" if suffix else canonical
        summary_bits = [bit for bit in [item, str(year) if year is not None else "",
                                        str(value) if value is not None else "", unit] if bit]
        value_summary = " ".join(summary_bits).strip()
        if evidence:
            value_summary = f"{value_summary} | {evidence}" if value_summary else evidence
        target_sheet_basis = str(obs.get("대상시트근거", "") or "").strip()
        if target_sheet_basis:
            marker = f"대상시트 {target_sheet_basis}"
            value_summary = f"{value_summary} | {marker}" if value_summary else marker

        target_sheet = obs.get("대상시트", "") or ""
        related_sheet = sheet_key_to_name.get(target_sheet, target_sheet)

        reflected = obs.get("반영여부")
        confidence = str(obs.get("신뢰도", "") or "").lower()
        # 본 시트에 반영되지 않았거나(검토) 신뢰도가 낮으면 사람이 직접 디지타이징해야 한다.
        needs_digitizing = reflected != "반영" or confidence in {"low", "medium"}

        inventory.append({
            "지자체명": obs.get("지자체명") or municipality,
            "시각자료ID": f"V{page}-{idx:03d}" if page is not None else f"V{idx:03d}",
            "캡션": obs.get("제목", "") or "",
            "유형": obs.get("그래프유형", "") or "",
            "데이터포함여부": value is not None,
            "추출값요약": value_summary,
            "원문값": obs.get("원문값", value),
            "원문단위": obs.get("원문단위", obs.get("단위", "")) or "",
            "정규화값": obs.get("정규화값"),
            "정규화단위": obs.get("정규화단위", "") or "",
            "정규화배율": obs.get("정규화배율"),
            "값근거": obs.get("값근거", "") or "",
            "값검증상태": obs.get("값검증상태", "") or "",
            "계약버전": obs.get("계약버전", "") or "",
            "디지타이징필요": needs_digitizing,
            "관련시트": related_sheet,
            "근거ID": obs.get("근거ID", "") or "",
            "근거매칭상태": obs.get("근거매칭상태", "") or "",
            "병합상태": obs.get("병합상태", "") or "",
            "병합차단사유": obs.get("병합차단사유", "") or "",
        })
    return inventory


def _visual_fields(observation: dict):
    raw_fields = observation.get("판독필드") or observation.get("fields")
    if isinstance(raw_fields, dict):
        return dict(raw_fields)
    evidence = str(observation.get("근거", "") or "").strip()
    if not evidence.startswith("{"):
        return {}
    candidate = evidence.split(" | ", 1)[0]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _visual_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().casefold()
    if not text:
        return None
    if text in {"false", "0", "no", "n", "off", "비추정", "원문", "라벨"}:
        return False
    if text in {"true", "1", "yes", "y", "on", "추정", "estimated"}:
        return True
    if "비추정" in text:
        return False
    if "추정" in text:
        return True
    return None


def _visual_estimated_state(observation: dict, fields: dict) -> bool | None:
    for key in ("estimated", "추정여부"):
        state = _visual_bool(fields.get(key))
        if state is not None:
            return state
    for key in ("estimated", "추정여부"):
        state = _visual_bool(observation.get(key))
        if state is not None:
            return state
    return None


def _confidence_at_least(value: Any, minimum: Any) -> bool:
    confidence = str(value or "").strip().lower()
    threshold = str(minimum or "high").strip().lower()
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK.get(threshold, 3)


def _is_reference_visual_observation(observation: dict) -> bool:
    text = " ".join(
        str(observation.get(key, "") or "")
        for key in ("제목", "근거", "단위", "그래프유형")
    ).casefold()
    return any(keyword.casefold() in text for keyword in config.IMAGE_CHART_REFERENCE_KEYWORDS)


def _visual_observation_value(observation: dict, fields: dict, value_field: str) -> Any:
    for key in (value_field, "값", "value"):
        value = fields.get(key)
        if _has_cell_value(value):
            return value
    return observation.get("값")


def _visual_merge_value_field(sheet_key: str, fields: dict) -> str | None:
    if sheet_key == "reduction_targets":
        role = str(fields.get("값역할") or "").strip()
        if role in _REDUCTION_VISUAL_VALUE_FIELDS:
            return role
    return _VISUAL_MERGE_VALUE_FIELDS.get(sheet_key)


def _visual_explicit_value(observation: dict, fields: dict, *keys: str) -> Any:
    for source in (fields, observation):
        for key in keys:
            value = source.get(key)
            if _has_cell_value(value):
                return value
    return None


def _infer_indicator_category(text: str) -> str | None:
    """지역여건 관찰 텍스트가 한 범주에만 해당할 때 표준 지표범주를 반환한다."""
    normalized = str(text or "").casefold()
    category_keywords = {
        "에너지": ["에너지", "전력", "도시가스", "석유", "신재생", "연료"],
        "자연환경": ["기온", "강수", "기후", "폭염", "한파", "공원", "녹지", "산림", "하천"],
        "인문사회": ["인구", "가구", "세대", "주택", "건축물"],
        "경제산업": ["사업체", "산업", "GRDP", "종사자", "자동차", "차량", "수송", "교통"],
    }
    matched = {
        category
        for category, keywords in category_keywords.items()
        if any(keyword.casefold() in normalized for keyword in keywords)
    }
    if len(matched) != 1:
        return None
    return matched.pop()


def _visual_candidate_row(sheet_key: str, observation: dict, fields: dict, municipality: str) -> dict | None:
    page = observation.get("페이지")
    title = str(observation.get("제목", "") or "").strip()
    explicit_item = str(_visual_explicit_value(observation, fields, "항목") or "").strip()
    item = explicit_item or title
    unit = _visual_explicit_value(observation, fields, "단위") or ""
    year = _visual_explicit_value(observation, fields, "연도")
    value_field = _visual_merge_value_field(sheet_key, fields)
    if value_field is None:
        return None
    value = _visual_observation_value(observation, fields, value_field)
    base = {
        "지자체명": observation.get("지자체명") or municipality,
        "출처페이지": page,
        "데이터상태": "visual_only",
        "근거ID": observation.get("근거ID") or "",
        "_시각값필드": value_field,
    }
    if sheet_key == "regional_conditions":
        indicator_category = _visual_explicit_value(observation, fields, "지표범주")
        if not _has_cell_value(indicator_category):
            indicator_category = _infer_indicator_category(" ".join(
                str(_visual_explicit_value(observation, fields, key) or "")
                for key in ("캡션", "항목", "제목", "지표명")
            ))
        series_label = str(_visual_explicit_value(observation, fields, "항목") or "").strip()
        chart_indicator_name = str(fields.get("지표명") or "").strip()
        uses_series_label = bool(
            series_label
            and _dedup_key_text(series_label) != _dedup_key_text(chart_indicator_name)
        )
        indicator_name = (
            series_label
            if uses_series_label
            else _visual_explicit_value(observation, fields, "지표명") or item
        )
        indicator_subcategory = _visual_explicit_value(observation, fields, "지표세부범주") or ""
        if not _has_cell_value(indicator_subcategory) and uses_series_label:
            indicator_subcategory = chart_indicator_name
        return base | {
            "지표범주": indicator_category,
            "지표세부범주": indicator_subcategory,
            "지표명": indicator_name,
            "연도": year,
            "값": value,
            "단위": unit,
            "출처": f"이미지 p.{page}" if page else "이미지",
        }
    if sheet_key == "emissions_regional":
        emission_type = _visual_explicit_value(observation, fields, "배출유형", "배출범위")
        return base | {
            "인벤토리출처": "이미지",
            "배출범위": _visual_explicit_value(observation, fields, "배출범위") or emission_type,
            "배출유형": emission_type,
            "부문": _visual_explicit_value(observation, fields, "부문") or explicit_item,
            "세부부문": _visual_explicit_value(observation, fields, "세부부문") or "",
            "연도": year,
            "배출량": value,
            "단위": unit,
            "흡수원여부": _visual_explicit_value(observation, fields, "흡수원여부") or emission_type == "흡수원",
        }
    if sheet_key == "emissions_management":
        return base | {
            "인벤토리출처": "이미지",
            "관리부문": _visual_explicit_value(observation, fields, "관리부문", "부문") or explicit_item,
            "세부부문": _visual_explicit_value(observation, fields, "세부부문") or "",
            "직간접구분": _visual_explicit_value(observation, fields, "직간접구분") or "",
            "연도": year,
            "배출량": value,
            "단위": unit,
            "합계포함여부": _visual_explicit_value(observation, fields, "합계포함여부") or "",
        }
    if sheet_key == "emissions_forecast":
        scenario = _visual_explicit_value(observation, fields, "시나리오")
        if not _has_cell_value(scenario):
            scenario_text = f"{title} {fields.get('캡션', '')}".casefold()
            if "bau" in scenario_text or "기준전망" in scenario_text:
                scenario = "BAU"
            elif "추가조치" in scenario_text:
                scenario = "추가조치"
            elif "정책반영" in scenario_text:
                scenario = "정책반영"
        return base | {
            "시나리오": scenario,
            "전망방법코드": _visual_explicit_value(observation, fields, "전망방법코드") or "",
            "전망방법원문": _visual_explicit_value(observation, fields, "전망방법원문") or "",
            "부문": _visual_explicit_value(observation, fields, "부문") or explicit_item,
            "세부부문": _visual_explicit_value(observation, fields, "세부부문") or "",
            "연도": year,
            "전망값": value,
            "단위": unit,
            "주요가정": f"이미지 p.{page}" if page else "이미지",
        }
    if sheet_key == "reduction_targets":
        target_level = _visual_explicit_value(observation, fields, "목표수준")
        if not _has_cell_value(target_level) and item:
            target_level = "총괄" if item in {"합계", "총괄", "전체"} else "부문"
        value_role = fields.get("값역할")
        value_columns = ("목표배출량", "목표감축량", "기준배출량", "배출전망")
        if _has_cell_value(value_role):
            role_values = {column: fields.get(column) for column in value_columns}
            if value_role in value_columns:
                role_values[value_role] = value
            reduction_rate = value if value_role == "감축률" else fields.get("감축률")
        else:
            role_values = {
                "기준배출량": _visual_explicit_value(observation, fields, "기준배출량"),
                "배출전망": _visual_explicit_value(observation, fields, "배출전망"),
                "목표감축량": _visual_explicit_value(observation, fields, "목표감축량"),
                "목표배출량": value,
            }
            reduction_rate = _visual_explicit_value(observation, fields, "감축률")
        return base | {
            "목표수준": target_level,
            "목표범위": _visual_explicit_value(observation, fields, "목표범위"),
            "부문": _visual_explicit_value(observation, fields, "부문") or item,
            "기준연도": _visual_explicit_value(observation, fields, "기준연도"),
            "기준배출량": role_values["기준배출량"],
            "목표연도": _visual_explicit_value(observation, fields, "목표연도") or year,
            "배출전망": role_values["배출전망"],
            "목표감축량": role_values["목표감축량"],
            "목표배출량": role_values["목표배출량"],
            "감축률": reduction_rate,
        }
    if sheet_key == "mitigation_projects":
        project_name = _visual_explicit_value(
            observation,
            fields,
            "사업명",
            "감축사업명",
        ) or explicit_item or title
        return base | {
            "관리번호": _visual_explicit_value(observation, fields, "관리번호", "과제ID") or "",
            "부문": _visual_explicit_value(observation, fields, "부문") or "",
            "핵심과제": _visual_explicit_value(observation, fields, "핵심과제") or "",
            "사업명": project_name,
            "사업유형": _visual_explicit_value(observation, fields, "사업유형") or "",
            "주관부서": _visual_explicit_value(observation, fields, "주관부서") or "",
            "협조부서": _visual_explicit_value(observation, fields, "협조부서") or "",
            "사업개요": _visual_explicit_value(observation, fields, "사업개요") or "",
            "성과지표명": _visual_explicit_value(observation, fields, "성과지표명", "성과지표") or "",
            "성과지표단위": _visual_explicit_value(observation, fields, "성과지표단위") or "",
            "정량여부": _visual_explicit_value(observation, fields, "정량여부"),
        }
    if sheet_key == "financial_plan":
        return base | {
            "계획구분": _visual_explicit_value(observation, fields, "계획구분"),
            "부문": _visual_explicit_value(observation, fields, "부문") or item,
            "사업명": _visual_explicit_value(observation, fields, "사업명") or title,
            "재원구분": _visual_explicit_value(observation, fields, "재원구분"),
            "연도": year,
            "예산액": value,
            "예산단위": unit,
        }
    return None


def _visual_allowed_fields(sheet_key: str) -> set[str]:
    sheet_name = config.SHEET_KEY_TO_NAME.get(sheet_key, "")
    headers = set(config.EXCEL_HEADERS.get(sheet_name, []))
    if "감축률(%)" in headers:
        headers.add("감축률")
    return headers


def _clean_visual_candidate(sheet_key: str, row: dict, municipality: str) -> dict | None:
    cleaner = _CLEANERS.get(sheet_key)
    if cleaner is None:
        return None
    cleaned_rows = cleaner([dict(row)], municipality)
    if not cleaned_rows:
        return None
    cleaned = _normalize_row_provenance(dict(cleaned_rows[0]))
    _apply_row_data_status(cleaned)
    allowed = _visual_allowed_fields(sheet_key)
    result = {key: value for key, value in cleaned.items() if key in allowed}
    value_field = str(row.get("_시각값필드") or "").strip() or _visual_merge_value_field(sheet_key, row)
    unit_field = "예산단위" if sheet_key == "financial_plan" else "단위"
    if value_field and sheet_key not in _VISUAL_QUALITATIVE_SHEETS:
        quantity = normalize_quantity(row.get(value_field), row.get(unit_field))
        result["_시각원문값"] = quantity["raw_value"]
        result["_시각원문단위"] = quantity["raw_unit"]
        result["_시각정규화값"] = quantity["value"]
        result["_시각정규화단위"] = quantity["unit"]
    return result


def _public_visual_candidate(row: dict) -> dict:
    """Remove comparison-only metadata before a candidate enters a workbook sheet."""
    return {
        key: value
        for key, value in row.items()
        if not str(key).startswith("_시각")
    }


def _visual_rows_conflict(
    left: dict,
    right: dict,
    sheet_key: str,
    value_field: str,
    *,
    strict_evidence: bool = False,
) -> bool:
    if sheet_key in _VISUAL_QUALITATIVE_SHEETS:
        return False
    unit_field = "예산단위" if sheet_key == "financial_plan" else "단위"
    left_unit = left.get("_시각원문단위") or left.get(unit_field)
    right_unit = right.get("_시각원문단위") or right.get(unit_field)
    # Legacy callers did not preserve table-level units on every text row.
    # Keep their raw-value comparison behavior, while the operational exact-
    # evidence path treats a missing unit conservatively through normalization.
    if not strict_evidence and (not _has_cell_value(left_unit) or not _has_cell_value(right_unit)):
        return _values_conflict(left.get(value_field), right.get(value_field))
    return quantities_conflict(
        left.get(value_field),
        left_unit,
        right.get(value_field),
        right_unit,
    )


def _visual_keys_match(left: dict, right: dict, key_fields: list[str]) -> bool:
    for field in key_fields:
        left_value = _visual_key_text(left.get(field))
        right_value = _visual_key_text(right.get(field))
        if field in _VISUAL_OPTIONAL_KEY_FIELDS and (not left_value or not right_value):
            continue
        if left_value != right_value:
            return False
    return True


def _visual_text_rows(rows: list[dict], candidate: dict, key_fields: list[str]) -> list[dict]:
    return [
        row for row in rows
        if _visual_keys_match(row, candidate, key_fields)
        and str(row.get("데이터상태", "") or "").strip() != "visual_only"
    ]


def _visual_only_rows(rows: list[dict], candidate: dict, key_fields: list[str]) -> list[dict]:
    return [
        row for row in rows
        if _visual_keys_match(row, candidate, key_fields)
        and str(row.get("데이터상태", "") or "").strip() == "visual_only"
    ]


def _restore_visual_fill_placeholders(
    sheet_key: str,
    raw_rows: list[dict],
    cleaned_rows: list[dict],
    cleaner,
    municipality: str,
) -> None:
    """Keep one keyed blank row only until the strict visual fill gate runs."""
    if sheet_key in _VISUAL_QUALITATIVE_SHEETS or sheet_key == "reduction_targets":
        return
    key_fields = _VISUAL_MERGE_KEY_FIELDS.get(sheet_key)
    value_field = _VISUAL_MERGE_VALUE_FIELDS.get(sheet_key)
    if not key_fields or not value_field:
        return
    required = [field for field in key_fields if field not in _VISUAL_OPTIONAL_KEY_FIELDS]
    for raw in raw_rows:
        if _has_cell_value(raw.get(value_field)):
            continue
        if any(not _has_cell_value(raw.get(field)) for field in required):
            continue
        probe = dict(raw)
        probe[value_field] = 0
        normalized = cleaner([probe], municipality)
        if not normalized:
            continue
        placeholder = _normalize_row_provenance(dict(normalized[0]))
        placeholder[value_field] = None
        placeholder["_시각보완대기"] = True
        if any(_visual_keys_match(row, placeholder, key_fields) for row in cleaned_rows):
            continue
        cleaned_rows.append(placeholder)


def _drop_unfilled_visual_placeholders(cleaned: dict) -> None:
    for sheet_key, value_field in _VISUAL_MERGE_VALUE_FIELDS.items():
        rows = cleaned.get(sheet_key)
        if not isinstance(rows, list):
            continue
        cleaned[sheet_key] = [
            row for row in rows
            if not (
                row.get("_시각보완대기")
                and not _has_cell_value(row.get(value_field))
            )
        ]
        for row in cleaned[sheet_key]:
            row.pop("_시각보완대기", None)


def _remember_visual_merge_issue(
    municipality: str,
    severity: str,
    item: str,
    detail: str,
    action: str,
    sheet_key: str | None,
) -> None:
    _remember_validation_issue(
        municipality,
        severity,
        "시각병합",
        item,
        detail,
        action,
        target_sheet_key=sheet_key,
    )


def _visual_key_summary(row: dict, key_fields: list[str]) -> str:
    return ", ".join(f"{field}={row.get(field) or ''}" for field in key_fields)


def _document_status_year_limit(cleaned: dict) -> int | None:
    years: list[int] = []
    rows = cleaned.get("document_meta", [])
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("작성연도", "승인연도", "계획시작연도"):
            value = row.get(field)
            parsed = _to_int(value)
            if parsed is None:
                match = re.search(r"(?:19|20)\d{2}", str(value or ""))
                parsed = int(match.group()) if match is not None else None
            if parsed is not None:
                years.append(parsed)
    return min(years) + 1 if years else None


def _set_visual_merge_state(observation: dict, status: str, reason: str = "") -> None:
    valid_statuses = {"accept", "fix_then_merge", "duplicate", "reject", "needs_review", "candidate"}
    legacy_aliases = {
        "merged": "accept",
        "verified_duplicate": "duplicate",
        "duplicate_visual": "duplicate",
    }
    status = legacy_aliases.get(status, status)
    if status not in valid_statuses:
        status = "needs_review"
    observation["병합상태"] = status
    observation["반영여부"] = "반영" if status in {"accept", "fix_then_merge"} else "검토"
    if reason:
        existing = str(observation.get("병합차단사유", "") or "").strip()
        parts = [part for part in (existing, reason) if part]
        observation["병합차단사유"] = "; ".join(dict.fromkeys(parts))
    elif status in {"accept", "fix_then_merge", "duplicate"}:
        observation["병합차단사유"] = ""


def _visual_observation_all_null(observation: dict, fields: dict, sheet_key: str = "") -> bool:
    if sheet_key in _VISUAL_QUALITATIVE_SHEETS:
        return not _has_cell_value(
            _visual_explicit_value(observation, fields, "사업명", "감축사업명")
            or observation.get("항목")
            or observation.get("제목")
        )
    value_keys = {
        "값", "배출량", "전망값", "예산액", "목표배출량", "목표감축량",
        "기준배출량", "배출전망", "대수", "주행거리", "활동량", "예상감축량",
    }
    return not any(
        _has_cell_value(source.get(key))
        for source in (observation, fields)
        for key in value_keys
    )


def _visual_value_source(observation: dict, fields: dict) -> str:
    source = normalize_value_source(
        observation.get("값근거") or fields.get("값근거") or fields.get("value_source")
    )
    if _visual_estimated_state(observation, fields) is True:
        source = "axis_estimate"
    chart_type = normalize_comparison_text(observation.get("그래프유형"))
    if source == "unknown" and chart_type in {"표", "이미지표", "table"}:
        source = "table_cell"
    observation["값근거"] = source
    fields["값근거"] = source
    return source


def _visual_is_total(observation: dict, fields: dict) -> bool:
    aggregate_level = normalize_comparison_text(fields.get("집계수준"))
    if aggregate_level in {"합계", "총계", "전체", "총괄", "total"}:
        return True
    label = normalize_comparison_text(
        observation.get("항목") or fields.get("항목") or fields.get("부문")
    )
    return bool(label) and (
        label in {"합계", "총계", "전체", "총괄", "total"}
        or label.startswith("전체")
        or label.endswith("합계")
        or label.endswith("총계")
    )


def _visual_display_resolution(value: Any) -> float:
    """Return the smallest displayed unit represented by a parsed value."""
    if isinstance(value, bool) or value is None:
        return 1.0
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"[-+]?\d+(?:\.(\d+))?", text)
    if match is None:
        return 1.0
    decimals = len(match.group(1) or "")
    return 10.0 ** (-decimals)


def _visual_value_validation_issues(observations: list[dict]) -> dict[int, list[str]]:
    """Validate values before evidence-gated auto merge.

    Exact evidence identity proves where a value came from, not that the value was
    read correctly. This pass therefore validates the value source, detects
    contradictory duplicate readings, and checks explicit totals against detail
    rows that share the same evidence group.
    """
    issues: dict[int, list[str]] = {}
    entries: list[dict[str, Any]] = []
    aggregate_pass: set[int] = set()

    def add_issue(observation: dict, reason: str) -> None:
        bucket = issues.setdefault(id(observation), [])
        if reason not in bucket:
            bucket.append(reason)

    for observation in observations:
        if not isinstance(observation, dict):
            continue
        fields = _visual_fields(observation)
        sheet_key = str(observation.get("대상시트", "") or "").strip()
        if sheet_key in _VISUAL_QUALITATIVE_SHEETS:
            observation["값검증상태"] = "qualitative_pass"
            continue
        source = _visual_value_source(observation, fields)
        if source not in {"explicit_label", "table_cell"}:
            add_issue(observation, f"G5 자동 병합 불가 값근거({source})")

        value_field = _visual_merge_value_field(sheet_key, fields) or "값"
        raw_value = _visual_observation_value(observation, fields, value_field)
        quantity = normalize_quantity(
            raw_value,
            observation.get("단위") or fields.get("단위"),
        )
        observation.setdefault("원문값", quantity["raw_value"])
        observation.setdefault("원문단위", quantity["raw_unit"])
        observation["정규화값"] = quantity["value"]
        observation["정규화단위"] = quantity["unit"]
        observation["정규화배율"] = quantity["multiplier"]
        fields.update({
            "원문값": observation["원문값"],
            "원문단위": observation["원문단위"],
            "정규화값": observation["정규화값"],
            "정규화단위": observation["정규화단위"],
            "정규화배율": observation["정규화배율"],
        })
        observation["판독필드"] = fields
        numeric_value = _to_float(raw_value)
        if numeric_value is None or not math.isfinite(numeric_value):
            add_issue(observation, "G5 숫자값 검증 실패")
            observation["값검증상태"] = "invalid_numeric"
            continue

        evidence_ids = normalize_evidence_ids(
            observation.get("근거ID목록"),
            observation.get("source_evidence_ids"),
            observation.get("근거ID"),
        )
        evidence_key = evidence_ids[0] if len(evidence_ids) == 1 else "|".join(evidence_ids)
        group_label = fields.get("합계그룹") or observation.get("제목") or ""
        item_label = (
            observation.get("항목")
            or fields.get("항목")
            or fields.get("부문")
            or fields.get("관리부문")
            or fields.get("지표명")
            or ""
        )
        year_or_period = observation.get("연도")
        if not _has_cell_value(year_or_period):
            year_or_period = fields.get("기간원문")
        unit = observation.get("단위") or fields.get("단위") or ""
        value_role = fields.get("값역할") or ""
        entries.append({
            "observation": observation,
            "fields": fields,
            "raw_value": raw_value,
            "value": numeric_value,
            "group_key": (
                evidence_key,
                observation.get("페이지"),
                sheet_key,
                normalize_comparison_text(group_label),
                normalize_comparison_text(year_or_period),
                normalize_comparison_text(unit, "단위"),
                normalize_comparison_text(value_role),
            ),
            "duplicate_key": (
                evidence_key,
                observation.get("페이지"),
                sheet_key,
                normalize_comparison_text(group_label),
                normalize_comparison_text(item_label),
                normalize_comparison_text(year_or_period),
                normalize_comparison_text(unit, "단위"),
                normalize_comparison_text(value_role),
            ),
            "is_total": _visual_is_total(observation, fields),
        })

    duplicate_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    aggregate_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for entry in entries:
        duplicate_groups.setdefault(entry["duplicate_key"], []).append(entry)
        aggregate_groups.setdefault(entry["group_key"], []).append(entry)

    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        values = [entry["value"] for entry in group]
        tolerance = max(_visual_display_resolution(entry["raw_value"]) for entry in group) * 0.5
        if max(values) - min(values) <= tolerance:
            continue
        detail = ", ".join(str(entry["raw_value"]) for entry in group)
        for entry in group:
            add_issue(entry["observation"], f"G5 동일 근거·항목 값 충돌({detail})")

    for group in aggregate_groups.values():
        totals = [entry for entry in group if entry["is_total"]]
        details = [entry for entry in group if not entry["is_total"]]
        if len(totals) > 1:
            total_values = ", ".join(str(entry["raw_value"]) for entry in totals)
            for entry in group:
                add_issue(entry["observation"], f"G5 합계 행 복수({total_values})")
            continue
        if len(totals) != 1 or len(details) < 2:
            continue
        total_entry = totals[0]
        detail_sum = sum(entry["value"] for entry in details)
        tolerance = sum(
            _visual_display_resolution(entry["raw_value"]) * 0.5
            for entry in group
        )
        if abs(total_entry["value"] - detail_sum) > max(tolerance, 1e-9):
            reason = (
                "G5 합계 불일치("
                f"합계={total_entry['raw_value']}, 세부합={detail_sum:g}, 허용오차={tolerance:g})"
            )
            for entry in group:
                add_issue(entry["observation"], reason)
            continue
        for entry in group:
            aggregate_pass.add(id(entry["observation"]))

    for entry in entries:
        observation = entry["observation"]
        if id(observation) in issues:
            observation["값검증상태"] = "needs_review"
        elif id(observation) in aggregate_pass:
            observation["값검증상태"] = "aggregate_pass"
        else:
            observation["값검증상태"] = "basic_pass"
    return issues


def _apply_visual_labeled_merge(
    cleaned: dict,
    observations: list[dict],
    municipality: str,
    *,
    evidence_contract_active: bool = False,
) -> None:
    if not getattr(config, "VISUAL_MERGE_LABELED_ENABLED", False):
        return
    strict_evidence = bool(
        evidence_contract_active
        and getattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True)
    )
    evidence_catalog = build_evidence_catalog(
        cleaned.get("document_objects", []),
        cleaned.get("object_triage", []),
    ) if strict_evidence else {}
    # Legacy callers can still supply visual rows without a document-object
    # inventory. Preserve that compatibility path; the strengthened value gate
    # is coupled to the exact-evidence contract used by the current pipeline.
    value_validation_issues = (
        _visual_value_validation_issues(observations) if strict_evidence else {}
    )
    min_confidence = getattr(config, "IMAGE_CHART_MERGE_MIN_CONFIDENCE", "medium")
    document_status_year_limit = _document_status_year_limit(cleaned)
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        if str(observation.get("반영여부", "") or "").strip() != "검토":
            continue
        sheet_key = str(observation.get("대상시트", "") or "").strip()
        decision_note = ""
        if sheet_key == "emissions_regional":
            title = str(observation.get("제목", "") or "")
            if "전망" in title or "bau" in title.casefold():
                sheet_key = "emissions_forecast"
                decision_note = "; 대상시트 재지정(전망 키워드)"
        fields = _visual_fields(observation)
        blockers: list[str] = []
        existing_blockers = str(observation.get("병합차단사유", "") or "").strip()
        if existing_blockers:
            blockers.extend(
                reason.strip()
                for reason in existing_blockers.split(";")
                if reason.strip()
            )
        blockers.extend(value_validation_issues.get(id(observation), []))
        if strict_evidence:
            evidence_ids = normalize_evidence_ids(
                observation.get("근거ID목록"),
                observation.get("source_evidence_ids"),
                observation.get("근거ID"),
            )
            evidence_match = match_evidence(evidence_ids, evidence_catalog)
            observation["근거ID목록"] = evidence_ids
            observation["근거ID"] = evidence_match.evidence_id or (
                evidence_ids[0] if len(evidence_ids) == 1 else ""
            )
            observation["근거객체ID"] = ",".join(evidence_match.object_ids)
            observation["근거매칭상태"] = evidence_match.status
            if not evidence_match.exact:
                blockers.append(f"G0 근거 ID 정확 매칭 실패({evidence_match.reason})")
        if _visual_observation_all_null(observation, fields, sheet_key):
            blockers.append("G0 판독값 전부 null")
        if not fields:
            blockers.append("G1 판독필드 없음")
        elif _visual_estimated_state(observation, fields) is True:
            blockers.append("G1 축 기반 추정값")
        if not _confidence_at_least(observation.get("신뢰도"), min_confidence):
            blockers.append(f"G2 신뢰도 기준 미달({observation.get('신뢰도') or ''} < {min_confidence})")
        reference_observation = _is_reference_visual_observation(observation)
        if reference_observation:
            blockers.append("G4 참고자료/사례 판정")
        if str(observation.get("종류추론", "") or "").strip() == "목표":
            blockers.append("G4 종류=목표 — 감축 경로표 값")

        candidate = None
        key_fields = _VISUAL_MERGE_KEY_FIELDS.get(sheet_key)
        value_field = _visual_merge_value_field(sheet_key, fields)
        raw_candidate = _visual_candidate_row(sheet_key, observation, fields, municipality)
        exceeds_document_status_year = bool(
            sheet_key == "emissions_regional"
            and raw_candidate is not None
            and document_status_year_limit is not None
            and (candidate_year := _to_int(raw_candidate.get("연도"))) is not None
            and candidate_year > document_status_year_limit
        )
        if raw_candidate is None or key_fields is None or value_field is None:
            blockers.append(f"G3 대상 시트 미지원 또는 키 정의 없음({sheet_key or '미지정'})")
        else:
            required_key_fields = [field for field in key_fields if field not in _VISUAL_OPTIONAL_KEY_FIELDS]
            raw_missing = [field for field in required_key_fields if not _has_cell_value(raw_candidate.get(field))]
            if raw_missing:
                blockers.append(f"G3 1차 키 누락({', '.join(raw_missing)})")
            if not _has_cell_value(raw_candidate.get(value_field)):
                blockers.append(f"G3 비교값 누락({value_field})")
            period_text = str(fields.get("기간원문") or "").strip()
            if period_text and sheet_key in {"emissions_forecast", "reduction_targets"}:
                blockers.append(f"G3 기간값은 단일 연도 아님({period_text})")
            if sheet_key in {"emissions_regional", "emissions_management"}:
                if "%" in str(raw_candidate.get("단위", "") or ""):
                    blockers.append("G3 비교값 단위 부적합(%)")
                sector_field = "부문" if sheet_key == "emissions_regional" else "관리부문"
                gas_names = {_dedup_key_text(name) for name in _IPCC_GAS_NAMES}
                if _gas_sector_key(raw_candidate.get(sector_field)) in gas_names:
                    blockers.append("G3 부문에 가스종(스키마 불일치)")
            if not raw_missing and _has_cell_value(raw_candidate.get(value_field)):
                candidate = _clean_visual_candidate(sheet_key, raw_candidate, municipality)
            if candidate is not None:
                missing = [field for field in required_key_fields if not _has_cell_value(candidate.get(field))]
                if missing:
                    blockers.append(f"G3 1차 키 누락({', '.join(missing)})")
                if not _has_cell_value(candidate.get(value_field)):
                    blockers.append(f"G3 비교값 누락({value_field})")
            elif not raw_missing and _has_cell_value(raw_candidate.get(value_field)):
                blockers.append("G3 정제 후 유효 행 없음")

        if blockers:
            blockers = list(dict.fromkeys(blockers))
            terminal_status = "reject" if reference_observation else "needs_review"
            _set_visual_merge_state(observation, terminal_status, "; ".join(blockers))
            _remember_visual_merge_issue(
                municipality,
                "정보",
                "차단",
                f"{sheet_key or '미지정'} p{observation.get('페이지') or '?'}: {'; '.join(blockers)}{decision_note}",
                "시각 판독 메타데이터와 원문 표기 확인",
                sheet_key or None,
            )
            continue

        if candidate is None or key_fields is None or value_field is None:
            _set_visual_merge_state(observation, "needs_review", "유효 병합 후보 생성 실패")
            continue
        if exceeds_document_status_year:
            _set_visual_merge_state(
                observation,
                "needs_review",
                f"현황 연도 상한 초과({candidate_year} > {document_status_year_limit})",
            )
            _remember_visual_merge_issue(
                municipality,
                "정보",
                "검토 강등",
                f"emissions_regional p{observation.get('페이지') or '?'}: "
                f"G3 현황 연도 상한 초과(작성연도 기준); "
                f"연도 {candidate_year} > 상한 {document_status_year_limit}{decision_note}",
                "16_시각자료목록에서 원문 시계열 성격을 확인",
                "emissions_regional",
            )
            continue
        rows = cleaned.setdefault(sheet_key, [])
        if not isinstance(rows, list):
            _set_visual_merge_state(observation, "needs_review", "대상 시트 행 컨테이너 오류")
            continue
        key_summary = _visual_key_summary(candidate, key_fields)
        visual_rows = _visual_only_rows(rows, candidate, key_fields)
        if visual_rows:
            visual_row = visual_rows[0]
            if _visual_rows_conflict(
                visual_row,
                candidate,
                sheet_key,
                value_field,
                strict_evidence=strict_evidence,
            ):
                _set_visual_merge_state(observation, "needs_review", "동일 키 시각 후보 값 충돌")
                _remember_visual_merge_issue(
                    municipality,
                    "경고",
                    "경고",
                    f"{sheet_key} {key_summary}: 동일 키 시각 행 존재, "
                    f"기존 시각 {visual_row.get(value_field)} vs 신규 시각 {candidate.get(value_field)}{decision_note}",
                    "기존 시각 행 유지 후 원문 차트 수동 확인",
                    sheet_key,
                )
            else:
                _set_visual_merge_state(observation, "duplicate")
                _remember_visual_merge_issue(
                    municipality,
                    "정보",
                    "생략",
                    f"{sheet_key} {key_summary}: 동일 키 시각 행 존재, "
                    f"기존 시각 {visual_row.get(value_field)} vs 신규 시각 {candidate.get(value_field)}{decision_note}",
                    "기존 시각 행 유지",
                    sheet_key,
                )
            continue

        text_rows = _visual_text_rows(rows, candidate, key_fields)
        if text_rows:
            blank_value_rows = [
                row for row in text_rows
                if not _has_cell_value(row.get(value_field))
            ]
            populated_value_rows = [
                row for row in text_rows
                if _has_cell_value(row.get(value_field))
            ]
            if len(blank_value_rows) == 1 and not populated_value_rows:
                target = blank_value_rows[0]
                target[value_field] = candidate.get(value_field)
                unit_field = "예산단위" if sheet_key == "financial_plan" else "단위"
                if not _has_cell_value(target.get(unit_field)) and _has_cell_value(candidate.get(unit_field)):
                    target[unit_field] = candidate.get(unit_field)
                target["출처페이지"] = _merge_provenance(
                    target.get("출처페이지"), candidate.get("출처페이지")
                )
                if not _has_cell_value(target.get("근거ID")):
                    target["근거ID"] = candidate.get("근거ID")
                target.pop("_시각보완대기", None)
                _set_visual_merge_state(observation, "fix_then_merge")
                _remember_visual_merge_issue(
                    municipality,
                    "정보",
                    "보완병합",
                    f"{sheet_key} {key_summary}: 기존 텍스트 행의 빈 {value_field}만 "
                    f"시각값 {candidate.get(value_field)}로 보완{decision_note}",
                    "기존 비어 있던 셀만 보완; 비어 있지 않은 값은 덮어쓰지 않음",
                    sheet_key,
                )
                continue
            if len(blank_value_rows) > 1 and not populated_value_rows:
                _set_visual_merge_state(observation, "needs_review", "동일 키 빈 텍스트 행 복수")
                _remember_visual_merge_issue(
                    municipality,
                    "경고",
                    "차단",
                    f"{sheet_key} {key_summary}: 보완 대상 빈 텍스트 행 {len(blank_value_rows)}개",
                    "원문 행 식별 후 하나의 대상 행을 확정",
                    sheet_key,
                )
                continue
            matching_rows = [
                row for row in text_rows
                if _has_cell_value(row.get(value_field))
                and not _visual_rows_conflict(
                    row,
                    candidate,
                    sheet_key,
                    value_field,
                    strict_evidence=strict_evidence,
                )
            ]
            if matching_rows:
                _set_visual_merge_state(observation, "duplicate")
                _remember_visual_merge_issue(
                    municipality,
                    "정보",
                    "생략",
                    f"{sheet_key} {key_summary}: 교차일치, 텍스트 {matching_rows[0].get(value_field)} vs 시각 {candidate.get(value_field)}{decision_note}",
                    "기존 텍스트 행 유지",
                    sheet_key,
                )
                continue
            conflict_row = text_rows[0]
            _set_visual_merge_state(observation, "needs_review", "텍스트-시각 값 불일치")
            _remember_visual_merge_issue(
                municipality,
                "경고",
                "차단",
                f"{sheet_key} {key_summary}: 텍스트-시각 값 불일치, 텍스트 {conflict_row.get(value_field)} vs 시각 {candidate.get(value_field)}{decision_note}",
                "원문 표·차트와 텍스트 추출값 수동 확인",
                sheet_key,
            )
            continue

        relaxed_conflict_note = ""
        if sheet_key == "regional_conditions":
            relaxed_key_fields = [field for field in key_fields if field != "지표세부범주"]
            relaxed_text_rows = _visual_text_rows(rows, candidate, relaxed_key_fields)
            if relaxed_text_rows:
                relaxed_matching_rows = [
                    row for row in relaxed_text_rows
                    if _has_cell_value(row.get(value_field))
                    and not _visual_rows_conflict(
                        row,
                        candidate,
                        sheet_key,
                        value_field,
                        strict_evidence=strict_evidence,
                    )
                ]
                if relaxed_matching_rows:
                    relaxed_row = relaxed_matching_rows[0]
                    _set_visual_merge_state(observation, "duplicate")
                    _remember_visual_merge_issue(
                        municipality,
                        "정보",
                        "생략",
                        f"{sheet_key} {key_summary}: 세부범주 완화 교차일치, "
                        f"텍스트 {relaxed_row.get(value_field)} vs 시각 {candidate.get(value_field)}{decision_note}",
                        "기존 텍스트 행 유지",
                        sheet_key,
                    )
                    continue
                if strict_evidence:
                    conflict_row = relaxed_text_rows[0]
                    _set_visual_merge_state(
                        observation,
                        "needs_review",
                        "세부범주 완화 매칭에서 텍스트-시각 값 불일치",
                    )
                    _remember_visual_merge_issue(
                        municipality,
                        "경고",
                        "차단",
                        f"{sheet_key} {key_summary}: 세부범주 완화 텍스트-시각 값 불일치, "
                        f"텍스트 {conflict_row.get(value_field)} vs 시각 {candidate.get(value_field)}{decision_note}",
                        "원문 객체와 텍스트 추출값 수동 확인",
                        sheet_key,
                    )
                    continue
                relaxed_conflict_note = "; 세부범주 상이 텍스트 값불일치 관찰"

        rows.append(_public_visual_candidate(candidate))
        _set_visual_merge_state(observation, "accept")
        _remember_visual_merge_issue(
            municipality,
            "정보",
            "병합",
            f"{sheet_key} {key_summary}: 텍스트 행 없음, 시각 전용 값 {candidate.get(value_field)} "
            f"병합{relaxed_conflict_note}{decision_note}",
            "visual_only 행으로 출처페이지 확인",
            sheet_key,
        )


def _issue(
    municipality: str,
    severity: str,
    area: str,
    item: str,
    detail: str,
    action: str,
    *,
    target_sheet_key: str | None = None,
    target_row_number: int | None = None,
) -> dict:
    issue = {
        "지자체명": municipality,
        "심각도": severity,
        "영역": area,
        "항목": item,
        "문제내용": detail,
        "권장조치": action,
    }
    if target_sheet_key:
        issue["대상시트키"] = target_sheet_key
    if target_row_number is not None:
        issue["대상행번호"] = target_row_number
    return issue


def _recompute_reduction_rate(rows: list[dict], municipality: str) -> list[dict]:
    """
    감축률 = (기준배출량 - 목표배출량) / 기준배출량 × 100 을 결정론적으로 재계산한다.

    - 감축률이 비어 있으면 계산값으로 채운다.
    - LLM이 준 감축률이 계산값과 크게(>1%p) 다르면 계산값으로 교정하고 리포트에 남긴다.
    (기준·목표 배출량이 둘 다 있을 때만. 흡수원 등으로 음수가 나오는 건 정상일 수 있어
     값 자체는 막지 않고, 0~100 범위를 벗어나면 점검 항목으로만 표시한다.)
    """
    issues: list[dict] = []
    for index, row in enumerate(rows, start=1):
        base = row.get("기준배출량")
        target = row.get("목표배출량")
        rate = row.get("감축률")
        if isinstance(base, (int, float)) and base != 0 and isinstance(target, (int, float)):
            computed = round((base - target) / base * 100, 1)
            sector = row.get("부문") or row.get("목표수준") or ""
            if rate is None:
                row["감축률"] = computed
                _set_data_status(row, "calculated")
            elif isinstance(rate, (int, float)) and abs(rate - computed) > 1.0:
                issues.append(_issue(
                    municipality, "경고", "감축목표", f"감축률 불일치({sector} {row.get('목표연도')})",
                    f"보고값 {rate} vs 산식 계산값 {computed}",
                    "산식 계산값으로 교정함. 기준/목표 배출량 원문 재확인 권장",
                    target_sheet_key="reduction_targets",
                    target_row_number=index,
                ))
                row["감축률"] = computed
                _set_data_status(row, "calculated")
        # 산식과 무관하게 비정상 범위는 점검 항목으로만 표시(흡수원 음수는 정상 가능).
        final_rate = row.get("감축률")
        if isinstance(final_rate, (int, float)) and (final_rate > 100 or final_rate < -50):
            issues.append(_issue(
                municipality, "경고", "감축목표", f"감축률 범위 의심({row.get('부문')} {row.get('목표연도')})",
                f"감축률 {final_rate}%는 통상 범위(0~100%)를 벗어남",
                "흡수원/증가 시나리오가 아니면 원문 수치 재확인",
                target_sheet_key="reduction_targets",
                target_row_number=index,
            ))
    return issues




def _sheet_area(sheet_key: str) -> str:
    return getattr(config, "SHEET_KEY_TO_NAME", {}).get(sheet_key, sheet_key)


def _tag_prior_plan_rows(cleaned: dict, municipality: str, prior_plan_pages: set[int]) -> None:
    if not prior_plan_pages:
        return
    for sheet_key in ("mitigation_projects", "annual_implementation", "quantitative_reductions", "financial_plan"):
        rows = cleaned.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        tagged_rows: list[int] = []
        tagged_pages: set[int] = set()
        for index, row in enumerate(rows, start=1):
            pages = _row_source_pages(row)
            if not pages:
                continue
            if pages <= prior_plan_pages:
                row[_PLAN_CONTEXT_FIELD] = "기존계획"
                tagged_rows.append(index)
                tagged_pages.update(pages)
            else:
                row[_PLAN_CONTEXT_FIELD] = "본계획"
        if tagged_rows:
            page_list = sorted(tagged_pages)
            page_summary = ",".join(f"p{page}" for page in page_list[:12])
            if len(page_list) > 12:
                page_summary += ",…"
            row_summary = f"행 {tagged_rows[0]}~{tagged_rows[-1]}" if len(tagged_rows) > 1 else f"행 {tagged_rows[0]}"
            _remember_validation_issue(
                municipality,
                "경고",
                _sheet_area(sheet_key),
                "기존계획 평가 장 유래 행",
                f"{_sheet_area(sheet_key)} {len(tagged_rows)}건({row_summary}, {page_summary})을 "
                "기존계획 평가 장 유래 문맥으로 태깅",
                "행별 중복 경고는 만들지 않음. 본계획 사업목록·사업카드와 시트 단위로 대조",
                target_sheet_key=sheet_key,
            )


def _project_name_key(row: dict) -> str:
    return _dedup_key_text(row.get("사업명") or row.get("과제명") or "")


def _append_project_id_consistency_issues(issues: list[dict], cleaned: dict, municipality: str) -> None:
    registry: dict[str, set[str]] = {}
    for row in cleaned.get("mitigation_projects", []):
        if row.get(_PLAN_CONTEXT_FIELD) == "기존계획":
            continue
        project_id = _normalize_project_id(row.get("관리번호"))
        project_name = _project_name_key(row)
        if not project_id or not project_name:
            continue
        registry.setdefault(project_id, set()).add(project_name)

    for project_id, names in registry.items():
        if len(names) > 1:
            issues.append(_issue(
                municipality,
                "경고",
                _sheet_area("mitigation_projects"),
                f"관리번호 {project_id}에 사업명 {len(names)}종",
                f"정규화 사업명: {sorted(names)}",
                "본계획 사업목록·사업카드에서 관리번호 기준 사업명을 확인",
            ))

    for sheet_key in ("annual_implementation", "quantitative_reductions", "financial_plan"):
        for row in cleaned.get(sheet_key, []):
            if row.get(_PLAN_CONTEXT_FIELD) == "기존계획":
                continue
            raw_id = str(row.get("관리번호", "") or "").strip()
            project_id = _normalize_project_id(raw_id)
            if not project_id:
                continue
            if raw_id and project_id != raw_id.upper():
                issues.append(_issue(
                    municipality,
                    "정보",
                    _sheet_area(sheet_key),
                    f"관리번호 표기 정규화({raw_id}→{project_id})",
                    f"{raw_id} 표기를 {project_id}로 흡수해 08 시트 레지스트리와 비교",
                    "원문 표기 차이인지 별도 과제인지 확인",
                ))
            project_name = _project_name_key(row)
            registered = registry.get(project_id)
            if registered is None or not project_name:
                continue
            if project_name not in registered:
                issues.append(_issue(
                    municipality,
                    "정보",
                    _sheet_area(sheet_key),
                    f"관리번호-사업명 불일치({project_id})",
                    f"{_sheet_area(sheet_key)} 사업명 '{row.get('사업명')}'이 08 시트 등록명과 다름",
                    "본계획 사업목록 기준으로 관리번호·사업명 매칭 확인",
                ))


def _record_attr(record, name: str, default=None):
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _relative_gap(left: float, right: float) -> float:
    return abs(left - right) / max(abs(left), abs(right), 1.0)


def _emissions_total_by_year(rows: list[dict]) -> dict[int, float]:
    totals: dict[int, float] = {}
    explicit_totals: dict[int, float] = {}
    for row in rows:
        year = row.get("연도")
        amount = row.get("배출량")
        if not isinstance(year, int) or not isinstance(amount, (int, float)):
            continue
        sector = str(row.get("부문", "") or "").strip()
        if sector in {"합계", "총계", "전체"}:
            explicit_totals[year] = float(amount)
        else:
            totals[year] = totals.get(year, 0.0) + float(amount)
    return {**totals, **explicit_totals}


def _append_dedup_conflict_issues(issues: list[dict], municipality: str) -> None:
    for conflict in _DEDUP_CONFLICTS:
        resolved = bool(conflict.get("resolved"))
        issues.append(_issue(
            municipality,
            "정보" if resolved else "경고",
            "중복제거",
            f"중복 키 값 충돌-{'자동해결' if resolved else '미해결'}({conflict['key']})",
            conflict["detail"],
            "선택 근거와 원문 페이지 확인" if resolved else "원문 페이지 재확인",
        ))


def _append_cross_sheet_issues(issues: list[dict], cleaned: dict, municipality: str) -> None:
    regional_totals = _emissions_total_by_year(cleaned.get("emissions_regional", []))
    for row in cleaned.get("reduction_targets", []):
        if str(row.get("목표수준", "")).strip() not in {"총괄", "합계", "전체", ""}:
            continue
        base_year = row.get("기준연도")
        base_amount = row.get("기준배출량")
        if not isinstance(base_year, int) or not isinstance(base_amount, (int, float)):
            continue
        regional = regional_totals.get(base_year)
        if regional is None or regional == 0:
            continue
        if _relative_gap(float(base_amount), regional) <= 0.05:
            continue
        scaled = regional * 1000
        if _relative_gap(float(base_amount), scaled) <= 0.05:
            issues.append(_issue(
                municipality, "정보", "교차시트", f"기준배출량 단위 스케일 차이 추정({base_year})",
                f"감축목표 {base_amount} vs 배출현황 {regional} (1000배 보정 시 근접)",
                "단위가 톤/천톤으로 섞였는지 확인",
            ))
            continue
        issues.append(_issue(
            municipality, "경고", "교차시트", f"기준배출량≠배출현황({base_year})",
            f"감축목표 기준배출량 {base_amount} vs 배출현황 합계 {regional}",
            "기준연도·단위·총괄 범위 원문 재확인",
        ))

    reductions_by_year: dict[int, float] = {}
    for row in cleaned.get("quantitative_reductions", []):
        year = row.get("연도")
        amount = row.get("예상감축량")
        if isinstance(year, int) and isinstance(amount, (int, float)):
            reductions_by_year[year] = reductions_by_year.get(year, 0.0) + float(amount)
    for row in cleaned.get("reduction_targets", []):
        target_year = row.get("목표연도")
        target_amount = row.get("목표감축량")
        if not isinstance(target_year, int) or not isinstance(target_amount, (int, float)):
            continue
        summed = reductions_by_year.get(target_year)
        if summed is None or target_amount == 0:
            continue
        if _relative_gap(float(target_amount), summed) > 0.10:
            issues.append(_issue(
                municipality, "정보", "교차시트", f"정량감축량 합≠목표감축량({target_year})",
                f"정량감축량 합 {round(summed, 2)} vs 목표감축량 {target_amount}",
                "계획서 자체 불일치 또는 일부 사업 감축량 누락 여부 확인",
            ))


def _append_gap_fill_summary(issues: list[dict], cleaned: dict, municipality: str) -> None:
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = cleaned.get(sheet_key, [])
        if not isinstance(rows, list):
            continue
        count = sum(1 for row in rows if isinstance(row, dict) and row.get("보완출처") == "gap_fill")
        if count:
            issues.append(_issue(
                municipality, "정보", _sheet_area(sheet_key), "GapFill 기여",
                f"빈칸보완 재추출 행 {count}건 반영",
                "보완 행은 출처페이지 기준으로 원문 확인",
            ))


def _append_ledger_issues(
    issues: list[dict],
    municipality: str,
    ledger: list | None,
    raw_counts: dict | None,
    routed_page_nums: dict[str, set[int]] | None,
    cleaned: dict,
) -> None:
    if ledger is None and raw_counts is None and routed_page_nums is None:
        return
    records = list(ledger or [])
    by_sheet: dict[str, list] = {}
    for record in records:
        by_sheet.setdefault(str(_record_attr(record, "sheet_key", "")), []).append(record)
        status = _record_attr(record, "status", "")
        if status in {"call_fail", "parse_fail"}:
            pages = ",".join(f"p{page}" for page in _record_attr(record, "page_nums", []) or [])
            label = "호출실패" if status == "call_fail" else "파싱실패"
            issues.append(_issue(
                municipality, "경고", _sheet_area(_record_attr(record, "sheet_key", "")),
                f"원장 {label}", f"{pages or 'p?'} 배치 실패: {_record_attr(record, 'error', '')}",
                "해당 페이지 재추출 또는 원문 수동 확인",
            ))

    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = cleaned.get(sheet_key, [])
        if isinstance(rows, list) and rows:
            continue
        sheet_records = by_sheet.get(sheet_key, [])
        raw_count = int((raw_counts or {}).get(sheet_key, 0) or 0)
        routed = (routed_page_nums or {}).get(sheet_key, set())
        if raw_count > 0:
            cause = "정제에서 전부 제거"
        elif any(_record_attr(record, "status", "") in {"call_fail", "parse_fail"} for record in sheet_records):
            cause = "호출·파싱 실패"
        elif any(_record_attr(record, "status", "") == "ok" and int(_record_attr(record, "rows", 0) or 0) == 0 for record in sheet_records):
            cause = "추출 0행"
        elif not routed and not sheet_records:
            cause = "라우팅 0페이지"
        else:
            cause = "추출 0행"
        issues.append(_issue(
            municipality, "정보", _sheet_area(sheet_key), "빈 시트 원인",
            cause,
            "원장·라우팅 후보·정제 필터를 순서대로 확인",
        ))

def _validate_final_data(
    cleaned: dict,
    municipality: str,
    *,
    ledger: list | None = None,
    raw_counts: dict | None = None,
    routed_page_nums: dict[str, set[int]] | None = None,
    prior_plan_pages: set[int] | None = None,
) -> list[dict]:
    """정제된 16시트 데이터의 완성도·정합성을 결정론적으로 점검해 리포트를 만든다."""
    issues: list[dict] = []

    # 1) 감축률 산식 재계산(인플레이스 교정 + 불일치 리포트)
    issues.extend(_recompute_reduction_rate(cleaned.get("reduction_targets", []), municipality))

    # 2) 지역 배출현황 8부문 커버리지
    regional = cleaned.get("emissions_regional", [])
    if regional:
        present = {r.get("부문") for r in regional}
        missing = [s for s in config.SECTORS if s not in present]
        if missing:
            issues.append(_issue(
                municipality, "정보", "배출현황_지역", "부문 누락 가능",
                f"표준 8부문 중 미등장: {', '.join(missing)}",
                "해당 부문이 원문에 있는지(다른 명칭 포함) 확인",
            ))

    # 3) 감축목표 목표연도(2030/2050) 존재
    targets = cleaned.get("reduction_targets", [])
    if targets:
        years = {r.get("목표연도") for r in targets}
        for y in (2030, 2050):
            if y not in years:
                issues.append(_issue(
                    municipality, "정보", "감축목표", f"목표연도 {y} 누락",
                    f"감축목표에 {y}년 행이 없음",
                    f"{y}년 목표가 원문에 있는지 확인",
                ))

    # 4) 배출 단위 스케일 혼재(천톤 vs 톤) 감지 — 자동 환산은 하지 않고 리포트만.
    for sheet_key, sheet_name in (("emissions_regional", "배출현황_지역"),
                                   ("emissions_management", "배출현황_관리권한")):
        units = {r.get("단위") for r in cleaned.get(sheet_key, []) if r.get("단위")}
        co2_units = {u for u in units if "CO2" in str(u)}
        scales = set()
        for u in co2_units:
            if u.startswith("백만톤"):
                scales.add("백만톤")
            elif u.startswith("천톤"):
                scales.add("천톤")
            else:
                scales.add("톤")
        if len(scales) > 1:
            issues.append(_issue(
                municipality, "경고", sheet_name, "단위 스케일 혼재",
                f"한 시트에 {', '.join(sorted(scales))} 단위가 섞여 값 비교 불가: {sorted(co2_units)}",
                "기준 단위로 환산하거나 출처별로 분리 검토",
            ))

    # 5) 재정 합계 vs 부분합 교차검증. 서로 다른 사업을 같은 부문·연도로 묶으면
    # 허위 경고가 대량 발생하므로 관리번호/사업명과 예산단위가 같은 행만 비교한다.
    fin = cleaned.get("financial_plan", [])
    groups: dict[tuple, dict] = {}
    for r in fin:
        entity = _dedup_key_text(r.get("관리번호") or r.get("사업명"))
        if not entity:
            continue
        key = (
            r.get("계획구분"),
            r.get("부문"),
            entity,
            r.get("연도"),
            _dedup_key_text(r.get("예산단위")),
        )
        g = groups.setdefault(key, {"합계": None, "부분합": 0.0, "부분수": 0})
        amount = r.get("예산액")
        if not isinstance(amount, (int, float)):
            continue
        if str(r.get("재원구분", "")).strip() in ("합계", "총계", "계"):
            g["합계"] = amount
        else:
            g["부분합"] += amount
            g["부분수"] += 1
    for (plan, sector, entity, year, unit), g in groups.items():
        total = g["합계"]
        if total and g["부분수"] >= 2 and total != 0:
            if abs(total - g["부분합"]) / abs(total) > 0.01:
                issues.append(_issue(
                    municipality, "경고", "재정투자계획",
                    f"합계≠부분합({entity} {year})",
                    f"{sector or '부문미상'} / {unit or '단위미상'}: "
                    f"합계 {total} vs 재원별 합 {round(g['부분합'],1)}",
                    "재원별 누락/중복 또는 합계 오기 확인",
                ))

    _append_dedup_conflict_issues(issues, municipality)
    issues.extend(_RECORDED_VALIDATION_ISSUES)
    if prior_plan_pages:
        _append_project_id_consistency_issues(issues, cleaned, municipality)
    _append_cross_sheet_issues(issues, cleaned, municipality)
    _append_gap_fill_summary(issues, cleaned, municipality)
    _append_ledger_issues(issues, municipality, ledger, raw_counts, routed_page_nums, cleaned)

    unique: list[dict] = []
    seen: set[tuple] = set()
    for issue in issues:
        identity = tuple(
            str(issue.get(field, "") or "").strip()
            for field in ("지자체명", "심각도", "영역", "항목", "문제내용", "권장조치")
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(issue)
    return unique


class OrganizerAgent:
    """에이전트 3: 정리·정제 에이전트 (가이드라인 기반 16개 시트)"""

    def __init__(self):
        self._final_data: dict = {}
        self._validation_report: list[dict] = []

    def organize_sheet(self, sheet_key: str, rows: list[dict], municipality: str) -> list[dict]:
        cleaner = _CLEANERS.get(sheet_key)
        if cleaner is None:
            return []
        source_rows = [dict(row) for row in rows if isinstance(row, dict)]
        cleaned_rows = cleaner(source_rows, municipality)
        cleaned = {
            "municipality_name": municipality,
            sheet_key: [_normalize_row_provenance(row) for row in cleaned_rows],
        }
        _apply_default_data_status(cleaned)
        return cleaned[sheet_key]

    def organize(
        self,
        raw_data: dict,
        full_text_excerpt: str = "",
        *,
        ledger: list | None = None,
        routed_page_nums: dict[str, set[int]] | None = None,
        prior_plan_pages: set[int] | None = None,
    ) -> dict:
        print("[에이전트3 정리] 정제 시작...")
        municipality = raw_data.get("municipality_name", "알 수 없음")

        _DEDUP_CONFLICTS.clear()
        _RECORDED_VALIDATION_ISSUES.clear()
        raw_counts: dict[str, int] = {}
        cleaned: dict = {"municipality_name": municipality}
        card_pages = _card_context_pages(raw_data)
        for sheet_key, cleaner in _CLEANERS.items():
            rows = raw_data.get(sheet_key, [])
            if not isinstance(rows, list):
                rows = []
            rows = [dict(r) for r in rows if isinstance(r, dict)]
            raw_counts[sheet_key] = len(rows)
            if sheet_key == "reduction_targets":
                rows = _retag_reduction_target_rows(
                    rows,
                    municipality,
                    card_pages,
                )
            cleaned_rows = cleaner(rows, municipality)
            if sheet_key == "reduction_targets":
                cleaned_rows = _absorb_suspected_target_scope_mixture(
                    cleaned_rows,
                    municipality,
                )
            _restore_visual_fill_placeholders(
                sheet_key,
                rows,
                cleaned_rows,
                cleaner,
                municipality,
            )
            cleaned[sheet_key] = [_normalize_row_provenance(row) for row in cleaned_rows]

        # 이미지 에이전트의 판독 관찰값을 16_시각자료목록 시트로 보존한다.
        # (이전에는 organize()가 시트 키만 복사해 chart_observations가 통째로 유실됐다.)
        observations = raw_data.get("chart_observations", [])
        if not isinstance(observations, list):
            observations = []
        cleaned["chart_observations"] = observations
        cleaned["visual_inventory"] = _build_visual_inventory(observations, municipality)
        # 선택적 OCR 계층의 객체와 판정 원장을 원문 객체 검증 단계까지 전달한다.
        # 엑셀 본문 시트에는 직접 쓰지 않고 21_원문객체인벤토리 생성에 사용한다.
        for object_key in ("object_triage", "ocr_document_objects", "document_objects"):
            object_rows = raw_data.get(object_key, [])
            cleaned[object_key] = object_rows if isinstance(object_rows, list) else []
        evidence_contract_active = any(
            object_key in raw_data
            for object_key in ("object_triage", "document_objects")
        )
        execution_info = raw_data.get("execution_info") if isinstance(raw_data.get("execution_info"), dict) else None
        cleaned["codebook"] = build_codebook_rows(execution_info) if getattr(config, "CODEBOOK_SHEET_ENABLED", True) else []
        _apply_default_data_status(cleaned)
        _apply_visual_labeled_merge(
            cleaned,
            observations,
            municipality,
            evidence_contract_active=evidence_contract_active,
        )
        _drop_unfilled_visual_placeholders(cleaned)
        # 병합 게이트가 관찰값에 기록한 최종 상태를 16번 감사 시트에 반영한다.
        cleaned["visual_inventory"] = _build_visual_inventory(observations, municipality)
        _tag_prior_plan_rows(cleaned, municipality, prior_plan_pages or set())

        # 결정론적 검증·정합성 점검(감축률 재계산은 reduction_targets를 인플레이스 교정).
        self._validation_report = _validate_final_data(
            cleaned,
            municipality,
            ledger=ledger,
            raw_counts=raw_counts,
            routed_page_nums=routed_page_nums,
            prior_plan_pages=prior_plan_pages or set(),
        )
        cleaned["validation_report"] = self._validation_report

        internal_keys = getattr(config, "INTERNAL_OBJECT_KEYS", set())
        counts = {
            k: len(v) for k, v in cleaned.items()
            if isinstance(v, list) and v and k not in internal_keys
        }
        print(f"[에이전트3 정리] 정제 완료: {counts}")
        if self._validation_report:
            sev = {}
            for it in self._validation_report:
                sev[it["심각도"]] = sev.get(it["심각도"], 0) + 1
            print(f"[에이전트3 정리] 검증 리포트 {len(self._validation_report)}건: {sev}")

        self._final_data = cleaned
        return cleaned

    def get_excel_ready(self) -> dict:
        internal_keys = getattr(config, "INTERNAL_OBJECT_KEYS", set())
        return {
            k: v for k, v in self._final_data.items()
            if isinstance(v, list) and k not in internal_keys
        }

    def report(self) -> str:
        d = self._final_data
        counts = {k: len(v) for k, v in d.items() if isinstance(v, list) and v}
        return (
            f"[에이전트3 정리] 정제 완료\n"
            f"  - 지자체명: {d.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )

dedup_key_text = _dedup_key_text
normalize_project_id = _normalize_project_id
normalize_direct_indirect_type = _normalize_direct_indirect_type
to_float = _to_float
normalize_provenance_pages = _normalize_provenance_pages
