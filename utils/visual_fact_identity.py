"""Conservative visual fact identity and routing, independent of gold/model.

This module never repairs readings or fills missing semantic metadata. A held
observation remains in the visual inventory, not in an unrelated business cell.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

from utils.visual_contract import normalize_comparison_text, normalize_quantity

VERSION = 'visual-fact/1'


def text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return normalize_comparison_text(value)


def fields(row):
    value = row.get('판독필드')
    return value if isinstance(value, dict) else {}


def context(row):
    envelope = fields(row).get('_reading')
    ctx = envelope.get('문맥 구분') if isinstance(envelope, dict) else None
    return ctx if isinstance(ctx, dict) else {}


def scope(row):
    ids = row.get('근거ID목록') or [row.get('근거ID')]
    if isinstance(ids, str):
        ids = [ids]
    return text(row.get('페이지')), tuple(sorted({str(x) for x in ids if x}))


def project_name(value):
    # Only an explicit terminal word; do not fuzzy-match substantive names.
    return text(re.sub(r'\s+사업$', '', str(value or '').strip()))


def measure_role(row):
    f, ctx = fields(row), context(row)
    labels = ' '.join(str(x or '') for x in (
        row.get('항목'), f.get('측정항목'), f.get('지표명'), f.get('값역할'),
        f.get('정보유형'), ctx.get('값의미'), ctx.get('측정유형')))
    unit = text(row.get('원문단위') or row.get('단위') or f.get('단위'))
    if '원단위' in labels or '/' in unit:
        return 'intensity'
    if re.search(r'증가량|감소량|증감량|변화량|감축량', labels):
        return 'delta'
    if '%' in unit or re.search(r'증가율|감소율|증감률|감축률|비율|비중|점유율', labels):
        return 'ratio'
    return 'amount'


def period(row):
    f = fields(row)
    year = row.get('연도') if row.get('연도') is not None else f.get('연도')
    original = text(f.get('기간원문'))
    # A printed single-year label may repeat an already explicit four-digit
    # year. Canonicalize it only when it agrees; never expand an unknown year.
    year_text = text(year)
    if re.fullmatch(r'\d{4}', year_text) and original in {
            year_text, year_text + '년', "'" + year_text[-2:], "'" + year_text[-2:] + '년'}:
        original = ''
    # Unknown is not an annual or multi-year wildcard.
    return tuple(text(v) for v in (year, f.get('기간시작'), f.get('기간종료'),
                                   original, f.get('시간기준')))


def funding(row):
    f = fields(row)
    value = f.get('재원구분')
    if not value and text(row.get('항목')) in {'계','합계','총계','국비','시비','도비','구비','군비','기타'}:
        value = row.get('항목')
    value = text(value)
    return '합계' if value in {'계','합계','총계'} else value


def financial_group(row):
    f = fields(row)
    return (scope(row), project_name(f.get('관리번호') or f.get('사업명') or row.get('항목')),
            period(row), text(f.get('계획구분')))


def fact_identity(row):
    """No measured value in this key. Preserve metric/series/period distinctions."""
    f, ctx = fields(row), context(row)
    sheet = row.get('대상시트')
    item = text(row.get('항목'))
    role = measure_role(row)
    region = text(ctx.get('자료지역') or f.get('자료지역'))
    qualifiers = tuple(text(f.get(k) or ctx.get(k)) for k in (
        '자료구분', '집계범위', '구성구분', '기준연도', '기준기간', '비교기준', '시간성격'))
    if sheet == 'emissions_forecast':
        sector = text(f.get('부문') or row.get('항목'))
        # Only harmless total-label suffixes, scoped to the SAME source object.
        sector = re.sub(r'(?:온실가스)?(?:총)?배출량$', '', sector)
        sector = re.sub(r'(?:전체|총계)$', '', sector)
        entity = (sector, text(f.get('세부부문')), text(f.get('시나리오')))
        metric = role if role == 'amount' else (role, item)
    elif sheet == 'financial_plan':
        entity = financial_group(row)[1]
        metric = ('budget', funding(row), text(f.get('집계대상원문')), text(f.get('정보유형')))
    elif sheet == 'mitigation_projects':
        entity = (text(f.get('관리번호')), project_name(f.get('사업명') or f.get('감축사업명')))
        # A project ID alone cannot identify area, household count, reduction,
        # parent node and department. Never collapse their item/legend labels.
        metric = (item, text(f.get('측정항목') or f.get('지표명')),
                  text(f.get('범례항목') or f.get('범례원문')), text(f.get('축항목')),
                  text(f.get('달성도범위') or ctx.get('달성도범위')), role,
                  text(f.get('정보유형')), text(f.get('구조역할')))
    elif sheet == 'governance_feedback':
        entity = (text(f.get('거버넌스기구')), text(f.get('상위기관원문') or ctx.get('기관명')),
                  text(f.get('구성경로원문') or ctx.get('구성경로')))
        metric = (text(f.get('정보유형')), text(f.get('항목원문') or ctx.get('구성구분')),
                  text(f.get('역할')))  # Distinct functions are not duplicate values.
    else:
        entity = item
        metric = tuple(text(f.get(k)) for k in ('부문','세부부문','지표명','값역할','구성구분','정보유형'))
    return scope(row), sheet, entity, metric, period(row), region, qualifiers


def same_quantity(left, right):
    """Exact numeric equality after supported scaling, never a 0.5% tolerance."""
    def quantity(row):
        raw = row.get('원문값', row.get('값'))
        unit = row.get('원문단위') or row.get('단위') or ''
        if isinstance(raw, bool) or raw is None:
            return None
        q = normalize_quantity(raw, unit)
        if q['value'] is None:
            return None
        try:
            value = Decimal(str(raw).replace(',', '').strip()) * Decimal(str(q['multiplier']))
            if not value.is_finite():
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
        return value, normalize_comparison_text(q['unit'], '단위')
    a, b = quantity(left), quantity(right)
    return a is not None and b is not None and a == b


def semantic_index(peers):
    """One linear scan; avoid repeatedly scanning full-document observations."""
    funds, codes, names = set(), {}, {}
    for p in peers:
        if not isinstance(p, dict):
            continue
        if p.get('대상시트') == 'financial_plan' and funding(p):
            funds.add(financial_group(p))
        code = str(fields(p).get('관리번호') or p.get('항목') or '').strip()
        codes.setdefault(scope(p), set()).add(code)
        f = fields(p)
        if p.get('대상시트') == 'mitigation_projects' and f.get('관리번호') and f.get('사업명'):
            names.setdefault((scope(p), text(f['관리번호'])), set()).add(project_name(f['사업명']))
    return {'funds': funds, 'codes': codes, 'names': names}


def semantic_block(row, peers=(), *, index=None):
    """Return (kind, reason), or None. Uncertain facts are preserved for review."""
    sheet, f = row.get('대상시트'), fields(row)
    item = text(row.get('항목'))
    if sheet == 'emissions_forecast' and measure_role(row) != 'amount':
        return 'non_total_measure', '총량 전망값과 증가량·비율·원단위 분리 필요'
    if sheet == 'mitigation_projects':
        name = text(f.get('사업명') or f.get('감축사업명') or row.get('항목'))
        headers = {'주관부서','협조부서','담당부서','사업명','사업개요','관리번호','과제번호','사업수','달성도'}
        if item in headers or name in headers or item.startswith('사업수('):
            return 'header_or_metadata', '표 머리글·담당부서·사업 수는 개별 사업이 아님'
        if item in {'계','합계','소계','총계'} or name in {'계','합계','소계','총계'}:
            return 'aggregate', '집계 행을 개별 사업명으로 저장할 수 없음'
        role = text(f.get('구조역할') or f.get('정보유형'))
        if role in {'상위과제','핵심과제','전략','주체','성과집계'}:
            return 'parent_or_metadata', '상위 과제·성과 집계·주체는 사업과 분리 필요'
        parent_code = str(f.get('관리번호') or row.get('항목') or '').strip()
        codes = (index['codes'].get(scope(row), ()) if index is not None else
                 (str(fields(p).get('관리번호') or p.get('항목') or '') for p in peers if scope(p) == scope(row)))
        if re.fullmatch(r'[A-Za-z]+\d+', parent_code) and any(
                re.match(re.escape(parent_code) + r'[-－–]\d+', code) for code in codes):
            return 'parent_task', '같은 객체의 하위 사업 ID가 있는 상위 과제'
        if f.get('관리번호') and f.get('사업명'):
            idx = index if index is not None else semantic_index(peers)
            if len(idx['names'].get((scope(row), text(f['관리번호'])), ())) > 1:
                return 'project_name_conflict', '동일 객체·관리번호에 서로 다른 사업명: 자동 교정하지 않음'
        raw_value = row.get('원문값', row.get('값'))
        if (raw_value is not None and (row.get('단위') or row.get('원문단위'))
                and not (f.get('사업명') or f.get('감축사업명'))):
            return 'project_measurement', '명시 사업명 없는 측정항목을 사업명으로 만들지 않음'
    if sheet == 'financial_plan' and not funding(row):
        explicit = bool(scope(row)[1]) and (financial_group(row) in index['funds'] if index is not None else any(
            p is not row and p.get('대상시트') == sheet and financial_group(p) == financial_group(row) and funding(p)
            for p in peers))
        if explicit:
            return 'funding_unresolved', '같은 사업·기간·객체의 재원 구분 존재: 미연결 값을 미표기로 추가하지 않음'
    return None


def duplicate_match(row, accepted):
    """Only already accepted exact-source observations can suppress an output."""
    if len(scope(row)[1]) != 1:
        return None
    identity = fact_identity(row)
    for other in accepted:
        if other is row or fact_identity(other) != identity:
            continue
        if same_quantity(row, other):
            return 'duplicate', other
        if row.get('원문값', row.get('값')) is not None:
            return 'needs_review', other
    return None
