"""Shared contracts for visual extraction, scoring, and evidence-gated merging."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any, Sequence


VISUAL_CONTRACT_VERSION = 5

VISUAL_CHART_TYPES = frozenset({
    "막대", "꺾은선", "영역", "원", "표", "복합", "기타",
    "diagram", "infographic", "flow", "strategy_map", "risk_map", "risk_matrix",
})
STRUCTURED_VISUAL_TYPES = frozenset({
    "diagram", "infographic", "flow", "strategy_map", "risk_map", "risk_matrix",
})
VISUAL_TARGET_SHEETS = frozenset({
    "regional_conditions",
    "emissions_regional",
    "emissions_management",
    "emissions_forecast",
    "reduction_targets",
    "vision_strategy",
    "mitigation_projects",
    "financial_plan",
    "foundation_measures",
    "other",
})


def is_structured_visual_type(value: Any) -> bool:
    return str(value or "").strip().casefold() in STRUCTURED_VISUAL_TYPES

_RANGE_RE = re.compile(r"(?:\d{1,4}\s*(?:~|〜|–|—)\s*\d{1,4})")
_NUMBER_RE = re.compile(r"[-+−]?\s*\d[\d,]*(?:\.\d+)?")
_PERCENT_RE = re.compile(r"([-+−]?\s*\d[\d,]*(?:\.\d+)?)\s*%")

_VALUE_SOURCE_MAP = {
    "explicitlabel": "explicit_label",
    "explicit_label": "explicit_label",
    "명시라벨": "explicit_label",
    "직접표기": "explicit_label",
    "표시값": "explicit_label",
    "tablecell": "table_cell",
    "table_cell": "table_cell",
    "표셀": "table_cell",
    "표수치": "table_cell",
    "axisestimate": "axis_estimate",
    "axis_estimate": "axis_estimate",
    "축추정": "axis_estimate",
    "추정": "axis_estimate",
    "derived": "derived",
    "계산값": "derived",
    "계산": "derived",
    "unknown": "unknown",
    "불명": "unknown",
}

_MEASURE_FIELDS = {
    "bau": ("BAU", "배출전망"),
    "bau대비감축량": ("BAU 대비 감축량", "목표감축량"),
    "감축률": ("감축률", "감축률"),
    "누계": ("누계", "누계"),
    "연간구축": ("신규", "신규"),
    "신규구축": ("신규", "신규"),
    "기준배출량": ("기준배출량", "기준배출량"),
    "목표배출량": ("목표배출량", "목표배출량"),
    "목표감축량": ("목표감축량", "목표감축량"),
    "배출전망": ("배출전망", "배출전망"),
    "감축량": ("감축량", "목표감축량"),
    "관리권한내감축": ("관리권한 내 감축", "감축률"),
    "예산액": ("예산액", "예산액"),
}

_UNIT_SCALES = {
    "tco2eq": ("tCO2eq", 1.0),
    "tco2e": ("tCO2eq", 1.0),
    "톤co2eq": ("tCO2eq", 1.0),
    "톤co2e": ("tCO2eq", 1.0),
    "천tco2eq": ("tCO2eq", 1_000.0),
    "천tco2e": ("tCO2eq", 1_000.0),
    "천톤co2eq": ("tCO2eq", 1_000.0),
    "천톤co2e": ("tCO2eq", 1_000.0),
    "ktco2eq": ("tCO2eq", 1_000.0),
    "만tco2eq": ("tCO2eq", 10_000.0),
    "만톤co2eq": ("tCO2eq", 10_000.0),
    "백만tco2eq": ("tCO2eq", 1_000_000.0),
    "백만톤co2eq": ("tCO2eq", 1_000_000.0),
    "원": ("원", 1.0),
    "천원": ("원", 1_000.0),
    "만원": ("원", 10_000.0),
    "백만원": ("원", 1_000_000.0),
    "억원": ("원", 100_000_000.0),
    "명": ("명", 1.0),
    "천명": ("명", 1_000.0),
    "만명": ("명", 10_000.0),
    "대": ("대", 1.0),
    "천대": ("대", 1_000.0),
    "만대": ("대", 10_000.0),
    "%": ("%", 1.0),
    "tj": ("TJ", 1.0),
    "toe": ("toe", 1.0),
    "mwh": ("MWh", 1.0),
    "gwh": ("MWh", 1_000.0),
}
_VALUE_SUFFIX_SCALES = {"천": 1_000.0, "만": 10_000.0, "백만": 1_000_000.0, "억": 100_000_000.0}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _compact_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    return re.sub(r"[\s·ㆍ._()（）\[\]{}]+", "", text)


def normalize_comparison_text(value: Any, field_name: str = "") -> str:
    """Normalize harmless typography without hiding substantive label differences."""
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("’", "'").replace("‘", "'").replace("`", "'")
    text = re.sub(r"\s+", "", text)
    if _compact_key(field_name) in {"단위", "unit"}:
        text = text.replace("co₂", "co2")
        text = text.rstrip(".")
        unit_aliases = {
            "천톤co2e": "천톤co2eq",
            "천tco2eq": "천톤co2eq",
            "톤co2e": "tco2eq",
            "톤co2eq": "tco2eq",
        }
        text = unit_aliases.get(text, text)
    return text


def comparison_equal(expected: Any, actual: Any, field_name: str = "") -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not math.isfinite(float(expected)) or not math.isfinite(float(actual)):
            return False
        tolerance = max(abs(float(expected)) * 0.005, 1e-9)
        return abs(float(expected) - float(actual)) <= tolerance
    return normalize_comparison_text(expected, field_name) == normalize_comparison_text(
        actual,
        field_name,
    )


def normalize_value_source(value: Any) -> str:
    return _VALUE_SOURCE_MAP.get(_compact_key(value), "unknown")


def _parse_number_and_unit(value: Any) -> tuple[float | int | None, str]:
    if isinstance(value, bool) or value is None:
        return None, ""
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None, ""
        return value, ""
    text = unicodedata.normalize("NFKC", str(value)).replace("−", "-")
    percent = _PERCENT_RE.search(text)
    match = percent or _NUMBER_RE.search(text)
    if match is None:
        return None, ""
    token = (match.group(1) if percent else match.group()).replace(" ", "").replace(",", "")
    try:
        number = float(token)
    except ValueError:
        return None, ""
    if number.is_integer():
        number = int(number)
    if percent:
        return number, "%"
    suffix = text[match.end():].strip(" .,:;()（）")
    unit = suffix if suffix and not re.search(r"\d", suffix) else ""
    return number, unit


def normalize_quantity(value: Any, unit: Any = "") -> dict[str, Any]:
    """Return a comparison quantity while retaining the source representation.

    The operational workbook keeps the reported value/unit. The normalized value
    is used only for duplicate and conflict checks, preventing `31.5 천tCO2eq`
    from conflicting with `31,500 tCO2eq`.
    """
    raw_value = value
    raw_unit = _text(unit)
    number, parsed_unit = _parse_number_and_unit(value)
    if number is None:
        return {
            "raw_value": raw_value,
            "raw_unit": raw_unit,
            "value": None,
            "unit": "",
            "multiplier": None,
            "status": "invalid_numeric",
        }

    value_text = unicodedata.normalize("NFKC", _text(value)).replace(" ", "")
    suffix_multiplier = 1.0
    suffix_match = re.search(r"(?:\d|\.)(백만|천|만|억)(?![가-힣])", value_text)
    if suffix_match:
        suffix_multiplier = _VALUE_SUFFIX_SCALES[suffix_match.group(1)]

    source_unit = raw_unit or parsed_unit
    compact_unit = _compact_key(source_unit).replace("co₂", "co2")
    canonical_unit, unit_multiplier = _UNIT_SCALES.get(
        compact_unit,
        (source_unit, 1.0),
    )
    multiplier = suffix_multiplier * unit_multiplier
    normalized_value = float(number) * multiplier
    if normalized_value.is_integer():
        normalized_value = int(normalized_value)
    status = "identity" if multiplier == 1.0 else "scaled"
    if source_unit and compact_unit not in _UNIT_SCALES:
        status = "unknown_unit"
    return {
        "raw_value": raw_value,
        "raw_unit": raw_unit or parsed_unit,
        "value": normalized_value,
        "unit": canonical_unit,
        "multiplier": multiplier,
        "status": status,
    }


def quantities_conflict(
    left_value: Any,
    left_unit: Any,
    right_value: Any,
    right_unit: Any,
    *,
    tolerance_ratio: float = 0.005,
) -> bool:
    left = normalize_quantity(left_value, left_unit)
    right = normalize_quantity(right_value, right_unit)
    if left["value"] is None or right["value"] is None:
        return False
    left_canonical = normalize_comparison_text(left["unit"], "단위")
    right_canonical = normalize_comparison_text(right["unit"], "단위")
    if left_canonical and right_canonical and left_canonical != right_canonical:
        return True
    left_number = float(left["value"])
    right_number = float(right["value"])
    scale = max(abs(left_number), abs(right_number), 1.0)
    return abs(left_number - right_number) / scale > tolerance_ratio


def _attach_quantity_metadata(row: dict[str, Any], fields: dict[str, Any]) -> None:
    quantity = normalize_quantity(row.get("값"), row.get("단위"))
    fields.setdefault("원문값", quantity["raw_value"])
    fields.setdefault("원문단위", quantity["raw_unit"])
    fields["정규화값"] = quantity["value"]
    fields["정규화단위"] = quantity["unit"]
    fields["정규화배율"] = quantity["multiplier"]
    fields["단위정규화상태"] = quantity["status"]


def _correct_visible_sign(row: dict[str, Any], fields: dict[str, Any]) -> None:
    value = row.get("값")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return
    for raw in fields.values():
        if not isinstance(raw, str):
            continue
        for token in _NUMBER_RE.findall(unicodedata.normalize("NFKC", raw).replace("−", "-")):
            compact = token.replace(" ", "").replace(",", "")
            if not compact.startswith("-"):
                continue
            try:
                signed = float(compact)
            except ValueError:
                continue
            if abs(abs(signed) - float(value)) <= max(abs(float(value)) * 0.005, 1e-9):
                row["값"] = int(signed) if signed.is_integer() else signed
                fields.setdefault("부호보정근거", raw)
                return


def _normalize_year(row: dict[str, Any], fields: dict[str, Any]) -> None:
    year = row.get("연도")
    if isinstance(year, float) and year.is_integer():
        row["연도"] = int(year)
        return
    if isinstance(year, int) and not isinstance(year, bool):
        return
    text = _text(year)
    if not text:
        row["연도"] = None
        return
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        row["연도"] = int(text)
        return
    if _RANGE_RE.search(text):
        fields.setdefault("기간원문", text)
    row["연도"] = None


def _is_total_label(value: Any) -> bool:
    text = _compact_key(value)
    return bool(text) and (
        text in {"합계", "총계", "전체", "총괄"}
        or text.startswith("전체")
        or text.endswith("합계")
        or text.endswith("총계")
    )


def _atomic_label(parent_label: str, label: str) -> str:
    if label == "누계":
        return f"{parent_label}(누계)" if parent_label else "누계"
    if label == "신규":
        return f"{parent_label}(신규)" if parent_label else "신규"
    return label


def normalize_visual_table_rows(
    rows: Sequence[dict[str, Any]],
    *,
    chart_type: str = "",
    title: str = "",
) -> list[dict[str, Any]]:
    """Enforce one explicit quantitative value per row and preserve visual provenance."""
    normalized: list[dict[str, Any]] = []
    table_like = _compact_key(chart_type) in {"표", "이미지표", "table"}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        fields = dict(row.get("fields")) if isinstance(row.get("fields"), dict) else {}
        _normalize_year(row, fields)
        _correct_visible_sign(row, fields)

        value_source = normalize_value_source(
            row.get("값근거") or fields.get("값근거") or fields.get("value_source")
        )
        if value_source == "unknown" and fields.get("estimated") is True:
            value_source = "axis_estimate"
        if value_source == "unknown" and table_like:
            value_source = "table_cell"
        fields["값근거"] = value_source

        item = _text(row.get("항목"))
        fields.setdefault("집계수준", "합계" if _is_total_label(item) else "세부")
        if title:
            fields.setdefault("합계그룹", title)

        generated: list[dict[str, Any]] = []
        measures: list[tuple[str, str, str, float, str]] = []
        for key, raw_value in list(fields.items()):
            spec = _MEASURE_FIELDS.get(_compact_key(key))
            if spec is None:
                continue
            number, parsed_unit = _parse_number_and_unit(raw_value)
            if number is None:
                continue
            label, value_role = spec
            measures.append((key, label, value_role, number, parsed_unit))

        # All quantitative fields must be removed before any generated row is
        # copied. Otherwise the first atomic row can still contain the other
        # quantitative values inside fields.
        categorical_fields = dict(fields)
        for key, *_rest in measures:
            categorical_fields.pop(key, None)

        for _key, label, value_role, number, parsed_unit in measures:
            generated_fields = dict(categorical_fields)
            generated_fields["값역할"] = value_role
            generated_fields["원본항목"] = item
            generated_fields["값근거"] = value_source
            generated_fields["집계수준"] = categorical_fields.get("집계수준", "세부")
            generated.append({
                "연도": row.get("연도"),
                "항목": _atomic_label(item, label),
                "종류": row.get("종류"),
                "값": number,
                "단위": parsed_unit or row.get("단위"),
                "fields": generated_fields,
            })

        row["fields"] = categorical_fields
        if row.get("값") is not None or not generated:
            _attach_quantity_metadata(row, categorical_fields)
            normalized.append(row)
        for generated_row in generated:
            generated_fields = generated_row.get("fields")
            if isinstance(generated_fields, dict):
                _attach_quantity_metadata(generated_row, generated_fields)
        normalized.extend(generated)

    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in normalized:
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        structural_key = json.dumps(
            {
                key: fields.get(key)
                for key in (
                    "구조역할", "상위항목", "관계", "순서", "단계",
                    "담당주체", "노드ID", "연결노드ID", "설명",
                )
                if fields.get(key) not in (None, "")
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        key = (
            normalize_comparison_text(row.get("항목")),
            normalize_comparison_text(row.get("연도")),
            normalize_comparison_text(row.get("값")),
            normalize_comparison_text(row.get("단위"), "단위"),
            normalize_comparison_text(fields.get("값역할")),
            structural_key,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(row)
    return deduplicated


def numeric_values(rows: Sequence[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("값")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                values.append(number)
    return values
