from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


BBox = tuple[float, float, float, float]


@dataclass
class ExperimentObject:
    """엔진과 무관하게 비교 가능한 문서 객체."""

    object_id: str
    engine: str
    page_number: int
    object_type: str
    sequence: int
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""
    number: str = ""
    section: str = ""
    bbox_norm: BBox | None = None
    bbox_pdf: BBox | None = None
    confidence: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.bbox_norm is not None:
            data["bbox_norm"] = list(self.bbox_norm)
        if self.bbox_pdf is not None:
            data["bbox_pdf"] = list(self.bbox_pdf)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperimentObject":
        values = dict(data)
        for key in ("bbox_norm", "bbox_pdf"):
            value = values.get(key)
            if isinstance(value, list) and len(value) == 4:
                values[key] = tuple(float(item) for item in value)
        values["rows"] = [
            [str(cell or "") for cell in row]
            for row in values.get("rows", [])
            if isinstance(row, list)
        ]
        values["metadata"] = values.get("metadata") if isinstance(values.get("metadata"), dict) else {}
        return cls(**values)

    def searchable_text(self) -> str:
        cells = " ".join(cell for row in self.rows for cell in row)
        return " ".join(part for part in (self.caption, self.number, self.text, cells) if part)

    def to_project_document_object(self):
        """현재 프로젝트의 DocumentObject로 변환한다."""
        from utils.document_objects import DocumentObject

        return DocumentObject(
            object_id=self.object_id,
            object_type=self.object_type,
            page_number=self.page_number,
            sequence=self.sequence,
            text=self.text,
            rows=self.rows,
            caption=self.caption,
            number=self.number,
            section=self.section,
            nearby_text=str(self.metadata.get("nearby_text", "") or ""),
            bbox=self.bbox_pdf,
            metadata={
                **self.metadata,
                "engine": self.engine,
                "confidence": self.confidence,
                "bbox_norm": list(self.bbox_norm) if self.bbox_norm else None,
            },
        )


@dataclass
class SamplePage:
    page_number: int
    image_path: str
    categories: list[str] = field(default_factory=list)
    source_statuses: list[str] = field(default_factory=list)
    expected_object_ids: list[str] = field(default_factory=list)
    pdf_width: float = 0.0
    pdf_height: float = 0.0
    image_width: int = 0
    image_height: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SamplePage":
        return cls(**data)

