"""Conservative TOC audit/selection. Never changes native text or text routing.

Printed references are preserved as navigation evidence, not resolved to PDF pages.
Uncertain pages/objects retain their existing reading path.
"""
from __future__ import annotations

from copy import deepcopy
import re
import config

VERSION = "vision-toc-v1"
_LABEL = re.compile(r"^\s*\[?\s*(표|그림|Table|Figure)\s*([\dⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:[-–.]\d+)*|부록[-–]\d+)\s*\]?\s*", re.I)
_HEADING = re.compile(r"^(?:(?:표|그림)\s*)?목\s*차(?:\s+[ivxlcdmⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ\d]+)?$|^(?:table\s+of\s+contents|list\s+of\s+(?:tables|figures))$", re.I)
_LEADER = re.compile(r"^(.+?)\s*[.·…․]{2,}\s*(\d(?:\s*\d){0,3})\s*$")


def policy_snapshot():
    mode = str(getattr(config, "VISION_TOC_MODE", "exclude")).strip().lower()
    if mode not in {"off", "audit", "exclude"}:
        raise ValueError("VISION_TOC_MODE must be off, audit, or exclude")
    return {"version": VERSION, "mode": mode}


def _compact(text):
    return re.sub(r"\s+", "", str(text or ""))


def _caption_body(text):
    return _compact(_LABEL.sub('', str(text or '')).strip(' []():'))


def _rect(value):
    try:
        x0, y0, x1, y1 = map(float, value)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None
    except (TypeError, ValueError):
        return None


def _within(inner, outer, tolerance=2):
    a, b = _rect(inner), _rect(outer)
    return bool(a and b and a[0] >= b[0]-tolerance and a[1] >= b[1]-tolerance
                and a[2] <= b[2]+tolerance and a[3] <= b[3]+tolerance)


def _entry(text, heading):
    raw = str(text or "").strip()
    if sum(bool(_LABEL.match(line)) for line in raw.splitlines()) > 1:
        return None
    label = _LABEL.match(raw)
    leader = _LEADER.fullmatch(raw)
    if not label and not (heading and leader):
        return None
    if leader:
        title, reference = leader.groups()
    else:
        # Separate trailing page-number lines, e.g. "1\n3\n5" -> 135.
        lines = raw.splitlines()
        tail = []
        while lines and re.fullmatch(r"\s*\d+(?:\s+\d+)*\s*", lines[-1]):
            tail.insert(0, lines.pop())
        title, reference = " ".join(lines).strip(), " ".join(tail)
        if not tail or not title:
            inline = re.fullmatch(r"(.+?)\s{2,}(\d(?:\s*\d){0,3})\s*", raw)
            if not inline:
                return None
            title, reference = inline.groups()
    digits = _compact(reference)
    if not re.fullmatch(r"\d{1,4}", digits) or int(digits) == 0:
        return None
    content = _LABEL.sub("", title).strip(" []().- ") if label else title
    if len(content) < 3 or not re.search(r"[가-힣A-Za-z]", content):
        return None
    return {"number": _compact(label.group(1) + label.group(2)) if label else "",
            "title": title, "raw_text": raw, "printed_page_reference": int(digits),
            "reference_raw": reference, "physical_page_resolved": False}


def analyze_page(page):
    text = str(page.text or "")
    heading = any(_HEADING.fullmatch(line.strip()) for line in text.splitlines())
    blocks = page.text_blocks or [{"text": text, "bbox": None}]
    entries = []
    for block in blocks:
        raw = str(block.get("text") or "").strip()
        direct = _entry(raw, heading)
        if direct:
            box = _rect(block.get("bbox"))
            direct["bbox"] = list(box) if box else None
            entries.append(direct)
            continue
        # A prose-containing block must not lend its rectangle to a TOC entry.
        chunks = re.split(r"(?m)(?=^\s*\[?(?:표|그림|Table|Figure)\s*(?:\d|부록|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]))", raw)
        if len(chunks) <= 1:
            chunks = raw.splitlines() if heading else []
        for chunk in chunks:
            entry = _entry(chunk, heading)
            if entry:
                entry["bbox"] = None
                entries.append(entry)
    entries = list({(e['number'], e['title'], e['printed_page_reference']): e for e in entries}.values())
    distinct = len({e['number'] or e['title'] for e in entries})
    ratio = min(1.0, sum(len(_compact(e['raw_text'])) for e in entries) / max(1, len(_compact(text))))
    confirmed = distinct >= (3 if heading else 8)
    # Tables/images may contain data invisible in page text: never exclude the
    # entire page when they exist, even if most text belongs to the index.
    embedded = any(image.get('source_kind', 'embedded') != 'page_render' for image in (page.images or []))
    index_titles = {_caption_body(e['title']) for e in entries}
    non_index_caption = any(_LABEL.match(line) and _caption_body(line) not in index_titles
                            for line in text.splitlines())
    pure = confirmed and ratio >= 0.90 and not page.tables and not embedded and not non_index_caption
    role = ('toc_index' if pure else 'mixed_toc') if confirmed else ('uncertain_toc' if heading or entries else 'unclassified')
    reasons = []
    if heading:
        reasons.append('explicit_toc_heading')
    if entries:
        reasons.append('repeated_number_title_page_reference')
    if confirmed and not heading:
        reasons.append('dense_independent_index_entries')
    if confirmed and not pure:
        reasons.append('retain_non_index_or_unseen_content')
    return {"page": page.page_number, "role": role, "confirmed": confirmed,
            "index_entry_count": len(entries), "entry_text_ratio": round(ratio, 4),
            "reasons": reasons, "entries": entries}


class TocPolicy:
    def __init__(self, pages):
        self.policy = policy_snapshot()
        self.pages = {p.page_number: analyze_page(p) for p in pages} if self.policy['mode'] != 'off' else {}
        self.objects = []
        self.images = []
        self.blocked_ids = set()

    def _match(self, obj):
        page = self.pages.get(obj.page_number, {})
        if not page.get('confirmed') or obj.rows or obj.object_type not in {'table', 'chart', 'figure'}:
            return None
        if not (obj.metadata.get('caption_only') or obj.metadata.get('missing_native')):
            return None
        hits = [e for e in page['entries'] if e['number'] == _compact(obj.number)
                and _caption_body(obj.caption) == _caption_body(e['title'])
                and bool(_caption_body(obj.caption))
                and (not obj.bbox or (e['bbox'] and _within(obj.bbox, e['bbox'])))]
        return hits[0] if len(hits) == 1 else None

    def apply(self, objects, decisions, *, source_objects=None):
        by_id = {obj.object_id: obj for obj in objects}
        sources = {obj.object_id: obj for obj in (source_objects if source_objects is not None else objects)}
        for row in decisions:
            obj = by_id.get(row.object_id)
            entry = self._match(obj) if obj else None
            if entry is None:
                continue
            # All merged aliases must independently be index proxies. A data
            # image/table absorbed into a canonical TOC object must survive.
            member_ids = {row.object_id, *row.alias_object_ids, *row.source_object_ids}
            if any(member not in sources or self._match(sources[member]) is None for member in member_ids):
                continue
            before = row.action
            excluded = self.policy['mode'] == 'exclude'
            record = {"object_id": row.object_id, "evidence_id": row.evidence_id,
                      "page": row.page_number, "role": "toc_index_entry",
                      "before_action": before, "would_exclude": True, "excluded": excluded,
                      "evidence": deepcopy(entry), "reason": "caption_proxy_matches_verified_index_entry",
                      "verified_source_object_ids": sorted(member_ids)}
            self.objects.append(record)
            row.toc = deepcopy(record)
            if excluded:
                self.blocked_ids.update([row.object_id, *row.alias_object_ids, *row.source_object_ids])
                row.initial_action = row.initial_action or before
                row.action = 'skip_non_data'
                row.backend = ''
                row.context_only = True
                row.status = 'toc_excluded'
                row.final_status = 'not_relevant'
                row.terminal_reason = '검증된 목차 항목: 탐색 근거 보존, Vision 호출 제외'
                row.reasons.append('explicit_non_data:toc_index_entry')

    def filter_images(self, images):
        kept = []
        for page, image in images:
            ids = set(image.get('source_object_ids') or [])
            role = self.pages.get(page.page_number, {})
            reason = ''
            if ids and ids <= self.blocked_ids:
                reason = 'all_source_objects_are_verified_toc'
            elif role.get('role') == 'toc_index' and image.get('source_kind') == 'page_render':
                reason = 'full_page_render_of_verified_index_only_page'
            if reason:
                excluded = self.policy['mode'] == 'exclude'
                self.images.append({"page": page.page_number, "object_ids": sorted(ids),
                                    "reason": reason, "would_exclude": True, "excluded": excluded})
                if excluded:
                    continue
            kept.append((page, image))
        return kept

    def audit(self):
        return {"policy": dict(self.policy), "pages": list(self.pages.values()),
                "objects": self.objects, "images": self.images, "summary": self.metrics(),
                "scope": "Vision selection only; native text/table payload and text routing unchanged"}

    def metrics(self):
        return {"toc_confirmed_pages": sum(p['confirmed'] for p in self.pages.values()),
                "toc_would_exclude_objects": len(self.objects),
                "toc_excluded_objects": sum(r['excluded'] for r in self.objects),
                "toc_excluded_ocr_objects": sum(r['excluded'] and r['before_action'] == 'ocr_required' for r in self.objects),
                "toc_excluded_fallback_images": sum(r['excluded'] for r in self.images)}
