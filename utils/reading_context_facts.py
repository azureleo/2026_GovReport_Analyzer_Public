"""Evidence-scoped municipality inheritance and separate typed facts."""
import json
import math
import re

COLUMNS = {
 'financial_plan': ['정보유형', '값원문'],
 'governance_feedback': ['상위기관원문', '구성경로원문', '정보유형', '항목원문', '측정값', '측정단위', '값원문', '역할요약원문'],
 'monitoring_performance': ['정보유형', '항목원문', '측정값', '측정단위', '값원문', '달성도범위원문', '집계범위원문'],
 'quantitative_reductions': ['정보유형', '감축원단위명', '감축원단위단위'],
 'regional_conditions': ['기간시작', '기간종료', '시간기준', '기간원문'],
}

# Exact administrative aliases only. Do not use substring matching: 수도권,
# 서울 외곽지역 and multi-region labels are not aliases for 서울특별시.
REGION_ALIAS_GROUPS = (('서울특별시', '서울시', '서울'), ('경기도',),
                       ('강원특별자치도', '강원도'))


def region_aliases(region):
    if not isinstance(region, str) or not region or region == '알 수 없음':
        return ()
    return next((group for group in REGION_ALIAS_GROUPS if region in group), (region,))


def same_region(left, right):
    return isinstance(right, str) and right in region_aliases(left)


def scoped_municipality(fields, municipality):
    """Unknown document scope may use this row's evidenced object, never a title."""
    if municipality and municipality != '알 수 없음':
        return municipality
    envelope = fields.get('_reading')
    if not isinstance(envelope, dict):
        return municipality
    common, ctx = envelope.get('객체문맥', {}), envelope.get('문맥 구분', {})
    if not isinstance(common, dict) or not isinstance(ctx, dict):
        return municipality
    if not all(isinstance(common.get(k), str) and common[k].strip()
               for k in ('자료지역', '지역근거', '적용범위')):
        return municipality
    normalize = lambda v: region_aliases(v)[0] if region_aliases(v) else v
    region = normalize(common['자료지역'].strip())
    if any(v and normalize(v) != region for v in (ctx.get('자료지역'), fields.get('지자체명'))):
        return municipality
    if re.search(r'전국|해외|타지역|국가\s*자료|참고자료', str(ctx.get('자료구분', ''))):
        return municipality
    return region


def resolve_region(record, obj, rules, context=None):
    """Never inherit across an explicit other/reference region or a conflict."""
    from utils.reading_adapter_v1 import scope
    ctx = record.get('문맥 구분') or {}
    common = obj.get('공통문맥') or {}
    aliases = {alias for region in rules['target']['source_aliases'] for alias in region_aliases(region)}
    existing = ctx.get('자료지역')
    business = (ctx.get('업무필드') or {}).get('지자체명')
    if business and business not in aliases:
        return None, 'explicit_other_business_region'
    if re.search(r'전국|해외|타지역|국가\s*자료|참고자료', str(ctx.get('자료구분') or '')):
        return None, 'reference_scope_preserved'
    if existing and business and existing != business and not {existing, business} <= aliases:
        return None, 'region_conflict'
    old, _ = scope(record, obj, rules)
    if old == 'reference':
        return None, 'reference_scope_preserved'
    if existing and existing not in aliases:
        return None, 'explicit_other_region'
    if old == 'own' or existing in aliases:
        return business or existing, 'existing_scope'
    if business in aliases:
        return business, 'explicit_business_municipality'
    region = common.get('자료지역') or common.get('지역')
    evidence = obj.get('공통문맥 근거') or common.get('지역근거')
    if region and region not in aliases:
        return None, 'object_other_region'
    if region in aliases and evidence:
        return region, 'evidenced_object_region'
    # Separate, caller-supplied evidence contract. No filename/global fallback.
    entries = (context or {}).get('objects', [])
    candidates = [e for e in entries if e.get('object_id') == obj['객체ID']]
    if candidates:
        regions = {e.get('region') for e in candidates}
        if len(regions) != 1 or not regions <= aliases:
            return None, 'document_region_conflict'
        if all(e.get('evidence') and e.get('scope') for e in candidates):
            return next(iter(regions)), 'explicit_document_object_scope'
    return None, 'not_explicit_own_scope'


def typed_fact(key, record, values):
    """Return None for ordinary contracts; otherwise explicit issues/events."""
    ctx=record.get('문맥 구분') or {}
    kind=record.get('레코드분류'); status=record.get('값상태')
    raw=record.get('값원문'); num=record.get('숫자값'); unit=record.get('단위원문')
    numeric=isinstance(num,(int,float)) and not isinstance(num,bool) and math.isfinite(num) and status=='명시값'
    issues=[]; events=[]
    def put(field,value):
        if values.get(field) not in (None,'') and values[field]!=value:
            issues.append('field_conflict:'+field)
        elif value is not None:
            values[field]=value
            events.append(dict(field=field,value=value,reason='typed_original_fact'))
    supported=False
    required=[]
    if key == 'mitigation_projects':
        # A named project is an entity, not a numeric measurement. Its source
        # measurements stay in the record/visual inventory; none become a name.
        from utils.visual_fact_identity import semantic_block
        supported = True
        required = ['지자체명', '사업명']
        if not isinstance(values.get('사업명'), str) or not values['사업명'].strip():
            issues.append('missing_explicit_project_name')
        view = {'대상시트': key, '항목': values.get('사업명'),
                '판독필드': {**values, '구조역할': ctx.get('구조역할'), '정보유형': ctx.get('정보유형')}}
        blocked = semantic_block(view)
        if blocked:
            issues.append('project_identity:' + blocked[0])
        events.append(dict(field='사업명', value=values.get('사업명'), reason='explicit_project_identity_only'))
    elif (key=='financial_plan' and kind=='text_fact' and num is None and isinstance(raw,str) and raw.strip()
          and (status=='정성상태' or (status=='정성표기' and raw.strip()=='비예산'))):
        supported=True; required=['지자체명']
        if not any(values.get(f) for f in ('사업명', '관리번호', '집계대상원문')):
            issues.append('missing:budget_identity')
        if values.get('예산액') is not None:
            issues.append('mixed_text_and_budget_amount')
        if (raw.strip()=='비예산' and not record.get('기간원문')
                and (values.get('연도') not in (None, '') or values.get('시간기준') in ('annual', '연간'))):
            # An annual-looking scalar may have come from a merged period cell.
            # Do not release it before the stated scope is recovered.
            issues.append('missing_budget_status_scope')
        if raw.strip()=='비예산' and values.get('연도') not in (None, '') and record.get('기간원문'):
            from utils.reading_year import same_year
            if not same_year(record['기간원문'],values['연도']):
                issues.append('budget_status_annual_scope_conflict')
        put('정보유형','예산상태'); put('값원문',raw)
    elif key=='governance_feedback':
        if (kind=='measurement' and numeric and ctx.get('측정유형')=='기관구성'
                and (unit, ctx.get('구성구분')) in (('개 과','과'), ('개 팀','팀'))
                and values.get('측정단위') == unit and values.get('항목원문') == ctx['구성구분']):
            values['측정단위'] = '개'
            events.append(dict(field='측정단위',before=unit,value='개',
                               reason='organization_count_unit_with_separate_item'))
            unit = '개'
        if kind=='text_fact' and num is None and isinstance(raw,str) and raw.strip():
            supported=True; required=['지자체명','거버넌스기구','역할']
            role = values.get('역할')
            compact = lambda value: re.sub(r'\s+', '', str(value or ''))
            original, summary = compact(raw), compact(role)
            evidence = compact(record.get('근거 문구'))
            # Only a literal prefix with a non-negative grammatical ending is
            # a safe summary alias. Paraphrases and negated claims stay held.
            suffix = original[len(summary):] if summary and original.startswith(summary) else None
            if (role not in (None, '', raw) and original in evidence and suffix is not None
                    and re.fullmatch(r'(?:하는|의)?(?:주관부서의)?역할을수행하고있음|(?:하는)?사업을담당하고있음', suffix)):
                put('역할요약원문', role)
                values['역할'] = raw
                events.append(dict(field='역할',before=role,value=raw,
                                   reason='literal_role_prefix_with_evidenced_ending'))
            put('역할',raw); put('정보유형','조직기능')
        elif kind=='measurement' and numeric and ctx.get('측정유형')=='기관구성' and unit in ('명','개'):
            supported=True; required=['지자체명','거버넌스기구','항목원문','측정단위']
            if num < 0 or num % 1 != 0:
                issues.append('invalid_organization_count')
            put('정보유형','기관구성'); put('측정값',num); put('측정단위',unit)
            put('항목원문',ctx.get('구성구분'))
        if supported:
            path = ctx.get('구성경로')
            parts = path if isinstance(path,list) else path.split(' > ') if isinstance(path,str) and ' > ' in path else None
            own = {v for v in (values.get('거버넌스기구'),values.get('담당부서')) if isinstance(v,str)}
            if parts and all(isinstance(p,str) and p.strip() for p in parts):
                put('구성경로원문',json.dumps(path,ensure_ascii=False) if isinstance(path,list) else path)
                if len(parts)>1 and parts[-1] in own:
                    put('상위기관원문',parts[-2])
                elif len(parts)>1:
                    issues.append('organization_path_leaf_conflict')
            # An entity's own name is not its parent. Preserve the stated name
            # in the audit; only a distinct, explicitly supplied parent applies.
            parent = ctx.get('상위기관원문')
            if parent:
                put('상위기관원문',parent)
            elif not parts and ctx.get('기관명') not in (values.get('거버넌스기구'),values.get('담당부서')):
                put('상위기관원문',ctx.get('기관명'))
            put('값원문',raw)
    elif key=='monitoring_performance':
        if kind=='text_fact' and num is None and status in ('아이콘범위','정성표기') and isinstance(raw,str) and raw.strip():
            supported=True; required=['지자체명','점검연도','사업명','이행실적']
            put('정보유형','정성성과'); put('값원문',raw)
            if status=='아이콘범위':
                if not record.get('범례원문') or not ctx.get('달성도범위'):
                    issues.append('missing_icon_legend')
                put('달성도범위원문',ctx.get('달성도범위'))
        elif kind=='measurement' and numeric and ctx.get('측정유형')=='사업 수':
            supported=True; required=['지자체명','점검연도','항목원문']
            put('정보유형','성과집계'); put('측정값',num); put('측정단위',unit)
            put('집계범위원문',ctx.get('집계범위'))
            if not values.get('항목원문'):
                put('항목원문',ctx.get('집계범위'))
            put('값원문',raw)
        elif kind == 'measurement' and num is None and status in ('명시값', '정성상태', '서술실적'):
            # A production performance row can contain several measured facts
            # in narrative fields. Preserve that text; do not choose/sum a
            # number or invent an achievement percentage or numeric unit.
            supported = True
            required = ['지자체명', '점검연도', '사업명', '이행실적']
            evidence = record.get('근거 문구')
            performance = values.get('이행실적')
            project = values.get('사업명')
            if (not isinstance(project, str) or not project.strip()
                    or re.fullmatch(r'합계|소계|총계|계|전체(?:\s*사업)?', project.strip())):
                issues.append('missing_explicit_project_name')
            if ctx.get('현황전망목표구분') not in ('실적', '이행실적', '계획 및 실적', '목표 및 실적', '목표 대비 실적'):
                issues.append('missing_explicit_performance_context')
            if not isinstance(evidence, str) or not evidence.strip() or not re.search(r'\d', evidence):
                issues.append('missing_performance_evidence')
            if not isinstance(raw, str) or not raw.strip():
                issues.append('missing_performance_literal')
            if not isinstance(performance, str) or not performance.strip() or not re.search(r'\d', performance):
                issues.append('missing_measured_performance_text')
            if type(values.get('점검연도')) is not int or not 1000 <= values['점검연도'] <= 9999:
                issues.append('invalid_performance_year')
            if values.get('측정값') is not None or values.get('측정단위') not in (None, ''):
                issues.append('mixed_scalar_and_performance_text')
            if ctx.get('구성구분') in ('합계', '소계', '분야별', '부문별') or values.get('정보유형') == '성과집계':
                issues.append('performance_aggregate_requires_separate_contract')
            put('정보유형', '사업실적'); put('값원문', raw)
            events.append(dict(field='이행실적', value=performance,
                               reason='explicit_performance_text_preserved', evidence=evidence))
    elif (key=='quantitative_reductions' and numeric and kind=='measurement'
          and (unit in ('tCO2eq/㎡','tCO₂eq/㎡') or
               (unit in ('tCO2eq/가구','tCO₂eq/가구') and values.get('정보유형')=='감축원단위'
                and ctx.get('값의미')=='감축원단위' and record.get('근거 문구')))):
        supported=True; required=['지자체명','사업명','감축원단위명','감축원단위단위']
        if values.get('예상감축량') is not None or values.get('활동량') is not None:
            issues.append('mixed_factor_and_measurement')
        put('감축원단위값',num); put('감축원단위단위',unit)
        put('감축원단위명',record.get('항목원문')); put('정보유형','감축원단위')
    elif key=='regional_conditions' and numeric and kind=='measurement' and ctx.get('시간성격')=='기간 평균':
        from utils.reading_year import explicit_period
        supported=True; required=['지자체명','지표명','단위']
        bounds = explicit_period(record.get('기간원문'))
        if bounds is None:
            issues.append('missing_explicit_average_period')
        else:
            put('기간시작',bounds[0]); put('기간종료',bounds[1])
            put('기간원문',record['기간원문']); put('시간기준','period_mean')
        put('값',num); put('단위',unit)
    if not supported:
        return None
    issues.extend('missing:'+f for f in required if values.get(f) in (None,''))
    return issues,events
