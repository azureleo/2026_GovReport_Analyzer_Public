"""Read-only, fixed-fact evaluation of a saved text A/B downstream replay.

Uses an existing source_facts.json; never builds answers from the replay itself.
No model calls or workbook modifications. Findings are written beside the replay.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config
import openpyxl


def norm(value):
    return re.sub(r'\s+', '', str(value or '')).replace(',', '').replace('CO₂', 'CO2').replace('㎥', 'm3')


def equal(actual, expected):
    if type(expected) in (int, float):
        try:
            return not isinstance(actual, bool) and abs(float(actual) - expected) < 1e-7
        except (ValueError, TypeError):
            return False
    return norm(actual) == norm(expected)


def matches(row, fact):
    identity = fact['identity']
    identified = norm(identity) in [norm(row.get(k)) for k in ('관리번호', '사업명', '집계대상원문')]
    if identity == 'B1-9':
        identified = identified or '희망의집수리' in norm(row.get('사업명'))
    if not identified:
        return False
    year = '점검연도' if fact['sheet'] == 'monitoring_performance' else '연도'
    if fact['total']:
        if row.get(year) not in (None, '') or (row.get('기간시작'), row.get('기간종료'), row.get('시간기준')) != (2024, 2033, 'period_total'):
            return False
    elif not equal(row.get(year), fact['year']):
        return False
    if fact['sheet'] == 'monitoring_performance':
        return norm(fact['value']) in norm(row.get(fact['field']))
    return equal(row.get(fact['field']), fact['value']) and equal(row.get(fact['unit_field']), fact['unit'])


def stored(value):
    if isinstance(value, bool):
        return 'Y' if value else 'N'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str):
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value) or None
    return value


def read_book(path, prepared=None):
    wb = openpyxl.load_workbook(path, read_only=False, data_only=False)
    data, checks, errors = {}, {}, []
    for key in config.EXTRACTION_SHEETS:
        name = config.SHEET_KEY_TO_NAME[key]
        ws = wb[name]
        values = list(ws.values)
        headers = values[0]
        matrix = [r for r in values[1:] if any(v is not None for v in r)]
        data[key] = [dict(zip(headers, row)) for row in matrix]
        if prepared is not None:
            expected = []
            for row in prepared.get(key, []):
                expected.append(tuple(stored(row.get(h, row.get('감축률') if h == '감축률(%)' else None)) for h in headers))
            checks[key] = dict(rows=len(matrix), expected_rows=len(expected),
                               headers_match=list(headers) == config.EXCEL_HEADERS[name],
                               cells_match=Counter(matrix) == Counter(expected),
                               checked_cells=len(matrix)*len(headers), freeze_panes=ws.freeze_panes)
    for ws in wb:
        for row in ws:
            for cell in row:
                if cell.data_type in ('e', 'f'):
                    errors.append(f'{ws.title}!{cell.coordinate}: {cell.value}')
    wb.close()
    return data, checks, errors


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--original-dir', type=Path, required=True)
    ap.add_argument('--replay-dir', type=Path, required=True)
    args = ap.parse_args()
    output = args.replay_dir / 'evaluation.json'
    if output.exists():
        ap.error('Evaluation exists; choose a new replay directory.')
    facts_path = args.original_dir / 'source_facts.json'
    facts = json.loads(facts_path.read_text(encoding='utf-8'))
    original_eval = json.loads((args.original_dir/'evaluation.json').read_text(encoding='utf-8'))
    result = dict(metric='Fixed source-fact preservation, not full-document cell accuracy or row precision.',
                  facts_count=len(facts), facts_sha256=digest(facts_path), arms={})
    details = []
    changed_sheets = {'annual_implementation', 'financial_plan', 'quantitative_reductions'}
    for arm, stem in [('baseline','baseline_per_sheet'), ('clustered','clustered_sheets')]:
        original = args.original_dir / (stem+'.xlsx')
        current = args.replay_dir / arm / 'reading_replayed.xlsx'
        data = json.loads((args.replay_dir/arm/'prepared_data.json').read_text(encoding='utf-8'))
        before, _, _ = read_book(original)
        after, checks, errors = read_book(current, data)
        scores = {}
        for fact in facts:
            old = sum(matches(r, fact) for r in before.get(fact['sheet'], []))
            new = sum(matches(r, fact) for r in after.get(fact['sheet'], []))
            group = scores.setdefault(fact['group'], dict(before=0, after=0, total=0))
            group['before'] += bool(old); group['after'] += bool(new); group['total'] += 1
            details.append(dict(arm=arm, fact_id=fact['id'], before=bool(old), after=bool(new), matches_after=new))
        untouched = {}
        for key in set(config.EXTRACTION_SHEETS) - changed_sheets:
            signature = lambda rows: Counter(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) for r in rows)
            untouched[key] = signature(before[key]) == signature(after[key])
        # Complete raw holds, with original IDs/rows and reason, remain in JSON.
        held = [r for r in data['reading_pipeline_audit'] if r['status'] == 'held']
        result['arms'][arm] = dict(scores=scores, cell_checks=checks, excel_errors=errors,
            original_workbook_unchanged=digest(original) == original_eval['arms'][arm]['workbook_sha256'],
            workbook_sha256=digest(current), unaffected_business_sheets_unchanged=untouched,
            held_rows=len(held), held_reasons=dict(Counter(reason for r in held for reason in r['issues'])),
            business_rows_before=sum(map(len, before.values())), business_rows_after=sum(map(len, after.values())),
            wrong_budget_annual_rows_after=sum(str(r.get('출처페이지')) == '6' for r in after['annual_implementation']),
            numeric_budget_rows_after=sum(type(r.get('예산액')) in (int, float) for r in after['financial_plan']),
            readback_passed=not errors and all(c['cells_match'] and c['headers_match'] for c in checks.values()))
    result['code_sha256'] = {p: digest(ROOT/p) for p in (
        'utils/reading_pipeline.py', 'utils/reading_semantic_binding.py', 'utils/reading_financial_period.py',
        'utils/reading_year.py', 'utils/semantic_contract_guard.py', 'agents/organizer_agent.py',
        'agents/extractor_agent.py', 'utils/excel_writer.py', 'scripts/replay_reading_pipeline.py',
        'scripts/evaluate_text_binding_replay.py')}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with (args.replay_dir/'source_fact_checks.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(details[0])); writer.writeheader(); writer.writerows(details)
    print(json.dumps({arm:{k:v for k,v in values.items() if k not in ('cell_checks',)} for arm,values in result['arms'].items()}, ensure_ascii=False, indent=2))
    if not all(a['readback_passed'] and a['original_workbook_unchanged'] for a in result['arms'].values()):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
