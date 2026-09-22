"""Source-scoped regression: categories, year aliases and budget leakage."""
from copy import deepcopy
import pytest
import config
from tests import test_reading_semantic_binding as base
from utils.reading_pipeline import integrate_rows, instruction
from utils.reading_year import same_year, normalize_year
from utils.semantic_contract_guard import guard_annual_budget_rows, apply_semantic_contract_guards


@pytest.mark.parametrize('alias', [2020, '2020', '2020년', '’20', "'20"])
def test_year_alias_same_record_metadata(alias):
    fixture = base.SemanticBindingTests(); fixture.setUp(); fixture.link(value=alias)
    before = deepcopy(fixture.raw)
    result = fixture.run_b()
    assert result['accepted_candidates'][0]['values']['연도'] == 2020
    assert result['binding_log'][0]['value'] == alias
    assert result['binding_log'][0]['normalized_value'] == 2020
    assert fixture.raw == before


@pytest.mark.parametrize('alias', [20, '20', True, '’21', '2021', '2020~2029'])
def test_year_non_alias_is_held(alias):
    fixture = base.SemanticBindingTests(); fixture.setUp(); fixture.link(value=alias)
    assert not fixture.run_b()['accepted_candidates']


def test_short_year_cannot_create_century_or_cross_object_scope():
    assert normalize_year('’24') is None
    assert not same_year(24, 2024, allow_short=True)
    fixture = base.SemanticBindingTests(); fixture.setUp(); fixture.link(value='’20')
    fixture.raw['records'][1]['객체ID'] = 'OTHER'
    fixture.raw['objects'].append({'객체ID':'OTHER','공통문맥':{}})
    assert not fixture.run_b()['accepted_candidates']
    fixture.raw['records'][1]['객체ID'] = 'O'
    fixture.business.pop('연도'); fixture.raw['records'][0].pop('연도')
    assert not fixture.run_b()['accepted_candidates']


def pair():
    row = {'지자체명':'서울시','관리번호':'X-1','표ID':'표 1','출처페이지':7,
           '연도':2024,'목표물량':10,'목표단위':'백만 원'}
    budget = {k:row[k] for k in ('지자체명','관리번호','표ID','출처페이지','연도')}
    budget.update(예산액=10, 예산단위='백만원')
    return row, budget


def test_budget_quarantine_is_lossless_and_not_transfer(monkeypatch):
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', True)
    row, budget = pair(); before = deepcopy(row)
    out, audit = integrate_rows('annual_implementation', [row], '서울시', financial_rows=[budget])
    assert not out and row == before
    assert audit[0]['original'] == before and audit[0]['status'] == 'held'
    assert audit[0]['events'][-1]['financial_matches'][0]['row_index'] == 1
    clean = {'annual_implementation':[row], 'financial_plan':[budget]}
    guard = apply_semantic_contract_guards(clean)
    assert not clean['annual_implementation'] and clean['financial_plan'] == [budget]
    assert guard[-1]['row'] == before


@pytest.mark.parametrize('field,value', [('표ID','표 2'),('출처페이지',8),('관리번호','X-2'),
                                       ('연도',2025),('예산액',11),('예산단위','억원'),
                                       ('지자체명','경기도'),('관리번호','X1'),('표ID',None)])
def test_same_page_or_currency_alone_not_enough(field, value):
    row, budget = pair(); budget[field] = value
    assert guard_annual_budget_rows([row], financial_rows=[budget]) == ([row], [])


def test_table_ids_do_not_drop_hyphens():
    row, budget = pair(); row['표ID'] = '표 1-1'; budget['표ID'] = '표 11'
    assert guard_annual_budget_rows([row], financial_rows=[budget]) == ([row], [])


def test_legacy_monetary_goal_text_remains_without_envelope():
    row, budget = pair(); row['연간계획'] = '투자유치 목표'
    assert guard_annual_budget_rows([row], financial_rows=[budget]) == ([row], [])


def test_normal_money_kpi_kept_even_with_coincident_budget():
    row, budget = pair()
    row['_reading'] = {'근거 문구':'투자유치액 목표 10백만원', '문맥 구분':{'값의미':'금액형 성과지표'}}
    assert guard_annual_budget_rows([row], financial_rows=[budget]) == ([row], [])


def test_explicit_budget_and_nonbudget_status():
    row, budget = pair()
    row['_reading'] = {'근거 문구':'연차별 재정투자 계획', '문맥 구분':{'값의미':'재정투자 계획 예산'}}
    assert not guard_annual_budget_rows([row])[0]
    row.pop('_reading'); row.update(목표물량=None, 연간계획='비예산')
    budget.update(예산액=None, 값원문='비예산')
    assert not guard_annual_budget_rows([row], financial_rows=[budget])[0]


def test_prompt_separates_meaning_axes():
    prompt = instruction(['financial_plan','annual_implementation','quantitative_reductions'])
    assert '정책 분류' in prompt and '금액형 성과지표' in prompt and '세기 근거' in prompt
