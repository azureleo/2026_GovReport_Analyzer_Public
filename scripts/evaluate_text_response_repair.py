"""Evaluate a saved-response repair with frozen source facts, without LLM calls."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_text_binding_replay import read_book, matches

def load(path):
    return json.loads(path.read_text(encoding='utf-8'))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline',type=Path,required=True)
    ap.add_argument('--replay-dir',type=Path,required=True)
    ap.add_argument('--facts',type=Path,required=True)
    args=ap.parse_args()
    output=args.replay_dir/'evaluation.json'
    if output.exists():
        ap.error('Evaluation exists; never overwrite prior results.')
    paths=[args.baseline,args.replay_dir/'reading_replayed.xlsx',args.facts]
    hashes={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    before,_,_=read_book(args.baseline)
    prepared=load(args.replay_dir/'prepared_data.json')
    after,checks,errors=read_book(paths[1],prepared)
    facts=load(args.facts)
    scores={};details=[]
    for fact in facts:
        old=sum(matches(r,fact) for r in before[fact['sheet']])
        new=sum(matches(r,fact) for r in after[fact['sheet']])
        group=scores.setdefault(fact['group'],dict(before=0,after=0,total=0))
        group['before']+=bool(old);group['after']+=bool(new);group['total']+=1
        details.append(dict(fact_id=fact['id'],before=bool(old),after=bool(new),matches_after=new))
    held=[r for r in prepared.get('reading_pipeline_audit',[]) if r['status']=='held']
    unchanged={}
    for key in ('document_meta','plan_overview','reduction_targets','mitigation_projects','annual_implementation','foundation_measures'):
        # New optional schema fields are ignored when testing unaffected values.
        old_headers=set().union(*(r.keys() for r in before[key])) if before[key] else set()
        signature=lambda rows:Counter(json.dumps({k:r.get(k) for k in old_headers},ensure_ascii=False,sort_keys=True,default=str) for r in rows)
        unchanged[key]=signature(before[key])==signature(after[key])
    result=dict(scope='Fixed selected source-fact preservation, not official full-document accuracy.',
        input_hashes=hashes,facts_count=len(facts),scores=scores,cell_checks=checks,excel_errors=errors,
        readback_passed=not errors and all(c['headers_match'] and c['cells_match'] for c in checks.values()),
        held_rows=len(held),held_reasons=dict(Counter(i for r in held for i in r['issues'])),
        business_rows={k:len(v) for k,v in after.items()},unchanged_business_values=unchanged,
        budget_misplacements=sum(str(r.get('출처페이지'))=='6' for r in after['annual_implementation']),
        numeric_budgets=sum(type(r.get('예산액')) in (int,float) for r in after['financial_plan']),
        duplicate_fact_matches=[r for r in details if r['matches_after']>1],
        source_files_unchanged=all(hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p.resolve())] for p in paths))
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    with (args.replay_dir/'source_fact_checks.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(details[0]));writer.writeheader();writer.writerows(details)
    print(json.dumps({k:v for k,v in result.items() if k not in ('cell_checks','input_hashes')},ensure_ascii=False,indent=2))
    if not result['readback_passed'] or not result['source_files_unchanged']:
        raise SystemExit(2)

if __name__=='__main__':
    main()
