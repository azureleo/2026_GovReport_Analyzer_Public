"""Deterministic context classifier for reduction-target row levels.

The classifier deliberately avoids municipality names, fixed pages, fixed years,
and workbook cell locations.  It combines row values with evidence/object metadata
and related-sheet context, then retags only when explicit semantic evidence clears
both a score and a margin gate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


_PROJECT_SHEETS = (
    "mitigation_projects",
    "annual_implementation",
    "quantitative_reductions",
)
_ANNUAL_SHEETS = ("annual_implementation", "quantitative_reductions")
_PROJECT_ID_FIELDS = ("관리번호", "과제ID", "사업ID", "사업코드")
_PROJECT_NAME_FIELDS = ("사업명", "과제명", "감축사업명", "사업명힌트", "_사업명힌트")
_NUMERIC_FIELDS = (
    "기준연도", "기준배출량", "목표연도", "배출전망", "목표감축량", "목표배출량", "감축률",
)
_VALUE_FIELDS = ("기준배출량", "배출전망", "목표감축량", "목표배출량", "감축률")
_EVIDENCE_FIELDS = (
    "근거ID", "근거ID목록", "source_evidence_id", "source_evidence_ids", "evidence_id",
)
_PHYSICAL_FIELDS = (
    "physical_object_id", "canonical_object_id", "근거객체ID", "source_object_ids",
)

def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


_PROJECT_ID_TERMS = tuple(map(_normalize_text, (
    "관리번호", "사업명", "과제명", "사업코드", "과제 ID",
)))
_PROJECT_EXECUTION_TERMS = tuple(map(_normalize_text, (
    "성과지표", "담당부서", "담당기관", "사업기간", "총사업비", "추진계획",
    "목표물량", "사업개요", "사업내용", "연차별 추진계획",
)))
_PROJECT_DESCRIPTOR_TERMS = tuple(map(_normalize_text, (
    "감축사업", "세부사업", "단위사업", "감축과제", "세부과제",
)))
_TARGET_SUMMARY_TERMS = tuple(map(_normalize_text, (
    "온실가스 감축목표", "부문별 감축목표", "중장기 감축목표", "총괄 감축목표",
    "관리권한 감축목표", "기준연도 대비", "목표 시나리오", "탄소중립 목표",
)))
_ANNUAL_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:연차별|연도별).{0,12}(?:감축량|감축률|감축목표|목표배출량)",
    r"(?:연차별|연도별).{0,12}감축경로",
    r"감축경로",
    r"(?:연차별|연도별)이행계획",
))


def _iter_scalar_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_scalar_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_scalar_values(item)
    elif value is not None:
        yield value


def _normalize_ids(*values: Any) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _iter_scalar_values(value):
            if isinstance(item, str) and item.strip().startswith("["):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    for parsed_item in parsed:
                        normalized = str(parsed_item or "").strip()
                        if normalized and normalized not in seen:
                            seen.add(normalized)
                            output.append(normalized)
                    continue
            for part in str(item or "").split("|"):
                normalized = part.strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    output.append(normalized)
    return tuple(output)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _row_ids(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    metadata = _metadata(row)
    return _normalize_ids(
        *(row.get(field) for field in fields),
        *(metadata.get(field) for field in fields),
    )


def _source_pages(row: dict[str, Any]) -> tuple[int, ...]:
    pages: set[int] = set()
    for value in (row.get("출처페이지"), row.get("출처페이지추정"), row.get("page_number")):
        for item in _iter_scalar_values(value):
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)) and int(item) > 0:
                pages.add(int(item))
                continue
            for token in re.findall(r"(?:p|P)?\s*(\d{1,4})", str(item or "")):
                page = int(token)
                if page > 0:
                    pages.add(page)
    return tuple(sorted(pages))


def _to_year(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        year = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return year if 1800 <= year <= 2200 else None


def _number_token(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return (f"{number:.6f}").rstrip("0").rstrip(".").replace(".", "")


def _row_number_tokens(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        token for field in _NUMERIC_FIELDS
        if (token := _number_token(row.get(field))) and len(token) >= 2
    ))


def _object_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ("number", "caption", "section", "nearby_text", "text"):
        value = row.get(field)
        if value:
            parts.append(str(value))
    rows = row.get("rows")
    if isinstance(rows, list):
        for table_row in rows[:80]:
            if isinstance(table_row, list):
                parts.extend(str(cell or "") for cell in table_row[:20])
    return _normalize_text(" ".join(parts)[:30000])


def _row_context_text(row: dict[str, Any]) -> str:
    values: list[str] = []
    for key, value in row.items():
        if key in {"metadata", "출처페이지", "출처페이지추정"} or key.startswith("__"):
            continue
        for item in _iter_scalar_values(value):
            values.append(str(item))
    return _normalize_text(" ".join(values))


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term and term in text for term in terms)


def _has_annual_phrase(text: str) -> bool:
    return any(pattern.search(text) for pattern in _ANNUAL_PATTERNS)


def _has_project_bundle(text: str) -> bool:
    identity = _has_any(text, _PROJECT_ID_TERMS)
    execution = _has_any(text, _PROJECT_EXECUTION_TERMS)
    descriptor = _has_any(text, _PROJECT_DESCRIPTOR_TERMS)
    specific_execution_terms = tuple(
        term for term in _PROJECT_EXECUTION_TERMS
        if term not in {_normalize_text("추진계획"), _normalize_text("사업내용")}
    )
    specific_execution_hits = sum(term in text for term in specific_execution_terms)
    return (
        identity and (execution or descriptor)
    ) or (
        descriptor and specific_execution_hits >= 2
    )


def _has_project_phrase(text: str) -> bool:
    return _has_any(
        text,
        _PROJECT_ID_TERMS + _PROJECT_EXECUTION_TERMS + _PROJECT_DESCRIPTOR_TERMS,
    )


def _has_target_summary_phrase(text: str) -> bool:
    return _has_any(text, _TARGET_SUMMARY_TERMS)


def _project_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        normalized
        for field in _PROJECT_ID_FIELDS + _PROJECT_NAME_FIELDS
        if (normalized := _normalize_text(row.get(field)))
    ))


@dataclass(frozen=True, slots=True)
class _ObjectContext:
    object_id: str
    physical_id: str
    page: int | None
    evidence_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class TargetContextDecision:
    row_index: int
    original_level: str
    suggested_level: str
    status: str
    confidence: str
    selected_score: float
    competing_score: float
    margin: float
    source_pages: tuple[int, ...]
    evidence_ids: tuple[str, ...]
    object_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    @property
    def retag(self) -> bool:
        return self.status == "retag" and self.suggested_level != self.original_level

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "original_level": self.original_level,
            "suggested_level": self.suggested_level,
            "status": self.status,
            "confidence": self.confidence,
            "selected_score": self.selected_score,
            "competing_score": self.competing_score,
            "margin": self.margin,
            "source_pages": list(self.source_pages),
            "evidence_ids": list(self.evidence_ids),
            "object_ids": list(self.object_ids),
            "reason_codes": list(self.reason_codes),
        }


class ReductionTargetContextClassifier:
    """Classify target rows using evidence objects and related-sheet context."""

    def __init__(
        self,
        raw_data: dict[str, Any],
        *,
        minimum_score: float = 6.0,
        minimum_margin: float = 2.0,
        review_score: float = 4.0,
        minimum_annual_run: int = 4,
    ) -> None:
        self.raw_data = raw_data
        self.minimum_score = minimum_score
        self.minimum_margin = minimum_margin
        self.review_score = review_score
        self.minimum_annual_run = max(3, minimum_annual_run)
        self.objects_by_page: dict[int, list[_ObjectContext]] = defaultdict(list)
        self.objects_by_evidence: dict[str, list[_ObjectContext]] = defaultdict(list)
        self.objects_by_physical: dict[str, list[_ObjectContext]] = defaultdict(list)
        self.cross_by_page: dict[str, dict[int, list[dict[str, Any]]]] = {
            key: defaultdict(list) for key in _PROJECT_SHEETS
        }
        self.cross_by_evidence: dict[str, dict[str, list[dict[str, Any]]]] = {
            key: defaultdict(list) for key in _PROJECT_SHEETS
        }
        self.cross_by_physical: dict[str, dict[str, list[dict[str, Any]]]] = {
            key: defaultdict(list) for key in _PROJECT_SHEETS
        }
        self._build_indexes()

    def _build_indexes(self) -> None:
        seen_objects: set[tuple[str, str]] = set()
        for row in self.raw_data.get("document_objects", []):
            if not isinstance(row, dict):
                continue
            metadata = _metadata(row)
            object_id = str(row.get("object_id") or "").strip()
            physical_ids = _row_ids(row, _PHYSICAL_FIELDS)
            physical_id = str(
                metadata.get("physical_object_id")
                or metadata.get("canonical_object_id")
                or (physical_ids[0] if physical_ids else object_id)
            ).strip()
            evidence_ids = _row_ids(row, _EVIDENCE_FIELDS)
            page = _source_pages(row)
            context = _ObjectContext(
                object_id=object_id,
                physical_id=physical_id,
                page=page[0] if page else None,
                evidence_ids=evidence_ids,
                text=_object_text(row),
            )
            identity = (physical_id or object_id, context.text)
            if identity in seen_objects:
                continue
            seen_objects.add(identity)
            if context.page is not None:
                self.objects_by_page[context.page].append(context)
            for evidence_id in evidence_ids:
                self.objects_by_evidence[evidence_id].append(context)
            if physical_id:
                self.objects_by_physical[physical_id].append(context)

        for sheet_key in _PROJECT_SHEETS:
            rows = self.raw_data.get(sheet_key, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for page in _source_pages(row):
                    self.cross_by_page[sheet_key][page].append(row)
                for evidence_id in _row_ids(row, _EVIDENCE_FIELDS):
                    self.cross_by_evidence[sheet_key][evidence_id].append(row)
                for physical_id in _row_ids(row, _PHYSICAL_FIELDS):
                    self.cross_by_physical[sheet_key][physical_id].append(row)

    def _annual_candidate_rows(self, rows: list[dict[str, Any]]) -> set[int]:
        groups: dict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)
        for index, row in enumerate(rows):
            year = _to_year(row.get("목표연도"))
            if year is None:
                continue
            key = (
                _source_pages(row),
                _normalize_text(row.get("목표수준")),
                _normalize_text(row.get("목표범위")),
                _normalize_text(row.get("부문")),
            )
            groups[key].append((index, year))

        qualifying: set[int] = set()
        for members in groups.values():
            by_year: dict[int, list[int]] = defaultdict(list)
            for index, year in members:
                by_year[year].append(index)
            run: list[int] = []
            for year in sorted(by_year):
                if run and year != run[-1] + 1:
                    if len(run) >= self.minimum_annual_run:
                        for run_year in run:
                            qualifying.update(by_year[run_year])
                    run = []
                run.append(year)
            if len(run) >= self.minimum_annual_run:
                for run_year in run:
                    qualifying.update(by_year[run_year])
        return qualifying

    @staticmethod
    def _deduplicate_objects(values: Iterable[_ObjectContext]) -> list[_ObjectContext]:
        output: list[_ObjectContext] = []
        seen: set[str] = set()
        for value in values:
            key = value.physical_id or value.object_id or f"page:{value.page}:{value.text}"
            if key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    def _matched_objects(self, row: dict[str, Any]) -> tuple[list[_ObjectContext], list[_ObjectContext]]:
        evidence_ids = _row_ids(row, _EVIDENCE_FIELDS)
        physical_ids = _row_ids(row, _PHYSICAL_FIELDS)
        exact = self._deduplicate_objects(
            context
            for identifier in evidence_ids
            for context in self.objects_by_evidence.get(identifier, [])
        )
        exact.extend(self._deduplicate_objects(
            context
            for identifier in physical_ids
            for context in self.objects_by_physical.get(identifier, [])
        ))
        exact = self._deduplicate_objects(exact)

        page_objects = self._deduplicate_objects(
            context
            for page in _source_pages(row)
            for context in self.objects_by_page.get(page, [])
        )
        tokens = _row_number_tokens(row)
        sector = _normalize_text(row.get("부문"))
        target_year = _number_token(row.get("목표연도"))
        signature_matches: list[_ObjectContext] = []
        for context in page_objects:
            numeric_matches = sum(token in context.text for token in tokens)
            year_and_sector = bool(
                target_year and target_year in context.text
                and sector and len(sector) >= 2 and sector in context.text
            )
            if numeric_matches >= 2 or (numeric_matches >= 1 and year_and_sector):
                signature_matches.append(context)
        return self._deduplicate_objects([*exact, *signature_matches]), page_objects

    def _cross_signal(self, row: dict[str, Any], sheet_keys: tuple[str, ...]) -> tuple[bool, bool]:
        evidence_ids = _row_ids(row, _EVIDENCE_FIELDS)
        physical_ids = _row_ids(row, _PHYSICAL_FIELDS)
        exact = any(
            self.cross_by_evidence[key].get(identifier)
            for key in sheet_keys for identifier in evidence_ids
        ) or any(
            self.cross_by_physical[key].get(identifier)
            for key in sheet_keys for identifier in physical_ids
        )
        page = any(
            self.cross_by_page[key].get(source_page)
            for key in sheet_keys for source_page in _source_pages(row)
        )
        return bool(exact), bool(page)

    def _project_name_match(self, row: dict[str, Any]) -> bool:
        target_names = set(_project_identity(row))
        if not target_names:
            return False
        for sheet_key in _PROJECT_SHEETS:
            for page in _source_pages(row):
                for candidate in self.cross_by_page[sheet_key].get(page, []):
                    for left in target_names:
                        for right in _project_identity(candidate):
                            if left == right or (min(len(left), len(right)) >= 5 and (left in right or right in left)):
                                return True
        return False

    def _score_row(
        self,
        row: dict[str, Any],
        *,
        annual_candidate: bool,
    ) -> tuple[dict[str, float], dict[str, list[str]], tuple[str, ...]]:
        matched_objects, page_objects = self._matched_objects(row)
        matched_project_bundle = any(_has_project_bundle(item.text) for item in matched_objects)
        matched_project_phrase = any(_has_project_phrase(item.text) for item in matched_objects)
        page_project_bundle = any(_has_project_bundle(item.text) for item in page_objects)
        page_project_phrase = any(_has_project_phrase(item.text) for item in page_objects)
        matched_annual_phrase = any(_has_annual_phrase(item.text) for item in matched_objects)
        page_annual_phrase = any(_has_annual_phrase(item.text) for item in page_objects)
        matched_summary_phrase = any(
            _has_target_summary_phrase(item.text) for item in matched_objects
        )
        page_summary_phrase = any(
            _has_target_summary_phrase(item.text) for item in page_objects
        )
        project_exact, project_page = self._cross_signal(row, _PROJECT_SHEETS)
        annual_exact, annual_page = self._cross_signal(row, _ANNUAL_SHEETS)

        scores = {"세부사업": 0.0, "연차경로": 0.0, "원래수준": 0.0}
        reasons: dict[str, list[str]] = defaultdict(list)

        if _project_identity(row):
            scores["세부사업"] += 8.0
            reasons["세부사업"].append("project_identity_on_target_row")
        if project_exact:
            scores["세부사업"] += 7.0
            reasons["세부사업"].append("exact_related_sheet_evidence")
        if self._project_name_match(row):
            scores["세부사업"] += 6.0
            reasons["세부사업"].append("project_name_match")
        if matched_project_bundle:
            scores["세부사업"] += 7.0
            reasons["세부사업"].append("matched_project_card_object")
        elif matched_project_phrase:
            scores["세부사업"] += 5.0
            reasons["세부사업"].append("matched_project_object")
        elif page_project_bundle:
            scores["세부사업"] += 4.0
            reasons["세부사업"].append("page_project_card_context")
        elif page_project_phrase:
            scores["세부사업"] += 2.0
            reasons["세부사업"].append("page_project_context")
        if project_page:
            scores["세부사업"] += 1.0
            reasons["세부사업"].append("related_sheet_page_overlap")

        if annual_candidate:
            scores["연차경로"] += 3.0
            reasons["연차경로"].append("consecutive_year_run")
            if annual_exact:
                scores["연차경로"] += 5.0
                reasons["연차경로"].append("exact_annual_sheet_evidence")
            elif annual_page:
                scores["연차경로"] += 1.0
                reasons["연차경로"].append("annual_sheet_page_overlap")
            if matched_annual_phrase:
                scores["연차경로"] += 7.0
                reasons["연차경로"].append("matched_annual_path_object")
            elif page_annual_phrase:
                scores["연차경로"] += 4.0
                reasons["연차경로"].append("page_annual_path_context")

        original_level = str(row.get("목표수준") or "").strip()
        if original_level in {"총괄", "부문", "세부부문"}:
            scores["원래수준"] += 1.0
            reasons["원래수준"].append("declared_target_level")
        value_count = sum(row.get(field) not in (None, "") for field in _VALUE_FIELDS)
        if value_count >= 3:
            scores["원래수준"] += 3.0
            reasons["원래수준"].append("complete_target_structure")
        elif value_count >= 2:
            scores["원래수준"] += 2.0
            reasons["원래수준"].append("partial_target_structure")
        if row.get("목표범위") not in (None, ""):
            scores["원래수준"] += 1.0
            reasons["원래수준"].append("explicit_target_scope")
        if _normalize_text(row.get("부문")) in {_normalize_text("합계"), _normalize_text("전체")}:
            scores["원래수준"] += 1.0
            reasons["원래수준"].append("aggregate_sector")
        if matched_summary_phrase:
            scores["원래수준"] += 7.0
            reasons["원래수준"].append("matched_target_summary_object")
        elif page_summary_phrase:
            scores["원래수준"] += 4.0
            reasons["원래수준"].append("page_target_summary_context")

        object_ids = tuple(dict.fromkeys(
            context.object_id for context in matched_objects if context.object_id
        ))
        return scores, reasons, object_ids

    def classify(self, rows: list[dict[str, Any]]) -> list[TargetContextDecision]:
        annual_candidates = self._annual_candidate_rows(rows)
        decisions: list[TargetContextDecision] = []
        for index, row in enumerate(rows):
            original = str(row.get("목표수준") or "").strip()
            scores, reasons, object_ids = self._score_row(
                row,
                annual_candidate=index in annual_candidates,
            )
            eligible_levels = ["세부사업"]
            if index in annual_candidates:
                eligible_levels.append("연차경로")
            selected = max(eligible_levels, key=lambda key: (scores[key], key))
            competing = max([
                scores["원래수준"],
                *(scores[key] for key in eligible_levels if key != selected),
            ])
            selected_score = scores[selected]
            margin = selected_score - competing
            strong_project_reasons = {
                "project_identity_on_target_row",
                "exact_related_sheet_evidence",
                "project_name_match",
                "matched_project_card_object",
            }
            weak_project_evidence = (
                selected == "세부사업"
                and not strong_project_reasons.intersection(reasons["세부사업"])
            )

            if original in {"세부사업", "연차경로"}:
                support_score = scores[original]
                other_special = "연차경로" if original == "세부사업" else "세부사업"
                support_competing = max(scores["원래수준"], scores[other_special])
                support_margin = support_score - support_competing
                summary_conflict = (
                    scores["원래수준"] >= self.minimum_score
                    and scores["원래수준"] - max(support_score, scores[other_special])
                    >= self.minimum_margin
                    and "matched_target_summary_object" in reasons["원래수준"]
                )
                if support_score >= self.minimum_score and support_margin >= self.minimum_margin:
                    status = "keep"
                    suggested = original
                    confidence = "high"
                    selected_score = support_score
                    competing = support_competing
                    margin = support_margin
                    reason_codes = tuple(reasons[original])
                elif summary_conflict:
                    status = "retag"
                    suggested = (
                        "총괄"
                        if _normalize_text(row.get("부문"))
                        in {_normalize_text("합계"), _normalize_text("전체")}
                        else "부문"
                    )
                    confidence = "high"
                    selected_score = scores["원래수준"]
                    competing = max(support_score, scores[other_special])
                    margin = selected_score - competing
                    reason_codes = tuple(dict.fromkeys([
                        *reasons["원래수준"],
                        "specialized_level_conflicts_with_target_summary",
                    ]))
                elif support_competing >= self.minimum_score:
                    status = "needs_review"
                    suggested = original
                    confidence = "low"
                    selected_score = support_score
                    competing = support_competing
                    margin = support_margin
                    reason_codes = tuple(dict.fromkeys([
                        *reasons[original],
                        *reasons["원래수준"],
                        "specialized_level_not_confirmed",
                    ]))
                else:
                    status = "keep"
                    suggested = original
                    confidence = "low"
                    selected_score = support_score
                    competing = support_competing
                    margin = support_margin
                    reason_codes = tuple(dict.fromkeys([
                        *reasons[original],
                        "specialized_level_unverified",
                    ]))
            elif (
                selected_score >= self.minimum_score
                and margin >= self.minimum_margin
                and not weak_project_evidence
            ):
                status = "retag"
                suggested = selected
                confidence = "high" if selected_score >= self.minimum_score + 3 else "medium"
                reason_codes = tuple(reasons[selected])
            elif selected_score >= self.review_score:
                status = "needs_review"
                suggested = original
                confidence = "low"
                reason_codes = tuple(dict.fromkeys([
                    *reasons[selected],
                    *reasons["원래수준"],
                    "score_or_margin_below_gate",
                ]))
            else:
                status = "keep"
                suggested = original
                confidence = "medium" if scores["원래수준"] >= self.review_score else "low"
                reason_codes = tuple(reasons["원래수준"] or ["insufficient_context"])

            decisions.append(TargetContextDecision(
                row_index=index + 1,
                original_level=original,
                suggested_level=suggested,
                status=status,
                confidence=confidence,
                selected_score=round(float(selected_score), 3),
                competing_score=round(float(competing), 3),
                margin=round(float(margin), 3),
                source_pages=_source_pages(row),
                evidence_ids=_row_ids(row, _EVIDENCE_FIELDS),
                object_ids=object_ids,
                reason_codes=reason_codes,
            ))
        return decisions


def classify_reduction_target_rows(
    rows: list[dict[str, Any]],
    raw_data: dict[str, Any],
    *,
    minimum_score: float = 6.0,
    minimum_margin: float = 2.0,
    review_score: float = 4.0,
    minimum_annual_run: int = 4,
) -> list[TargetContextDecision]:
    return ReductionTargetContextClassifier(
        raw_data,
        minimum_score=minimum_score,
        minimum_margin=minimum_margin,
        review_score=review_score,
        minimum_annual_run=minimum_annual_run,
    ).classify(rows)
