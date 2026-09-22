"""표 캡션·섹션 문맥으로 최종 행의 시트 배치를 검증한다."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

import config
from utils.document_objects import DocumentObject, build_document_objects
from utils.evidence_merge import normalize_evidence_ids
from utils.pdf_reader import PDFContent


_SEMANTIC_TERMS: dict[str, tuple[str, ...]] = {
    "document_meta": (
        "계획의 범위", "계획 기간", "계획기간", "공간적 범위", "시간적 범위", "발간기관",
    ),
    "plan_overview": (
        "계획 수립 배경", "수립 배경", "추진 경과", "수립 절차", "법적 근거", "계획 개요",
    ),
    "regional_conditions": (
        "지역 여건", "지역여건", "기후 특성", "인구 현황", "산업 현황", "에너지 소비 현황",
        "자동차 등록 현황",
    ),
    "emissions_regional": (
        "지역 온실가스 인벤토리", "지역 인벤토리", "지역 배출량 현황", "직접배출량 현황",
        "간접배출량 현황",
    ),
    "emissions_management": (
        "관리권한 배출량", "관리 권한 배출량", "관리권한 인벤토리", "관리 권한 인벤토리",
    ),
    "emissions_forecast": (
        "온실가스 배출량 전망", "배출량 전망", "배출 전망", "기준전망", "전망 결과",
        "bau 시나리오", "bau 전망",
    ),
    "reduction_targets": (
        "온실가스 감축 목표", "온실가스 감축목표", "부문별 감축 목표", "부문별 감축목표",
        "목표 배출량", "감축 경로",
    ),
    "vision_strategy": (
        "비전 및 전략", "비전과 전략", "추진 전략", "추진전략", "비전 체계",
    ),
    "mitigation_projects": (
        "온실가스 감축사업", "감축 사업 목록", "감축사업 목록", "세부 감축사업", "감축사업 총괄",
    ),
    "annual_implementation": (
        "연차별 이행계획", "연차별 추진계획", "연도별 이행계획", "연도별 목표물량",
    ),
    "quantitative_reductions": (
        "정량 감축량", "정량감축량", "예상 감축량", "예상감축량", "감축원단위",
    ),
    "financial_plan": (
        "재정 투자계획", "재정투자계획", "재원 조달계획", "연도별 예산", "소요 예산",
    ),
    "foundation_measures": (
        "대응기반 강화", "대응 기반 강화", "기후위기 적응", "녹색성장", "교육 및 홍보",
        "기후변화 감시", "기후 전망", "기후변화 영향", "취약성 평가", "기후 리스크",
        "위험도", "ssp 시나리오", "rcp 시나리오", "재난방지",
    ),
    "governance_feedback": (
        "이행관리 및 환류", "이행관리 환류", "거버넌스", "추진체계", "환류 체계",
    ),
    "monitoring_performance": (
        "추진상황 점검", "이행실적", "점검 실적", "점검실적", "달성 여부",
    ),
    "changes_actions": (
        "변경 과제", "변경과제", "변경 사항", "지연 미달성", "지연·미달성", "조치계획",
    ),
}

_CURRENT_EMISSION_SHEETS = {"emissions_regional", "emissions_management"}
_NON_SEMANTIC_KEYS = {
    "_row_id", "_entity_id", "출처페이지", "출처페이지추정", "데이터상태",
}
_PROVENANCE_COPY_FIELDS = (
    "출처페이지",
    "출처페이지추정",
    "근거ID",
    "근거ID목록",
    "source_evidence_id",
    "source_evidence_ids",
    "객체ID",
    "근거객체ID",
    "물리객체ID",
    "derivation_type",
    "데이터상태",
)
_DERIVATION_PRIORITY = {
    "explicit": 0,
    "normalized": 1,
    "calculated": 2,
    "inferred": 3,
    "external_lookup": 4,
}
_DATA_STATUS_PRIORITY = {
    "reported": 0,
    "gap_fill": 1,
    "visual_only": 2,
    "calculated": 3,
    "conflicting": 4,
}


def _normalize(value: Any) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[\s\[\](){}'\"“”‘’·ㆍ.:：;,_/\\|%-]+", "", text)


def _parse_pages(value: Any, page_count: int) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        tokens = [str(item) for item in value]
    else:
        tokens = [str(value or "")]
    pages: list[int] = []
    for token in tokens:
        for match in re.findall(r"(?:p|P)?\s*(\d{1,4})", token):
            page = int(match)
            if 1 <= page <= page_count and page not in pages:
                pages.append(page)
    return sorted(pages)


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class SemanticTarget:
    sheet_key: str
    score: float
    margin: float
    keywords: tuple[str, ...]
    evidence: str


@dataclass(frozen=True, slots=True)
class RoutingAction:
    source_sheet: str
    predicted_sheet: str
    status: str
    action: str
    source_pages: tuple[int, ...]
    evidence: str
    keywords: tuple[str, ...]
    row_summary: str

    def validation_row(self, municipality: str) -> dict[str, str]:
        source_name = config.SHEET_KEY_TO_NAME.get(self.source_sheet, self.source_sheet)
        target_name = config.SHEET_KEY_TO_NAME.get(self.predicted_sheet, self.predicted_sheet)
        return {
            "지자체명": municipality,
            "심각도": "정보" if self.status in {"재분류", "중복제거"} else "경고",
            "영역": "시트의미검증",
            "항목": f"{source_name} → {target_name}",
            "문제내용": (
                f"{self.status}; p{','.join(str(page) for page in self.source_pages)}; "
                f"{self.evidence[:180]}"
            ),
            "권장조치": (
                "결정론적 재분류 완료"
                if self.status in {"재분류", "중복제거"}
                else "표 캡션·섹션과 행의 의미를 대조해 시트 이동 여부 확인"
            ),
        }


@dataclass(slots=True)
class SemanticRoutingReport:
    checked_rows: int = 0
    evaluable_rows: int = 0
    mismatches_before: int = 0
    mismatches_after: int = 0
    reclassified_rows: int = 0
    removed_duplicates: int = 0
    ambiguous_rows: int = 0
    actions: list[RoutingAction] = field(default_factory=list)

    @property
    def error_rate_before(self) -> float:
        return self.mismatches_before / self.evaluable_rows if self.evaluable_rows else 0.0

    @property
    def error_rate(self) -> float:
        return self.mismatches_after / self.evaluable_rows if self.evaluable_rows else 0.0

    def metrics(self) -> dict[str, Any]:
        return {
            "routing_rows_checked": self.checked_rows,
            "routing_rows_evaluable": self.evaluable_rows,
            "routing_mismatches_before": self.mismatches_before,
            "routing_mismatches_after": self.mismatches_after,
            "routing_reclassified_rows": self.reclassified_rows,
            "routing_removed_duplicates": self.removed_duplicates,
            "routing_ambiguous_rows": self.ambiguous_rows,
            "routing_error_rate_before": round(self.error_rate_before, 4),
            "routing_error_rate": round(self.error_rate, 4),
        }

    def summary_text(self) -> str:
        return (
            f"검사 {self.checked_rows}행, 의미판정 가능 {self.evaluable_rows}행, "
            f"오배치 {self.mismatches_before}→{self.mismatches_after}행, "
            f"재분류 {self.reclassified_rows}행, 중복제거 {self.removed_duplicates}행, "
            f"잔여 오류율 {self.error_rate:.1%}"
        )

    def validation_issues(self, municipality: str) -> list[dict[str, str]]:
        rows = [{
            "지자체명": municipality,
            "심각도": "정보" if not self.mismatches_after else "경고",
            "영역": "시트의미검증",
            "항목": "캡션·섹션 기반 시트 라우팅",
            "문제내용": self.summary_text(),
            "권장조치": (
                "잔여 오배치 의심 행 없음"
                if not self.mismatches_after
                else "오배치 의심 행의 표 캡션·섹션과 출처페이지를 확인"
            ),
        }]
        unresolved = [action for action in self.actions if action.status == "오배치의심"]
        rows.extend(action.validation_row(municipality) for action in unresolved[:30])
        if len(unresolved) > 30:
            rows.append({
                "지자체명": municipality,
                "심각도": "정보",
                "영역": "시트의미검증",
                "항목": "오배치 상세 생략",
                "문제내용": f"동일 유형 상세 {len(unresolved) - 30}건은 집계에만 반영했습니다.",
                "권장조치": "평가 JSON의 routing 상세 또는 출처페이지를 기준으로 표본 검토",
            })
        return rows


def _semantic_candidates(
    *,
    caption: str = "",
    section: str = "",
    nearby_text: str = "",
    page_text: str = "",
) -> list[SemanticTarget]:
    channels = (
        (_normalize(caption), 3.0, "캡션"),
        (_normalize(section), 2.0, "섹션"),
        (_normalize(nearby_text), 0.75, "근처본문"),
        (_normalize(page_text), 0.35, "페이지본문"),
    )
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, list[str]] = defaultdict(list)
    evidence_parts: dict[str, list[str]] = defaultdict(list)
    for sheet_key, terms in _SEMANTIC_TERMS.items():
        for term in terms:
            normalized_term = _normalize(term)
            if not normalized_term:
                continue
            for body, multiplier, channel_name in channels:
                if body and normalized_term in body:
                    scores[sheet_key] += multiplier
                    if term not in hits[sheet_key]:
                        hits[sheet_key].append(term)
                    marker = f"{channel_name}:{term}"
                    if marker not in evidence_parts[sheet_key]:
                        evidence_parts[sheet_key].append(marker)

    if not scores:
        return []
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates: list[SemanticTarget] = []
    for index, (sheet_key, score) in enumerate(ordered):
        runner_up = ordered[index + 1][1] if index + 1 < len(ordered) else 0.0
        candidates.append(SemanticTarget(
            sheet_key=sheet_key,
            score=score,
            margin=score - runner_up,
            keywords=tuple(hits[sheet_key]),
            evidence="; ".join(evidence_parts[sheet_key][:6]),
        ))
    return candidates


def infer_semantic_target(
    *,
    caption: str = "",
    section: str = "",
    nearby_text: str = "",
    page_text: str = "",
) -> SemanticTarget | None:
    """명시적인 캡션·섹션 신호가 충분할 때만 하나의 시트를 반환한다."""
    candidates = _semantic_candidates(
        caption=caption,
        section=section,
        nearby_text=nearby_text,
        page_text=page_text,
    )
    if not candidates:
        return None
    target = candidates[0]
    min_score = float(getattr(config, "SEMANTIC_ROUTING_MIN_SCORE", 5.0))
    min_margin = float(getattr(config, "SEMANTIC_ROUTING_MIN_MARGIN", 1.5))
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    margin = target.score - runner_up
    if target.score < min_score or margin < min_margin:
        return None
    return SemanticTarget(
        target.sheet_key,
        target.score,
        margin,
        target.keywords,
        target.evidence,
    )


def infer_semantic_targets(
    *,
    caption: str = "",
    section: str = "",
    nearby_text: str = "",
    page_text: str = "",
) -> list[SemanticTarget]:
    """한 객체가 복수 시트에 유효한 경우 허용 가능한 후보 집합을 반환한다."""
    candidates = _semantic_candidates(
        caption=caption,
        section=section,
        nearby_text=nearby_text,
        page_text=page_text,
    )
    if not candidates:
        return []
    min_score = float(getattr(config, "SEMANTIC_ROUTING_MIN_SCORE", 5.0))
    delta = max(
        0.0,
        float(getattr(config, "SEMANTIC_ROUTING_ALLOWED_SCORE_DELTA", 1.5)),
    )
    limit = max(1, int(getattr(config, "SEMANTIC_ROUTING_MAX_ALLOWED_TARGETS", 3)))
    top_score = candidates[0].score
    return [
        candidate
        for candidate in candidates
        if candidate.score >= min_score and top_score - candidate.score <= delta
    ][:limit]


def infer_object_semantic_target(obj: DocumentObject) -> SemanticTarget | None:
    return infer_semantic_target(
        caption=obj.caption,
        section=obj.section,
        nearby_text=obj.nearby_text,
    )


def infer_object_semantic_targets(obj: DocumentObject) -> list[SemanticTarget]:
    """객체 메타데이터의 사람 확정 허용 시트를 우선하고 문맥 후보를 보조로 쓴다."""
    raw_allowed = obj.metadata.get("allowed_sheet_keys") or obj.metadata.get("allowed_sheets")
    if isinstance(raw_allowed, str):
        raw_allowed = re.split(r"[,|;\n]+", raw_allowed)
    if isinstance(raw_allowed, (list, tuple, set)):
        name_to_key = {
            _normalize(name): key
            for key, name in getattr(config, "SHEET_KEY_TO_NAME", {}).items()
        }
        explicit: list[SemanticTarget] = []
        for value in raw_allowed:
            text = str(value or "").strip()
            key = text if text in getattr(config, "EXTRACTION_SHEETS", []) else name_to_key.get(_normalize(text))
            if key and all(item.sheet_key != key for item in explicit):
                explicit.append(SemanticTarget(
                    sheet_key=key,
                    score=100.0,
                    margin=100.0,
                    keywords=("사람확정",),
                    evidence="객체 메타데이터의 허용 시트",
                ))
        if explicit:
            return explicit
    return infer_semantic_targets(
        caption=obj.caption,
        section=obj.section,
        nearby_text=obj.nearby_text,
    )


def _page_targets(
    document: PDFContent,
    document_objects: Sequence[DocumentObject] | None = None,
) -> dict[int, SemanticTarget | None]:
    by_page: dict[int, list[DocumentObject]] = defaultdict(list)
    source_objects = document_objects if document_objects is not None else build_document_objects(document.pages)
    for obj in source_objects:
        if obj.object_type in {"table", "chart", "figure"}:
            by_page[obj.page_number].append(obj)

    targets: dict[int, SemanticTarget | None] = {}
    for page in document.pages:
        object_targets = [
            target
            for obj in by_page.get(page.page_number, [])
            if (target := infer_object_semantic_target(obj)) is not None
        ]
        if object_targets:
            target_sheets = {target.sheet_key for target in object_targets}
            if len(target_sheets) == 1:
                best_sheet = next(iter(target_sheets))
                best = max(
                    (target for target in object_targets if target.sheet_key == best_sheet),
                    key=lambda target: (target.score, target.margin),
                )
                targets[page.page_number] = best
                continue
            targets[page.page_number] = None
            continue
        targets[page.page_number] = infer_semantic_target(page_text=page.text[:5000])
    return targets


def _row_target(
    row: dict[str, Any],
    page_targets: dict[int, SemanticTarget | None],
    page_count: int,
) -> tuple[SemanticTarget | None, tuple[int, ...]]:
    pages = tuple(_parse_pages(row.get("출처페이지"), page_count))
    candidates = [page_targets.get(page) for page in pages]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if not candidates:
        return None, pages
    sheets = {candidate.sheet_key for candidate in candidates}
    if len(sheets) != 1:
        return None, pages
    return max(candidates, key=lambda candidate: (candidate.score, candidate.margin)), pages


def _plan_start_year(final_data: dict[str, Any]) -> int | None:
    rows = final_data.get("document_meta", [])
    if not isinstance(rows, list):
        return None
    years = [
        year
        for row in rows
        if isinstance(row, dict)
        if (year := _to_int(row.get("계획시작연도"))) is not None
    ]
    return min(years) if years else None


def _row_summary(row: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in row.items()
        if key not in _NON_SEMANTIC_KEYS and value not in (None, "")
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= 300 else text[:299] + "…"


def _copy_provenance(source: dict[str, Any], target: dict[str, Any]) -> None:
    for field in _PROVENANCE_COPY_FIELDS:
        value = source.get(field)
        if value not in (None, "", [], ()):
            target[field] = value

    evidence_ids = normalize_evidence_ids(
        source.get("근거ID"),
        source.get("근거ID목록"),
        source.get("source_evidence_id"),
        source.get("source_evidence_ids"),
    )
    if evidence_ids:
        target["근거ID"] = str(source.get("근거ID") or evidence_ids[0])
        if len(evidence_ids) > 1:
            target["근거ID목록"] = evidence_ids


def _page_numbers(value: Any) -> list[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    pages: list[int] = []
    for item in values:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)) and int(item) > 0:
            page = int(item)
            if page not in pages:
                pages.append(page)
            continue
        for token in re.findall(r"(?:p|P)?\s*(\d{1,4})", str(item or "")):
            page = int(token)
            if page > 0 and page not in pages:
                pages.append(page)
    return pages


def _merge_page_provenance(current: Any, incoming: Any) -> Any:
    if current in (None, "", [], ()):
        return incoming
    if incoming in (None, "", [], ()):
        return current
    pages = sorted(set(_page_numbers(current)) | set(_page_numbers(incoming)))
    if not pages:
        return current
    current_pages = _page_numbers(current)
    if len(pages) == 1 and current_pages == pages:
        return current
    return ",".join(str(page) for page in pages)


def _more_conservative_value(
    current: Any,
    incoming: Any,
    priorities: dict[str, int],
) -> Any:
    current_text = str(current or "").strip()
    incoming_text = str(incoming or "").strip()
    if not current_text:
        return incoming
    if not incoming_text:
        return current
    if priorities.get(incoming_text, -1) > priorities.get(current_text, -1):
        return incoming
    return current


def _merge_forecast_provenance(target: dict[str, Any], source: dict[str, Any]) -> None:
    target["출처페이지"] = _merge_page_provenance(
        target.get("출처페이지"), source.get("출처페이지")
    )
    if source.get("출처페이지추정") not in (None, "", [], ()):
        target["출처페이지추정"] = _merge_page_provenance(
            target.get("출처페이지추정"), source.get("출처페이지추정")
        )

    evidence_ids = normalize_evidence_ids(
        target.get("근거ID"),
        target.get("근거ID목록"),
        target.get("source_evidence_id"),
        target.get("source_evidence_ids"),
        source.get("근거ID"),
        source.get("근거ID목록"),
        source.get("source_evidence_id"),
        source.get("source_evidence_ids"),
    )
    if evidence_ids:
        if not target.get("근거ID"):
            target["근거ID"] = evidence_ids[0]
        if len(evidence_ids) > 1:
            target["근거ID목록"] = evidence_ids

    target["derivation_type"] = _more_conservative_value(
        target.get("derivation_type"),
        source.get("derivation_type"),
        _DERIVATION_PRIORITY,
    )
    target["데이터상태"] = _more_conservative_value(
        target.get("데이터상태"),
        source.get("데이터상태"),
        _DATA_STATUS_PRIORITY,
    )
    for field in ("객체ID", "근거객체ID", "물리객체ID"):
        if not target.get(field) and source.get(field):
            target[field] = source[field]


def _forecast_row(source_sheet: str, row: dict[str, Any], target: SemanticTarget) -> dict[str, Any]:
    sector = row.get("부문") if source_sheet == "emissions_regional" else row.get("관리부문")
    scenario = "BAU" if any("bau" in keyword.casefold() for keyword in target.keywords) else "기준전망"
    mapped = {
        "지자체명": row.get("지자체명"),
        "시나리오": scenario,
        "전망방법코드": None,
        "전망방법원문": None,
        "부문": sector,
        "세부부문": row.get("세부부문"),
        "연도": row.get("연도"),
        "전망값": row.get("배출량"),
        "단위": row.get("단위"),
        "주요가정": None,
        "출처페이지": row.get("출처페이지"),
        "데이터상태": row.get("데이터상태") or "reported",
    }
    mapped = {key: value for key, value in mapped.items() if value is not None}
    _copy_provenance(row, mapped)
    return mapped


def _forecast_signature(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        _normalize(row.get(field))
        for field in ("지자체명", "부문", "세부부문", "연도", "전망값", "단위")
    )


def validate_and_reclassify(
    final_data: dict[str, Any],
    document: PDFContent,
    *,
    auto_reclassify: bool | None = None,
    document_objects: Sequence[DocumentObject] | None = None,
) -> SemanticRoutingReport:
    """
    시트 의미를 검증하고 안전한 미래연도 배출전망 오배치만 자동 이동한다.

    과거 기준연도 값은 전망표 안에 함께 있어도 현황 행으로 유효할 수 있으므로
    계획시작연도보다 이른 행은 오배치 분모와 자동 이동에서 제외한다.
    """
    report = SemanticRoutingReport()
    page_targets = _page_targets(document, document_objects)
    plan_start = _plan_start_year(final_data)
    if auto_reclassify is None:
        auto_reclassify = bool(getattr(config, "SEMANTIC_ROUTING_AUTO_RECLASSIFY", True))

    removals: dict[str, set[int]] = defaultdict(set)
    forecast_rows = final_data.get("emissions_forecast", [])
    if not isinstance(forecast_rows, list):
        forecast_rows = []
        final_data["emissions_forecast"] = forecast_rows
    forecast_by_signature: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in forecast_rows:
        if isinstance(row, dict):
            forecast_by_signature.setdefault(_forecast_signature(row), row)
    source_snapshots = {
        sheet_key: list(rows)
        for sheet_key in getattr(config, "EXTRACTION_SHEETS", [])
        if isinstance((rows := final_data.get(sheet_key, [])), list)
    }

    for source_sheet, rows in source_snapshots.items():
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            report.checked_rows += 1
            target, source_pages = _row_target(row, page_targets, document.total_pages)
            if target is None:
                report.ambiguous_rows += 1
                continue

            year = _to_int(row.get("연도"))
            historical_on_forecast_page = (
                source_sheet in _CURRENT_EMISSION_SHEETS
                and target.sheet_key == "emissions_forecast"
                and plan_start is not None
                and year is not None
                and year < plan_start
            )
            if historical_on_forecast_page:
                continue

            report.evaluable_rows += 1
            if source_sheet == target.sheet_key:
                continue

            report.mismatches_before += 1
            status = "오배치의심"
            action = "검토필요"
            can_move_forecast = (
                auto_reclassify
                and source_sheet in _CURRENT_EMISSION_SHEETS
                and target.sheet_key == "emissions_forecast"
                and plan_start is not None
                and year is not None
                and year >= plan_start
                and row.get("배출량") not in (None, "")
                and (
                    row.get("부문")
                    if source_sheet == "emissions_regional"
                    else row.get("관리부문")
                ) not in (None, "")
                and any(
                    marker in target.evidence
                    for marker in ("캡션:", "섹션:")
                )
            )
            if can_move_forecast:
                mapped = _forecast_row(source_sheet, row, target)
                signature = _forecast_signature(mapped)
                removals[source_sheet].add(index)
                if signature in forecast_by_signature:
                    _merge_forecast_provenance(forecast_by_signature[signature], mapped)
                    report.removed_duplicates += 1
                    status = "중복제거"
                    action = "근거 병합 후 원본시트에서 제거"
                else:
                    forecast_rows.append(mapped)
                    forecast_by_signature[signature] = mapped
                    report.reclassified_rows += 1
                    status = "재분류"
                    action = "05_배출전망으로 이동"
            else:
                report.mismatches_after += 1

            report.actions.append(RoutingAction(
                source_sheet=source_sheet,
                predicted_sheet=target.sheet_key,
                status=status,
                action=action,
                source_pages=source_pages,
                evidence=target.evidence,
                keywords=target.keywords,
                row_summary=_row_summary(row),
            ))

    for sheet_key, indices in removals.items():
        rows = final_data.get(sheet_key, [])
        if isinstance(rows, list):
            final_data[sheet_key] = [
                row for index, row in enumerate(rows) if index not in indices
            ]
    final_data["emissions_forecast"] = forecast_rows
    return report
