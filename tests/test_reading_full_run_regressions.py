"""Full-run adapter regressions with synthetic, held-out-style boundary cases."""
from copy import deepcopy

import pytest

import config
from utils.reading_financial_period import apply_contract
from utils.reading_pipeline import integrate_rows, instruction


@pytest.fixture(autouse=True)
def fixed_config(monkeypatch):
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', True)
    monkeypatch.setattr(config, 'REFERENCE_ENRICHMENT_ENABLED', False)


@pytest.mark.parametrize('key', ['financial_plan', 'quantitative_reductions'])
@pytest.mark.parametrize('marker', [None, '', 'annual', '연간'])
def test_single_year_period_does_not_require_a_total_marker(key, marker):
    values = {'연도': 2028, '기간시작': 2028, '기간종료': 2028, '시간기준': marker,
              '사업명': '시험 사업', '재원구분': '시비', '예산액': 0}
    before = deepcopy(values)
    waived, issues, events = apply_contract(key, {'연도': 2028, '기간원문': "’28"}, values)
    assert not issues and '연도' not in waived and values == before
    assert events[-1]['reason'] == 'explicit_single_year_period'


@pytest.mark.parametrize('patch', [
    {'연도': None}, {'연도': True}, {'기간시작': None}, {'기간종료': None},
    {'기간종료': 2029}, {'기간시작': 2027}, {'기간시작': '2028'},
    {'시간기준': 'cumulative'}, {'시간기준': 'unknown'},
])
def test_invalid_annual_period_stays_held(patch):
    values = {'사업명': '시험', '연도': 2028, '기간시작': 2028, '기간종료': 2028, **patch}
    before = deepcopy(values)
    assert 'period_without_total_marker' in apply_contract('quantitative_reductions', {}, values)[1]
    assert values == before


@pytest.mark.parametrize('record', [{'연도': 2029}, {'연도': True}, {'기간원문': '2028~2029'}])
def test_annual_record_period_conflicts_are_not_ignored(record):
    values = {'연도': 2028, '기간시작': 2028, '기간종료': 2028}
    assert apply_contract('quantitative_reductions', record, values)[1]


@pytest.mark.parametrize('context', ['기간 합계', '누적', 'period_total'])
def test_explicit_total_context_cannot_be_treated_as_annual(context):
    values = {'연도': 2028, '기간시작': 2028, '기간종료': 2028, '시간기준': 'annual'}
    assert apply_contract('quantitative_reductions', {'문맥 구분': {'시간성격': context}}, values)[1]


def test_total_still_requires_explicit_period_and_no_annual_year():
    values = {'시간기준': 'period_total', '연도': None}
    assert 'missing_or_invalid_total_period' in apply_contract('quantitative_reductions', {}, values)[1]
    values.update(연도=2028, 기간시작=2028, 기간종료=2028)
    assert 'period_total_year_conflict' in apply_contract('quantitative_reductions', {}, values)[1]


def performance():
    return {'지자체명': '서울특별시', '사업명': '시험 시설 개선', '점검연도': 2028,
            '이행실적': '목표 80개소 중 실적 72개소, 달성률 90%',
            '출처페이지': 12, '근거ID': 'original-id',
            '_reading': {'레코드분류': 'measurement', '값상태': '명시값',
                '근거문구': '목표 80개소, 실적 72개소, 달성률 90%',
                '값원문': '80개소, 72개소, 90%',
                '문맥': {'자료지역': '서울', '현황전망목표구분': '목표 대비 실적',
                         '측정유형': '사업 실적', '집계범위': '개별 사업'}}}


def bind(row, key='monitoring_performance', municipality='서울특별시'):
    return integrate_rows(key, [row], municipality)


def test_compound_performance_preserves_text_and_provenance_without_numeric_inference():
    row = performance(); before = deepcopy(row)
    rows, audit = bind(row)
    assert len(rows) == 1 and row == before
    out = rows[0]
    assert out['정보유형'] == '사업실적' and out['이행실적'] == row['이행실적']
    assert out['값원문'] == row['_reading']['값원문']
    assert out['근거ID'] == 'original-id' and out['출처페이지'] == 12
    assert all(out.get(k) is None for k in ['측정값', '측정단위', '달성여부'])
    assert audit[0]['original'] == before and '_reading' not in out
    assert any(e['reason'] == 'exact_reading_key_alias' for e in audit[0]['events'])
    assert bind(row) == (rows, audit)
    assert bind(out)[0] == rows  # A second pass does not reinterpret the envelope.


@pytest.mark.parametrize('status', [None, '정성상태'])
def test_literal_performance_without_a_scalar_status_does_not_invent_one(status):
    row = performance()
    if status is None:
        row['_reading'].pop('값상태')
    else:
        row['_reading']['값상태'] = status
        row['_reading']['값원문'] = '목표 달성'
    rows, audit = bind(row)
    assert rows and rows[0].get('측정값') is None
    assert audit[0]['original'] == row


@pytest.mark.parametrize('field,value', [('사업명', None), ('점검연도', None),
    ('사업명', '   '), ('사업명', '합계'),
    ('점검연도', True), ('점검연도', '28'), ('이행실적', '추진 예정'),
    ('측정값', 72), ('측정단위', '개소'), ('정보유형', '성과집계')])
def test_incomplete_or_mixed_performance_is_not_promoted(field, value):
    row = performance(); row[field] = value
    assert not bind(row)[0]


@pytest.mark.parametrize('year', ['2028', '2028년'])
def test_explicit_four_digit_performance_year_alias(year):
    row = performance(); row['점검연도'] = year
    rows, audit = bind(row)
    assert rows[0]['점검연도'] == 2028
    assert rows[0]['이행실적'] == row['이행실적']
    assert any(e.get('before') == year and e.get('value') == 2028 for e in audit[0]['events'])


@pytest.mark.parametrize('field,value', [('현황전망목표구분', '계획'),
    ('현황전망목표구분', None), ('구성구분', '합계'), ('구성구분', '분야별'),
    ('자료지역', '경기도'), ('자료구분', '전국 참고자료')])
def test_other_region_plans_and_aggregate_performance_remain_held(field, value):
    row = performance(); row['_reading']['문맥'][field] = value
    assert not bind(row)[0]


@pytest.mark.parametrize('field,value', [('근거문구', ''), ('근거문구', '추진'),
    ('값원문', ''), ('값상태', '추정값'), ('값상태', '미확인'), ('숫자값', True)])
def test_missing_or_uncertain_performance_evidence_remains_held(field, value):
    row = performance(); row['_reading'][field] = value
    assert not bind(row)[0]


@pytest.mark.parametrize('patch', [{'근거 문구': '서로 다른 근거'},
    {'문맥 구분': {'자료지역': '경기도'}}, {'문맥': 'not an object'}])
def test_conflicting_or_invalid_envelope_aliases_are_audited(patch):
    row = performance(); row['_reading'].update(patch); before = deepcopy(row)
    rows, audit = bind(row)
    assert not rows and row == before and audit[0]['original'] == before
    assert any('reading_alias_conflict' in i or 'invalid_record_context' in i for i in audit[0]['issues'])


def regional(region):
    return {'지자체명': '서울특별시', '지표범주': '인문사회', '지표명': '시험 지수',
            '연도': 2028, '값': 123.4, '단위': '지수', '출처페이지': 11,
            '_reading': {'문맥 구분': {'자료지역': region}}}


@pytest.mark.parametrize('region', ['서울', '서울시', '서울특별시'])
def test_exact_seoul_aliases_preserve_original_source_and_values(region):
    row = regional(region); before = deepcopy(row)
    rows, audit = bind(row, 'regional_conditions')
    assert len(rows) == 1 and rows[0]['값'] == 123.4
    assert rows[0]['지자체명'] == '서울특별시' and row == before
    assert audit[0]['original']['_reading']['문맥 구분']['자료지역'] == region


@pytest.mark.parametrize('region', ['수도권', '서울 및 수도권', '서울 외곽지역',
    '서울시·세종특별자치시·대전광역시', '경기도', '서울시 강남구'])
def test_region_substrings_and_multiple_regions_are_not_aliases(region):
    assert not bind(regional(region), 'regional_conditions')[0]


def test_unknown_region_not_filled_from_document_name():
    row = regional(None); row.pop('지자체명')
    assert not bind(row, 'regional_conditions')[0]
    row['_reading']['객체문맥'] = {'자료지역': '서울', '지역근거': '표의 서울시 지수', '적용범위': '이 표'}
    assert bind(row, 'regional_conditions')[0][0]['지자체명'] == '서울'


def test_seoul_alias_does_not_make_seoul_a_local_gyeonggi_row():
    assert not bind(regional('서울'), 'regional_conditions', '경기도')[0]


def test_prompt_explains_annual_and_compound_performance_contracts():
    assert '단일 연간 값' in instruction(['financial_plan'])
    assert '복합 실적' in instruction(['monitoring_performance'])
    assert not instruction(['document_meta'])
