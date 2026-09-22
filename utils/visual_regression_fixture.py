"""Build and validate locked page-level visual regression fixtures."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz


SCHEMA_VERSION = "visual-regression-fixture-v1"
REQUIRED_CASE_FIELDS = {
    "case_id",
    "source_page",
    "caption",
    "expected_object_type",
    "expected_triage",
    "expected_status",
    "expected_sheet",
    "expected_render_variants",
}


class VisualRegressionFixtureError(ValueError):
    """Raised when a regression fixture contract is invalid."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_case_specs(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} JSON 오류: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} 회귀 사례는 JSON 객체여야 합니다."
            )
        missing = sorted(REQUIRED_CASE_FIELDS - set(row))
        if missing:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} 필수 필드 누락: {', '.join(missing)}"
            )
        case_id = str(row.get("case_id") or "").strip()
        if not case_id or case_id in seen:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} case_id가 비어 있거나 중복입니다: {case_id}"
            )
        try:
            page = int(row.get("source_page"))
        except (TypeError, ValueError) as exc:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} source_page가 정수가 아닙니다."
            ) from exc
        if page < 1:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} source_page는 1 이상이어야 합니다."
            )
        bbox = row.get("bbox_pdf")
        if bbox is not None and (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} bbox_pdf는 숫자 4개의 배열이어야 합니다."
            )
        variants = row.get("expected_render_variants")
        if not isinstance(variants, list) or not variants:
            raise VisualRegressionFixtureError(
                f"{source}:{line_number} expected_render_variants가 비어 있습니다."
            )
        row = dict(row)
        row["case_id"] = case_id
        row["source_page"] = page
        cases.append(row)
        seen.add(case_id)
    if not cases:
        raise VisualRegressionFixtureError(f"회귀 사례가 없습니다: {source}")
    return cases


def _relative(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _clamped_rect(page: fitz.Page, bbox: Sequence[float]) -> fitz.Rect:
    rect = fitz.Rect(*(float(value) for value in bbox))
    rect &= page.rect
    if rect.is_empty or rect.width < 2 or rect.height < 2:
        raise VisualRegressionFixtureError(f"유효하지 않은 crop 좌표입니다: {bbox}")
    return rect


def _render(page: fitz.Page, path: Path, dpi: int, clip: fitz.Rect | None = None) -> None:
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False, clip=clip)
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(path))


def _fingerprint(source_hash: str, cases_hash: str, dpi: int) -> str:
    payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": source_hash,
            "cases_sha256": cases_hash,
            "dpi": dpi,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_visual_regression_fixture(
    source_pdf: str | Path,
    cases_path: str | Path,
    output_dir: str | Path,
    *,
    dataset_id: str,
    dpi: int = 108,
    force: bool = False,
) -> Path:
    source = Path(source_pdf).resolve()
    case_file = Path(cases_path).resolve()
    output = Path(output_dir).resolve()
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and not force:
        raise VisualRegressionFixtureError(
            f"이미 회귀 표본이 존재합니다: {manifest_path}. --force를 지정하세요."
        )
    if not source.exists():
        raise FileNotFoundError(source)
    if not case_file.exists():
        raise FileNotFoundError(case_file)
    if dpi < 72 or dpi > 300:
        raise VisualRegressionFixtureError("회귀 렌더 DPI는 72~300 범위여야 합니다.")

    cases = load_case_specs(case_file)
    output.mkdir(parents=True, exist_ok=True)
    page_dir = output / "pages"
    crop_dir = output / "crops"
    text_dir = output / "page_text"
    page_dir.mkdir(parents=True, exist_ok=True)
    crop_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    document = fitz.open(str(source))
    source_page_count = len(document)
    page_rows: list[dict[str, Any]] = []
    object_rows: list[dict[str, Any]] = []
    subset = fitz.open()
    try:
        unique_pages = sorted({int(case["source_page"]) for case in cases})
        for subset_index, page_number in enumerate(unique_pages, start=1):
            if page_number > len(document):
                raise VisualRegressionFixtureError(
                    f"p{page_number}가 원본 {len(document)}페이지 범위를 벗어납니다."
                )
            page = document[page_number - 1]
            page_image = page_dir / f"p{page_number:04d}_full.png"
            page_text = text_dir / f"p{page_number:04d}.txt"
            _render(page, page_image, dpi)
            page_text.write_text(page.get_text("text", sort=True), encoding="utf-8")
            subset.insert_pdf(document, from_page=page_number - 1, to_page=page_number - 1)
            page_rows.append(
                {
                    "source_page": page_number,
                    "subset_page": subset_index,
                    "page_image": _relative(page_image, output),
                    "page_image_sha256": sha256_file(page_image),
                    "page_text": _relative(page_text, output),
                    "page_text_sha256": sha256_file(page_text),
                    "width_pdf": float(page.rect.width),
                    "height_pdf": float(page.rect.height),
                }
            )

        for case in cases:
            page_number = int(case["source_page"])
            page = document[page_number - 1]
            crop_path: Path | None = None
            bbox = case.get("bbox_pdf")
            if bbox is not None:
                crop_path = crop_dir / f"{case['case_id']}.png"
                _render(page, crop_path, dpi, _clamped_rect(page, bbox))
            object_row = {
                key: value
                for key, value in case.items()
                if not key.startswith("expected_") and key not in {"notes"}
            }
            object_row.update(
                {
                    "page_image": f"pages/p{page_number:04d}_full.png",
                    "crop_image": _relative(crop_path, output) if crop_path else "",
                    "crop_image_sha256": sha256_file(crop_path) if crop_path else "",
                    "page_text": f"page_text/p{page_number:04d}.txt",
                }
            )
            object_rows.append(object_row)
    finally:
        document.close()

    subset_path = output / "source_subset.pdf"
    try:
        subset.save(str(subset_path), garbage=4, deflate=True)
    finally:
        subset.close()

    objects_path = output / "objects.jsonl"
    annotations_path = output / "annotations.jsonl"
    _write_jsonl(objects_path, object_rows)
    _write_jsonl(
        annotations_path,
        (
            {
                "case_id": case["case_id"],
                "source_page": case["source_page"],
                "review_status": "reviewed",
                "expected_triage": case["expected_triage"],
                "expected_status": case["expected_status"],
                "expected_object_type": case["expected_object_type"],
                "expected_sheet": case["expected_sheet"],
                "expected_reference_policy": case.get(
                    "expected_reference_policy", "analyze_then_merge"
                ),
                "expected_render_variants": case["expected_render_variants"],
                "expected_text_anchors": case.get("expected_text_anchors", []),
                "expected_value_anchors": case.get("expected_value_anchors", []),
                "notes": case.get("notes", ""),
            }
            for case in cases
        ),
    )

    source_hash = sha256_file(source)
    cases_hash = sha256_file(case_file)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "role": "regression",
        "review_status": "reviewed",
        "locked": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_fingerprint": _fingerprint(source_hash, cases_hash, dpi),
        "source": {
            "path": _relative(source, output),
            "sha256": source_hash,
            "page_count": source_page_count,
        },
        "cases_source": {
            "path": _relative(case_file, output),
            "sha256": cases_hash,
        },
        "render": {"dpi": dpi, "format": "png", "color_space": "rgb"},
        "pages": page_rows,
        "objects_file": "objects.jsonl",
        "objects_sha256": sha256_file(objects_path),
        "annotations_file": "annotations.jsonl",
        "annotations_sha256": sha256_file(annotations_path),
        "subset_pdf": "source_subset.pdf",
        "subset_pdf_sha256": sha256_file(subset_path),
        "counts": {
            "cases": len(cases),
            "source_pages": len(page_rows),
            "positive": sum(case["expected_status"] == "extracted" for case in cases),
            "negative": sum(case["expected_status"] != "extracted" for case in cases),
        },
        "metric_contract": {
            "triage_false_negative_rate": "expected_triage=ocr_required 객체의 탈락 비율",
            "render_contract_recall": "기대 렌더 변형 생성 비율",
            "object_status_accuracy": "사람 확정 최종 상태 일치율",
            "evidence_match_accuracy": "근거ID 또는 번호+캡션+핵심값 정확 매칭률",
        },
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest_path


def validate_visual_regression_fixture(
    manifest_path: str | Path,
    *,
    source_override: str | Path | None = None,
) -> dict[str, Any]:
    manifest = Path(manifest_path).resolve()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("회귀 표본 schema_version이 다릅니다.")
    if payload.get("locked") is not True:
        errors.append("회귀 표본이 locked 상태가 아닙니다.")

    def check(relative: str, expected_hash: str, label: str) -> None:
        path = (manifest.parent / relative).resolve()
        if not path.exists():
            errors.append(f"{label} 파일이 없습니다: {path}")
        elif sha256_file(path) != expected_hash:
            errors.append(f"{label} SHA256이 다릅니다: {path}")

    check(payload["objects_file"], payload["objects_sha256"], "objects")
    check(payload["annotations_file"], payload["annotations_sha256"], "annotations")
    check(payload["subset_pdf"], payload["subset_pdf_sha256"], "subset PDF")
    for page in payload.get("pages", []):
        check(page["page_image"], page["page_image_sha256"], "page image")
        check(page["page_text"], page["page_text_sha256"], "page text")

    source_data = payload.get("source") or {}
    source = (
        Path(source_override).resolve()
        if source_override
        else (manifest.parent / str(source_data.get("path") or "")).resolve()
    )
    if source.exists():
        if sha256_file(source) != source_data.get("sha256"):
            errors.append("원본 PDF SHA256이 다릅니다.")
    else:
        warnings.append(f"원본 PDF가 없어 렌더 산출물 해시만 검증했습니다: {source}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "dataset_id": payload.get("dataset_id"),
        "fingerprint": payload.get("dataset_fingerprint"),
        "counts": payload.get("counts", {}),
    }
