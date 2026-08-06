from __future__ import annotations

from pathlib import Path

from .io_utils import load_objects, write_jsonl


def export_project_objects(manifest_path: str | Path, engine: str = "hybrid") -> Path:
    """실험 객체를 현재 프로젝트의 DocumentObject 직렬화 형태로 내보낸다."""
    run_dir = Path(manifest_path).resolve().parent
    objects = load_objects(run_dir / "outputs" / engine / "objects.jsonl")
    rows = []
    for item in objects:
        project = item.to_project_document_object()
        rows.append({
            "object_id": project.object_id,
            "object_type": project.object_type,
            "page_number": project.page_number,
            "sequence": project.sequence,
            "text": project.text,
            "rows": project.rows,
            "caption": project.caption,
            "number": project.number,
            "section": project.section,
            "nearby_text": project.nearby_text,
            "bbox": list(project.bbox) if project.bbox else None,
            "metadata": project.metadata,
        })
    output = run_dir / "outputs" / engine / "project_document_objects.jsonl"
    return write_jsonl(output, rows)
