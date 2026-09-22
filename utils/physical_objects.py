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
    physical_object_id: str = ""
    alias_object_ids: tuple[str, ...] = ()
    render_group_id: str = ""
    render_variant: str = ""
    panel_index: int | None = None
    panel_count: int = 0
    context_only: bool = False


@dataclass(frozen=True, slots=True)
class PhysicalObjectMatch:
    """두 객체 표현의 통합 가능 여부와 판정 근거."""

    same: bool
    method: str = ""
    confidence: float = 0.0
    blocked: bool = False
    reason: str = ""


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


def _panel_conflict(
    left: PhysicalObjectIdentity,
    right: PhysicalObjectIdentity,
) -> bool:
    """같은 렌더 그룹의 서로 다른 패널은 동일 객체로 합치지 않는다."""
    if not left.render_group_id or left.render_group_id != right.render_group_id:
        return False
    if left.panel_index is None or right.panel_index is None:
        return False
    return left.panel_index != right.panel_index


def match_physical_objects(
    left: PhysicalObjectIdentity,
    right: PhysicalObjectIdentity,
) -> PhysicalObjectMatch:
    """강한 구조적 근거만 사용해 물리 객체 통합 여부를 판정한다."""
    if (
        left.page_number is not None
        and right.page_number is not None
        and left.page_number != right.page_number
    ):
        return PhysicalObjectMatch(False, reason="페이지 불일치")

    if _panel_conflict(left, right):
        return PhysicalObjectMatch(
            False,
            "panel_conflict",
            1.0,
            blocked=True,
            reason="같은 렌더 그룹의 서로 다른 패널",
        )

    left_ids = identity_object_ids(left)
    right_ids = identity_object_ids(right)
    if left_ids & right_ids:
        return PhysicalObjectMatch(True, "shared_object_id", 1.0)

    if (
        left.physical_object_id
        and left.physical_object_id == right.physical_object_id
    ):
        return PhysicalObjectMatch(True, "physical_object_id", 1.0)

    if (
        left.page_number is None
        or right.page_number is None
    ):
        return PhysicalObjectMatch(False, reason="페이지 정보 부족")
    if object_type_family(left.object_type) != object_type_family(right.object_type):
        return PhysicalObjectMatch(False, reason="객체 유형 불일치")

    both_have_evidence = bool(left.evidence_id and right.evidence_id)
    same_evidence = both_have_evidence and left.evidence_id == right.evidence_id
    if both_have_evidence and not same_evidence:
        return PhysicalObjectMatch(False, reason="근거 ID 불일치")

    left_number = normalize_object_number(left.number or left.caption)
    right_number = normalize_object_number(right.number or right.caption)
    same_number = bool(left_number and left_number == right_number)
    similarity = caption_similarity(left, right)
    overlap = bbox_iou(left.bbox, right.bbox)

    # 페이지 렌더는 전송 변형이다. 빈 프록시는 같은 근거 ID에만 연결하고,
    # 데이터가 있는 렌더 결과는 번호·캡션·좌표 중 추가 근거를 요구한다.
    if left.render_proxy or right.render_proxy:
        proxy = left if left.render_proxy else right
        if same_evidence and not (proxy.caption or proxy.number or proxy.bbox):
            return PhysicalObjectMatch(True, "transport_proxy", 0.98)
        if same_evidence and (same_number or similarity >= 0.72 or overlap >= 0.50):
            return PhysicalObjectMatch(True, "render_proxy_support", 0.94)
        return PhysicalObjectMatch(False, reason="렌더 프록시의 물리 근거 부족")

    left_placeholder = left.caption_only or left.missing_native
    right_placeholder = right.caption_only or right.missing_native
    if left_placeholder or right_placeholder:
        if same_number and similarity >= 0.45:
            return PhysicalObjectMatch(True, "caption_number", 0.96)
        if same_evidence and (similarity >= 0.72 or overlap >= 0.35):
            return PhysicalObjectMatch(True, "placeholder_support", 0.90)
        if similarity >= 0.94:
            return PhysicalObjectMatch(True, "caption_exact", 0.92)
        return PhysicalObjectMatch(False, reason="캡션 프록시의 물리 근거 부족")

    if same_number and (same_evidence or similarity >= 0.72):
        return PhysicalObjectMatch(True, "number_caption", 0.96)

    different_engine = bool(left.engine and right.engine and left.engine != right.engine)
    if similarity >= 0.94 and (same_evidence or different_engine):
        return PhysicalObjectMatch(True, "caption_engine", 0.92)
    if overlap >= 0.80 and (same_evidence or different_engine):
        return PhysicalObjectMatch(True, "bbox_overlap", 0.90)
    return PhysicalObjectMatch(False, reason="통합 근거 부족")


def same_physical_object(
    left: PhysicalObjectIdentity,
    right: PhysicalObjectIdentity,
) -> bool:
    """강한 구조적 근거가 있을 때만 두 표현을 동일 객체로 간주한다."""
    return match_physical_objects(left, right).same


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
        physical_object_id=str(metadata.get("physical_object_id") or "").strip(),
        alias_object_ids=aliases,
        render_group_id=str(metadata.get("render_group_id") or "").strip(),
        render_variant=str(metadata.get("render_variant") or "").strip(),
        panel_index=_positive_int(metadata.get("panel_index")),
        panel_count=_positive_int(metadata.get("panel_count")) or 0,
        context_only=bool(metadata.get("context_only")),
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
        "source_physical_object_ids": list(normalize_object_ids(
            *(identity.physical_object_id for identity in values)
        )),
        "had_caption_proxy": any(identity.caption_only for identity in values),
        "had_render_proxy": any(identity.render_proxy for identity in values),
        "had_missing_native_proxy": any(identity.missing_native for identity in values),
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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
