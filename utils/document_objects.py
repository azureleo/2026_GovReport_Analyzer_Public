"""Document objects selectively ported from V9, retaining main's page contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import math
import re
from typing import Any

from utils.pdf_reader import PageContent


@dataclass
class DocumentObject:
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
        return value if len(value) <= max_chars else value[:max(0, max_chars - 1)] + "…"

    def searchable_text(self) -> str:
        return " ".join(filter(None, [
            self.number, self.caption, self.section, self.text,
            " ".join(cell for row in self.rows for cell in row), self.nearby_text,
        ]))

    def to_dict(self) -> dict:
        result = asdict(self)
        if self.bbox is not None:
            result["bbox"] = list(self.bbox)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "DocumentObject":
        if not isinstance(value, dict):
            raise ValueError("DocumentObject must be a JSON object")
        data = {key: item for key, item in value.items() if key in cls.__dataclass_fields__}
        data["metadata"] = dict(data.get("metadata") or {})
        for key in ("engine", "confidence", "evidence_id"):
            if key in value:
                data["metadata"].setdefault(key, value[key])
        data["bbox"] = _record_bbox(value.get("bbox") or value.get("bbox_pdf"))
        for required in ("object_id", "object_type", "page_number"):
            if required not in data:
                raise ValueError(f"missing {required}")
        data["page_number"] = int(data["page_number"])
        if data["page_number"] < 1 or not str(data["object_id"]).strip():
            raise ValueError("invalid object ID/page number")
        data.setdefault("sequence", 1)
        raw_rows = data.get("rows") or []
        if not isinstance(raw_rows, list) or any(not isinstance(row, list) for row in raw_rows):
            raise ValueError("table rows must be arrays")
        data["rows"] = [[str(cell) if cell is not None else "" for cell in row] for row in raw_rows]
        return cls(**data)


def _record_bbox(value: Any) -> tuple[float, float, float, float] | None:
    try:
        if not isinstance(value, (tuple, list)) or len(value) != 4:
            return None
        x0, y0, x1, y1 = (float(item) for item in value)
        if not all(math.isfinite(item) for item in (x0, y0, x1, y1)) or x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1
    except (TypeError, ValueError):
        return None


class _HTMLTableParser(HTMLParser):
    """Expand spans into a rectangular grid while preserving header blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raw_rows: list[list[tuple[str, int, int, bool]]] = []
        self.header_flags: list[bool] = []
        self._row = None
        self._cell = None
        self._attrs = {}
        self._header = False
        self._thead = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "thead":
            self._thead = True
        elif tag == "tr":
            self._row = []
            self._header = self._thead
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
            self._attrs = dict(attrs)
            self._header = self._header or tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            def span(name):
                try:
                    return min(1000, max(1, int(self._attrs.get(name) or 1)))
                except ValueError:
                    return 1
            self._row.append((re.sub(r"\s+", " ", "".join(self._cell)).strip(),
                              span("rowspan"), span("colspan"), tag == "th"))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.raw_rows.append(self._row)
            self.header_flags.append(self._thead or bool(self._row) and all(cell[3] for cell in self._row))
            self._row = None
        elif tag == "thead":
            self._thead = False

    def expanded_rows(self):
        cells = {}
        width = 0
        for r, row in enumerate(self.raw_rows):
            col = 0
            for value, rowspan, colspan, _ in row:
                while (r, col) in cells:
                    col += 1
                # Keep malformed overlapping spans from shifting later columns silently.
                if any((r, c) in cells for c in range(col, col + colspan)):
                    raise ValueError("overlapping table spans")
                for rr in range(r, r + rowspan):
                    for cc in range(col, col + colspan):
                        if (rr, cc) in cells:
                            raise ValueError("overlapping table spans")
                        cells[rr, cc] = value
                col += colspan
                width = max(width, col)
        height = max((r + 1 for r, _ in cells), default=0)
        return [[cells.get((r, c), "") for c in range(width)] for r in range(height)]


def parse_html_table_structure(table_html: str) -> tuple[list[list[str]], int]:
    parser = _HTMLTableParser()
    parser.feed(table_html or "")
    rows = parser.expanded_rows()
    headers = 0
    for flag in parser.header_flags:
        if not flag:
            break
        headers += 1
    return rows, min(headers, len(rows))


def parse_html_table(table_html: str) -> list[list[str]]:
    return parse_html_table_structure(table_html)[0]


_NUMERIC = re.compile(r"^[-+]?\d+(?:,\d{3})*(?:\.\d+)?\s*[%％]?$")
_YEAR = re.compile(r"^(?:19|20)\d{2}\s*년?$")


def table_header_row_count(rows: list[list[str]], max_rows: int = 3) -> int:
    if not rows:
        return 0
    count = 0
    for row in rows[:max_rows]:
        if any(_NUMERIC.fullmatch(str(cell).replace(" ", "")) and not _YEAR.fullmatch(str(cell).strip())
               for cell in row if cell):
            break
        count += 1
    return 1 if count >= len(rows) else max(1, count)


def table_to_markdown(rows: list[list[str]], max_rows: int = 35,
                      max_cols: int | None = None, header_rows: int = 1) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    if max_cols is not None:
        width = min(width, max_cols)
    if not width:
        return ""
    def line(row):
        cells = [str(cell or "").replace("|", "/").replace("\n", " ").strip() for cell in row[:width]]
        cells += [""] * (width - len(cells))
        return "| " + " | ".join(cells) + " |"
    selected = rows[:max_rows]
    lines = [line(selected[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(line(row) for row in selected[1:])
    if len(rows) > max_rows:
        lines.append(line([f"... {len(rows) - max_rows} more rows omitted"]))
    return "\n".join(lines)


_CAPTION = re.compile(r"(표|그림|Figure|Fig\.?)\s*([0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:[-–]\d+)?)\s*[\]\)>]?\s*(.*)", re.I)
_HEADING = re.compile(r"^\s*(?:제\s*\d+\s*[장절]|\d+(?:\.\d+){0,3}[.)]?|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s])")


def _captions(page):
    result = []
    for block in page.text_blocks or [{"text": page.text}]:
        for line in str(block.get("text") or "").splitlines():
            match = _CAPTION.search(line)
            if match:
                number = f"{match.group(1)} {match.group(2)}"
                result.append((number, f"{number} {match.group(3)}".strip(), _record_bbox(block.get("bbox"))))
    return result


def build_document_objects(pages: list[PageContent]) -> list[DocumentObject]:
    from utils.object_routing import is_index_page
    if hasattr(pages, "pages"):
        pages = pages.pages
    objects = []
    section = ""
    for page in pages:
        page_engine = str(page.metadata.get("engine") or "pymupdf")
        lines = [line.strip() for line in page.text.splitlines() if line.strip()]
        section = next((line[:100] for line in lines[:40] if _HEADING.match(line)), section)
        nearby = " ".join(lines[:20])[:900]
        is_index = is_index_page(page.text)
        for index, block in enumerate(page.text_blocks or [{"text": page.text}], 1):
            objects.append(DocumentObject(f"p{page.page_number}_text_{index}", "text", page.page_number, index,
                text=str(block.get("text") or ""), section=section, nearby_text=nearby,
                bbox=_record_bbox(block.get("bbox")),
                metadata={"engine": page_engine, "index_reference": is_index}))
        captions = [] if is_index else _captions(page)
        table_captions = [item for item in captions if item[0].startswith("표")]
        records = page.table_records or [{"html": table} for table in page.tables]
        for index, record in enumerate(records, 1):
            parse_error = ""
            try:
                rows, explicit_headers = parse_html_table_structure(str(record.get("html") or ""))
            except ValueError as exc:
                rows, explicit_headers = [], 0
                parse_error = str(exc)
            caption = table_captions[index - 1] if index <= len(table_captions) else ("", "", None)
            objects.append(DocumentObject(f"p{page.page_number}_table_{index}", "table", page.page_number, index,
                rows=rows, caption=caption[1], number=caption[0], section=section,
                nearby_text=nearby, bbox=_record_bbox(record.get("bbox")),
                metadata={"engine": str(record.get("engine") or page_engine),
                          "header_row_count": explicit_headers or table_header_row_count(rows),
                          "cell_count": sum(len(row) for row in rows), "index_reference": is_index,
                          "table_parse_error": parse_error,
                          "raw_html": str(record.get("html") or "") if parse_error else ""}))
        for index, image in enumerate(page.images, 1):
            objects.append(DocumentObject(f"p{page.page_number}_image_{index}", "image", page.page_number, index,
                caption=str(image.get("caption") or ""), section=section, nearby_text=nearby,
                bbox=_record_bbox(image.get("bbox")), metadata={
                    "engine": page_engine, "source_kind": image.get("source_kind", "embedded"),
                    "render_proxy": image.get("source_kind") == "page_render",
                    "index_reference": is_index, "width": image.get("width"), "height": image.get("height"),
                }))
        for index, (number, caption, bbox) in enumerate(captions, 1):
            kind = "table" if number.startswith("표") else "chart"
            if kind == "table" and records:
                continue
            objects.append(DocumentObject(f"p{page.page_number}_{kind}_caption_{index}", kind, page.page_number, index,
                caption=caption, number=number, section=section, nearby_text=nearby, bbox=bbox,
                metadata={"engine": page_engine, "caption_only": True, "missing_native": True}))
    for obj in objects:
        obj.metadata.setdefault("evidence_id", f"ev-{obj.object_id}")
        obj.metadata.setdefault("physical_object_id", f"physical:{obj.object_id}")
    return objects


def render_table_object_chunks(obj: DocumentObject, *, rows_per_chunk: int = 20,
                                max_chars: int | None = None, max_cells: int | None = None) -> list[str]:
    """Repeat every header; reject a single oversized row instead of dropping cells."""
    if obj.object_type != "table" or not obj.rows:
        return []
    headers = int(obj.metadata.get("header_row_count") or obj.metadata.get("header_rows") or table_header_row_count(obj.rows))
    headers = max(1, min(headers, len(obj.rows)))
    width = max(len(row) for row in obj.rows)
    rectangular = [list(row) + [""] * (width - len(row)) for row in obj.rows]
    header = rectangular[:headers]
    body = rectangular[headers:]
    def render(chunk, index, total):
        rows = header + chunk
        return "\n".join([
            f"=== 페이지 {obj.page_number} ===", "[문서객체: 표 우선 데이터]",
            f"object_id: {obj.object_id}", f"evidence_id: {obj.metadata.get('evidence_id', '')}",
            f"object_chunk: {index}/{total}", f"caption: {obj.caption}",
            f"section: {obj.section}", f"nearby_text: {obj.nearby_text}",
            f"header_rows: {headers}", "table_markdown:",
            table_to_markdown(rows, max_rows=len(rows), header_rows=headers),
        ])
    chunks = []
    chunk = []
    for row in body:
        candidate = chunk + [row]
        rows = header + candidate
        over = (len(candidate) + headers > max(headers + 1, int(rows_per_chunk))
                or (max_cells is not None and sum(len(r) for r in rows) > max_cells)
                or (max_chars is not None and len(render(candidate, 1, max(1, len(body)))) > max_chars))
        if over and chunk:
            chunks.append(chunk)
            chunk = [row]
        else:
            chunk = candidate
        if (max_cells is not None and sum(len(r) for r in header + chunk) > max_cells
                or max_chars is not None and len(render(chunk, 1, max(1, len(body)))) > max_chars):
            raise ValueError(f"object {obj.object_id}: header/single row exceeds configured batch limit")
    if chunk or not chunks:
        chunks.append(chunk)
    rendered = [render(chunk, i, len(chunks)) for i, chunk in enumerate(chunks, 1)]
    if max_chars is not None and any(len(text) > max_chars for text in rendered):
        raise ValueError(f"object {obj.object_id}: chunk marker exceeds character limit")
    return rendered


def objects_for_pages(objects, pages, object_type=None):
    numbers = {page.page_number for page in pages}
    return [obj for obj in objects if obj.page_number in numbers and (not object_type or obj.object_type == object_type)]


def consolidate_document_objects(objects):
    from utils.object_routing import deduplicate_evidence_objects
    return deduplicate_evidence_objects(objects)[0]


def render_table_objects_for_prompt(objects, *, max_objects=8, max_rows=35):
    return "\n\n".join(chunk for obj in objects[:max_objects]
                        for chunk in render_table_object_chunks(obj, rows_per_chunk=max_rows))
