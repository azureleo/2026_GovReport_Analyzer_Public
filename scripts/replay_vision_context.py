"""Offline raw-response replay of a bounded Vision live-check delivery.

Never re-read the PDF, invoke an LLM, edit answers, or supply missing table IDs.
Exit 2 means preserved review items; exit 1 means a failed verification.
"""
from pathlib import Path
from copy import deepcopy
from collections import Counter
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_vision_recovery import PROFILE, code_hashes
os.environ.update(PROFILE)
from utils.vision_recovery_workflow import read_json, file_hash, write_json
from utils.vision_recovery_runtime import offline_guard, observations_from_response, save_and_verify
from utils.visual_contract import normalize_visual_table_rows


def replay(source, output):
    source, output = source.resolve(), output.resolve()
    plan = read_json(source / 'plan.json')
    paths = [source / 'plan.json', source / 'manifest.json', source / 'fresh_snapshot.json',
             source / 'downstream/backend_output.json', source / 'downstream/cell_validation.json',
             source / 'downstream/result.xlsx']
    paths += [source / f"page_{task['page']}" / name for task in plan['tasks']
              for name in ('raw_response.txt', 'normalized_response.json')]
    original_hashes = {str(p): file_hash(p) for p in paths}
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    combined = {'municipality_name': '알 수 없음', 'chart_observations': [], 'document_objects': []}
    pages = {}
    baseline = read_json(source / 'downstream/backend_output.json')
    with offline_guard():
        for task in plan['tasks']:
            page = task['page']
            (output / f'page_{page}').mkdir()
            raw = read_json(source / f'page_{page}/raw_response.txt')
            old = read_json(source / f'page_{page}/normalized_response.json')
            if raw.get('type') != 'chart_table' or raw.get('page_number') != page:
                raise ValueError('Unsupported raw response or page mismatch')
            # Transport provenance is copied from the original successful call,
            # then checked against the frozen plan by observations_from_response.
            parsed = deepcopy(old)
            parsed.update(deepcopy(raw))
            parsed['table'] = normalize_visual_table_rows(
                raw['table'], chart_type=raw.get('chart_type', ''), title=raw.get('title', ''))
            observations = observations_from_response(parsed, task)
            for i, row in enumerate(observations, 1):
                row['_recovery_id'] = f'reuse-p{page}-{i:04}'
            combined['chart_observations'].extend(observations)
            combined['document_objects'].append({'object_id': task['object_ids'][0],
                'object_type': 'image', 'page_number': page, 'bbox': deepcopy(task['bbox']),
                'metadata': {'evidence_id': task['evidence_ids'][0], 'final_status': 'extracted'}})
            write_json(output / f'page_{page}/normalized_response.json', parsed)
            write_json(output / f'page_{page}/observations.json', observations)
            functions = lambda table: [r for r in table if r.get('fields', {}).get('정보유형') == '조직기능']
            pages[str(page)] = {'raw_rows': len(raw['table']), 'before_normalized_rows': len(old['table']),
                'after_normalized_rows': len(parsed['table']), 'raw_functions': len(functions(raw['table'])),
                'before_functions': len(functions(old['table'])), 'after_functions': len(functions(parsed['table']))}
        write_json(output / 'fresh_snapshot.json', combined)
        cleaned, cells = save_and_verify(combined, output / 'downstream', False)
    import config
    for page, stats in pages.items():
        page_num = int(page)
        current = [r for r in cleaned['chart_observations'] if r['페이지'] == page_num]
        old_rows = [r for key in config.EXTRACTION_SHEETS for r in baseline.get(key, [])
                    if str(r.get('출처페이지')) == page]
        new_rows = [r for key in config.EXTRACTION_SHEETS for r in cleaned.get(key, [])
                    if str(r.get('출처페이지')) == page]
        stats.update(before_business_rows=len(old_rows), after_business_rows=len(new_rows),
                     observation_status=dict(Counter(r.get('병합상태') for r in current)))
    held = [{k: r.get(k) for k in ('_recovery_id', '페이지', '항목', '값', '단위', '판독필드', '병합상태', '병합차단사유')}
            for r in cleaned['chart_observations'] if r.get('병합상태') not in ('accept', 'fix_then_merge', 'duplicate')]
    preserved = all(file_hash(p) == sha for p, sha in original_hashes.items())
    report = {'mode': 'offline_raw_response_reuse', 'llm_calls': 0, 'pdf_rereads': 0,
        'source': str(source), 'original_hashes': original_hashes, 'original_files_unchanged': preserved,
        'code_hashes': code_hashes(), 'seconds': time.perf_counter() - started, 'pages': pages,
        'cell_validation': {k: cells[k] for k in ('passed', 'deterministic', 'issues')},
        'reading_issues': dict(Counter(issue for r in cleaned.get('reading_pipeline_audit', []) for issue in r.get('issues', []))),
        'held_count': len(held), 'held_rows': held,
        'limitations': ['Development sample; not an independent holdout or whole-document accuracy.',
                       'New prompt wording is not tested by offline response reuse.',
                       'Missing table references are not supplied from page order or organization names.']}
    report['exit_code'] = 1 if not cells['passed'] or not preserved else 2 if held else 0
    write_json(output / 'evaluation.json', report)
    print(json.dumps({k: report[k] for k in ('llm_calls', 'seconds', 'pages', 'cell_validation',
                                            'held_count', 'original_files_unchanged', 'exit_code')}, ensure_ascii=False, indent=2))
    return report['exit_code']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    sys.exit(replay(args.source, args.output_dir))
