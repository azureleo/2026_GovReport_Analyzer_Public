"""Stage A: lossless format adaptation, not semantic repair or inference."""
from copy import deepcopy
from collections import defaultdict
import math
from utils.reading_position import coordinate_key, same_cell_key

VERSION = 'reading-compatibility-A/1.3'
UNIT_ALIASES = {
    '백만 원': '백만원', '억 원': '억원',
    # Same scale and same CO2-equivalent meaning only; no numeric conversion.
    'tCO₂eq.': 'tCO2eq', 'tCO₂eq': 'tCO2eq', 'tCO2eq.': 'tCO2eq',
    '천 톤CO₂eq.': '천톤CO2eq', '천 톤CO₂eq': '천톤CO2eq',
    '천 tCO₂eq': '천톤CO2eq', '천 톤CO2eq.': '천톤CO2eq',
    '백만 톤CO₂eq': '백만톤CO2eq',
}


def convert(source, metadata, schema):
    """Return a separate input, reversible change log, and structural issues.

    No linked_value is copied into business fields. Original strings, scopes,
    classifications, numbers, and units are retained in the source artifact.
    """
    out = deepcopy(source)
    changes, issues = [], []

    def issue(code, path, **detail):
        issues.append(dict(code=code, path=path, **detail))

    def replace(obj, key, value, path, rule):
        old = deepcopy(obj.get(key))
        if old != value:
            changes.append(dict(path=path, before=old, after=deepcopy(value), rule=rule))
            obj[key] = value

    indexes = {}
    for area, idkey in [('objects', '객체ID'), ('records', '레코드ID'), ('relations', '관계ID')]:
        rows = out.get(area)
        if not isinstance(rows, list):
            issue('expected_list', area)
            indexes[area] = set()
            continue
        ids = set()
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                issue('expected_object', f'{area}/{i}')
                continue
            rid = row.get(idkey)
            if not isinstance(rid, str) or not rid or rid in ids:
                issue('missing_or_duplicate_id', f'{area}/{i}/{idkey}')
            else:
                ids.add(rid)
        indexes[area] = ids
    for i, obj in enumerate(out.get('objects', []) if isinstance(out.get('objects'), list) else []):
        if not isinstance(obj, dict):
            continue
        value = obj.get('공통문맥')
        if isinstance(value, str):
            # Existing consumer reads named keys; no properties are inferred.
            replace(obj, '공통문맥', {'원문텍스트': value}, f'objects/{i}/공통문맥', 'context_string_wrapper')
        elif value is not None and not isinstance(value, dict):
            issue('invalid_context_type', f'objects/{i}/공통문맥', object_id=obj.get('객체ID'))
    reverse = {v: k for k, v in schema['SHEET_KEY_TO_NAME'].items()}
    for i, row in enumerate(out.get('records', []) if isinstance(out.get('records'), list) else []):
        if not isinstance(row, dict):
            continue
        path = f'records/{i}'
        rid = row.get('레코드ID')
        position = row.get('객체 내 위치')
        if isinstance(position, dict):
            try:
                for coordinate in ('데이터행', '데이터열'):
                    if position.get(coordinate) is not None:
                        coordinate_key(position[coordinate])
                same_cell_key(row.get('객체ID'), position)
            except ValueError as exc:
                issue('invalid_cell_position', path+'/객체 내 위치', record_id=rid, detail=str(exc))
        elif position is not None and not isinstance(position, str):
            issue('invalid_position_type', path+'/객체 내 위치', record_id=rid)
        if row.get('객체ID') not in indexes['objects']:
            issue('unknown_object', path, record_id=rid)
        for field in ('행머리글 경로', '열머리글 경로'):
            if row.get(field) is not None and not isinstance(row[field], list):
                issue('expected_list', path+'/'+field, record_id=rid)
        number = row.get('숫자값')
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)):
            issue('invalid_number', path+'/숫자값', record_id=rid)
        ctx = row.get('문맥 구분')
        if ctx is None:
            ctx = {}
        if not isinstance(ctx, dict):
            issue('invalid_record_context', path+'/문맥 구분', record_id=rid)
            continue
        target = ctx.get('대상시트')
        if target is not None and not isinstance(target, str):
            issue('invalid_sheet_type', path+'/문맥 구분/대상시트', record_id=rid)
        elif target in reverse:
            replace(ctx, '대상시트', reverse[target], path+'/문맥 구분/대상시트', 'exact_sheet_alias')
        elif target is not None and target not in schema['EXTRACTION_SHEETS']:
            issue('unknown_sheet', path+'/문맥 구분/대상시트', record_id=rid)
        business = ctx.get('업무필드')
        if business is not None and not isinstance(business, dict):
            issue('invalid_business_fields', path+'/문맥 구분/업무필드', record_id=rid)
        if row.get('값상태') == '명시':
            replace(row, '값상태', '명시값', path+'/값상태', 'exact_status_alias')
        for obj, prefix, fields in [(row, path, ['단위원문']), (business or {}, path+'/문맥 구분/업무필드', ['단위', '예산단위', '목표단위', '활동단위'])]:
            if not isinstance(obj, dict):
                continue
            for field in fields:
                unit = obj.get(field)
                if isinstance(unit, str) and unit in UNIT_ALIASES:
                    replace(obj, field, UNIT_ALIASES[unit], prefix+'/'+field, 'same_scale_unit_alias')
    for i, rel in enumerate(out.get('relations', []) if isinstance(out.get('relations'), list) else []):
        if not isinstance(rel, dict):
            continue
        for key in ('출발 레코드 또는 노드ID', '도착 레코드 또는 노드ID'):
            if rel.get(key) not in indexes['records']:
                issue('unknown_relation_endpoint', f'relations/{i}/{key}')
    links = metadata.get('links') if isinstance(metadata, dict) else None
    if not isinstance(links, list):
        issue('invalid_metadata_links', 'metadata/links')
        links = []
    link_ids = set()
    groups = defaultdict(list)
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            issue('invalid_metadata_link', f'metadata/links/{i}')
            continue
        lid = link.get('link_id')
        if not isinstance(lid, str) or lid in link_ids:
            issue('invalid_link_id', f'metadata/links/{i}')
        link_ids.add(lid)
        for key in ('measurement_id', 'metadata_id'):
            if link.get(key) not in indexes['records']:
                issue('unknown_metadata_endpoint', f'metadata/links/{i}/{key}')
        if link.get('target_field'):
            groups[(link.get('measurement_id'), link['target_field'])].append(link)
    import json
    conflicts = [dict(record_id=k[0], field=k[1], links=v) for k, v in groups.items()
                 if len({json.dumps(l.get('linked_value'), sort_keys=True, ensure_ascii=False) for l in v}) > 1]
    return out, dict(version=VERSION, changes=changes, structural_issues=issues, field_conflicts=conflicts)


def evaluate_candidates(compatible, metadata, schema, rules, selected_ids=None):
    """Run the frozen adapter; validate, never silently repair its decisions."""
    from utils import reading_adapter_v1 as legacy
    items = legacy.normalize(compatible, rules)
    if selected_ids is not None:
        unknown = set(selected_ids) - {i['source_record_ids'][0] for i in items}
        if unknown:
            raise ValueError('Unknown selected record IDs: '+repr(sorted(unknown)))
        items = [i for i in items if i['source_record_ids'][0] in selected_ids]
    candidates, reasons, duplicates = legacy.make_candidates(compatible, items, rules, schema)
    rows = {r['레코드ID']: r for r in compatible['records']}
    accepted, rejected = [], []
    for candidate in candidates:
        problems = []
        for rid in candidate['source_record_ids']:
            original = rows[rid]
            target = (original.get('문맥 구분') or {}).get('대상시트')
            if target and target != candidate['sheet_key']:
                problems.append('explicit_sheet_mismatch')
            # Model classification is evidence, not a silently overriding rule.
            declared = original.get('레코드분류')
            calculated = next(i['classification'] for i in items if i['source_record_ids'][0] == rid)
            if declared and declared != calculated:
                problems.append('classification_mismatch')
        if problems:
            rejected.append(dict(candidate=candidate, issues=sorted(set(problems))))
        else:
            accepted.append(candidate)
    return dict(accepted_candidates=accepted, rejected_candidates=rejected,
                adapter_reasons=reasons, duplicate_events=duplicates,
                selected_records=len(items),
                backend_executed=False, accuracy_evaluated=False)
