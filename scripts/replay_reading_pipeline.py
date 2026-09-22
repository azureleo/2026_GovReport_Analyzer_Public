"""Replay saved readings through production downstream code, without LLM calls.

Outputs go to a new directory. The source PDF is parsed locally for deterministic
routing/verification only; no text extraction agent, OCR, or Vision model runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--json-only', action='store_true')
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Use a new output directory; existing results are never overwritten.')
    inputs = {str(p.resolve()): digest(p) for p in (args.snapshot, args.source)}
    args.output_dir.mkdir(parents=True)
    attempts = []

    def offline(event, details):
        if event in {'socket.connect', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
            attempts.append(event)
            raise PermissionError('Offline replay prohibits external calls: ' + event)
    sys.addaudithook(offline)

    import config
    from agents.organizer_agent import OrganizerAgent, detect_prior_plan_pages
    from agents.excel_agent import ExcelAgent
    from scripts.compare_operational_visual_merge import _prepare_evaluation_data
    from utils.pdf_reader import extract_pdf
    from utils.visual_merge_ab import load_visual_merge_snapshot
    from utils.reading_pipeline import audit_summary

    start = time.perf_counter()
    snapshot = load_visual_merge_snapshot(args.snapshot)
    document = extract_pdf(args.source, render_graph_pages=False)
    parsed = time.perf_counter()
    agent = OrganizerAgent()
    data = agent.organize(snapshot['data'], prior_plan_pages=detect_prior_plan_pages(document.pages))
    data = _prepare_evaluation_data(data, document)
    prepared = time.perf_counter()
    (args.output_dir / 'prepared_data.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    audit = data.get('reading_pipeline_audit', [])
    (args.output_dir / 'reading_pipeline.json').write_text(json.dumps(
        {**audit_summary(audit), 'records': audit}, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    output = None
    if not args.json_only:
        output = ExcelAgent().write(data, args.output_dir / 'reading_replayed.xlsx')
    ended = time.perf_counter()
    unchanged = all(digest(p) == h for p, h in inputs.items())
    if not unchanged:
        raise RuntimeError('Input file hash changed during replay')
    summary = {
        'input_hashes': inputs, 'inputs_unchanged': unchanged,
        'external_call_attempts': attempts, 'llm_calls': 0,
        'output': str(output) if output else None,
        'seconds': {'local_pdf_parsing': parsed-start, 'downstream': prepared-parsed,
                    'save': ended-prepared, 'total': ended-start},
        'reading': audit_summary(audit),
        'held_reasons': dict(Counter(i for r in audit if r['status']=='held' for i in r['issues'])),
        'business_rows': {k: len(data.get(k, [])) for k in config.EXTRACTION_SHEETS},
        'config': {k: getattr(config, k, None) for k in (
            'READING_PIPELINE_ENABLED', 'REFERENCE_ENRICHMENT_ENABLED',
            'SEMANTIC_ROUTING_ENABLED', 'SEMANTIC_ROUTING_AUTO_RECLASSIFY',
            'VISUAL_MERGE_LABELED_ENABLED', 'VISUAL_EVIDENCE_MERGE_ENABLED',
            'SEMANTIC_OVEREXTRACTION_GUARD_ENABLED')},
        'scope': 'Saved readings + Organizer + deterministic routing/source checks + Excel; no model rereading.',
    }
    (args.output_dir / 'replay_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
