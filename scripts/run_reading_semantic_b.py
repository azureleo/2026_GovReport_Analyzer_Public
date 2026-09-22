"""Offline, opt-in B experiment; never rereads PDFs or changes A artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.reading_compatibility import convert, evaluate_candidates
from utils.reading_semantic_binding import bind_candidates


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--schema', type=Path, required=True)
    p.add_argument('--record-ids', type=Path)
    p.add_argument('--region-context', type=Path, help='Optional explicit object-scoped region evidence JSON; never modifies raw input')
    p.add_argument('--output-dir', type=Path, required=True)
    a = p.parse_args()
    if a.output_dir.exists():
        p.error('Choose a new output directory; existing outputs are never overwritten.')
    paths = [a.input, a.metadata, a.schema] + ([a.record_ids] if a.record_ids else [])
    frozen = {x: x.read_bytes() for x in paths}
    read = lambda x: json.loads(frozen[x].decode('utf-8-sig'))
    raw, meta, schema = [read(x) for x in paths[:3]]
    selected = read(a.record_ids) if a.record_ids else None
    region_context = None
    if a.region_context:
        frozen[a.region_context] = a.region_context.read_bytes()
        region_context = read(a.region_context)
        if not isinstance(region_context, dict) or not isinstance(region_context.get('objects'), list):
            p.error('region-context requires an objects array')
        known_objects = {o['객체ID'] for o in raw['objects']}
        for entry in region_context['objects']:
            if not isinstance(entry, dict) or entry.get('object_id') not in known_objects or not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('region','evidence','scope')):
                p.error('Invalid region-context object, region, evidence, or scope')
    if selected is not None and (not isinstance(selected, list) or any(not isinstance(x, str) for x in selected)):
        p.error('record-ids must be a JSON array of strings')
    rules_path = ROOT / 'data/reading_mapping_rules_v1.json'
    rules = json.loads(rules_path.read_text(encoding='utf-8'))
    compatible, report = convert(raw, meta, schema)
    if report['structural_issues']:
        p.error('A structural validation failed: ' + repr(report['structural_issues']))
    baseline = evaluate_candidates(compatible, meta, schema, rules, selected)
    result = bind_candidates(compatible, meta, schema, rules, selected, region_context)
    if result != bind_candidates(compatible, meta, schema, rules, selected, region_context):
        raise RuntimeError('Replay mismatch')
    ids = lambda r: {i for c in r['accepted_candidates'] for i in c['source_record_ids']}
    before, after = ids(baseline), ids(result)
    comparison = dict(a_candidates=len(before), b_candidates=len(after),
                      gained_ids=sorted(after-before), lost_ids=sorted(before-after),
                      deterministic_replay=True, accuracy_evaluated=False)
    for path, content in frozen.items():
        if path.read_bytes() != content:
            raise RuntimeError('Input changed: ' + str(path))
    report['input_sha256'] = {str(x.resolve()): hashlib.sha256(v).hexdigest() for x,v in frozen.items()}
    report['code_sha256'] = {str(x): hashlib.sha256(x.read_bytes()).hexdigest() for x in [
        Path(__file__), ROOT/'utils/reading_semantic_binding.py', ROOT/'utils/reading_compatibility.py',
        ROOT/'utils/reading_adapter_v1.py', ROOT/'utils/reading_position.py', ROOT/'utils/reading_financial_period.py', ROOT/'utils/reading_context_facts.py', rules_path]}
    a.output_dir.mkdir(parents=True)
    for name, value in [('compatible_reading_b.json', compatible), ('metadata_links.json', meta),
                        ('compatibility_report.json', report), ('candidate_validation.json', result),
                        ('baseline_a_candidates.json', baseline), ('comparison_a_b.json', comparison)]:
        (a.output_dir/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps(comparison, ensure_ascii=False))


if __name__ == '__main__':
    main()
