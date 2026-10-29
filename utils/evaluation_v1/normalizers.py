"""과거 점수 재현에 사용한 Organizer 정규화를 고정한다. 업무 정제와 분리."""
import re
import unicodedata
from typing import Any

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


def to_float(val: Any) -> float | None:
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
