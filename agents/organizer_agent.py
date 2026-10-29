"""
에이전트 3: 정리·정제 에이전트

16개 시트 구조에 맞춰 원시 추출 결과를 정제합니다.
가이드라인(carbon_guideline.md) 기준 표준 코드에 맞춰 정규화하고,
중복 제거, 이상치 처리, 단위 검증을 수행합니다.
"""

import logging
import json
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
}

_AXIS_ENUM = {
    "배출유형": {"직접배출", "간접배출", "흡수원"},
    "직간접구분": {"직접", "간접", "흡수", "직접+간접"},
}
_DIRECT_INDIRECT_TYPE_MAP = {
    "direct": "직접", "직접배출": "직접", "직접 배출": "직접",
    "indirect": "간접", "간접배출": "간접", "간접 배출": "간접",
    "sink": "흡수", "absorption": "흡수", "흡수원": "흡수", "흡수량": "흡수",
    "direct+indirect": "직접+간접", "직접+간접배출": "직접+간접",
    "직접 및 간접": "직접+간접", "직접 + 간접": "직접+간접",
}


def dedup_key_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value if value is not None else "")).casefold()
    return re.sub(r"\s+", "", text)


def normalize_direct_indirect_type(value: Any) -> str:
    text = str(value or "").strip()
    return _DIRECT_INDIRECT_TYPE_MAP.get(text.casefold(), text)


def normalize_project_id(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper().replace("_", "-")
    return re.sub(r"(?<!\d)0+(\d+)", lambda match: str(int(match.group(0))), text)


def normalize_provenance_pages(value: Any) -> str:
    pages: set[int] = set()
    def visit(item: Any) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
            return
        text = re.sub(r"(?:표|그림|figure|fig\.?)\s*\d+\s*[-–—.]\s*\d+", " ", str(item), flags=re.I)
        for start, end in re.findall(r"(\d+)\s*(?:~|-|–|—)\s*(\d+)", text):
            low, high = sorted((int(start), int(end)))
            if high - low < 5000:
                pages.update(range(low, high + 1))
        pages.update(int(number) for number in re.findall(r"\d+", text))
    visit(value)
    return ",".join(str(page) for page in sorted(pages) if page > 0)


def _normalize_forecast_scope(value: Any) -> str:
    """Only fold explicit scope notation; locality and page numbers are not evidence."""
    raw = str(value or "").strip()
    text = re.sub(r"\s+", "", raw)
    if (re.fullmatch(r"관리권한(?:내)?(?:온실가스)?(?:배출량|배출)?(?:전망)?", text)
            or text in {"관리권한범위", "관리권한인벤토리"}):
        return "관리권한"
    if text in {"지역전체", "지역전체배출량", "지역전체전망", "시전체", "도전체", "지역단위"}:
        return "지역전체"
    return raw


def _axis_contract(row: dict, field: str) -> None:
    original = row.get(field)
    normalized = (_normalize(original, _TYPE_MAP) if field == "배출유형"
                  else normalize_direct_indirect_type(original))
    if normalized and normalized not in _AXIS_ENUM[field]:
        row.setdefault("_axis_original", {})[field] = original
        row.setdefault("_axis_validation", []).append({
            "field": field, "original": original, "normalized": normalized,
            "reason": "axis_outside_allowed_enum", "rule": "axis_enum_v1",
        })
        normalized = ""
    row[field] = normalized
    if normalized:
        return
    # Explicit row labels or source header are required. Sector alone never fills an axis.
    source = " ".join(str(row.get(key) or "") for key in
                      ("세부부문", "_원문헤더", "원문헤더", "_축근거", "축근거"))
    folded = re.sub(r"\s+", "", source)
    direct = "직접배출" in folded or "직접+간접" in folded
    indirect = "간접배출" in folded or "직접+간접" in folded or bool(re.search(r"[（(](?:전력|열)[）)]", folded))
    sink = "흡수량" in folded or "흡수원" in folded
    recovered = ""
    if sink and not direct and not indirect:
        recovered = "흡수원" if field == "배출유형" else "흡수"
    elif direct and indirect and field == "직간접구분":
        recovered = "직접+간접"
    elif direct != indirect:
        recovered = ("직접배출" if direct else "간접배출") if field == "배출유형" else ("직접" if direct else "간접")
    if recovered:
        row[field] = recovered
        row.setdefault("_axis_validation", []).append({
            "field": field, "recovered": recovered, "reason": "explicit_source_axis_token",
            "evidence": source, "rule": "axis_grounded_refill_v1",
        })


_PROVENANCE_FIELDS = {"출처페이지", "출처페이지추정", "근거ID", "근거ID목록", "객체ID", "근거객체ID",
                      "source_evidence_id", "source_evidence_ids", "source_object_ids", "object_id", "object_ids",
                      "evidence_id", "evidence_ids", "물리객체ID", "추출원천", "추출엔진", "OCR보완여부",
                      "출처계보", "원본근거ID", "원본객체ID", "source_engines", "ocr_source_file", "ocr_source_line",
                      "_row_id", "_entity_id", "_추출원천"}


def _merge_row_evidence(target: dict, source: dict) -> None:
    records = {}
    for row in (target, source):
        lineage = row.get("_extraction_sources") or row.get("출처계보") or []
        if isinstance(lineage, str):
            try:
                lineage = json.loads(lineage)
            except ValueError:
                lineage = [{"legacy_lineage_unparsed": lineage}]
        if not isinstance(lineage, list):
            lineage = [lineage]
        for record in lineage:
            records[json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)] = deepcopy(record)
    if records:
        target["_extraction_sources"] = [records[key] for key in sorted(records)]
        target["출처계보"] = json.dumps(target["_extraction_sources"], ensure_ascii=False, sort_keys=True)
    for field in ("source_object_ids", "object_ids", "evidence_ids", "source_evidence_ids", "source_engines"):
        values = set()
        for row in (target, source):
            value = row.get(field)
            values.update(str(item) for item in (value if isinstance(value, (list, tuple, set)) else [value])
                          if item not in (None, ""))
        if values:
            target[field] = sorted(values)
    if source.get("OCR보완여부"):
        target["OCR보완여부"] = True
    pages = normalize_provenance_pages([target.get("출처페이지"), source.get("출처페이지")])
    if pages:
        target["출처페이지"] = pages
    ids: set[str] = set()
    for row in (target, source):
        for field in ("근거ID", "근거ID목록", "source_evidence_id", "source_evidence_ids"):
            value = row.get(field)
            for item in value if isinstance(value, (list, tuple, set)) else [value]:
                if item not in (None, ""):
                    ids.add(str(item))
    if ids:
        target["근거ID"] = sorted(ids)[0]
        if len(ids) > 1:
            target["근거ID목록"] = sorted(ids)
    for key, value in source.items():
        if target.get(key) in (None, "") and value not in (None, ""):
            target[key] = deepcopy(value)


def _contract_dedup(rows: list[dict], key_fields: list[str], value_field: str) -> list[dict]:
    """Keep value conflicts and axis-invalid rows separate; merge only equal facts."""
    seen: dict[tuple, list[dict]] = {}
    ordered = sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
    for row in ordered:
        key = tuple(dedup_key_text(row.get(field)) for field in key_fields)
        key += (dedup_key_text(row.get(value_field)),
                json.dumps(row.get("_axis_original", {}), ensure_ascii=False, sort_keys=True))
        candidates = seen.setdefault(key, [])
        compatible = [candidate for candidate in candidates if all(
            dedup_key_text(candidate.get(field)) == dedup_key_text(value)
            for field, value in row.items()
            if field not in _PROVENANCE_FIELDS and not field.startswith("_")
            and value not in (None, "") and candidate.get(field) not in (None, "")
        )]
        if len(compatible) == 1:
            _merge_row_evidence(compatible[0], row)
        else:
            candidates.append(row)
    groups: dict[tuple, list[dict]] = {}
    for key, members in seen.items():
        groups.setdefault(key[:-2], []).extend(members)
    for members in groups.values():
        values = {dedup_key_text(row.get(value_field)) for row in members}
        if len(members) > 1:
            for row in members:
                row["_merge_conflict"] = {"reason": "same_entity_different_value" if len(values) > 1 else "same_entity_different_field", "values": sorted(values)}
    return [row for members in seen.values() for row in members]


def deduplicate_forecast_rows(rows: list[dict]) -> list[dict]:
    """Resolve blank scopes from all candidates at once, independent of input order."""
    fields = ["지자체명", "전망범위", "시나리오", "부문", "세부부문", "연도", "단위"]
    rows = _contract_dedup(rows, fields, "전망값")
    known = [row for row in rows if row.get("전망범위") in {"지역전체", "관리권한"}]
    kept: list[dict] = []
    for row in rows:
        if row.get("전망범위") or row.get("_axis_original"):
            kept.append(row)
            continue
        identity_fields = [field for field in fields if field != "전망범위"] + ["전망값"]
        keyed_candidates = [candidate for candidate in known if all(
            dedup_key_text(candidate.get(field)) == dedup_key_text(row.get(field))
            for field in identity_fields
        )]
        candidates = [candidate for candidate in keyed_candidates if all(
            dedup_key_text(candidate.get(field)) == dedup_key_text(value)
            for field, value in row.items()
            if field not in _PROVENANCE_FIELDS and not field.startswith("_")
            and value not in (None, "") and candidate.get(field) not in (None, "")
        )]
        scopes = {candidate["전망범위"] for candidate in candidates}
        if len(scopes) == 1 and len(candidates) == 1:
            _merge_row_evidence(candidates[0], row)
        else:
            if len(scopes) > 1:
                row["_forecast_scope_review"] = {"reason": "ambiguous_blank_scope", "candidate_scopes": sorted(scopes)}
            elif keyed_candidates and not candidates:
                row["_forecast_scope_review"] = {"reason": "scope_candidate_payload_conflict",
                                                 "candidate_scopes": sorted({item["전망범위"] for item in keyed_candidates})}
            kept.append(row)
    return sorted(kept, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))


def collect_key_blank_stats(data: dict) -> dict[str, dict]:
    contracts = {
        "emissions_regional": ("배출유형", "부문", "연도"),
        "emissions_management": ("관리부문", "직간접구분", "연도"),
        "emissions_forecast": ("전망범위", "시나리오", "부문", "연도"),
    }
    result = {}
    for sheet, fields in contracts.items():
        rows = [row for row in data.get(sheet, []) if isinstance(row, dict)]
        blanks = {field: sum(row.get(field) in (None, "") for row in rows) for field in fields}
        result[sheet] = {"rows": len(rows), "blank_counts": blanks,
                         "blank_rates": {field: count / len(rows) if rows else 0.0 for field, count in blanks.items()}}
    return result

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


to_float = _to_float


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
        _axis_contract(row, "배출유형")
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
    rows = _filter_empty_rows(rows, ["배출량"])
    return _contract_dedup(rows, ["지자체명", "배출유형", "부문", "세부부문", "연도", "단위"], "배출량")


def _clean_emissions_management(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["관리부문"] = _normalize_sector(row.get("관리부문", ""))
        _axis_contract(row, "직간접구분")
        row["연도"] = _to_int(row.get("연도"))
        row["배출량"] = _to_float(row.get("배출량"))
    rows = [r for r in rows if r.get("관리부문")]
    rows = _filter_empty_rows(rows, ["배출량"])
    return _contract_dedup(rows, ["지자체명", "관리부문", "세부부문", "직간접구분", "연도", "단위"], "배출량")


def _clean_emissions_forecast(rows: list[dict], municipality: str) -> list[dict]:
    for row in rows:
        row["지자체명"] = row.get("지자체명") or municipality
        row["전망범위"] = _normalize_forecast_scope(row.get("전망범위"))
        if row["전망범위"] not in {"", "지역전체", "관리권한"}:
            row["_forecast_scope_review"] = {"reason": "scope_outside_allowed_enum", "original": row["전망범위"]}
            row["전망범위"] = ""
        row["연도"] = _to_int(row.get("연도"))
        row["전망값"] = _to_float(row.get("전망값"))
        method_raw = row.get("전망방법원문", "")
        if not row.get("전망방법코드") and method_raw:
            for keyword, code in _FORECAST_METHOD_MAP.items():
                if keyword in method_raw:
                    row["전망방법코드"] = code
                    break
    rows = _filter_empty_rows(rows, ["전망값"])
    return deduplicate_forecast_rows(rows)


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
    # A management-only target and a target including additional reductions are
    # different facts even when their sector/goal year agree. Never fill a blank
    # scope from a neighbouring row, or let the first non-null value win.
    from utils.evidence_merge import merge_extraction_rows
    conflicts: list[dict] = []
    for row in rows:
        if row.get("_merge_conflict", {}).get("reason") == "reduction_target_value_conflict_retained":
            row.pop("_merge_conflict", None)
            row.pop("_merge_conflicts", None)
    result = merge_extraction_rows("reduction_targets", [], rows, conflicts)
    by_id = {record["conflict_id"]: record for record in conflicts}
    for row in result:
        if any(key in by_id for key in row.get("_merge_conflicts", [])):
            row["_merge_conflict"] = {
                "reason": "reduction_target_value_conflict_retained",
                "conflicts": [by_id[key] for key in sorted(set(row["_merge_conflicts"])) if key in by_id],
            }
    return result


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
            rows = [deepcopy(r) for r in rows if isinstance(r, dict)]
            cleaned[sheet_key] = cleaner(rows, municipality)

        # Carry independent output/diagnostic data through cleaning instead of silently
        # dropping OCR provenance or the main pipeline's existing validation records.
        for key, value in raw_data.items():
            if key not in cleaned:
                cleaned[key] = deepcopy(value)
        issues = cleaned.setdefault("validation_report", [])
        if not isinstance(issues, list):
            issues = []
            cleaned["validation_report"] = issues
        key_fields = {
            "emissions_regional": ("배출유형", "부문", "연도"),
            "emissions_management": ("관리부문", "직간접구분", "연도"),
            "emissions_forecast": ("전망범위", "시나리오", "부문", "연도"),
            "reduction_targets": ("목표범위", "기준연도", "목표연도"),
        }
        stats: dict[str, dict] = {}
        for sheet_key, fields in key_fields.items():
            rows = cleaned[sheet_key]
            blanks = {field: sum(row.get(field) in (None, "") for row in rows) for field in fields}
            stats[sheet_key] = {"rows": len(rows), "blank_counts": blanks,
                                "blank_rates": {field: count / len(rows) if rows else 0.0 for field, count in blanks.items()}}
            for row in rows:
                observations = list(row.get("_axis_validation", []))
                for field in ("_forecast_scope_review", "_merge_conflict"):
                    if row.get(field):
                        observations.append(row[field])
                for observation in observations:
                    issue = {
                        "지자체명": municipality, "심각도": "경고", "영역": "키계약검증",
                        "항목": "축 필드 enum 밖 값 제거" if observation.get("reason") == "axis_outside_allowed_enum" else observation.get("reason", "키계약"),
                        "문제내용": json.dumps(observation, ensure_ascii=False, sort_keys=True),
                        "권장조치": "원문·근거ID 확인", "대상시트키": sheet_key,
                        "대상시트": sheet_key, "사유": json.dumps(observation, ensure_ascii=False, sort_keys=True),
                        "출처페이지": row.get("출처페이지"), "근거ID": row.get("근거ID"),
                    }
                    if issue not in issues:
                        issues.append(issue)
        cleaned["_key_blank_stats"] = stats
        # Main did not have these cross-sheet guards. Keep broader contract changes
        # opt-in; incomplete emission rows discarded by main still gain an audit trail.
        from utils.semantic_contract_guard import apply_semantic_contract_guards
        drops = {}
        for sheet_key in key_fields:
            if sheet_key == "reduction_targets":
                continue  # Target warnings do not imply missing emission values.
            value_field = "전망값" if sheet_key == "emissions_forecast" else "배출량"
            excluded = [row for row in raw_data.get(sheet_key, []) if isinstance(row, dict)
                        and (_to_float(row.get(value_field)) is None
                             or (sheet_key == "emissions_management" and not row.get("관리부문")))]
            if excluded:
                drops[sheet_key] = excluded
        if getattr(config, "SEMANTIC_CONTRACT_GUARDS_ENABLED", False):
            quarantine = apply_semantic_contract_guards(cleaned, contract_drops=drops)
        else:
            quarantine = [{"sheet_key": key, "reason_codes": ["missing_key_contract"], "row": deepcopy(row)}
                          for key, rows in drops.items() for row in rows]
        all_quarantine = deepcopy(raw_data.get("_semantic_contract_quarantine", [])) + quarantine
        cleaned["_semantic_contract_quarantine"] = list({
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str): record
            for record in all_quarantine
        }.values())

        counts = {k: len(v) for k, v in cleaned.items() if isinstance(v, list) and v}
        print(f"[에이전트3 정리] 정제 완료: {counts}")

        self._final_data = cleaned
        return cleaned

    def get_excel_ready(self) -> dict:
        return {k: v for k, v in self._final_data.items() if isinstance(v, list) and not k.startswith("_")}

    def get_quarantine_records(self) -> list[dict]:
        return deepcopy(self._final_data.get("_semantic_contract_quarantine", []))

    def report(self) -> str:
        d = self._final_data
        counts = {k: len(v) for k, v in d.items() if isinstance(v, list) and v}
        return (
            f"[에이전트3 정리] 정제 완료\n"
            f"  - 지자체명: {d.get('municipality_name', '미확인')}\n"
            + "\n".join(f"  - {k}: {v}건" for k, v in counts.items())
        )
