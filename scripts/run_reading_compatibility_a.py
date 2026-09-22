"""Offline Stage A audit. Does not call models, Organizer, or Excel writer."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.reading_compatibility import convert, evaluate_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Frozen raw_reading.json')
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--schema', type=Path, required=True, help='Frozen backend_schema.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--record-ids', type=Path, help='JSON array of selected IDs; retain full context')
    args = parser.parse_args()
    paths = [args.input, args.metadata, args.schema]
    if args.record_ids:
        paths.append(args.record_ids)
    original = {str(p.resolve()): p.read_bytes() for p in paths}
    read = lambda p: json.loads(original[str(p.resolve())].decode('utf-8-sig'))
    source, metadata, schema = map(read, paths[:3])
    rules_path = Path(__file__).resolve().parents[1]/'data/reading_mapping_rules_v1.json'
    rules = json.loads(rules_path.read_text(encoding='utf-8'))
    if args.output_dir.exists():
        parser.error('Output directory already exists; choose a new directory.')
    args.output_dir.mkdir(parents=True)
    compatible, report = convert(source, metadata, schema)
    selected = read(args.record_ids) if args.record_ids else None
    if selected is not None and (not isinstance(selected, list) or any(not isinstance(x, str) for x in selected)):
        raise ValueError('--record-ids must contain a JSON array of strings')
    result = {'backend_executed': False, 'accuracy_evaluated': False}
    if not report['structural_issues']:
        try:
            result = evaluate_candidates(compatible, metadata, schema, rules, selected)
        except Exception as exc:
            import traceback
            result['adapter_error'] = dict(type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
    for p, data in original.items():
        if Path(p).read_bytes() != data:
            raise RuntimeError('Input changed during execution: '+p)
    report['input_sha256'] = {p: hashlib.sha256(data).hexdigest() for p, data in original.items()}
    report['code_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), Path(__file__).resolve().parents[1]/'utils/reading_compatibility.py', Path(__file__).resolve().parents[1]/'utils/reading_adapter_v1.py', Path(__file__).resolve().parents[1]/'utils/reading_position.py', rules_path]}
    # Conversion and metadata are separate; source is never overwritten.
    for name, value in [('compatible_reading_a.json', compatible), ('compatibility_report.json', report), ('candidate_validation.json', result)]:
        (args.output_dir/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output_dir), 'changes': len(report['changes']), 'structural_issues': len(report['structural_issues']), 'field_conflicts': len(report['field_conflicts']), 'accepted_candidates': len(result.get('accepted_candidates', [])), 'rejected_candidates': len(result.get('rejected_candidates', [])), 'adapter_error': result.get('adapter_error'), 'backend_executed': False}, ensure_ascii=False))
    return 1 if report['structural_issues'] or result.get('adapter_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
