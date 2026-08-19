"""문서 엔진마다 다르게 표현된 동일 물리 객체를 식별하는 공통 규칙."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, Iterable


_VISUAL_TYPES = {"chart", "figure", "image"}
_NON_WORD_RE = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)
_NUMBER_RE = re.compile(
    r"\b(표|그림|figure|fig\.?)\s*"
    r"([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:\s*[-–—.]\s*\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PhysicalObjectIdentity:
    """동일 물리 객체 판정에 필요한 엔진 독립 메타데이터."""

    object_id: str
    page_number: int | None
    object_type: str
    evidence_id: str = ""
    bbox: tuple[float, float, float, float] | None = None
    caption: str = ""
    number: str = ""
    engine: str = ""
    caption_only: bool = False
    render_proxy: bool = False
    missing_native: bool = False
    canonical_object_id: str = ""
    alias_object_ids: tuple[str, ...] = ()


def normalize_object_label(value: object) -> str:
    """캡션 비교용으로 괄호·공백·구두점을 제거한다."""
    return _NON_WORD_RE.sub("", str(value or "").casefold())


def normalize_object_number(value: object) -> str:
    """`표 2-3`, `[표 2–3]` 같은 번호 표기를 같은 키로 만든다."""
    match = _NUMBER_RE.search(str(value or "").strip())
    if not match:
        return ""
    kind = match.group(1).casefold()
    kind = "figure" if kind.startswith(("그림", "fig")) else "table"
    number = re.sub(r"\s+", "", match.group(2))
    number = re.sub(r"[–—.]", "-", number)
    return f"{kind}:{number.casefold()}"


def object_type_family(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return "visual" if normalized in _VISUAL_TYPES else normalized


def normalize_object_ids(*values: Any) -> tuple[str, ...]:
    """문자열·배열 형태의 객체 ID를 입력 순서대로 중복 없이 정리한다."""
    output: list[str] = []
    seen: set[str] = set()

    def append(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                append(item)
            return
        text = str(value or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        output.append(text)

    for value in values:
        append(value)
    return tuple(output)


def bbox_iou(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> float:
    if left is None or right is None:
        return 0.0
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if intersection <= 0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _caption_without_number(identity: PhysicalObjectIdentity) -> str:
    caption = str(identity.caption or "")
    if identity.number:
        caption = caption.replace(str(identity.number), " ", 1)
    caption = _NUMBER_RE.sub(" ", caption, count=1)
    return normalize_object_label(caption)


def caption_similarity(
    left: PhysicalObjectIdentity,
    right: PhysicalObjectIdentity,
) -> float:
    a = _caption_without_number(left)
    b = _caption_without_number(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 8 and (a in b or b in a):
        return min(len(a), len(b)) / max(len(a), len(b))
    return SequenceMatcher(None, a[:500], b[:500]).ratio()


def identity_object_ids(identity: PhysicalObjectIdentity) -> set[str]:
    return {
        value
        for value in (
            identity.object_id,
            identity.canonical_object_id,
            *identity.alias_object_ids,
        )
        if value
    }


def same_physical_object(
    left: PhysicalObjectIdentity,
    right: PhysicalObjectIdentity,
) -> bool:
    """강한 구조적 근거가 있을 때만 두 표현을 동일 객체로 간주한다."""
    left_ids = identity_object_ids(left)
    right_ids = identity_object_ids(right)
    if left_ids & right_ids:
        return True

    if (
        left.page_number is None
        or right.page_number is None
        or left.page_number != right.page_number
    ):
        return False
    if object_type_family(left.object_type) != object_type_family(right.object_type):
        return False

    both_have_evidence = bool(left.evidence_id and right.evidence_id)
    same_evidence = both_have_evidence and left.evidence_id == right.evidence_id
    if both_have_evidence and not same_evidence:
        return False

    # 페이지 렌더 이미지는 분석 전송 수단일 뿐이다. 같은 근거 ID에 연결된
    # 실제 표·차트와는 합치되, 페이지의 다른 객체까지 흡수하지 않는다.
    if left.render_proxy or right.render_proxy:
        if same_evidence:
            return True
        left_number = normalize_object_number(left.number or left.caption)
        right_number = normalize_object_number(right.number or right.caption)
        return bool(
            left_number
            and left_number == right_number
            and caption_similarity(left, right) >= 0.72
        )

    left_placeholder = left.caption_only or left.missing_native
    right_placeholder = right.caption_only or right.missing_native
    if left_placeholder or right_placeholder:
        if same_evidence:
            return True
        left_number = normalize_object_number(left.number or left.caption)
        right_number = normalize_object_number(right.number or right.caption)
        if left_number and left_number == right_number:
            return caption_similarity(left, right) >= 0.45
        return caption_similarity(left, right) >= 0.94

    left_number = normalize_object_number(left.number or left.caption)
    right_number = normalize_object_number(right.number or right.caption)
    similarity = caption_similarity(left, right)
    if left_number and left_number == right_number and (same_evidence or similarity >= 0.72):
        return True

    different_engine = bool(left.engine and right.engine and left.engine != right.engine)
    if similarity >= 0.94 and (same_evidence or different_engine):
        return True
    return bbox_iou(left.bbox, right.bbox) >= 0.80 and (same_evidence or different_engine)


def physical_object_id(identity: PhysicalObjectIdentity) -> str:
    """한 실행 안에서 대표 객체를 추적할 수 있는 안정적인 ID를 만든다."""
    if identity.canonical_object_id:
        return f"physical:{identity.canonical_object_id}"
    if identity.object_id:
        return f"physical:{identity.object_id}"
    page = identity.page_number or 0
    family = object_type_family(identity.object_type) or "unknown"
    label = normalize_object_number(identity.number or identity.caption)
    return f"physical:p{page}:{family}:{label or 'unresolved'}"


def aliases_from_metadata(metadata: dict[str, Any], object_id: str = "") -> tuple[str, ...]:
    """중복 제거가 명시적으로 확정한 별칭만 동일성 판정에 사용한다."""
    return normalize_object_ids(
        object_id,
        metadata.get("canonical_object_id"),
        metadata.get("alias_object_ids"),
        metadata.get("duplicate_object_ids"),
    )


def identity_from_document_object(obj: Any) -> PhysicalObjectIdentity:
    metadata = obj.metadata if isinstance(getattr(obj, "metadata", None), dict) else {}
    object_id = str(getattr(obj, "object_id", "") or "").strip()
    aliases = tuple(
        value for value in aliases_from_metadata(metadata, object_id) if value != object_id
    )
    return PhysicalObjectIdentity(
        object_id=object_id,
        page_number=_page_number(getattr(obj, "page_number", None)),
        object_type=str(getattr(obj, "object_type", "") or "").strip(),
        evidence_id=str(
            metadata.get("evidence_id") or metadata.get("source_evidence_id") or ""
        ).strip(),
        bbox=_bbox(getattr(obj, "bbox", None)),
        caption=str(getattr(obj, "caption", "") or metadata.get("caption") or "").strip(),
        number=str(getattr(obj, "number", "") or "").strip(),
        engine=str(metadata.get("engine") or metadata.get("ocr_backend") or "").strip(),
        caption_only=bool(metadata.get("caption_only")),
        render_proxy=bool(metadata.get("render_proxy")),
        missing_native=bool(metadata.get("missing_native")),
        canonical_object_id=str(metadata.get("canonical_object_id") or "").strip(),
        alias_object_ids=aliases,
    )


def aggregate_identity_metadata(
    canonical_object_id: str,
    identities: Iterable[PhysicalObjectIdentity],
    *,
    existing_physical_id: str = "",
) -> dict[str, Any]:
    """대표 객체에 별칭과 프록시 이력을 손실 없이 기록한다."""
    values = list(identities)
    aliases = normalize_object_ids(
        *(value for identity in values for value in identity_object_ids(identity))
    )
    aliases = tuple(value for value in aliases if value != canonical_object_id)
    canonical_identity = next(
        (identity for identity in values if identity.object_id == canonical_object_id),
        values[0] if values else PhysicalObjectIdentity(canonical_object_id, None, ""),
    )
    return {
        "canonical_object_id": canonical_object_id,
        "physical_object_id": existing_physical_id or physical_object_id(
            PhysicalObjectIdentity(
                object_id=canonical_object_id,
                page_number=canonical_identity.page_number,
                object_type=canonical_identity.object_type,
                canonical_object_id=canonical_object_id,
            )
        ),
        "alias_object_ids": list(aliases),
        "had_caption_proxy": any(identity.caption_only for identity in values),
        "had_render_proxy": any(identity.render_proxy for identity in values),
        "had_missing_native_proxy": any(identity.missing_native for identity in values),
    }


def _page_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None
