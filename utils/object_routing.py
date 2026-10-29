"""Index exclusion and conservative physical consolidation adapted from V9."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re

from utils.document_objects import DocumentObject
from utils.physical_objects import (aggregate_identity_metadata, identity_from_document_object,
                                    match_physical_objects)


@dataclass(frozen=True)
class PreparedObjectInventory:
    objects: list[DocumentObject]
    index_references: list[DocumentObject]
    resolved_references: int = 0
    deduplicated_objects: int = 0


def index_page_kind(page_text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in (page_text or "").splitlines() if line.strip()]
    for line in lines[:3]:
        if re.fullmatch(r"표\s*(?:목\s*차|차\s*례)|tables?", line, re.I):
            return "table_index"
        if re.fullmatch(r"(?:그림|도|사진)\s*(?:목\s*차|차\s*례)|figures?", line, re.I):
            return "figure_index"
        if re.fullmatch(r"목\s*차|차\s*례|contents?", line, re.I):
            return "contents"
    reference_lines = [line for line in lines if re.match(r"[<\[(]?\s*(?:표|그림|Figure)\s*\d", line, re.I)]
    leaders = sum(bool(re.search(r"(?:\.{3,}|·{4,}|…{2,})\s*\d+\s*$", line)) for line in reference_lines)
    return "contents" if len(reference_lines) >= 8 and leaders >= 4 else ""


def is_index_page(page_text: str) -> bool:
    return bool(index_page_kind(page_text))


def deduplicate_evidence_objects(objects: list[DocumentObject]) -> tuple[list[DocumentObject], int]:
    """Merge proxies/exact content only; preserve conflicting observations as aliases."""
    objects = list(objects)
    output = []
    for original in sorted(objects, key=lambda obj: (obj.page_number, obj.object_type, obj.sequence, obj.object_id)):
        obj = deepcopy(original)
        candidates = []
        for index, candidate in enumerate(output):
            match = match_physical_objects(identity_from_document_object(candidate), identity_from_document_object(obj))
            if match.same:
                candidates.append((index, match))
        if len(candidates) != 1:
            if len(candidates) > 1:
                obj.metadata["physical_match_status"] = "ambiguous"
                obj.metadata["candidate_object_ids"] = [output[index].object_id for index, _ in candidates]
            output.append(obj)
            continue
        index, match = candidates[0]
        candidate = output[index]
        left_payload = (candidate.text, candidate.rows)
        right_payload = (obj.text, obj.rows)
        left_proxy = not (candidate.text or candidate.rows)
        right_proxy = not (obj.text or obj.rows)
        if left_payload != right_payload and not (left_proxy or right_proxy):
            obj.metadata.update({
                "physical_object_id": candidate.metadata.get("physical_object_id") or f"physical:{candidate.object_id}",
                "physical_match_status": "content_conflict_retained",
                "matched_object_id": candidate.object_id, "physical_match_method": match.method,
            })
            candidate.metadata.setdefault("content_conflict_object_ids", []).append(obj.object_id)
            output.append(obj)
            continue
        preferred = obj if left_proxy and not right_proxy else candidate
        merged = deepcopy(preferred)
        merged.metadata.update(aggregate_identity_metadata(
            merged.object_id, [identity_from_document_object(candidate), identity_from_document_object(obj)],
            existing_physical_id=candidate.metadata.get("physical_object_id", ""),
        ))
        merged.metadata.setdefault("evidence_id", candidate.metadata.get("evidence_id") or obj.metadata.get("evidence_id"))
        merged.metadata["physical_match_method"] = match.method
        output[index] = merged
    for obj in output:
        obj.metadata.setdefault("physical_object_id", f"physical:{obj.object_id}")
        obj.metadata.setdefault("evidence_id", f"ev-{obj.object_id}")
    return output, len(objects) - len(output)


def prepare_object_inventory(objects, page_texts=None):
    page_texts = page_texts or {}
    references = [deepcopy(obj) for obj in objects if obj.metadata.get("index_reference")
                  or is_index_page(page_texts.get(obj.page_number, ""))]
    selected = [obj for obj in objects if not obj.metadata.get("index_reference")
                and not is_index_page(page_texts.get(obj.page_number, ""))]
    consolidated, removed = deduplicate_evidence_objects(selected)
    return PreparedObjectInventory(consolidated, references, 0, removed)


def consolidate_document_objects(objects):
    return deduplicate_evidence_objects(objects)[0]


def target_sheets_for_object(obj: DocumentObject) -> list[str]:
    explicit = obj.metadata.get("target_sheets") or obj.metadata.get("related_sheets") or obj.metadata.get("target_sheet")
    if explicit:
        return [explicit] if isinstance(explicit, str) else list(explicit)
    text = obj.searchable_text()
    sheets = []
    rules = [
        ("emissions_forecast", ("전망", "BAU", "예측", "정책반영")),
        ("reduction_targets", ("감축목표", "목표배출량", "감축률")),
        ("financial_plan", ("재정", "예산", "국비", "도비", "시비", "백만원")),
        ("regional_conditions", ("인구", "기온", "GRDP", "차량", "에너지 소비")),
        ("quantitative_reductions", ("감축원단위", "배출계수", "예상감축량")),
    ]
    for sheet, keywords in rules:
        if any(word in text for word in keywords):
            sheets.append(sheet)
    if not any(sheet in sheets for sheet in ("emissions_forecast", "reduction_targets")) and any(
        word in text for word in ("배출량", "온실가스", "흡수원", "인벤토리")
    ):
        sheets.append("emissions_management" if "관리권한" in text or "관리 권한" in text else "emissions_regional")
    if any(word in text for word in ("연차별", "목표물량", "연도별 목표")):
        sheets.append("annual_implementation")
    if any(word in text for word in ("사업명", "관리번호", "주관부서", "세부사업")):
        sheets.append("mitigation_projects")
    return list(dict.fromkeys(sheets))
