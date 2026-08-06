"""문서 종류에 의존하지 않는 원문 객체 정리와 라우팅 보조 규칙."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Iterable

from utils.document_objects import DocumentObject


_VISUAL_TYPES = {"chart", "figure", "image"}
_INDEX_HEADING_RE = re.compile(
    r"^(?:(?:표|그림|도|사진)\s*)?(?:목\s*차|차\s*례)$"
    r"|^(?:contents?|tables?|figures?|pictures?)$",
    re.IGNORECASE,
)
_INDEX_BANNER_RE = re.compile(
    r"^(?:목\s*차|차\s*례)\s*[|｜∣:]?"
    r"|^(?:contents?|tables?|figures?|pictures?)\s*[|｜∣:]?",
    re.IGNORECASE,
)
_TABLE_LIST_HEADING_RE = re.compile(r"^(?:표\s*(?:목\s*차|차\s*례)|tables?)$", re.IGNORECASE)
_FIGURE_LIST_HEADING_RE = re.compile(
    r"^(?:(?:그림|도|사진)\s*(?:목\s*차|차\s*례)|figures?|pictures?)$",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(
    r"(?:^|[<\[(])\s*(표|그림|figure|fig\.?)\s*"
    r"([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:\s*[-–—.]\s*\d+)?)",
    re.IGNORECASE,
)
_DOTTED_LEADER_RE = re.compile(
    r"(?:\.{3,}|(?:\.\s*){4,}|·{4,}|(?:·\s*){4,}|…{2,})\s*\d+\s*$"
)
_TRAILING_PAGE_RE = re.compile(r"\s\d{1,4}\s*$")
_NUMBER_ONLY_RE = re.compile(r"^\d{1,4}$")
# 앞부분 목차의 로마자 면수는 소문자다. 본문의 부문 코드 `M`, `C`를 오탐하지 않는다.
_ROMAN_FOLIO_RE = re.compile(r"^[ivxlcdm]+$")
_CONTENTS_ENTRY_RE = re.compile(
    r"^(?:제\s*\d+\s*[장절]|\d+(?:\.\d+)*\s*[.)]|참고문헌|부록)"
)
_NON_WORD_RE = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)
_NUMBER_RE = re.compile(
    r"\b(표|그림|figure|fig\.?)\s*"
    r"([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:\s*[-–—.]\s*\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PreparedObjectInventory:
    objects: list[DocumentObject]
    index_references: list[DocumentObject]
    resolved_references: int
    deduplicated_objects: int


def _lines(text: str) -> list[str]:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text or "")
    return [re.sub(r"\s+", " ", line).strip() for line in cleaned.splitlines() if line.strip()]


def normalize_object_label(value: object) -> str:
    """캡션 비교용으로 괄호·공백·구두점을 제거한다."""
    return _NON_WORD_RE.sub("", str(value or "").casefold())


def normalize_object_number(value: object) -> str:
    """`표 2-3`, `[표 2–3]` 같은 번호 표기를 같은 키로 만든다."""
    text = str(value or "").strip()
    match = _NUMBER_RE.search(text)
    if not match:
        return ""
    kind = match.group(1).casefold()
    kind = "figure" if kind.startswith(("그림", "fig")) else "table"
    number = re.sub(r"\s+", "", match.group(2))
    number = re.sub(r"[–—.]", "-", number)
    return f"{kind}:{number.casefold()}"


def index_page_kind(page_text: str) -> str:
    """목차·표목차·그림목차를 페이지 번호와 무관하게 판별한다."""
    lines = _lines(page_text)
    if not lines:
        return ""
    top = lines[:12]
    normalized_top = [re.sub(r"\s+", "", line).casefold() for line in top]
    table_heading = any(_TABLE_LIST_HEADING_RE.fullmatch(line) for line in top)
    figure_heading = any(_FIGURE_LIST_HEADING_RE.fullmatch(line) for line in top)
    generic_heading = any(_INDEX_HEADING_RE.fullmatch(line) for line in top)
    continuation_banner = any(_INDEX_BANNER_RE.match(line) for line in top[:4])

    reference_lines = [line for line in lines if _REFERENCE_RE.search(line)]
    table_refs = sum(
        1 for line in reference_lines
        if (match := _REFERENCE_RE.search(line)) and match.group(1).casefold() == "표"
    )
    figure_refs = len(reference_lines) - table_refs
    leader_lines = sum(bool(_DOTTED_LEADER_RE.search(line)) for line in lines)
    page_number_lines = sum(bool(_TRAILING_PAGE_RE.search(line)) for line in reference_lines)
    number_only_lines = sum(bool(_NUMBER_ONLY_RE.fullmatch(line)) for line in lines)
    roman_folio = any(_ROMAN_FOLIO_RE.fullmatch(line) for line in top[:4])
    contents_entries = sum(bool(_CONTENTS_ENTRY_RE.match(line)) for line in lines)

    # 명시적인 표/그림 목록 제목은 가장 강한 신호다.
    if table_heading:
        return "table_index"
    if figure_heading:
        return "figure_index"

    # 첫 두 줄의 단독 목차/차례/CONTENTS 제목은 일반 목차의 안정적인 문서 공통 신호다.
    if any(_INDEX_HEADING_RE.fullmatch(line) for line in top[:2]):
        return "contents"

    # 연속 페이지는 상단의 `목차｜`와 반복되는 참조/페이지 번호를 함께 요구한다.
    dense_references = len(reference_lines) >= 4 and (
        leader_lines >= 2 or page_number_lines >= 3 or len(reference_lines) >= 8
    )
    if (generic_heading or continuation_banner) and dense_references:
        if table_refs >= max(3, figure_refs * 2):
            return "table_index"
        if figure_refs >= max(3, table_refs * 2):
            return "figure_index"
        return "contents"

    # 일반 목차는 표/그림 표기가 없어도 점선 리더와 끝 페이지 번호가 반복된다.
    if (generic_heading or continuation_banner) and leader_lines >= 4:
        return "contents"

    # 양면 편집 문서는 목록 제목을 첫 페이지만 인쇄한다. 후속 페이지는 다수의
    # 표/그림 참조와 점선 또는 별도 줄의 페이지 번호 조합으로 식별한다.
    if len(reference_lines) >= 8 and (
        leader_lines >= 4 or number_only_lines >= 4 or page_number_lines >= 4
    ):
        if table_refs > figure_refs:
            return "table_index"
        if figure_refs > table_refs:
            return "figure_index"
    if roman_folio and number_only_lines >= 4 and contents_entries >= 3:
        return "contents"
    return ""


def is_index_page(page_text: str) -> bool:
    return bool(index_page_kind(page_text))


def _caption_without_number(obj: DocumentObject) -> str:
    caption = str(obj.caption or obj.text or "")
    if obj.number:
        caption = caption.replace(str(obj.number), " ", 1)
    caption = _NUMBER_RE.sub(" ", caption, count=1)
    return normalize_object_label(caption)


def _caption_similarity(left: DocumentObject, right: DocumentObject) -> float:
    a = _caption_without_number(left)
    b = _caption_without_number(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 8 and (a in b or b in a):
        return min(len(a), len(b)) / max(len(a), len(b))
    return SequenceMatcher(None, a[:500], b[:500]).ratio()


def _compatible_types(left: DocumentObject, right: DocumentObject) -> bool:
    return left.object_type == right.object_type or {
        left.object_type, right.object_type
    } <= _VISUAL_TYPES


def _nonempty_cells(obj: DocumentObject) -> int:
    return sum(1 for row in obj.rows for cell in row if str(cell or "").strip())


def _object_quality(obj: DocumentObject) -> tuple[int, int, int, int, int]:
    return (
        _nonempty_cells(obj),
        len(obj.rows),
        len(obj.text or ""),
        1 if obj.bbox is not None else 0,
        0 if obj.metadata.get("caption_only") else 1,
    )


def _copy_object(obj: DocumentObject) -> DocumentObject:
    copied = replace(obj)
    copied.rows = [list(row) for row in obj.rows]
    copied.metadata = dict(obj.metadata)
    return copied


def _merge_duplicate_group(group: list[DocumentObject]) -> DocumentObject:
    preferred = max(group, key=_object_quality)
    merged = _copy_object(preferred)
    richest_rows = max(group, key=lambda obj: (_nonempty_cells(obj), len(obj.rows)))
    if _nonempty_cells(richest_rows) > _nonempty_cells(merged):
        merged.rows = [list(row) for row in richest_rows.rows]
    for candidate in group:
        if not merged.text and candidate.text:
            merged.text = candidate.text
        if not merged.caption and candidate.caption:
            merged.caption = candidate.caption
        if not merged.number and candidate.number:
            merged.number = candidate.number
        if not merged.section and candidate.section:
            merged.section = candidate.section
        if not merged.nearby_text and candidate.nearby_text:
            merged.nearby_text = candidate.nearby_text
        if merged.bbox is None and candidate.bbox is not None:
            merged.bbox = candidate.bbox
    aliases = sorted({obj.object_id for obj in group if obj.object_id != merged.object_id})
    source_ids = {
        str(value)
        for obj in group
        for value in (obj.metadata.get("source_object_ids") or [])
        if value
    }
    source_ids.update(obj.object_id for obj in group)
    reference_ids = {
        str(value)
        for obj in group
        for value in (obj.metadata.get("index_reference_ids") or [])
        if value
    }
    reference_pages = {
        int(value)
        for obj in group
        for value in (obj.metadata.get("index_reference_pages") or [])
        if str(value).isdigit()
    }
    merged.metadata["duplicate_object_ids"] = aliases
    merged.metadata["source_object_ids"] = sorted(source_ids)
    merged.metadata["deduplicated_count"] = len(group) - 1
    if reference_ids:
        merged.metadata["index_reference_ids"] = sorted(reference_ids)
    if reference_pages:
        merged.metadata["index_reference_pages"] = sorted(reference_pages)
    return merged


def _same_evidence(left: DocumentObject, right: DocumentObject) -> bool:
    if left.page_number != right.page_number or not _compatible_types(left, right):
        return False
    left_evidence = str(left.metadata.get("evidence_id") or "").strip()
    right_evidence = str(right.metadata.get("evidence_id") or "").strip()
    if left_evidence and left_evidence == right_evidence:
        return True

    left_number = normalize_object_number(left.number or left.caption)
    right_number = normalize_object_number(right.number or right.caption)
    similarity = _caption_similarity(left, right)
    different_engine = str(left.metadata.get("engine") or "") != str(
        right.metadata.get("engine") or ""
    )
    caption_proxy = bool(left.metadata.get("caption_only") or right.metadata.get("caption_only"))
    if left_number and left_number == right_number and similarity >= 0.72:
        return different_engine or caption_proxy
    if similarity >= 0.94 and (different_engine or caption_proxy):
        return True
    return False


def deduplicate_evidence_objects(objects: Iterable[DocumentObject]) -> tuple[list[DocumentObject], int]:
    """동일 페이지의 native/OCR/VLM 표현을 한 근거 객체로 합친다."""
    pending = [_copy_object(obj) for obj in objects]
    groups: list[list[DocumentObject]] = []
    while pending:
        seed = pending.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            remaining: list[DocumentObject] = []
            for candidate in pending:
                if any(_same_evidence(member, candidate) for member in group):
                    group.append(candidate)
                    changed = True
                else:
                    remaining.append(candidate)
            pending = remaining
        groups.append(group)
    merged = [_merge_duplicate_group(group) for group in groups]
    merged.sort(key=lambda obj: (obj.page_number, obj.sequence, obj.object_type, obj.object_id))
    return merged, sum(len(group) - 1 for group in groups)


def _resolve_index_references(
    references: list[DocumentObject],
    body_objects: list[DocumentObject],
) -> int:
    by_number: dict[str, list[DocumentObject]] = {}
    for obj in body_objects:
        key = normalize_object_number(obj.number or obj.caption)
        if key:
            by_number.setdefault(key, []).append(obj)

    resolved = 0
    for reference in references:
        key = normalize_object_number(reference.number or reference.caption)
        candidates = list(by_number.get(key, [])) if key else []
        if not candidates:
            continue
        ranked = sorted(
            ((_caption_similarity(reference, candidate), candidate) for candidate in candidates),
            key=lambda pair: (-pair[0], pair[1].page_number, pair[1].object_id),
        )
        best_score, best = ranked[0]
        if len(ranked) > 1 and best_score < 0.45:
            continue
        if len(ranked) > 1 and best_score - ranked[1][0] < 0.08:
            continue
        reference.metadata["body_object_id"] = best.object_id
        reference.metadata["body_page_number"] = best.page_number
        best.metadata.setdefault("index_reference_ids", []).append(reference.object_id)
        best.metadata.setdefault("index_reference_pages", []).append(reference.page_number)
        resolved += 1
    return resolved


def prepare_object_inventory(
    objects: Iterable[DocumentObject],
    page_texts: dict[int, str],
) -> PreparedObjectInventory:
    """목차 참조를 본문에 연결하고 실제 본문 객체만 중복 제거해 반환한다."""
    references: list[DocumentObject] = []
    body: list[DocumentObject] = []
    for original in objects:
        obj = _copy_object(original)
        kind = index_page_kind(page_texts.get(obj.page_number, ""))
        if kind and obj.object_type in {"table", "chart", "figure", "image"}:
            obj.metadata["is_index_reference"] = True
            obj.metadata["index_page_kind"] = kind
            references.append(obj)
        else:
            body.append(obj)
    resolved = _resolve_index_references(references, body)
    deduplicated, removed = deduplicate_evidence_objects(body)
    return PreparedObjectInventory(
        objects=deduplicated,
        index_references=references,
        resolved_references=resolved,
        deduplicated_objects=removed,
    )
