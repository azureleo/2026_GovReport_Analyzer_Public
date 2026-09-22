"""Explicit financial unknowns and period totals; no inferred amounts or years."""
import json
import re
from copy import deepcopy
from utils.reading_year import same_year

EXTRA_COLUMNS = {
    'quantitative_reductions': ['기간시작', '기간종료'],
    'financial_plan': ['기간시작', '기간종료', '시간기준', '재원표기상태', '관리번호', '집계대상원문', '계획구분상태'],
}
PLAN_KINDS = frozenset(('투자계획', '재정투자계획', '집행실적', '예산집행액'))


def apply_contract(key, record, values, *, verified_fields=()):
    """Mutate candidate only; return waived required fields, issues, audit events."""
    if key not in EXTRA_COLUMNS:
        return set(), [], []
    ctx = record.get('문맥 구분') or {}
    waived, issues, events = set(), [], []
    def assign(field, value, reason, **provenance):
        if values.get(field) not in (None, '') and values[field] != value:
            issues.append('field_conflict:' + field)
            return
        if values.get(field) != value:
            values[field] = value
            events.append(dict(field=field, value=value, reason=reason, **provenance))
    if key == 'financial_plan':
        # Policy category and planned/actual financial kind are independent.
        # Keep the latter in the audit; never invent a policy category from it.
        evidence = record.get('근거 문구')
        if not isinstance(evidence, str) or not evidence.strip():
            evidence = None
        explicit_plan = (record.get('레코드분류') == 'measurement'
                and record.get('값상태') == '명시값'
                and ctx.get('현황전망목표구분') == '계획'
                and ctx.get('값의미') == '재정투자 계획 예산'
                and evidence)
        if explicit_plan:
            events.append(dict(field='재정자료성격', value='투자계획',
                               reason='explicit_financial_plan_context', audit_only=True,
                               source_fields=['문맥 구분.현황전망목표구분', '문맥 구분.값의미', '근거 문구'],
                               evidence=deepcopy(evidence)))
            if values.get('계획구분') in ('집행실적', '예산집행액'):
                issues.append('field_conflict:계획구분')
        if record.get('레코드분류') and values.get('계획구분') in PLAN_KINDS:
            before = values['계획구분']
            values['계획구분'] = None
            events.append(dict(field='재정자료성격', value=before, before=before,
                               source_field='계획구분', audit_only=True,
                               reason='legacy_financial_kind_not_policy_category'))
        if explicit_plan and values.get('계획구분') in (None, ''):
            # Missing classification must not discard a separately evidenced
            # amount. The normal number/unit/region/year checks still apply.
            waived.add('계획구분')
            assign('계획구분상태', '미확정', 'explicit_budget_with_unresolved_policy_category')
        if record.get('레코드분류') and evidence:
            subtotal = ctx.get('집계범위')
            if isinstance(subtotal, str) and subtotal.strip() and not values.get('사업명'):
                assign('집계대상원문', subtotal, 'explicit_record_aggregate_scope')
            # A table total is not evidence for total funding. Keep the original
            # assertion in the audit and expose its unresolved status, not zero.
            if (values.get('재원구분') == '합계' and '재원구분' not in verified_fields
                    and not re.search(r'(?:재원|재원별)\s*(?:구분|합계|총계)', evidence)
                    and ctx.get('재원구분') != '합계'):
                values['재원구분'] = None
                assign('재원표기상태', '근거미확인', 'funding_total_not_evidenced')
                events.append(dict(field='재원구분', before='합계', value=None,
                                   reason='table_total_is_not_funding_total'))
        if values.get('재원구분') in (None, ''):
            # Null is not a statement of zero, public funding, or total funding.
            if values.get('재원표기상태') not in ('근거미확인', '미표기'):
                assign('재원표기상태', '미표기', 'funding_absent_in_fixed_input')
            waived.add('재원구분')
            path = record.get('행머리글 경로')
            if isinstance(path, list) and path:
                assign('집계대상원문', json.dumps(path, ensure_ascii=False), 'literal_row_header_path')
            if not any(values.get(f) for f in ('사업명', '관리번호', '집계대상원문')):
                issues.append('missing:budget_identity')
        item = record.get('항목원문') or values.get('사업명')
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z]+\d*-\d+', item):
            assign('관리번호', item, 'explicit_project_code')
        if (record.get('레코드분류') and explicit_plan
                and not any(values.get(f) for f in ('사업명', '관리번호', '집계대상원문'))):
            issues.append('missing:budget_identity')
    if key == 'financial_plan' and values.get('시간기준') == 'period_status':
        from utils.reading_year import explicit_period
        bounds = (values.get('기간시작'), values.get('기간종료'))
        stated = explicit_period(record.get('기간원문'), bounds)
        if (record.get('레코드분류') != 'text_fact' or stated != bounds
                or not all(type(v) is int and 1000 <= v <= 9999 for v in bounds)
                or values.get('연도') not in (None, '') or record.get('연도') not in (None, '')):
            issues.append('invalid_budget_status_period')
        return waived, issues, events
    # Only an explicit total marker + record-level period can waive year.
    total = values.get('시간기준') in ('합계', '기간합계', 'period_total') or bool(
        re.fullmatch(r'\d{4}\s*[~\-–]\s*\d{4}\s*합계', str(ctx.get('시간성격') or '')))
    if total:
        if values.get('시간기준') not in (None, '', '합계', '기간합계', 'period_total'):
            issues.append('field_conflict:시간기준')
        match = re.fullmatch(r'\s*(\d{4})\s*[~\-–]\s*(\d{4})\s*', str(record.get('기간원문') or ''))
        start, end = values.get('기간시작'), values.get('기간종료')
        if match:
            start, end = map(int, match.groups())
            assign('기간시작', start, 'explicit_record_period')
            assign('기간종료', end, 'explicit_record_period')
        context_period = re.fullmatch(r'(\d{4})\s*[~\-–]\s*(\d{4})\s*합계', str(ctx.get('시간성격') or ''))
        if context_period and (start, end) != tuple(map(int, context_period.groups())):
            issues.append('field_conflict:기간')
        valid = type(start) is int and type(end) is int and 1000 <= start <= end <= 9999
        if not valid:
            issues.append('missing_or_invalid_total_period')
        elif values.get('연도') not in (None, '') or record.get('연도') not in (None, ''):
            issues.append('period_total_year_conflict')
        else:
            # Replace only a known synonymous temporal label, never a year.
            before = values.get('시간기준')
            values['시간기준'] = 'period_total'
            events.append(dict(field='시간기준', before=before, value='period_total', reason='explicit_period_total'))
            waived.add('연도')
    elif values.get('기간시작') is not None or values.get('기간종료') is not None:
        year = values.get('연도')
        start, end = values.get('기간시작'), values.get('기간종료')
        # An explicit one-year interval is redundant annual provenance, not a
        # multi-year total. Never infer a missing year or collapse an interval.
        annual = (type(year) is int and 1000 <= year <= 9999
                  and type(start) is int and type(end) is int
                  and start == end == year
                  and values.get('시간기준') in (None, '', 'annual', '연간'))
        record_year = record.get('연도')
        if record_year not in (None, '') and not same_year(record_year, year, allow_short=bool(record.get('근거 문구'))):
            annual = False
        stated_period = re.fullmatch(r'\s*(\d{4})\s*[~\-–]\s*(\d{4})\s*', str(record.get('기간원문') or ''))
        if stated_period and tuple(map(int, stated_period.groups())) != (year, year):
            annual = False
        if ctx.get('시간성격') in ('합계', '기간합계', '기간 합계', '누적', 'cumulative', 'period_total'):
            annual = False
        if annual:
            events.append(dict(field='연도', value=year, reason='explicit_single_year_period',
                               period_start=start, period_end=end))
        else:
            issues.append('period_without_total_marker')
    return waived, issues, events
