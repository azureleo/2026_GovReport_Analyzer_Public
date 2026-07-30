"""표 캡션·섹션 문맥으로 최종 행의 시트 배치를 검증한다."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import config
from utils.document_objects import DocumentObject, build_document_objects
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


def infer_semantic_target(
    *,
    caption: str = "",
    section: str = "",
    nearby_text: str = "",
    page_text: str = "",
) -> SemanticTarget | None:
    """명시적인 캡션·섹션 신호가 충분할 때만 하나의 시트를 반환한다."""
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
        return None
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    sheet_key, score = ordered[0]
    runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
    margin = score - runner_up
    min_score = float(getattr(config, "SEMANTIC_ROUTING_MIN_SCORE", 5.0))
    min_margin = float(getattr(config, "SEMANTIC_ROUTING_MIN_MARGIN", 1.5))
    if score < min_score or margin < min_margin:
        return None
    evidence = "; ".join(evidence_parts[sheet_key][:6])
    return SemanticTarget(sheet_key, score, margin, tuple(hits[sheet_key]), evidence)


def infer_object_semantic_target(obj: DocumentObject) -> SemanticTarget | None:
    return infer_semantic_target(
        caption=obj.caption,
        section=obj.section,
        nearby_text=obj.nearby_text,
    )


def _page_targets(document: PDFContent) -> dict[int, SemanticTarget | None]:
    by_page: dict[int, list[DocumentObject]] = defaultdict(list)
    for obj in build_document_objects(document.pages):
        if obj.object_type in {"table", "figure"}:
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
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text if len(text) <= 300 else text[:299] + "…"


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
    return {key: value for key, value in mapped.items() if value is not None}


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
) -> SemanticRoutingReport:
    """
    시트 의미를 검증하고 안전한 미래연도 배출전망 오배치만 자동 이동한다.

    과거 기준연도 값은 전망표 안에 함께 있어도 현황 행으로 유효할 수 있으므로
    계획시작연도보다 이른 행은 오배치 분모와 자동 이동에서 제외한다.
    """
    report = SemanticRoutingReport()
    page_targets = _page_targets(document)
    plan_start = _plan_start_year(final_data)
    if auto_reclassify is None:
        auto_reclassify = bool(getattr(config, "SEMANTIC_ROUTING_AUTO_RECLASSIFY", True))

    removals: dict[str, set[int]] = defaultdict(set)
    forecast_rows = final_data.get("emissions_forecast", [])
    if not isinstance(forecast_rows, list):
        forecast_rows = []
        final_data["emissions_forecast"] = forecast_rows
    forecast_signatures = {
        _forecast_signature(row)
        for row in forecast_rows
        if isinstance(row, dict)
    }
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
                if signature in forecast_signatures:
                    report.removed_duplicates += 1
                    status = "중복제거"
                    action = "원본시트에서 제거"
                else:
                    forecast_rows.append(mapped)
                    forecast_signatures.add(signature)
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
