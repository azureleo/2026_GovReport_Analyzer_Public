"""
에이전트 3: 정리·정제 에이전트

16개 시트 구조에 맞춰 원시 추출 결과를 정제합니다.
가이드라인(carbon_guideline.md) 기준 표준 코드에 맞춰 정규화하고,
중복 제거, 이상치 처리, 단위 검증을 수행합니다.
"""
# noqa: SIZE_OK — 16개 시트 정제 계약을 보존하는 기존 모놀리식 cleaner. WP별로만 국소 수정.

import logging
import re
import unicodedata
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
    "MARKAL-MACRO": "bottom_up_hybrid_MARKAL_MACRO",
    "MARKAL": "bottom_up_optimization_MARKAL",
    "ENPEP": "bottom_up_simulation_ENPEP",
}

_MANAGEMENT_ENERGY_SOURCE_TERMS = {"전력", "열", "에너지"}


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


def _chapter_id(text: str) -> str:
    head = (text or "")[:200]
    match = re.search(r"제\s*(\d+)\s*장", head)
    if match is not None:
        return match.group(1)
    match = re.search(r"^\s*(\d+)\s*[장.]\s+", head)
    return match.group(1) if match is not None else ""


def _has_chapter_heading(text: str) -> bool:
    head = (text or "")[:200]
    return bool(re.search(r"제\s*\d+\s*장|^\s*\d+\s*[장.]\s+", head))


def _is_prior_plan_heading(text: str) -> bool:
    head = (text or "")[:200]
    return any(re.search(pattern, head) for pattern in getattr(config, "PRIOR_PLAN_HEADING_PATTERNS", []))


def detect_prior_plan_pages(pages: list) -> set[int]:
    sorted_pages = sorted(pages, key=lambda page: getattr(page, "page_number", 0))
    start_page = None
    start_chapter = ""
    for page in sorted_pages:
        text = getattr(page, "text", "")
        if not _is_prior_plan_heading(text):
            continue
        start_page = int(getattr(page, "page_number", 0))
        start_chapter = _chapter_id(text)
        break
    if start_page is None:
        return set()

    detected: set[int] = set()
    active = False
    for page in sorted_pages:
        page_number = int(getattr(page, "page_number", 0))
        text = getattr(page, "text", "")
        if page_number == start_page:
            active = True
        if not active:
            continue
        current_chapter = _chapter_id(text)
        if page_number != start_page and _has_chapter_heading(text):
            if not start_chapter or (current_chapter and current_chapter != start_chapter):
                break
        detected.add(page_number)
    return detected


_DEDUP_CONFLICTS: list[dict] = []
_RECORDED_VALIDATION_ISSUES: list[dict] = []
_NUMERIC_CONFLICT_FIELDS = {
    "배출량", "전망값", "예산액", "예상감축량", "목표배출량", "목표감축량",
    "기준배출량", "배출전망", "값", "활동량", "목표물량", "감축률",
}
_PLAN_CONTEXT_FIELD = "계획구분출처"


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


def _dedup_key_text(val: Any) -> str:
    """dedup 키 생성 전용 텍스트 정규화. 원본 셀 값은 바꾸지 않는다."""
    if val is None:
        return ""
    text = unicodedata.normalize("NFKC", str(val))
    text = text.replace("ㆍ", "·").replace("･", "·").replace("ᆞ", "·").replace("・", "·")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text.replace(" ", "")


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
        row[raw_field] = raw_text
    return normalized


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


def _remember_dedup_conflict(key_fields: list[str], kept: dict, discarded: dict, fields: list[str]) -> None:
    key_summary = ", ".join(f"{field}={kept.get(field) or discarded.get(field) or ''}" for field in key_fields)
    detail = "; ".join(
        f"{field}: 유지 {kept.get(field)} vs 폐기 {discarded.get(field)}"
        for field in fields
    )
    _DEDUP_CONFLICTS.append({"key": key_summary, "detail": detail})


def _merge_missing_values(target: dict, source: dict) -> None:
    for key, value in source.items():
        if key == "출처페이지":
            merged = _merge_provenance(target.get("출처페이지"), value)
            if merged:
                target["출처페이지"] = merged
            continue
        if _has_cell_value(value) and not _has_cell_value(target.get(key)):
            target[key] = value


def _same_except_detail(left: dict, right: dict, key_fields: list[str], detail_field: str) -> bool:
    return all(
        field == detail_field or _dedup_key_text(left.get(field)) == _dedup_key_text(right.get(field))
        for field in key_fields
    )


def _absorb_blank_detail_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
    detail_field = "세부부문"
    if detail_field not in key_fields:
        return rows
    retained: list[dict] = [row for row in rows if _dedup_key_text(row.get(detail_field))]
    blanks = [row for row in rows if not _dedup_key_text(row.get(detail_field))]
    for blank in blanks:
        absorbed = False
        for candidate in retained:
            if not _same_except_detail(blank, candidate, key_fields, detail_field):
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


def _deduplicate_rows(rows: list[dict], key_fields: list[str]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for source_row in rows:
        row = _normalize_row_provenance(dict(source_row))
        key = tuple(_dedup_key_text(row.get(f)) for f in key_fields)
        if key not in seen:
            seen[key] = row
            continue
        kept = seen[key]
        conflicts = _row_conflicts(kept, row)
        if conflicts:
            _remember_dedup_conflict(key_fields, kept, row, conflicts)
        _merge_missing_values(kept, row)
    return _absorb_blank_detail_rows(list(seen.values()), key_fields)


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
        row["배출유형"] = _normalize(row.get("배출유형", ""), _TYPE_MAP)
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, ["지자체명", "배출유형", "부문", "세부부문", "연도"])


def _clean_emissions_management(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        raw_sector = str(row.get("관리부문", "") or "").strip()
        normalized_sector = _normalise_sector_with_raw(row, "관리부문", "관리부문원문")
        if normalized_sector == "전환" and raw_sector in _MANAGEMENT_ENERGY_SOURCE_TERMS:
            row["관리부문"] = raw_sector
            row["관리부문원문"] = raw_sector
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
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
        row["단위"] = _normalize_co2_unit(row.get("단위", ""))
    rows = [r for r in rows if r.get("관리부문")]
    rows = _filter_empty_rows(rows, ["배출량"])
    return _deduplicate_rows(rows, ["지자체명", "관리부문", "세부부문", "직간접구분", "연도"])


def _clean_emissions_forecast(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
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
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalise_sector_with_raw(row, "부문", "부문원문")
        row["기준연도"] = _to_int(row.get("기준연도"))
        row["목표연도"] = _to_int(row.get("목표연도"))
        row["기준배출량"] = _to_float(row.get("기준배출량"))
        row["배출전망"] = _to_float(row.get("배출전망"))
        row["목표감축량"] = _to_float(row.get("목표감축량"))
        row["목표배출량"] = _to_float(row.get("목표배출량"))
        row["감축률"] = _to_float(row.get("감축률"))
    return _deduplicate_rows(rows, ["지자체명", "목표수준", "목표범위", "부문", "목표연도"])


def _clean_vision_strategy(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
    return _filter_empty_rows(rows, ["비전문구", "전략명"])


def _clean_mitigation_projects(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["부문"] = _normalise_sector_with_raw(row, "부문", "부문원문")
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


def _build_visual_inventory(observations: list[dict], municipality: str) -> list[dict]:
    """
    이미지 에이전트의 chart_observations(감사 가능한 판독 관찰값)를
    16_시각자료목록 시트 헤더에 맞춰 변환한다.

    config.EXCEL_HEADERS["16_시각자료목록"]:
      지자체명, 시각자료ID, 캡션, 유형, 데이터포함여부, 추출값요약, 디지타이징필요, 관련시트
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
        summary_bits = [bit for bit in [item, str(year) if year is not None else "",
                                        str(value) if value is not None else "", unit] if bit]
        value_summary = " ".join(summary_bits).strip()
        if evidence:
            value_summary = f"{value_summary} | {evidence}" if value_summary else evidence

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
            "디지타이징필요": needs_digitizing,
            "관련시트": related_sheet,
        })
    return inventory


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
            elif isinstance(rate, (int, float)) and abs(rate - computed) > 1.0:
                issues.append(_issue(
                    municipality, "경고", "감축목표", f"감축률 불일치({sector} {row.get('목표연도')})",
                    f"보고값 {rate} vs 산식 계산값 {computed}",
                    "산식 계산값으로 교정함. 기준/목표 배출량 원문 재확인 권장",
                    target_sheet_key="reduction_targets",
                    target_row_number=index,
                ))
                row["감축률"] = computed
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
        tagged_count = 0
        for index, row in enumerate(rows, start=1):
            pages = _row_source_pages(row)
            if not pages:
                continue
            if pages <= prior_plan_pages:
                row[_PLAN_CONTEXT_FIELD] = "기존계획"
                tagged_count += 1
                _remember_validation_issue(
                    municipality,
                    "경고",
                    _sheet_area(sheet_key),
                    f"{_sheet_area(sheet_key)} 행 {index}: 기존계획 평가 장 유래",
                    f"기존계획 평가 장({','.join(f'p{page}' for page in sorted(pages))}) 유래 — 본계획 사업 목록과 관리번호 충돌 가능",
                    "본계획 사업목록·사업카드 기준으로 원문 확인",
                    target_sheet_key=sheet_key,
                    target_row_number=index,
                )
            else:
                row[_PLAN_CONTEXT_FIELD] = "본계획"
        if tagged_count:
            _remember_validation_issue(
                municipality,
                "정보",
                _sheet_area(sheet_key),
                f"기존계획 평가 장 유래 {tagged_count}건",
                f"{_sheet_area(sheet_key)}에서 기존계획 평가 장 유래 행 {tagged_count}건 태깅",
                "자동 이동·삭제하지 않고 타깃 검수 대상으로 넘김",
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
        issues.append(_issue(
            municipality,
            "경고",
            "중복제거",
            f"중복 키 값 충돌({conflict['key']})",
            conflict["detail"],
            "원문 페이지 재확인",
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

    # 5) 재정 합계 vs 부분합 교차검증(보수적): (계획구분,부문,연도)별로 '합계' 행과
    #    재원별 행의 합을 비교해 1% 이상 어긋나면 표시.
    fin = cleaned.get("financial_plan", [])
    groups: dict[tuple, dict] = {}
    for r in fin:
        key = (r.get("계획구분"), r.get("부문"), r.get("연도"))
        g = groups.setdefault(key, {"합계": None, "부분합": 0.0, "부분수": 0})
        amount = r.get("예산액")
        if not isinstance(amount, (int, float)):
            continue
        if str(r.get("재원구분", "")).strip() in ("합계", "총계", "계"):
            g["합계"] = amount
        else:
            g["부분합"] += amount
            g["부분수"] += 1
    for (plan, sector, year), g in groups.items():
        total = g["합계"]
        if total and g["부분수"] >= 2 and total != 0:
            if abs(total - g["부분합"]) / abs(total) > 0.01:
                issues.append(_issue(
                    municipality, "경고", "재정투자계획", f"합계≠부분합({sector} {year})",
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

    return issues


class OrganizerAgent:
    """에이전트 3: 정리·정제 에이전트 (가이드라인 기반 16개 시트)"""

    def __init__(self):
        self._final_data: dict = {}
        self._validation_report: list[dict] = []

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
        for sheet_key, cleaner in _CLEANERS.items():
            rows = raw_data.get(sheet_key, [])
            if not isinstance(rows, list):
                rows = []
            rows = [dict(r) for r in rows if isinstance(r, dict)]
            raw_counts[sheet_key] = len(rows)
            cleaned_rows = cleaner(rows, municipality)
            cleaned[sheet_key] = [_normalize_row_provenance(row) for row in cleaned_rows]

        # 이미지 에이전트의 판독 관찰값을 16_시각자료목록 시트로 보존한다.
        # (이전에는 organize()가 시트 키만 복사해 chart_observations가 통째로 유실됐다.)
        observations = raw_data.get("chart_observations", [])
        if not isinstance(observations, list):
            observations = []
        cleaned["chart_observations"] = observations
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

        counts = {k: len(v) for k, v in cleaned.items() if isinstance(v, list) and v}
        print(f"[에이전트3 정리] 정제 완료: {counts}")
        if self._validation_report:
            sev = {}
            for it in self._validation_report:
                sev[it["심각도"]] = sev.get(it["심각도"], 0) + 1
            print(f"[에이전트3 정리] 검증 리포트 {len(self._validation_report)}건: {sev}")

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
