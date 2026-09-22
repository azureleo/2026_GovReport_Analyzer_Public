"""Auditable, loss-averse text inputs. Source PageContent objects are never edited."""
from __future__ import annotations

from contextvars import ContextVar
from collections import Counter
from dataclasses import replace
from functools import wraps
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
import time

import config
from utils.vision_toc import analyze_page, _within

VERSION = "text-input-v1"
CURRENT_TRACE = ContextVar("text_trace", default="")


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def policy_snapshot():
    result = {"version": VERSION}
    for key in ("TEXT_ROUTING_MODE", "TEXT_INPUT_MODE"):
        mode = str(getattr(config, key, "audit")).strip().lower()
        if mode not in {"off", "audit", "optimize"}:
            raise ValueError(f"{key} must be off, audit, or optimize")
        result[key] = mode
    result["cluster_member_recovery_attempts"] = max(0, int(getattr(config, "TEXT_CLUSTER_MEMBER_RETRIES", 1)))
    return result


class _TableText(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.header_parts = []
        self.header_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'th':
            self.header_depth += 1

    def handle_endtag(self, tag):
        if tag == 'th':
            self.header_depth = max(0, self.header_depth - 1)

    def handle_data(self, data):
        self.parts.append(data)
        if self.header_depth:
            self.header_parts.append(data)


def compact_page(page):
    """Remove only unique, bbox-contained blocks also represented in table HTML.

    Exact whitespace-delimited token sequences avoid 12 matching 120, or a
    numerical match that loses its unit. Captions, notes and source lines stay.
    """
    text = page.text or ""
    removed = []
    for block in page.text_blocks:
        raw = str(block.get("text") or "").strip()
        tokens = raw.split()
        if len(tokens) < 3 or text.count(raw) != 1 or re.search(r"단위|자료|출처|주\s*[:：]|^(?:표|그림)\s*\d|^\s*(?:\d+[.)]|제\s*\d+[장절])\s", raw):
            continue
        for record in page.table_records:
            # Text-alignment fallback often wraps prose/section headings in a
            # synthetic table. Its bounding rectangle is not sufficient proof.
            if record.get('strategy') not in {'lines', 'lines_strict'}:
                continue
            html = str(record.get("html") or "")
            if html not in page.tables or not _within(block.get("bbox"), record.get("bbox"), tolerance=0):
                continue
            parsed = _TableText(html)
            table_tokens = " ".join(parsed.parts).split()
            header_tokens = " ".join(parsed.header_parts).split()
            if any(header_tokens[i:i + len(tokens)] == tokens for i in range(len(header_tokens) - len(tokens) + 1)):
                continue
            if not any(table_tokens[i:i + len(tokens)] == tokens for i in range(len(table_tokens) - len(tokens) + 1)):
                continue
            text = text.replace(raw, "", 1)
            removed.append({"block_id": block.get("block_id"), "bbox": block.get("bbox"),
                            "table_index": record.get("table_index"), "raw_text": raw,
                            "reason": "unique_block_inside_table_with_exact_token_sequence"})
            break
    return replace(page, text=text), removed


def text_index_only(page, toc):
    """Vision's 90% rule is insufficient to discard text: retain any residue
    other than an index heading and a Roman/Arabic printed page number.
    """
    if toc.get('role') != 'toc_index':
        return False
    residue = re.sub(r'\s+', '', page.text or '')
    for entry in toc['entries']:
        raw = re.sub(r'\s+', '', entry['raw_text'])
        if residue.count(raw) != 1:
            return False
        residue = residue.replace(raw, '', 1)
    residue = re.sub(r'^(?:(?:표|그림)?목차|tableofcontents|listof(?:tables|figures))', '', residue, flags=re.I)
    return not residue or bool(re.fullmatch(r'[\divxlcdmⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+', residue, re.I))


def repeated_margin_blocks(pages):
    """Shadow evidence only: never discard a header/footnote based on frequency."""
    candidates = {}
    counts = Counter()
    for page in pages:
        rows = []
        for block in page.text_blocks:
            bbox = block.get('bbox')
            text = str(block.get('text') or '').strip()
            if not bbox or len(bbox) != 4 or not page.height or not text or len(text) > 160:
                continue
            if bbox[3] <= page.height * .12 or bbox[1] >= page.height * .9:
                rows.append(block)
        candidates[page.page_number] = rows
        counts.update(set(re.sub(r'\s+', '', row['text']) for row in rows))
    limit = max(8, int(len(pages) * .6))
    return {n: [r for r in rows if counts[re.sub(r'\s+', '', r['text'])] >= limit]
            for n, rows in candidates.items()}


class TextPolicy:
    def __init__(self, pages):
        self.policy = policy_snapshot()
        self.pages = list(pages)
        self.page_audit = []
        self.by_number = {}
        margins = repeated_margin_blocks(pages)
        for page in pages:
            toc = analyze_page(page) if self.policy["TEXT_ROUTING_MODE"] != "off" else {}
            compact, removed = compact_page(page) if self.policy["TEXT_INPUT_MODE"] != "off" else (page, [])
            self.by_number[page.page_number] = compact if self.policy["TEXT_INPUT_MODE"] == "optimize" else page
            self.page_audit.append({"page": page.page_number, "toc": toc,
                                   "text_index_only": text_index_only(page, toc),
                                   "repeated_margin_blocks_shadow_only": margins.get(page.page_number, []),
                                   "original_chars": len(page.text or ""), "candidate_chars": len(compact.text or ""),
                                   "removed_blocks": removed, "input_applied": self.policy["TEXT_INPUT_MODE"] == "optimize",
                                   "source_sha256": digest([page.text, page.tables])})
        self.route_audit = []

    def route(self, routed):
        # Body routing is deliberately unchanged. Repeated headers alone are
        # insufficient negative evidence; no score/cap/first-N-page shortcuts.
        excluded = {r["page"] for r in self.page_audit if r["text_index_only"]}
        result = {}
        for key, pages in routed.items():
            kept = []
            for page in pages:
                proposed = page.page_number in excluded
                applied = proposed and self.policy["TEXT_ROUTING_MODE"] == "optimize"
                self.route_audit.append({"sheet": key, "page": page.page_number,
                                         "would_exclude": proposed, "excluded": applied,
                                         "reason": "verified_index_only" if proposed else "existing_route_and_context_preserved"})
                if not applied:
                    kept.append(self.by_number.get(page.page_number, page))
            result[key] = kept
        return result

    def audit(self):
        return {"policy": self.policy, "pages": self.page_audit, "routes": self.route_audit,
                "summary": {"page_sheet_assignments": len(self.route_audit),
                            "would_exclude_assignments": sum(r["would_exclude"] for r in self.route_audit),
                            "excluded_assignments": sum(r["excluded"] for r in self.route_audit),
                            "duplicate_blocks": sum(len(r["removed_blocks"]) for r in self.page_audit),
                            "candidate_saved_chars_unique_pages": sum(r["original_chars"] - r["candidate_chars"] for r in self.page_audit)},
                "limits": "TOC-only routing reduction; body scores/context unchanged. Character counts are not tokens or accuracy."}


def trace_task(kind):
    """Track recursive and restored tasks even without a persistent RunState."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(self, task, *args, **kwargs):
            trace_id = digest([kind, self._task_sheet_keys(task), task.get("batch_text"), task.get("object_id"), task.get("parent_trace_id")])
            task["trace_id"] = trace_id
            row = {"trace_id": trace_id, "parent_trace_id": task.get("parent_trace_id"),
                   "kind": kind, "sheets": self._task_sheet_keys(task), "pages": task.get("page_nums", []),
                   "object_ids": [o.object_id for p in task.get("page_nums", []) for o in self._document_objects_by_page.get(p, ())],
                   "split_depth": int(task.get("split_depth", 0)), "input_chars": len(task.get("batch_text", "")),
                   "input_sha256": digest(task.get("batch_text", "")), "status": "running"}
            start = time.perf_counter()
            token = CURRENT_TRACE.set(trace_id)
            self.task_audit.append(row)
            try:
                result = fn(self, task, *args, **kwargs)
                row["status"] = task.get("effective_status", "restored_or_skipped")
                row["rows"] = {k: len(v) for k, v in result.items()} if isinstance(result, dict) else {task.get("sheet_key"): len(result or [])}
                return result
            except Exception as exc:
                row["status"] = "parse_fail" if type(exc).__name__.endswith("ParseError") else "call_fail"
                row["error"] = str(exc)[:300]
                raise
            finally:
                row["elapsed_seconds"] = round(time.perf_counter() - start, 6)
                CURRENT_TRACE.reset(token)
        return wrapped
    return decorate


def plan_summary(tasks):
    return {"root_tasks": len(tasks), "input_chars": sum(len(t.get("batch_text", "")) for t in tasks),
            "sheet_page_pairs": sorted({(k, p) for t in tasks for k in t.get("members", [t.get("sheet_key")]) for p in t.get("page_nums", [])}),
            "task_sha256": digest([[t.get("members", [t.get("sheet_key")]), t.get("page_nums"), t.get("batch_text")] for t in tasks])}
