"""
문서 객체 계층.

기존 PageContent(text/tables/images) 위에 표, 텍스트, 이미지 객체 ID와 메타데이터를
부여한다. 1차 목표는 표를 HTML 문자열이 아니라 caption/section/근처 본문이 붙은
DocumentObject로 만들어 LLM 추출 전에 우선 컨텍스트로 제공하는 것이다.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from utils.pdf_reader import PageContent


_HEADING_RE = re.compile(
    r"^\s*((제\s*\d+\s*[장절])|(\d+(?:\.\d+){0,3})|([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s]))\s*(.+)$"
)
_TABLE_CAPTION_RE = re.compile(r"(?:\[?\s*)?(표)\s*([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:[-–]\d+)?)\s*[\]\)]?\s*(.*)")
_FIGURE_CAPTION_RE = re.compile(r"(?:\[?\s*)?(그림|Figure|Fig\.?)\s*([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:[-–]\d+)?)\s*[\]\)]?\s*(.*)", re.I)


@dataclass
class DocumentObject:
    """페이지 내부의 추출 가능한 의미 단위."""

    object_id: str
    object_type: str
    page_number: int
    sequence: int
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""
    number: str = ""
    section: str = ""
    nearby_text: str = ""
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def compact_text(self, max_chars: int = 1000) -> str:
        value = re.sub(r"\s+", " ", self.text or self.nearby_text or "").strip()
        if len(value) > max_chars:
            return value[: max_chars - 1].rstrip() + "…"
        return value


class _HTMLTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
            self._in_cell = True

    def handle_data(self, data: str):
        if self._in_cell and self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"td", "th"} and self._row is not None:
            value = html.unescape(" ".join(self._cell or []))
            value = re.sub(r"\s+", " ", value).strip()
            self._row.append(value)
            self._cell = None
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None


def parse_html_table(table_html: str) -> list[list[str]]:
    parser = _HTMLTableParser()
    try:
        parser.feed(table_html or "")
    except Exception:
        return []
    return parser.rows


def table_to_markdown(rows: list[list[str]], max_rows: int = 35, max_cols: int = 12) -> str:
    if not rows:
        return ""
    width = min(max(len(row) for row in rows), max_cols)
    if width <= 0:
        return ""

    def norm(row: list[str]) -> list[str]:
        values = [(cell or "").replace("|", "/").strip() for cell in row[:width]]
        return values + [""] * (width - len(values))

    trimmed = [norm(row) for row in rows[:max_rows]]
    header = trimmed[0]
    body = trimmed[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    if len(rows) > max_rows:
        omitted = [f"... {len(rows) - max_rows} more rows omitted"] + [""] * (width - 1)
        lines.append("| " + " | ".join(omitted) + " |")
    return "\n".join(lines)


def _page_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _infer_section(lines: list[str], fallback: str = "") -> str:
    for line in lines[:40]:
        match = _HEADING_RE.match(line)
        if match:
            value = re.sub(r"\s+", " ", line).strip()
            if 4 <= len(value) <= 80:
                return value
    return fallback


def _caption_candidates(lines: list[str], pattern: re.Pattern) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        number = f"{match.group(1)} {match.group(2)}"
        tail = match.group(3).strip()
        caption = f"{number} {tail}".strip()
        out.append((number, caption))
    return out


def _nearby_text(lines: list[str], limit: int = 900) -> str:
    text = " ".join(lines[:20])
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def build_document_objects(pages: list[PageContent]) -> list[DocumentObject]:
    """PageContent 목록을 DocumentObject 목록으로 변환."""
    objects: list[DocumentObject] = []
    current_section = ""

    for page in pages:
        lines = _page_lines(page.text)
        current_section = _infer_section(lines, current_section)
        nearby = _nearby_text(lines)

        objects.append(DocumentObject(
            object_id=f"p{page.page_number}_text_1",
            object_type="text",
            page_number=page.page_number,
            sequence=1,
            text=page.text or "",
            section=current_section,
            nearby_text=nearby,
        ))

        table_captions = _caption_candidates(lines, _TABLE_CAPTION_RE)
        for index, table_html in enumerate(page.tables or [], start=1):
            rows = parse_html_table(table_html)
            number, caption = table_captions[min(index - 1, len(table_captions) - 1)] if table_captions else ("", "")
            objects.append(DocumentObject(
                object_id=f"p{page.page_number}_table_{index}",
                object_type="table",
                page_number=page.page_number,
                sequence=index,
                text=table_html,
                rows=rows,
                caption=caption,
                number=number,
                section=current_section,
                nearby_text=nearby,
                metadata={"row_count": len(rows), "column_count": max((len(r) for r in rows), default=0)},
            ))

        figure_captions = _caption_candidates(lines, _FIGURE_CAPTION_RE)
        # 벡터 차트는 page.images에 나타나지 않을 수 있다. 캡션 자체를 독립 객체로
        # 등록해야 원문 객체 인벤토리가 이런 그래프를 누락하지 않는다.
        for index, (number, caption) in enumerate(figure_captions, start=1):
            objects.append(DocumentObject(
                object_id=f"p{page.page_number}_figure_{index}",
                object_type="figure",
                page_number=page.page_number,
                sequence=index,
                caption=caption,
                number=number,
                section=current_section,
                nearby_text=nearby,
            ))
        for index, image in enumerate(page.images or [], start=1):
            number, caption = figure_captions[min(index - 1, len(figure_captions) - 1)] if figure_captions else ("", image.get("caption", ""))
            objects.append(DocumentObject(
                object_id=f"p{page.page_number}_image_{index}",
                object_type="image",
                page_number=page.page_number,
                sequence=index,
                caption=caption,
                number=number,
                section=current_section,
                nearby_text=nearby,
                metadata={
                    "width": image.get("width"),
                    "height": image.get("height"),
                    "caption": image.get("caption", ""),
                },
            ))

    return objects


def render_table_object_chunks(
    obj: DocumentObject,
    *,
    rows_per_chunk: int = 20,
    max_chars: int | None = None,
) -> list[str]:
    """큰 표를 헤더 반복 행 청크로 만들어 한 호출의 출력·입력 크기를 제한한다."""
    if obj.object_type != "table" or not obj.rows:
        return []
    rows_per_chunk = max(2, int(rows_per_chunk))
    header = list(obj.rows[0])
    body = list(obj.rows[1:])
    body_chunks = [body[i:i + rows_per_chunk - 1] for i in range(0, len(body), rows_per_chunk - 1)]
    if not body_chunks:
        body_chunks = [[]]
    def render(chunk: list[list[str]], index: int, total: int) -> str:
        rows = [header, *chunk]
        return "\n".join([
            "[문서객체: 표 우선 데이터]",
            f"object_id: {obj.object_id}",
            f"object_chunk: {index}/{total}",
            f"type: {obj.object_type}",
            f"page: {obj.page_number}",
            f"number: {obj.number or 'unknown'}",
            f"caption: {obj.caption or 'unknown'}",
            f"section: {obj.section or 'unknown'}",
            f"nearby_text: {obj.nearby_text or 'unknown'}",
            "table_markdown:",
            table_to_markdown(rows, max_rows=max(len(rows), 2)),
        ])

    # 행 수 기준 청크가 긴 셀 때문에 문자 상한을 넘으면 행 묶음을 다시 반분한다.
    if max_chars is not None:
        max_chars = max(1000, int(max_chars))
        pending = list(body_chunks)
        bounded: list[list[list[str]]] = []
        while pending:
            chunk = pending.pop(0)
            if len(chunk) > 1 and len(render(chunk, 1, 1)) > max_chars:
                midpoint = max(1, len(chunk) // 2)
                pending[0:0] = [chunk[:midpoint], chunk[midpoint:]]
            else:
                bounded.append(chunk)
        body_chunks = bounded

    return [
        render(chunk, index, len(body_chunks))
        for index, chunk in enumerate(body_chunks, start=1)
    ]


def objects_for_pages(
    objects: list[DocumentObject],
    pages: list[PageContent],
    object_type: str | None = None,
) -> list[DocumentObject]:
    page_nums = {page.page_number for page in pages}
    selected = [obj for obj in objects if obj.page_number in page_nums]
    if object_type:
        selected = [obj for obj in selected if obj.object_type == object_type]
    return selected


def render_table_objects_for_prompt(
    objects: list[DocumentObject],
    *,
    max_objects: int = 8,
    max_rows: int = 35,
) -> str:
    table_objects = [obj for obj in objects if obj.object_type == "table" and obj.rows]
    if not table_objects:
        return ""

    parts = ["[문서객체: 표 우선 데이터]"]
    for obj in table_objects[:max_objects]:
        nearby = re.sub(r"\s+", " ", obj.nearby_text or "").strip()
        if len(nearby) > 500:
            nearby = nearby[:499].rstrip() + "…"
        parts.append(
            "\n".join([
                f"object_id: {obj.object_id}",
                f"type: {obj.object_type}",
                f"page: {obj.page_number}",
                f"number: {obj.number or 'unknown'}",
                f"caption: {obj.caption or 'unknown'}",
                f"section: {obj.section or 'unknown'}",
                f"nearby_text: {nearby or 'unknown'}",
                "table_markdown:",
                table_to_markdown(obj.rows, max_rows=max_rows),
            ])
        )
    if len(table_objects) > max_objects:
        parts.append(f"... {len(table_objects) - max_objects} additional table objects omitted in this batch")
    return "\n\n".join(parts)
