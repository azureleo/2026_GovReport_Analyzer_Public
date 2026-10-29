"""Validation and object-level selection for pre-generated OCR (no model calls)."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

import config
from utils.document_objects import DocumentObject


def _ids(value):
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if v}
    return {str(value)} if value else set()


def _overlap(a, b):
    x = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = x * y
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - area
    return area / union if union else 0


def normalized_bbox_to_pdf(box, page, scale=999.0):
    """Convert rendered-page coordinates to PyMuPDF's unrotated PDF space.

    0..999 is the Unlimited-OCR contract; normalized_1000 is separately
    supported for older providers. Invalid boxes never get silently clipped.
    """
    width, height = float(page.width), float(page.height)
    if not all(math.isfinite(v) and v > 0 for v in (width, height, scale)):
        raise ValueError('valid page dimensions required for normalized coordinates')
    if len(box) != 4 or not all(math.isfinite(v) for v in box):
        raise ValueError('bbox must contain four finite values')
    if box[0] < 0 or box[1] < 0 or box[2] > scale or box[3] > scale or box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError('normalized bbox has invalid bounds')
    x0, y0, x1, y1 = box[0]*width/scale, box[1]*height/scale, box[2]*width/scale, box[3]*height/scale
    rotation = int((page.metadata or {}).get('rotation', 0))
    if rotation not in {0, 90, 180, 270}:
        raise ValueError('unsupported page rotation')
    if rotation == 90:
        return y0, width-x1, y1, width-x0
    if rotation == 180:
        return width-x1, height-y1, width-x0, height-y0
    if rotation == 270:
        return height-y1, x0, height-y0, x1
    return x0, y0, x1, y1


def load_selected_ocr(backend, document, candidates, parse_text):
    backend.diagnostics.clear()
    backend.review_objects.clear()
    required = [c for c in candidates if c.action == 'ocr_required']

    def diagnose(code, path='', line=None, obj=None, severity='warning', **details):
        backend.diagnostics.append(dict(code=code, severity=severity, file=str(path), line=line,
            object_id=obj.object_id if obj else '', page=obj.page_number if obj else None, **details))

    if not required:
        diagnose('no_candidates_no_load', severity='info')
        return []
    if not backend.root.is_dir():
        diagnose('results_directory_missing', backend.root, severity='error')
        return []
    by_page = {p.page_number: p for p in document.pages}
    selected_pages = {c.page_number for c in required}
    source = Path(document.source_path) if document.source_path else None
    source_hash = None

    def validated(row, path, line=None):
        nonlocal source_hash
        if not isinstance(row, dict):
            diagnose('invalid_object_record', path, line, severity='error'); return None
        try:
            page = row.get('page_number')
            if isinstance(page, bool) or not str(page).isdigit():
                raise ValueError('page_number must be a positive integer')
            page = int(page)
            if not 1 <= page <= document.total_pages or page not in by_page:
                diagnose('page_out_of_range', path, line, severity='error', supplied_page=page); return None
            meta = row.get('metadata', {})
            if not isinstance(meta, dict):
                raise ValueError('metadata must be an object')
            rows = row.get('rows', [])
            if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
                raise ValueError('rows must be an array of arrays')
            if any(isinstance(cell, (dict, list)) for cells in rows for cell in cells):
                raise ValueError('table cells must be scalar values')
            if not isinstance(row.get('text', ''), str):
                raise ValueError('text must be a string')
            obj = DocumentObject.from_dict({**row, 'page_number': page})
            if not isinstance(obj.object_id, str) or not obj.object_id.strip() or obj.object_type not in {'table', 'text', 'image', 'chart', 'figure'}:
                raise ValueError('object_id/object_type is invalid')
            if not isinstance(obj.text, str):
                raise ValueError('text must be a string')
            obj.metadata = dict(meta)
            if meta.get('location_status') == 'unsupported' or meta.get('table_structure_status') == 'unsupported':
                obj.metadata['ocr_application_status'] = 'held_unsupported_structure'
                backend.review_objects.append(obj.to_dict())
                diagnose('unsupported_import_structure', path, line, obj)
                return None
            supplied_hash = row.get('document_sha256') or meta.get('document_sha256') or meta.get('pdf_sha256')
            supplied_name = row.get('source_filename') or meta.get('source_filename') or meta.get('document_name')
            if supplied_hash:
                if source and source.is_file():
                    if source_hash is None:
                        digest = hashlib.sha256()
                        with source.open('rb') as stream:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                        source_hash = digest.hexdigest()
                    if str(supplied_hash).lower() != source_hash:
                        diagnose('document_hash_mismatch', path, line, obj, severity='error'); return None
                else:
                    diagnose('document_hash_unverifiable', path, line, obj)
            if supplied_name and source and str(supplied_name).replace('\\', '/').split('/')[-1] != source.name:
                diagnose('document_name_mismatch', path, line, obj, severity='error'); return None
            trust = 'document_verified' if supplied_hash and source_hash else 'filename_matched' if supplied_name and source else 'legacy_unverified'
            supplied_pages = row.get('document_total_pages', meta.get('document_total_pages'))
            if supplied_pages is not None and int(supplied_pages) != document.total_pages:
                diagnose('document_page_count_mismatch', path, line, obj, severity='error'); return None
            if trust == 'legacy_unverified':
                diagnose('document_metadata_missing', path, line, obj)
            elif trust == 'filename_matched':
                diagnose('document_content_hash_missing', path, line, obj)
            raw_bbox = row.get('bbox_pdf', row.get('bbox'))
            if raw_bbox is not None:
                if not isinstance(raw_bbox, (tuple, list)) or len(raw_bbox) != 4:
                    raise ValueError('bbox must have four coordinates')
                box = tuple(float(v) for v in raw_bbox)
                if any(isinstance(v, bool) for v in raw_bbox) or not all(math.isfinite(v) for v in box):
                    raise ValueError('bbox must be finite numbers')
                page_obj = by_page[page]
                space = meta.get('coordinate_space', 'pdf_points')
                if space in {'normalized', 'normalized_999', 'normalized_1000'}:
                    scale = {'normalized': 1, 'normalized_999': 999, 'normalized_1000': 1000}[space]
                    box = normalized_bbox_to_pdf(box, page_obj, scale)
                elif space not in {'pdf_points', 'pdf_points_unrotated'}:
                    raise ValueError('unsupported coordinate_space')
                if box[0] < 0 or box[1] < 0 or box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError('bbox has invalid bounds')
                rotated = int(page_obj.metadata.get('rotation', 0)) in {90, 270}
                limit_w = float(page_obj.metadata.get('unrotated_width') or (page_obj.height if rotated else page_obj.width))
                limit_h = float(page_obj.metadata.get('unrotated_height') or (page_obj.width if rotated else page_obj.height))
                if (limit_w and box[2] > limit_w+1) or (limit_h and box[3] > limit_h+1):
                    raise ValueError('bbox exceeds page dimensions')
                obj.bbox = box
            engine = str(meta.get('engine') or row.get('engine') or backend.name)
            if engine.casefold() in {'pymupdf', 'native', 'pdf', 'fitz'}:
                diagnose('hybrid_native_object_preserved_skip', path, line, obj, severity='info', engine=engine)
                return None
            obj.metadata.update(engine=engine, ocr_backend=backend.name, ocr_status='loaded',
                ocr_source_file=str(path), ocr_source_line=line, ocr_document_trust=trust,
                ocr_original_object_id=obj.object_id, coordinate_space='pdf_points_unrotated')
            return obj
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            diagnose('invalid_object_or_coordinates', path, line, severity='error', reason=str(exc))
            return None

    loaded = []
    jsonl_files = sorted(backend.root.rglob('project_document_objects.jsonl')) or sorted(backend.root.glob('*.jsonl'))
    for path in jsonl_files:
        try:
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError) as exc:
            diagnose('input_read_error', path, severity='error', reason=str(exc)); continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                diagnose('invalid_jsonl', path, number, severity='error', reason=str(exc)); continue
            obj = validated(row, path, number)
            if obj is not None and obj.page_number in selected_pages:
                loaded.append(obj)
    loaded_pages = {o.page_number for o in loaded}
    for path in sorted([*backend.root.rglob('*.md'), *backend.root.rglob('*.txt')]):
        match = re.search(r'(?:page|p)[_-]?(\d+)', path.stem, re.I)
        digits = re.findall(r'\d+', path.stem)
        page = int(match.group(1)) if match else int(digits[-1]) if digits else 0
        if not 1 <= page <= document.total_pages or page not in by_page:
            diagnose('page_out_of_range', path, supplied_page=page); continue
        if page not in selected_pages or page in loaded_pages:
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as exc:
            diagnose('input_read_error', path, severity='error', reason=str(exc)); continue
        p = by_page[page]
        try:
            objects = parse_text(text, page_number=page, page_width=p.width, page_height=p.height, backend=backend.name)
        except Exception as exc:
            diagnose('raw_parse_failed', path, severity='error', reason=str(exc))
            continue
        if not objects:
            diagnose('raw_no_objects', path)
        for o in objects:
            if o.metadata.get('location_status') == 'unsupported':
                o.metadata['ocr_application_status'] = 'held_unsupported_location'
                backend.review_objects.append(o.to_dict())
                diagnose('unsupported_location_tag', path, obj=o); continue
            if o.metadata.get('table_structure_status') == 'unsupported':
                backend.review_objects.append(o.to_dict())
                diagnose('unsupported_table_structure', path, obj=o); continue
            raw = o.to_dict()
            if o.metadata.get('raw_bbox_norm') is not None:
                raw['bbox'] = o.metadata['raw_bbox_norm']
                raw['metadata'] = {**o.metadata, 'coordinate_space': 'normalized_999'}
            obj = validated(raw, path)
            if obj:
                loaded.append(obj)

    selected = []
    for obj in loaded:
        same_page = [c for c in required if c.page_number == obj.page_number]
        explicit = _ids(obj.metadata.get('source_object_ids')) | _ids(obj.metadata.get('canonical_object_id'))
        explicit |= _ids(obj.metadata.get('source_object_id'))
        eid = obj.metadata.get('source_evidence_id') or obj.metadata.get('evidence_id')
        linked = [c for c in same_page if (explicit & (_ids(c.object_id) | _ids(getattr(c, 'alias_object_ids', [])))) or (eid and eid == c.evidence_id)]
        if linked:
            matches = linked
        elif obj.bbox:
            threshold = float(getattr(config, 'OCR_OBJECT_MATCH_MIN_IOU', .55))
            matches = [c for c in same_page if c.bbox and _overlap(obj.bbox, c.bbox) >= threshold
                and (c.object_type == obj.object_type or {c.object_type, obj.object_type} <= {'figure', 'chart', 'image'})]
        else:
            matches = [c for c in same_page if c.object_type == obj.object_type]
            if len(same_page) > 1 or not getattr(config, 'OCR_ALLOW_LEGACY_UNLOCATED', False):
                matches = []  # Unlocated legacy outputs are review-only by default.
        if len(matches) != 1:
            obj.metadata['ocr_application_status'] = 'held_ambiguous_or_unselected'
            backend.review_objects.append(obj.to_dict())
            diagnose('object_attribution_ambiguous_or_unselected', obj.metadata['ocr_source_file'], obj=obj)
            continue
        match = matches[0]
        if obj.bbox and match.bbox and _overlap(obj.bbox, match.bbox) < .10:
            obj.metadata['ocr_application_status'] = 'held_location_mismatch'
            backend.review_objects.append(obj.to_dict())
            diagnose('object_location_mismatch', obj.metadata['ocr_source_file'], obj=obj); continue
        obj.metadata.update(source_evidence_id=match.evidence_id, evidence_id=match.evidence_id,
            source_object_ids=sorted(explicit | {match.object_id, obj.object_id}),
            canonical_object_id=getattr(match, 'canonical_object_id', '') or match.object_id,
            ocr_application_status='selected', ocr_selection_method='explicit' if linked else 'spatial' if obj.bbox else 'legacy_unique_candidate')
        selected.append(obj)
    # The same anchor with different payloads is not a richer result by default.
    groups = {}
    for obj in selected:
        groups.setdefault(obj.metadata['source_evidence_id'], []).append(obj)
    result = []
    for eid, objects in groups.items():
        signatures = {json.dumps([o.text, o.rows], ensure_ascii=False, sort_keys=True) for o in objects}
        if len(signatures) > 1:
            for o in objects:
                o.metadata['ocr_application_status'] = 'held_conflicting_ocr'
                backend.review_objects.append(o.to_dict())
            diagnose('conflicting_ocr_payloads', obj=objects[0], evidence_id=eid)
        else:
            result.append(objects[0])
            if len(objects) > 1:
                diagnose('duplicate_ocr_skipped', obj=objects[0], severity='info', count=len(objects)-1)
    loaded_evidence = {o.metadata['source_evidence_id'] for o in result}
    for candidate in required:
        if candidate.evidence_id not in loaded_evidence:
            diagnose('candidate_result_missing', severity='warning',
                     candidate_object_id=candidate.object_id, evidence_id=candidate.evidence_id,
                     candidate_page=candidate.page_number)
    return result
