"""Local bridge from production sheet rows to the frozen A/B reading contracts.

No model calls and no document-wide municipality/period inference. Legacy rows
remain on the legacy path unless they opt into the explicit ``_reading`` record.
"""
from copy import deepcopy
from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
import re

from utils.reading_compatibility import UNIT_ALIASES, convert
from utils.reading_financial_period import apply_contract
from utils.reading_semantic_binding import bind_candidates, equal, number, MEASURE_FIELDS
from utils.reading_context_facts import scoped_municipality, region_aliases, same_region
from utils.reading_year import YEAR_FIELDS, same_year, normalize_year

VERSION = 'reading-pipeline/1.4'
SUPPORTED = set(MEASURE_FIELDS) | {'mitigation_projects', 'governance_feedback', 'monitoring_performance'}
UNIT_FIELDS = ('단위', '예산단위', '목표단위', '활동단위', '측정단위', '감축원단위단위')


def instruction(sheet_keys):
    """An additive contract in existing calls, not a second reading request."""
    keys = set(sheet_keys)
    if not SUPPORTED.intersection(keys):
        return ''
    common = '''
[근거 기반 연결 확장 — 기존 JSON 행 형식 유지]
지역문맥·머리글·의미 연결이 필요한 행에만 _reading 객체를 추가하세요.
_reading에는 해당하는 근거만 기록합니다: 근거 문구, 기간원문, 범례원문, 값원문,
레코드분류(measurement|text_fact), 값상태(명시값|정성상태|정성표기|아이콘범위),
문맥 구분:{자료지역, 자료구분, 현황전망목표구분, 값의미, 시간성격, 측정유형, 집계범위,
구성구분, 기관명, 구성경로, 달성도범위}.
행의 지역이 비어 있고 표 전체에 지역이 명시된 경우에만 _reading.객체문맥에
{자료지역:"원문 지역", 지역근거:"원문 인용", 적용범위:"해당 표/행 범위"}를 넣으세요.
인쇄된 표 번호가 있으면 각 행의 표ID에 같은 번호(예: "표 7-1")를 반복하세요.
한 이미지의 다른 표를 같은 표ID로 묶지 마세요. 공통 지역 근거를 첫 행에만 쓸 경우
그 근거의 적용범위와 나머지 행의 표ID가 정확히 같아야 합니다. 표 번호가 없으면
각 적용 행에 검증된 객체문맥을 반복하고, 행 순서나 기관명이 같다는 이유로 연결하지 마세요.
머리글 보완은 _reading.메타데이터:[{역할:"연도|단위|지역|부문|사업명|자료구분|재원구분",
필드:"대응하는 기존 필드명", 값:원문값, 근거:"원문 인용", 적용범위:"이 행에 적용되는 범위"}]로 기록할 수 있습니다.
연도는 해당 표의 명시된 전체 연도 근거로만 정수화하고, '24 같은 머리글 원문은 메타데이터 값에 보존하세요.
전체 연도/세기 근거가 없으면 24를 2024로 추정하지 말고 기간 합계를 단일 연도에 배정하지 마세요.
숫자값·단위원문을 _reading에 반복할 필요는 없습니다. 행의 수치·단위가 기준입니다.
안내된 문서 지자체명을 모든 행의 지역 근거로 간주하지 마세요. 타지역·전국·참고자료는 명시하세요.
불명확한 연결을 만들지 말고 모르는 필드는 생략/null로 유지하세요.
'''.strip()
    extra = []
    if 'emissions_forecast' in keys:
        extra.append('전망 총량과 증가량·감축량·비율·원단위는 다른 측정항목입니다. '
                     '총량이 아닌 값을 전망값에 넣지 말고 원문 의미와 비교 기준을 보존하세요.')
    if 'mitigation_projects' in keys:
        extra.append('개별 사업명과 관리번호를 함께 보존하세요. 소계·합계·주관부서·표 머리글·상위 과제를 '
                     '개별 사업으로 만들지 말고 메타데이터·집계·상하위 관계로 분리하세요. '
                     '서로 다른 측정항목을 사업명만 같다는 이유로 묶지 마세요. '
                     '사업 식별 행은 사업명·관리번호를 fields에 명시하고 지역은 근거가 있는 '
                     '_reading.문맥 구분 또는 객체문맥에 기록하세요. 숫자·성과 구간은 사업명으로 변환하지 마세요.')
    if keys.intersection({'financial_plan', 'quantitative_reductions'}):
        extra.append('기존 스키마 외에 기간시작·기간종료·시간기준·기간원문을 보존하세요. '
                     '기간 합계는 연도=null, 시간기준="period_total"로 표시하세요. '
                     '단일 연간 값은 원문의 연도를 유지하고 시간기준="annual"로 표시하세요. '
                     '기간시작·기간종료를 반복하면 반드시 그 연도와 같아야 하며, 합계 기간을 연간 행에 복사하지 마세요. '
                     '합계의 기간 머리글은 단일 연도 메타데이터로 연결하지 말고 기간시작·기간종료에 연결하세요.')
    if 'financial_plan' in keys:
        extra.append('계획구분은 총계·온실가스감축대책·대응기반강화·기타의 정책 분류이며 투자계획/집행실적과 다릅니다. '
                     '원문에 분류 근거가 없으면 계획구분을 null로 두고 기타로 채우지 마세요. '
                     '재정투자 계획 예산으로 명시된 수치는 문맥 구분에 현황전망목표구분="계획", '
                     '값의미="재정투자 계획 예산" 및 근거 문구를 기록하세요. 관리번호·집계대상원문을 보존하세요. '
                     '없는 재원·연도·금액은 추정하지 마세요. 비예산은 예산액=null, 정보유형="예산상태", '
                     '값원문=원문 표현이며, 빈칸이나 비예산을 0으로 만들지 마세요. '
                     '연도 합계/사업 소계는 재원구분 합계의 근거가 아닙니다. 재원 구분이 없으면 '
                     '재원구분=null, 재원표기상태="미표기"로 유지하세요. '
                     '비예산 병합 셀이 명시된 여러 연도에 걸치면 연도=null, 기간시작·기간종료·기간원문과 '
                     '시간기준="period_status"로 그 범위를 보존하세요. 한 해로 축약하지 마세요.')
    if 'annual_implementation' in keys:
        extra.append('재정투자표의 연도별 예산액과 비예산 표기는 annual_implementation의 목표물량/연간계획이 아닙니다. '
                     'financial_plan에만 기록하고 표ID와 원문 예산 근거를 보존하세요. '
                     '투자유치액 등 명시된 금액형 성과지표는 목표로 유지하되 실제 성과지표명과 근거를 기록하세요.')
    if 'quantitative_reductions' in keys:
        extra.append('원단위는 예상감축량·활동량과 분리하여 정보유형="감축원단위", '
                     '감축원단위명·감축원단위값·감축원단위단위로 보존하세요.')
    if keys.intersection({'governance_feedback', 'monitoring_performance'}):
        extra.append('정보유형·항목원문·측정값·측정단위·값원문을 보존하세요. '
                     '성과집계는 정보유형="성과집계", 측정유형="사업 수", 집계범위=원문 구간; '
                     '기관구성은 정보유형="기관구성", 측정유형="기관구성", 구성구분=원문 항목. '
                     '수는 측정값에, 조직기능·정성성과는 해당 정보유형과 값원문에 보존하세요.')
    if 'governance_feedback' in keys:
        extra.append('조직기능의 역할·값원문에는 실제 업무/기능 문구를 동일하게 기록하세요. '
                     '요약이 필요하면 역할요약원문으로 분리하며 역할을 요약으로 대체하지 마세요. '
                     '기능 설명과 기관구성 수치는 서로 다른 행으로 분리하세요. '
                     '전체회의·분과위원회 같은 구성요소 이름은 항목·구성요소원문에, '
                     '명시된 상하위 경로는 문맥 구분.구성경로에 별도 보존하세요. '
                     '같은 기관의 서로 다른 기능은 별도 행으로 유지하고 담당 부서는 담당부서 필드를 사용하세요.')
    if 'monitoring_performance' in keys:
        extra.append('개별 사업의 복합 실적(목표·실적·달성률 등)은 기존 사업명·점검연도·이행실적에 '
                     '원문대로 보존하고 _reading의 근거 문구·값원문·문맥 구분을 함께 기록하세요. '
                     '여러 수치를 하나의 측정값으로 합치거나 골라 넣지 마세요. '
                     '계획만 있는 값은 실적으로 만들지 말고 분야별·전체 집계는 개별 사업 실적과 분리하세요.')
    return '\n'.join([common, *extra])


@lru_cache(maxsize=1)
def _base_rules():
    path = Path(__file__).resolve().parents[1] / 'data/reading_mapping_rules_v1.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _rules(municipality):
    result = deepcopy(_base_rules())
    aliases = list(region_aliases(municipality))
    result['target']['source_aliases'] = aliases
    # Never carry Seoul-specific title patterns into other municipalities.
    result['target']['own_pattern'] = r'^(?:' + '|'.join(map(re.escape, aliases)) + r')$' if aliases else r'(?!)'
    return result


def _schema():
    import config
    return {k: getattr(config, k) for k in ('EXTRACTION_SHEETS', 'SHEET_KEY_TO_NAME', 'EXCEL_HEADERS')}


def _record_from_row(key, row, envelope):
    record = deepcopy(envelope)
    record.pop('객체문맥', None)
    record.pop('메타데이터', None)
    info = row.get('정보유형')
    vf, uf = MEASURE_FIELDS.get(key, (None, None))
    if info in ('성과집계', '기관구성'):
        vf, uf = '측정값', '측정단위'
    elif info == '감축원단위':
        vf, uf = '감축원단위값', '감축원단위단위'
    value = row.get(vf) if vf else None
    unit = row.get(uf) if uf else None
    text_fact = info in ('예산상태', '조직기능', '정성성과')
    record.setdefault('레코드분류', 'text_fact' if text_fact else 'measurement')
    record.setdefault('값상태', '정성상태' if info == '예산상태' else
                      '정성표기' if text_fact else '명시값' if number(value) else
                      '서술실적' if key == 'monitoring_performance' and isinstance(row.get('이행실적'), str)
                      else '미확인')
    record.setdefault('숫자값', value)
    record.setdefault('단위원문', unit)
    for field in ('값원문', '기간원문', '범례원문', '연도', '항목원문'):
        if field in row:
            record.setdefault(field, deepcopy(row[field]))
    if info == '감축원단위':
        record.setdefault('항목원문', row.get('감축원단위명'))
    ctx = record.setdefault('문맥 구분', {})
    if not isinstance(ctx, dict):
        raise ValueError('invalid_record_context')
    for field in ('구조역할', '정보유형'):
        if field in row:
            ctx.setdefault(field, row[field])
    if info in ('성과집계', '기관구성'):
        ctx.setdefault('측정유형', '사업 수' if info == '성과집계' else '기관구성')
        ctx.setdefault('집계범위' if info == '성과집계' else '구성구분', row.get('항목원문'))
    return record


def _monitoring_envelope(envelope):
    """Known producer spellings only; conflicting aliases stay held."""
    out, events = deepcopy(envelope), []
    for alias, canonical in (('문맥', '문맥 구분'), ('근거문구', '근거 문구')):
        if alias not in out:
            continue
        value = out[alias]
        if canonical == '문맥 구분':
            if not isinstance(value, dict) or not isinstance(out.get(canonical, {}), dict):
                raise ValueError('invalid_record_context')
            merged = deepcopy(out.get(canonical, {}))
            for field, incoming in value.items():
                if field in merged and merged[field] != incoming:
                    raise ValueError('reading_alias_conflict:' + canonical + '.' + field)
                merged[field] = deepcopy(incoming)
            out[canonical] = merged
        else:
            if canonical in out and out[canonical] != value:
                raise ValueError('reading_alias_conflict:' + canonical)
            out[canonical] = deepcopy(value)
        events.append(dict(field=canonical, value=deepcopy(out[canonical]),
                           reason='exact_reading_key_alias', source_field=alias))
    return out, events


def _bind_row(key, row, municipality):
    """Reuse A and B, with per-row IDs so implicit cross-object links are impossible."""
    envelope = row['_reading']
    if not isinstance(envelope, dict):
        raise ValueError('invalid_reading_envelope')
    alias_events = []
    envelope, alias_events = _monitoring_envelope(envelope)
    municipality = scoped_municipality(row, municipality)
    schema = _schema()
    record = _record_from_row(key, row, envelope)
    if (key == 'governance_feedback' and record.get('레코드분류') == 'text_fact'
            and not record.get('값원문') and isinstance(row.get('역할'), str)
            and row['역할'].strip() and isinstance(record.get('근거 문구'), str)
            and re.sub(r'\s+', '', row['역할']) in re.sub(r'\s+', '', record['근거 문구'])):
        # The producer omitted a duplicate literal field, not the fact itself.
        record['값원문'] = row['역할']
        alias_events.append(dict(field='값원문', value=row['역할'], source_field='역할',
                                 reason='role_literal_present_in_record_evidence'))
    ctx = record['문맥 구분']
    explicit = ctx.get('대상시트', key)
    if explicit not in (key, schema['SHEET_KEY_TO_NAME'][key]):
        raise ValueError('explicit_sheet_conflict')
    ctx['대상시트'] = key
    columns = schema['EXCEL_HEADERS'][schema['SHEET_KEY_TO_NAME'][key]]
    business = {k: deepcopy(v) for k, v in row.items() if k in columns}
    embedded = ctx.get('업무필드', {})
    if not isinstance(embedded, dict):
        raise ValueError('invalid_business_fields')
    for field, value in embedded.items():
        equivalent = same_region(business.get(field), value) if field == '지자체명' else equal(business.get(field), value)
        if field in YEAR_FIELDS:
            equivalent = same_year(business.get(field), value, allow_short=bool(record.get('근거 문구')))
        if field in business and business[field] not in (None, '') and not equivalent:
            raise ValueError('field_conflict:' + field)
        if field in columns:
            if field in YEAR_FIELDS:
                normalized = normalize_year(value, business.get(field))
                business[field] = normalized if normalized is not None else value
                if normalized is not None and type(value) is not int:
                    alias_events.append(dict(field=field, before=value, value=normalized,
                                             reason='same_record_embedded_year_alias'))
            else:
                business[field] = value
    ctx['업무필드'] = business
    record.update({'레코드ID': 'R', '객체ID': 'O'})
    common = envelope.get('객체문맥', {})
    if not isinstance(common, dict):
        raise ValueError('invalid_object_context')
    obj = {'객체ID': 'O', '공통문맥': {}, 'PDF 실제 페이지': row.get('출처페이지')}
    if common:
        # Plain object-wide region text is insufficient without an explicit scope.
        if not all(isinstance(common.get(f), str) and common[f].strip()
                   for f in ('자료지역', '지역근거', '적용범위')):
            raise ValueError('missing_object_region_evidence_or_scope')
        aliases = set(_rules(municipality)['target']['source_aliases'])
        for region in (business.get('지자체명'), ctx.get('자료지역')):
            if region and region != common['자료지역'] and not {region, common['자료지역']} <= aliases:
                raise ValueError('record_object_region_conflict')
        obj['공통문맥'] = {'자료지역': common['자료지역'], '지역근거': common['지역근거']}
    source = {'objects': [obj], 'records': [record], 'relations': []}
    metadata = {'links': []}
    links = envelope.get('메타데이터', [])
    if not isinstance(links, list):
        raise ValueError('invalid_metadata_links')
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            raise ValueError('invalid_metadata_link')
        if not all(isinstance(link.get(f), str) and link[f].strip()
                   for f in ('역할', '필드', '근거', '적용범위')):
            raise ValueError('missing_metadata_evidence_or_scope')
        if link.get('객체ID') not in (None, '', envelope.get('객체ID')):
            raise ValueError('cross_object_metadata_not_supported')
        mid, lid = f'M{i}', f'L{i}'
        source['records'].append({'레코드ID': mid, '객체ID': 'O', '레코드분류': 'qualifier', '문맥 구분': {}})
        source['relations'].append({'관계ID': lid, '객체ID': 'O', '출발 레코드 또는 노드ID': mid,
            '도착 레코드 또는 노드ID': 'R', '근거 위치': {'근거': link['근거'], '적용범위': link['적용범위']}})
        metadata['links'].append({'link_id': lid, 'measurement_id': 'R', 'metadata_id': mid,
            'role': link['역할'], 'target_field': link['필드'], 'linked_value': link.get('값'),
            'evidence': link['근거'], 'scope': link['적용범위'],
            'conflict': link.get('conflict', link.get('충돌')),
            'uncertainty': link.get('uncertainty', link.get('불확실성'))})
    compatible, conversion = convert(source, metadata, schema)
    if conversion['structural_issues']:
        return None, conversion['structural_issues'], conversion['changes']
    result = bind_candidates(compatible, metadata, schema, _rules(municipality), ['R'])
    events = alias_events + conversion['changes'] + result['binding_log']
    if not result['accepted_candidates']:
        return None, result['adapter_reasons'].get('R', ['no_candidate']), events
    candidate = result['accepted_candidates'][0]['values']
    out = deepcopy(row)
    # This envelope has already passed A/B. Do not reinterpret it after the
    # legacy cleaner normalizes the row in a later closed-loop/full pass.
    # Its complete original remains in reading_pipeline_audit.
    out.pop('_reading', None)
    # R/O are local bridge IDs, never substitute them for original provenance.
    for field, value in candidate.items():
        if field not in ('근거ID', '출처페이지', '데이터상태', 'derivation_type'):
            out[field] = value
    return out, [], events


def integrate_rows(key, rows, municipality, *, financial_rows=None):
    """Return new rows plus a lossless audit; do not mutate caller-owned records."""
    import config
    if not getattr(config, 'READING_PIPELINE_ENABLED', True):
        return deepcopy(rows), []
    accepted, audit = [], []
    explicit_groups = defaultdict(list)
    quarantines = {}
    if key == 'annual_implementation':
        from utils.semantic_contract_guard import guard_annual_budget_rows
        _, records = guard_annual_budget_rows(rows, financial_rows=financial_rows)
        quarantines = {r['row_index']: r for r in records}
    for index, source in enumerate(rows, 1):
        row = deepcopy(source)
        changes, issues = [], []
        try:
            for field in UNIT_FIELDS:
                value = row.get(field)
                if isinstance(value, str) and value in UNIT_ALIASES:
                    row[field] = UNIT_ALIASES[value]
                    changes.append({'field': field, 'before': value, 'value': row[field], 'reason': 'same_scale_unit_alias'})
            if index in quarantines:
                changes.append(quarantines[index])
                raise ValueError(quarantines[index]['reason_codes'][0])
            if '_reading' in row:
                if key not in SUPPORTED:
                    raise ValueError('unsupported_explicit_reading_sheet')
                row, issues, events = _bind_row(key, row, municipality)
                changes.extend(events)
            elif key in ('financial_plan', 'quantitative_reductions'):
                # No missing plan/region inferred for legacy rows. The only
                # defaults here describe absence, never a monetary value.
                before = deepcopy(row)
                _, contract_issues, events = apply_contract(key, {'기간원문': row.get('기간원문')}, row)
                issues = [x for x in contract_issues if x != 'missing:budget_identity']
                if 'missing:budget_identity' in contract_issues:
                    row = before
                    events = []
                changes.extend(events)
        except (ValueError, TypeError, KeyError) as exc:
            issues = [str(exc)]
        if issues:
            audit.append({'sheet_key': key, 'row_index': index, 'evidence_id': source.get('근거ID'),
                          'status': 'held', 'issues': issues, 'original': deepcopy(source), 'events': changes})
        else:
            accepted.append(row)
            if changes or '_reading' in source:
                event = {'sheet_key': key, 'row_index': index, 'evidence_id': source.get('근거ID'),
                         'status': 'applied', 'issues': [], 'original': deepcopy(source), 'events': changes}
                audit.append(event)
                if '_reading' in source:
                    measure = MEASURE_FIELDS.get(key, (None, None))[0]
                    info = row.get('정보유형')
                    if info in ('기관구성', '성과집계'):
                        measure = '측정값'
                    elif info == '감축원단위':
                        measure = '감축원단위값'
                    if measure and number(row.get(measure)):
                        headers = config.EXCEL_HEADERS[config.SHEET_KEY_TO_NAME[key]]
                        identity = {f: row[f] for f in headers if row.get(f) not in (None, '')
                                    and f not in {measure, '근거ID', '출처페이지', 'derivation_type', '데이터상태', '값원문'}}
                        signature = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
                        explicit_groups[signature].append((row, measure, event))
    blocked = set()
    for group in explicit_groups.values():
        if any(not equal(group[0][0][group[0][1]], row[measure]) for row, measure, _ in group[1:]):
            for row, _, event in group:
                blocked.add(id(row))
                event.update(status='held', issues=['conflicting_duplicate_business_identity'])
    accepted = [row for row in accepted if id(row) not in blocked]
    return accepted, audit


def audit_summary(audit):
    return {'version': VERSION, 'applied_rows': sum(x['status'] == 'applied' for x in audit),
            'held_rows': sum(x['status'] == 'held' for x in audit)}
