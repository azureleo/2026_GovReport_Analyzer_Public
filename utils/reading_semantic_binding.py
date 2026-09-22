"""Opt-in B adapter. Frozen A conversion stays untouched; no model calls.

Only explicit same-object relations may supply approved metadata fields.
This validates contracts, not the factual correctness of the PDF reading.
"""
from collections import defaultdict
from copy import deepcopy
import math
import json

from utils.reading_compatibility import UNIT_ALIASES, evaluate_candidates
from utils.reading_adapter_v1 import scope
from utils.reading_financial_period import EXTRA_COLUMNS, PLAN_KINDS, apply_contract
from utils.reading_year import YEAR_FIELDS, full_year, normalize_year, same_year, explicit_period
from utils.reading_context_facts import COLUMNS, resolve_region, typed_fact, same_region

VERSION = 'reading-semantic-B/1.7'
ROLES = {
    '연도': ['연도'], '기준연도': ['기준연도'], '목표연도': ['목표연도'],
    '지역': ['지자체명'], '지자체명': ['지자체명'],
    '단위': ['단위', '예산단위', '목표단위', '활동단위', '감축원단위단위'],
    '부문': ['부문', '관리부문'], '대상': ['사업명', '세부부문', '목표범위'],
    '사업명': ['사업명'], '인벤토리출처': ['인벤토리출처'],
    '배출구분': ['배출유형', '직간접구분'], '재원구분': ['재원구분'],
    '자료구분': ['계획구분'], '계획구분': ['계획구분'], '관리번호': ['관리번호'],
    '관리범위': ['목표범위'], '기간시작': ['기간시작'], '기간종료': ['기간종료'],
}
# Metadata labels never become measured numbers. Other contracts stay held.
MEASURE_FIELDS = {
    'regional_conditions': ('값', '단위'),
    'emissions_regional': ('배출량', '단위'),
    'emissions_management': ('배출량', '단위'),
    'emissions_forecast': ('전망값', '단위'),
    'financial_plan': ('예산액', '예산단위'),
    'annual_implementation': ('목표물량', '목표단위'),
    'quantitative_reductions': ('예상감축량', '단위'),
}


def present(value):
    return value is not None and value != ''


def number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def canonical_value(value):
    return UNIT_ALIASES.get(value, value) if isinstance(value, str) else value


def equal(a, b):
    a, b = canonical_value(a), canonical_value(b)
    return a == b and (type(a) is type(b) or (number(a) and number(b)))


def bind_candidates(source, metadata, schema, rules, selected_ids=None, region_context=None):
    """Input must have passed A structural validation. Never mutate inputs."""
    rows = {r['레코드ID']: r for r in source['records']}
    objects = {o['객체ID']: o for o in source['objects']}
    relations = {r['관계ID']: r for r in source['relations']}
    selected = set(rows) if selected_ids is None else set(selected_ids)
    if selected - rows.keys():
        raise ValueError('Unknown selected IDs: ' + repr(sorted(selected - rows.keys())))
    links = defaultdict(list)
    for link in metadata['links']:
        links[link['measurement_id']].append(link)
    accepted, holds, audit = [], {}, []
    # No-target records retain the frozen A path; explicit targets never enter it.
    fallback_ids = [rid for rid in rows if rid in selected and not (rows[rid].get('문맥 구분') or {}).get('대상시트')]
    fallback = evaluate_candidates(source, metadata, schema, rules, fallback_ids)
    accepted.extend(fallback['accepted_candidates'])
    holds.update(fallback['adapter_reasons'])
    for rejected in fallback['rejected_candidates']:
        for rid in rejected['candidate']['source_record_ids']:
            holds[rid] = rejected['issues']
    for rid, r in rows.items():
        ctx = r.get('문맥 구분') or {}
        key = ctx.get('대상시트')
        if rid not in selected or not key:
            continue
        problems = []
        if key not in schema['EXTRACTION_SHEETS']:
            holds[rid] = ['unknown_explicit_sheet']; continue
        region, region_reason = resolve_region(r, objects[r['객체ID']], rules, region_context)
        if region is None and region_reason not in ('existing_scope', 'not_explicit_own_scope'):
            holds[rid] = [region_reason]; continue
        heads = list(schema['EXCEL_HEADERS'][schema['SHEET_KEY_TO_NAME'][key]])
        heads += [f for f in EXTRA_COLUMNS.get(key, []) if f not in heads]
        heads += [f for f in COLUMNS.get(key, []) if f not in heads]
        values = {k: deepcopy(v) for k, v in (ctx.get('업무필드') or {}).items() if k in heads}
        sources = {k: [rid] for k, v in values.items() if present(v)}
        for field in YEAR_FIELDS & values.keys():
            original = values[field]
            normalized = full_year(original)
            if normalized is not None and (type(original) is not int or original != normalized):
                values[field] = normalized
                audit.append(dict(record_id=rid, field=field, before=original, value=normalized,
                                  status='year_alias_normalized', reason='explicit_four_digit_year'))
        if not present(values.get('지자체명')) and region:
            values['지자체명'] = region
            sources['지자체명'] = [r['객체ID']]
            audit.append(dict(record_id=rid, field='지자체명', value=region,
                              status='region_context_filled', reason=region_reason,
                              object_id=r['객체ID'], evidence=(region_context if region_reason=='explicit_document_object_scope' else objects[r['객체ID']].get('공통문맥 근거'))))
        proposed = defaultdict(list)
        for link in links[rid]:
            field, mid = link.get('target_field'), link['metadata_id']
            event = dict(record_id=rid, link_id=link['link_id'], metadata_id=mid,
                         field=field, role=link.get('role'), value=link.get('linked_value'))
            rel = relations.get(link['link_id'], {})
            location = rel.get('근거 위치') or {}
            # Cross-object links may corroborate an already explicit identical
            # field, but never fill a missing field without a scope contract.
            within_scope = mid in rows and (rows[mid]['객체ID'] == r['객체ID']
                or (present(values.get(field)) and equal(values[field], link.get('linked_value'))))
            valid_relation = (within_scope
                and rel.get('객체ID') == r['객체ID']
                and rel.get('출발 레코드 또는 노드ID') == mid
                and rel.get('도착 레코드 또는 노드ID') == rid
                and bool(link.get('evidence')) and bool(link.get('scope'))
                and ((isinstance(location, dict)
                      and location.get('근거') == link.get('evidence')
                      and location.get('적용범위') == link.get('scope'))
                     or (isinstance(location, str) and bool(location.strip()))))
            if field not in ROLES.get(link.get('role'), []) or field not in heads:
                event['status'] = 'preserved_unsupported_role'
            elif not valid_relation or link.get('conflict') or link.get('uncertainty'):
                event['status'] = 'blocked_unverified_relation'
                problems.append('unverified_link:' + link['link_id'])
            elif not present(link.get('linked_value')) or isinstance(link['linked_value'], (dict, list, bool)):
                event['status'] = 'blocked_invalid_value'; problems.append('invalid_link_value:' + field)
            elif (field == '연도' and values.get('시간기준') == 'period_total'
                    and values.get('연도') in (None, '') and r.get('연도') in (None, '')
                    and all(type(values.get(f)) is int and 1000 <= values[f] <= 9999
                            for f in ('기간시작', '기간종료'))
                    and explicit_period(link['linked_value'], (values['기간시작'], values['기간종료']))
                        == (values['기간시작'], values['기간종료'])):
                event.update(status='confirmed_explicit_period_not_year', audit_only=True,
                             period_start=values['기간시작'], period_end=values['기간종료'])
            elif key == 'financial_plan' and field == '계획구분' and link['linked_value'] in PLAN_KINDS:
                event['status'] = 'preserved_financial_kind_not_policy_category'
                event['audit_only'] = True
                if (link['linked_value'] in ('집행실적', '예산집행액')
                        and ctx.get('현황전망목표구분') == '계획'):
                    problems.append('field_conflict:계획구분')
            else:
                event['status'] = 'pending'
                proposed[field].append(event)
            audit.append(event)
        for field, events in proposed.items():
            if field in YEAR_FIELDS:
                choices = [e['value'] for e in events]
                if present(values.get(field)):
                    choices.append(values[field])
                if field == '연도' and present(r.get('연도')):
                    choices.append(r['연도'])
                anchors = {full_year(v) for v in choices} - {None}
                anchor = next(iter(anchors)) if len(anchors) == 1 else None
                normalized = [normalize_year(v, anchor) for v in choices]
                if None in normalized or len(set(normalized)) != 1:
                    problems.append('field_conflict:' + field)
                    for event in events:
                        event['status'] = 'held_conflict'
                    continue
                existed = present(values.get(field))
                values[field] = normalized[0]
                sources[field] = sorted(set(sources.get(field, []) + [e['metadata_id'] for e in events]))
                for event in events:
                    event.update(status='confirmed' if existed else 'filled',
                                 normalized_value=normalized[0], full_year_anchor=anchor,
                                 normalization_reason='same_record_verified_year_alias')
                continue
            # A structured parent + child already exists. A literal full-path
            # metadata value can confirm both; never split free text to infer it.
            if field == '부문' and present(values.get('부문')) and present(values.get('세부부문')):
                path = str(values['부문']) + ' > ' + str(values['세부부문'])
                hierarchical = [e for e in events if e['value'] == path]
                for event in hierarchical:
                    event['status'] = 'confirmed_explicit_hierarchy'
                    for f in ('부문', '세부부문'):
                        sources[f] = sorted(set(sources.get(f, []) + [event['metadata_id']]))
                events = [e for e in events if e not in hierarchical]
                if not events:
                    continue
            choices = [e['value'] for e in events]
            if present(values.get(field)):
                choices.append(values[field])
            comparator = same_region if field == '지자체명' else equal
            if any(not comparator(choices[0], v) for v in choices[1:]):
                problems.append('field_conflict:' + field)
                for event in events:
                    event['status'] = 'held_conflict'
                continue
            existed = present(values.get(field))
            values[field] = values[field] if field == '지자체명' and existed else canonical_value(choices[0])
            sources[field] = sorted(set(sources.get(field, []) + [e['metadata_id'] for e in events]))
            for event in events:
                event['status'] = 'confirmed' if existed else 'filled'
        if region is None and region_reason == 'not_explicit_own_scope':
            # A verified, same-object region link may supply the missing field.
            # Never reject it before the role/relation/conflict checks above,
            # and never bypass an explicit other/reference region.
            scoped_record = deepcopy(r)
            scoped_record['문맥 구분']['업무필드'] = values
            _, resolved_reason = resolve_region(scoped_record, objects[r['객체ID']], rules, region_context)
            if resolved_reason not in ('existing_scope', 'explicit_business_municipality', 'evidenced_object_region', 'explicit_document_object_scope'):
                problems.append(resolved_reason)
        verified_fields = {field for field, ids in sources.items() if any(i != rid for i in ids)}
        waived, contract_issues, contract_events = apply_contract(key, r, values, verified_fields=verified_fields)
        problems.extend(contract_issues)
        for event in contract_events:
            event.update(record_id=rid, status='explicit_contract', candidate_accepted=False)
            if not event.get('audit_only'):
                sources[event['field']] = [rid]
            audit.append(event)
        special = typed_fact(key, r, values)
        if special is not None:
            special_issues, special_events = special
            problems.extend(special_issues)
            for event in special_events:
                event.update(record_id=rid,status='typed_fact_preserved')
                audit.append(event)
                sources[event['field']] = [rid]
        missing = [] if special is not None else [f for f in rules['required_fields'][key] if f not in waived and not present(values.get(f))]
        problems.extend('missing:' + f for f in missing)
        if special is not None:
            pass
        elif r.get('레코드분류') == 'measurement':
            contract = MEASURE_FIELDS.get(key)
            if contract is None:
                problems.append('unsupported_measurement_contract')
            else:
                vf, uf = contract
                if r.get('값상태') != '명시값' or not number(r.get('숫자값')):
                    problems.append('no_explicit_number')
                if not number(values.get(vf)) or not equal(values.get(vf), r.get('숫자값')):
                    problems.append('measured_value_mismatch:' + vf)
                if not present(values.get(uf)) or not equal(values.get(uf), r.get('단위원문')):
                    problems.append('unit_mismatch:' + uf)
                allowed = rules['units']['allowed'].get(key)
                if allowed and values.get(uf) not in allowed:
                    problems.append('unit_not_allowed')
                if present(r.get('연도')) and present(values.get('연도')) and not same_year(
                        r['연도'], values['연도'], allow_short=bool(r.get('근거 문구'))):
                    problems.append('embedded_year_conflict')
        else:
            # Qualifiers/identifiers must not be promoted to measured business rows.
            problems.append('non_measurement_explicit_contract_not_enabled')
        if problems:
            holds[rid] = sorted(set(problems)); continue
        values.update({'근거ID': rid, '출처페이지': objects[r['객체ID']].get('PDF 실제 페이지'),
                       'derivation_type': 'normalized', '데이터상태': '고정 판독 연결; 원문 재검증 미수행'})
        for f in ('근거ID', '출처페이지', 'derivation_type', '데이터상태'):
            sources[f] = [rid]
        accepted.append(dict(sheet_key=key, sheet_name=schema['SHEET_KEY_TO_NAME'][key],
                             source_record_ids=[rid], values=values, field_sources=sources,
                             rule_id='B_explicit_role_binding', object_id=r['객체ID']))
    # Do not let downstream deduplication silently select a measured value.
    groups = defaultdict(list)
    for c in accepted:
        measure = MEASURE_FIELDS.get(c['sheet_key'], (None, None))[0]
        if c['values'].get('정보유형') in ('기관구성', '성과집계'):
            measure = '측정값'
        elif c['values'].get('정보유형') == '감축원단위':
            measure = '감축원단위값'
        identity = {k:v for k,v in c['values'].items()
                    if k not in (measure, '근거ID', '출처페이지', 'derivation_type', '데이터상태', '값원문')}
        groups[(c['sheet_key'], json.dumps(identity, ensure_ascii=False, sort_keys=True))].append(c)
    accepted = []
    for group in groups.values():
        if len(group) == 1:
            accepted.extend(group)
        else:
            for c in group:
                for rid in c['source_record_ids']:
                    holds[rid] = ['ambiguous_duplicate_business_identity']
    emitted = {rid for c in accepted for rid in c['source_record_ids']}
    for rid in sorted(selected - emitted - holds.keys()):
        holds[rid] = ['preserved_without_business_candidate']
    for event in audit:
        event['candidate_accepted'] = event['record_id'] in emitted
    return dict(version=VERSION, accepted_candidates=accepted, rejected_candidates=[],
                adapter_reasons=holds, duplicate_events=fallback['duplicate_events'], binding_log=audit,
                selected_records=len(selected), backend_executed=False, accuracy_evaluated=False)
