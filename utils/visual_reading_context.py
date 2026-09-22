"""Audited, exact-label projections into the explicit reading contracts.

No model, gold, title-based region guessing, cross-object inheritance or new
numbers. A projection is only a candidate; all Organizer/A-B gates still apply.
"""
from copy import deepcopy
from collections import defaultdict
import re


def _table_id(value):
    """Exact printed table identifier only, never title/position inference."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\[?표\s*(\d+(?:\s*[-－–]\s*\d+)*)\]?', value.strip())
    return '표 ' + re.sub(r'\s*[-－–]\s*', '-', match[1]) if match else None


def _table_scope(row):
    f = row.get('판독필드') or {}
    env = f.get('_reading', {})
    if not isinstance(env, dict):
        return None
    common = env.get('객체문맥', {})
    ctx = env.get('문맥 구분', {})
    if not isinstance(common, dict) or not isinstance(ctx, dict):
        return None
    # 합계그룹's exact first segment is an explicit table reference. Never
    # borrow a table number from the page title or from a neighbouring row.
    group = f.get('합계그룹')
    labels = [f.get('표ID'), env.get('표ID'), ctx.get('표ID'), common.get('적용범위')]
    if isinstance(group, str) and not f.get('_합계그룹자동생성'):
        labels.append(group.split('/')[0].strip())
    tables = {t for value in labels if (t := _table_id(value))}
    explicit = [v for v in (f.get('표ID'), env.get('표ID'), ctx.get('표ID')) if v not in (None, '')]
    if any(_table_id(v) is None for v in explicit) or len(tables) != 1:
        return None
    ids = row.get('근거ID목록') or [row.get('근거ID')]
    objects = row.get('원본객체ID목록') or [row.get('원본객체ID')]
    if isinstance(ids, str): ids = [ids]
    if isinstance(objects, str): objects = [objects]
    if (not isinstance(ids, list) or not isinstance(objects, list)
            or len(ids) != 1 or len(objects) != 1
            or not all(isinstance(x, str) and x for x in ids + objects)
            or type(row.get('페이지')) is not int):
        return None
    return row['페이지'], objects[0], ids[0], next(iter(tables))


def _reference_or_uncertain(row):
    f = row.get('판독필드') or {}
    env = f.get('_reading') or {}
    ctx = env.get('문맥 구분') or {}
    common = env.get('객체문맥') or {}
    return (row.get('참고자료여부') is True or f.get('estimated') is True
            or any(obj.get(k) for obj in (f, env, ctx, common) if isinstance(obj, dict)
                   for k in ('불확실성', 'uncertainty', '충돌', 'conflict'))
            or bool(re.search(r'전국|해외|타지역|국가\s*자료|참고자료', str(ctx.get('자료구분', '')))))


def _region(value):
    return {'서울시': '서울특별시', '강원도': '강원특별자치도'}.get(value, value) if isinstance(value, str) else None


def _share_table_regions(rows):
    """Share only evidenced, unambiguous regions within an exact source table.

    No transitive propagation: the index contains original donors only. Audit
    each donor/receiver so missing table membership remains visible and held.
    """
    donors = defaultdict(list)
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get('판독필드'), dict):
            continue
        key = _table_scope(row)
        if (key is None or _reference_or_uncertain(row)
                or str(row.get('신뢰도') or '').lower() not in ('medium', 'high')):
            continue
        env = row['판독필드'].get('_reading') or {}
        common = env.get('객체문맥') or {}
        ctx = env.get('문맥 구분') or {}
        declared = [row['판독필드'].get('지자체명'), row['판독필드'].get('자료지역'), ctx.get('자료지역')]
        if isinstance(ctx.get('업무필드'), dict): declared.append(ctx['업무필드'].get('지자체명'))
        if any(v and _region(v) != _region(common.get('자료지역')) for v in declared):
            continue
        if all(isinstance(common.get(k), str) and common[k].strip()
               for k in ('자료지역', '지역근거', '적용범위')) and _table_id(common['적용범위']) == key[-1]:
            donors[key].append((index, common))
    result, events = [], {}
    for index, source in enumerate(rows):
        row = source
        if not isinstance(source, dict) or not isinstance(source.get('판독필드'), dict):
            result.append(row); continue
        key = _table_scope(source)
        candidates = donors.get(key, [])
        if candidates and not _reference_or_uncertain(source):
            f = source['판독필드']; env = f.get('_reading') or {}; ctx = env.get('문맥 구분') or {}
            regions = {_region(c['자료지역']) for _, c in candidates}
            declared = [f.get('지자체명'), f.get('자료지역'), ctx.get('자료지역')]
            business = ctx.get('업무필드')
            if isinstance(business, dict): declared.append(business.get('지자체명'))
            conflict = len(regions) != 1 or any(v and _region(v) not in regions for v in declared)
            if conflict:
                row = deepcopy(source)
                issue = 'explicit_table_region_conflict'
                row['병합차단사유'] = '; '.join(filter(None, [row.get('병합차단사유'), issue]))
                events[index] = {'changes': [], 'issues': [issue]}
            elif not env.get('객체문맥'):
                row = deepcopy(source)
                common = deepcopy(candidates[0][1])
                row['판독필드'].setdefault('_reading', {})['객체문맥'] = common
                events[index] = {'issues': [], 'changes': [{
                    'field': '_reading.객체문맥', 'value': common, 'reason': 'exact_source_table_region',
                    'table_id': key[-1], 'source_observation_indices': [i for i, _ in candidates],
                    'source_record_ids': [rows[i].get('_recovery_id') for i, _ in candidates],
                    'source_object_id': key[1], 'source_evidence_id': key[2]}]}
        result.append(row)
    return result, events


def adapt_observation(source):
    row = deepcopy(source)
    f = row.get('판독필드')
    if not isinstance(f, dict):
        return row, None
    target = row.get('대상시트')
    envelope = f.get('_reading', {})
    if not isinstance(envelope, dict):
        return row, None  # The existing invalid-envelope gate reports this.
    ctx = envelope.get('문맥 구분', {})
    if not isinstance(ctx, dict):
        return row, None
    changes, conflicts = [], []

    def put(obj, key, value, reason):
        if value in (None, ''):
            return
        if obj.get(key) not in (None, '') and obj[key] != value:
            conflicts.append('explicit_context_conflict:' + key)
        elif obj.get(key) in (None, ''):
            obj[key] = deepcopy(value)
            changes.append({'field': key, 'value': deepcopy(value), 'reason': reason})

    if target == 'mitigation_projects' and '_reading' in f:
        # An observed name adjacent to an explicit individual project code is
        # not a name invented from a numeric measure, heading or parent code.
        code = f.get('관리번호')
        if (not f.get('사업명') and isinstance(code, str)
                and re.fullmatch(r'[A-Za-z]+\d*[-－–]\d+(?:[-－–]\d+)*', code.strip())
                and row.get('원문값', row.get('값')) is None):
            put(f, '사업명', row.get('항목'), 'same_row_project_code_and_label')

    explicit_target = ctx.get('대상시트')
    org_target = explicit_target in ('governance_feedback', '13_이행관리환류')
    organization = f.get('거버넌스기구')
    metric = f.get('측정유형') or ctx.get('측정유형') or f.get('지표명')
    # Legacy other rows may carry an exact organization label + count/function
    # role. Merely mentioning a committee in a title is never sufficient.
    count_label = metric in ('위원수', '위원 수', '기관구성')
    function_label = f.get('구조역할') == '주체' and isinstance(f.get('설명'), str) and bool(f['설명'].strip())
    if target == 'other' and not organization and (count_label or function_label):
        label = row.get('항목')
        if isinstance(label, str) and (re.search(r'위원회|심의회|협의체|지원센터|평가단', label)
                                      or (function_label and f.get('상위항목'))):
            organization = label
    info = f.get('정보유형')
    if target == 'other' and (org_target or (organization and (count_label or function_label or info in ('기관구성', '조직기능')))):
        row['대상시트'] = 'governance_feedback'
        changes.append({'field': '대상시트', 'before': target, 'value': row['대상시트'],
                        'reason': 'explicit_organization_role'})
        target = row['대상시트']
    if target == 'governance_feedback':
        if explicit_target and not org_target:
            conflicts.append('explicit_sheet_conflict')
        put(f, '거버넌스기구', organization, 'explicit_organization_label')
        put(f, '담당부서', f.get('담당 부서'), 'exact_department_header_alias')
        envelope = f.setdefault('_reading', deepcopy(envelope))
        ctx = envelope.setdefault('문맥 구분', {})
        if info == '기관구성' or count_label:
            put(f, '정보유형', '기관구성', 'explicit_composition_measure')
            put(f, '측정값', row.get('값'), 'original_observed_number')
            put(f, '측정단위', row.get('단위'), 'original_observed_unit')
            label = f.get('항목원문') or f.get('구성구분') or ctx.get('구성구분') or f.get('지표명')
            put(f, '항목원문', label, 'explicit_composition_label')
            put(ctx, '측정유형', '기관구성', 'explicit_composition_measure')
            put(ctx, '구성구분', label, 'explicit_composition_label')
        elif info == '조직기능' or function_label:
            put(f, '정보유형', '조직기능', 'explicit_function_role')
            role = f.get('값원문') or envelope.get('값원문') or f.get('설명') or f.get('역할')
            # A component label may occupy the legacy role key. Reinterpret
            # only when it exactly repeats the observed entity label; genuine
            # differing function texts remain conflicts, never last-wins.
            component = f.get('역할')
            if component and role and component != role and component == row.get('항목'):
                put(f, '구성요소원문', component, 'explicit_component_label')
                path = [x for x in (f.get('상위항목'), component) if x]
                put(ctx, '구성경로', path, 'explicit_component_path')
                f['역할'] = role
                changes.append({'field': '역할', 'before': component, 'value': role,
                                'reason': 'separate_component_from_function_text'})
            put(f, '역할', role, 'original_function_text')
            put(f, '값원문', role, 'original_function_text')
            put(envelope, '레코드분류', 'text_fact', 'explicit_function_role')
            put(envelope, '값상태', '정성표기', 'explicit_function_role')
            put(envelope, '값원문', role, 'original_function_text')
            if row.get('값') is not None:
                conflicts.append('mixed_function_and_measurement')
        put(ctx, '기관명', f.get('상위기관원문') or f.get('상위항목'), 'explicit_parent_label')
    if conflicts:
        row['병합차단사유'] = '; '.join(filter(None, [row.get('병합차단사유'), *sorted(set(conflicts))]))
    if not changes and not conflicts:
        return row, None
    return row, {'record_id': row.get('_recovery_id'), 'page': row.get('페이지'),
                 'evidence_ids': row.get('근거ID목록'), 'original': deepcopy(source),
                 'changes': changes, 'issues': conflicts, 'status': 'candidate_only'}


def adapt_observations(rows):
    result, audit = [], []
    scoped, region_events = _share_table_regions(rows)
    for row_index, source in enumerate(scoped):
        if not isinstance(source, dict):
            result.append(deepcopy(source))
            continue
        row, event = adapt_observation(source)
        region_event = region_events.get(row_index)
        if region_event:
            event = event or {'record_id': row.get('_recovery_id'), 'page': row.get('페이지'),
                             'evidence_ids': row.get('근거ID목록'), 'changes': [], 'issues': [],
                             'status': 'candidate_only'}
            event['original'] = deepcopy(rows[row_index])
            event['changes'] = region_event['changes'] + event['changes']
            event['issues'] = sorted(set(region_event['issues'] + event['issues']))
        # Preserve the existing Organizer in-place status contract for rows
        # whose interpretation did not change. Projections use separate copies.
        result.append(row if event else source)
        if event:
            event['observation_index'] = row_index
            audit.append(event)
    return result, audit
