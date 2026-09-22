"""Run the existing sheet cleaners/semantic guard/writer, then verify saved cells.

Exit 0: no drift; 2: file produced but backend drift or cell problems; 1: execution failure.
No model calls; no full Organizer.organize(), no PDF/source CSV reads.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.reading_cell_validation import compare_backend, inspect_workbook

PROFILE = {
    'READING_PIPELINE_ENABLED': '0',  # A/B has already run; preserve frozen verification profile.
    'REFERENCE_ENRICHMENT_ENABLED': '0',
    'CODEBOOK_SHEET_ENABLED': '0', 'PROVENANCE_ENABLED': '1', 'DATA_STATUS_ENABLED': '1',
    'SEMANTIC_OVEREXTRACTION_GUARD_ENABLED': '0', 'SOURCE_VERIFICATION_ENABLED': '0',
    'SOURCE_OBJECT_INVENTORY_ENABLED': '0', 'VISUAL_MERGE_LABELED_ENABLED': '0',
    'RUN_STATE_ENABLED': '0', 'REDUCTION_TARGET_CONTEXT_MODE': 'off', 'PYTHON_DOTENV_DISABLED': '1',
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--stage-a-dir', type=Path, default=ROOT/'output/reading_compatibility_a_smoke_20260907')
    ap.add_argument('--output-dir', type=Path)
    ap.add_argument('--offline-dotenv-shim', action='store_true', help='Explicit no-op load_dotenv, as in the prior isolated experiment')
    args = ap.parse_args()
    out = args.output_dir or ROOT/'output'/('reading_excel_a_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    if out.exists():
        ap.error('Choose a new output directory; overwriting is prohibited.')
    source_path = args.stage_a_dir/'candidate_validation.json'
    source_bytes = source_path.read_bytes()
    candidates = json.loads(source_bytes)['accepted_candidates']
    if not candidates:
        ap.error('No accepted candidates; empty-workbook success is not a valid test.')
    out.mkdir(parents=True)
    def save(name, value):
        (out/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    manifest = dict(started_utc=datetime.now(timezone.utc).isoformat(), input=str(source_path.resolve()),
                    input_sha256=hashlib.sha256(source_bytes).hexdigest(), profile=PROFILE,
                    dotenv_shim=args.offline_dotenv_shim, calls=[], timings={}, source_reads_blocked=[],
                    full_organize_called=False, accuracy_evaluated=False)
    def audit(event, params):
        if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.startfile'):
            raise PermissionError('Offline verification: external calls are prohibited')
        if event == 'open' and params and isinstance(params[0], (str, bytes, os.PathLike)):
            p = Path(os.fsdecode(params[0]))
            mode = params[1] if len(params) > 1 else None
            reading = not isinstance(mode, str) or 'r' in mode or '+' in mode
            if reading and (p.name == '.env' or p.name.startswith('.env.') or p.suffix.lower() in ('.pdf', '.csv', '.png', '.jpg')):
                manifest['source_reads_blocked'].append(str(p))
                raise PermissionError('Source reads prohibited: '+str(p))
    sys.addaudithook(audit)
    try:
        os.environ.update(PROFILE)
        if args.offline_dotenv_shim:
            shim = types.ModuleType('dotenv')
            shim.load_dotenv = lambda *a, **k: False
            sys.modules['dotenv'] = shim
        import config
        import agents.organizer_agent as organizer
        from agents.excel_agent import ExcelAgent
        from utils.semantic_contract_guard import apply_semantic_contract_guards
        schema = {key: getattr(config, key) for key in ('EXTRACTION_SHEETS', 'SHEET_KEY_TO_NAME', 'EXCEL_HEADERS')}
        save('backend_schema.json', schema)
        manifest['effective_profile'] = {k: getattr(config, k) for k in PROFILE if hasattr(config, k)}
        manifest['code_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), ROOT/'config.py', ROOT/'utils/reading_cell_validation.py'] +
            [Path(m.__file__) for n,m in list(sys.modules.items()) if getattr(m, '__file__', None)
             and n.startswith(('utils.', 'agents.')) and Path(m.__file__).is_relative_to(ROOT)]}
        payload = {k: [] for k in config.EXTRACTION_SHEETS}
        for c in candidates:
            key = c['sheet_key']
            if key not in payload or c['sheet_name'] != config.SHEET_KEY_TO_NAME[key]:
                raise ValueError('Candidate sheet contract mismatch')
            ev = c['values'].get('근거ID')
            if not isinstance(ev, str) or set(ev.split(';')) != set(c['source_record_ids']):
                raise ValueError('Candidate evidence/source ID mismatch')
            payload[key].append(deepcopy(c['values']))
        save('backend_input.json', payload)
        manifest['reference_enrichment_skipped'] = [
            dict(function=function, sheet=key,
                 evidence_id=row.get('근거ID'), row_number=i,
                 reason='Fixed-reading verification: reference CSV access disabled')
            for key, function in [('mitigation_projects', '_apply_appendix4_project_match'),
                                  ('quantitative_reductions', '_apply_appendix3_unit_match')]
            for i, row in enumerate(payload[key], 1)
        ]
        clean = {}
        agent = organizer.OrganizerAgent()
        tick = time.perf_counter()
        for key in config.EXTRACTION_SHEETS:
            rows = payload[key]
            municipalities = {r.get('지자체명') for r in rows}
            if len(municipalities) > 1:
                raise ValueError('Mixed municipality sample is not supported')
            municipality = next(iter(municipalities), '알 수 없음')
            clean[key] = agent.organize_sheet(key, deepcopy(rows), municipality)
            manifest['calls'].append('OrganizerAgent.organize_sheet:'+key)
        save('after_sheet_cleaning.json', clean)
        guards = apply_semantic_contract_guards(clean)
        manifest['calls'].append('apply_semantic_contract_guards')
        manifest['timings']['organizer_guard_seconds'] = time.perf_counter()-tick
        save('semantic_guard_records.json', guards)
        save('backend_output.json', clean)
        changes = compare_backend(payload, clean, config.EXTRACTION_SHEETS)
        tick = time.perf_counter()
        ExcelAgent().write(deepcopy(clean), out/'mapped_reading_a.xlsx')
        manifest['calls'].append('ExcelAgent.write')
        manifest['timings']['writer_seconds'] = time.perf_counter()-tick
        tick = time.perf_counter()
        validation = inspect_workbook(out/'mapped_reading_a.xlsx', clean, schema)
        manifest['timings']['cell_check_seconds'] = time.perf_counter()-tick
        validation.update(backend_changes=changes, input_rows=len(candidates),
                          output_rows=sum(len(clean[k]) for k in config.EXTRACTION_SHEETS),
                          accuracy_evaluated=False, strict_pipeline_passed=not changes and validation['passed'])
        save('cell_validation.json', validation)
        code = 0 if validation['strict_pipeline_passed'] else 2
        manifest['status'] = 'completed' if code == 0 else 'completed_with_differences'
        print(json.dumps(dict(output=str(out), input_rows=len(candidates), output_rows=validation['output_rows'],
                              writer_passed=validation['passed'], backend_changes=len(changes), exit_code=code), ensure_ascii=False))
    except Exception:
        code = 1
        manifest['status'] = 'execution_failed'
        manifest['error'] = traceback.format_exc()
        print(manifest['error'], file=sys.stderr)
    finally:
        manifest['input_unchanged'] = source_path.read_bytes() == source_bytes
        if not manifest['input_unchanged']:
            code = 1
        save('execution_manifest.json', manifest)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
