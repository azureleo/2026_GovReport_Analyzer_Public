"""한 파이프라인 실행에서 반복 사용되는 결정론적 산출물."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from utils.document_objects import DocumentObject, build_document_objects
from utils.pdf_reader import PDFContent


def _clone_object_for_mutation(obj: DocumentObject) -> DocumentObject:
    """행 payload는 공유하고 변경되는 metadata만 분리한 경량 복제본을 만든다."""
    return DocumentObject(
        object_id=obj.object_id,
        object_type=obj.object_type,
        page_number=obj.page_number,
        sequence=obj.sequence,
        text=obj.text,
        rows=obj.rows,
        caption=obj.caption,
        number=obj.number,
        section=obj.section,
        nearby_text=obj.nearby_text,
        bbox=obj.bbox,
        metadata=dict(obj.metadata),
    )


@dataclass(slots=True)
class PipelineArtifacts:
    """PDF 파싱 뒤 한 번 생성해 모든 에이전트에 전달하는 실행 단위 객체."""

    document: PDFContent
    extraction_prompts: dict[str, str]
    document_objects: tuple[DocumentObject, ...]
    guideline_report: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    _objects_by_page: dict[int, tuple[DocumentObject, ...]] = field(
        default_factory=dict,
        repr=False,
    )

    @classmethod
    def create(
        cls,
        document: PDFContent,
        extraction_prompts: dict[str, str],
        *,
        guideline_report: str = "",
    ) -> "PipelineArtifacts":
        started = time.perf_counter()
        objects = tuple(build_document_objects(document.pages))
        by_page: dict[int, list[DocumentObject]] = {}
        for obj in objects:
            by_page.setdefault(obj.page_number, []).append(obj)
        elapsed = time.perf_counter() - started
        prompts = dict(extraction_prompts)
        return cls(
            document=document,
            extraction_prompts=prompts,
            document_objects=objects,
            guideline_report=guideline_report,
            stats={
                "document_object_count": len(objects),
                "document_object_build_seconds": round(elapsed, 3),
                "guideline_prompt_count": len(prompts),
                "guideline_prompt_chars": sum(len(value) for value in prompts.values()),
            },
            _objects_by_page={
                page_number: tuple(page_objects)
                for page_number, page_objects in by_page.items()
            },
        )

    def objects_for_pages(self, page_numbers: Iterable[int]) -> list[DocumentObject]:
        """원본 객체를 페이지 순서대로 반환한다. 호출자는 객체를 변경하지 않아야 한다."""
        objects: list[DocumentObject] = []
        for page_number in page_numbers:
            objects.extend(self._objects_by_page.get(int(page_number), ()))
        return objects

    def clone_document_objects(self) -> list[DocumentObject]:
        """triage metadata를 기록하는 단계에 전달할 경량 변경 가능 복제본."""
        return [_clone_object_for_mutation(obj) for obj in self.document_objects]
