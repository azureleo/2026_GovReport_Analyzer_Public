from copy import deepcopy

import pytest

from utils.visual_fact_identity import (
    fact_identity, semantic_block, semantic_index, duplicate_match, same_quantity,
)
from utils.vision_recovery_workflow import merge_fresh
from utils.vision_recovery_runtime import settings, offline_guard, save_and_verify


def obs(item='건물', sheet='emissions_forecast', value=100, unit='천tCO2eq', **f):
    return {'페이지': 1, '항목': item, '대상시트': sheet, '원문값': value, '값': value,
            '연도': 2030, '단위': unit, '원문단위': unit, '판독필드': f,
            '근거ID목록': ['ev-1'], '근거ID': 'ev-1', '원본객체ID목록': ['obj-1'],
            '값근거': 'table_cell', '그래프유형': '표', '신뢰도': 'high', '반영여부': '검토',
            '병합상태': 'candidate', '병합차단사유': ''}


def source(rows):
    return {'municipality_name': '서울특별시', 'chart_observations': deepcopy(rows),
            'document_objects': [{'object_id': 'obj-1', 'page_number': 1, 'object_type': 'image',
                                  'metadata': {'evidence_id': 'ev-1', 'final_status': 'extracted'}}]}


@pytest.mark.parametrize('label,unit', [('2021년 대비 증가량','천tCO2eq'),
    ('감축량','tCO2eq'),('점유율','%'),('감축원단위','tCO2eq/㎡')])
def test_non_total_measure_kept_out_of_forecast_even_if_role_claims_total(label, unit):
    row = obs(label, unit=unit, 값역할='배출전망')
    before = deepcopy(row)
    assert semantic_block(row)[0] == 'non_total_measure'
    assert row == before


@pytest.mark.parametrize('label', ['소계','합계','주관부서','협조부서','사업 수(24)'])
def test_headers_are_not_projects_even_with_explicit_project_name(label):
    assert semantic_block(obs(label, 'mitigation_projects', None, '', 사업명=label))


def test_project_measurements_are_separate_and_real_names_still_pass():
    assert semantic_block(obs('주관부서 역량강화 사업', 'mitigation_projects', None, '')) is None
    assert semantic_block(obs('공공주택', 'mitigation_projects', 1000, '세대'))[0] == 'project_measurement'
    assert semantic_block(obs('공공주택', 'mitigation_projects', 1000, '세대', 사업명='단열 시공')) is None


def test_parent_code_requires_evidence_of_child_in_same_object():
    parent = obs('B1', 'mitigation_projects', None, '', 사업명='기존 건물 전환')
    child = obs('B1-11', 'mitigation_projects', None, '', 사업명='창호 시공')
    assert semantic_block(parent, [parent]) is None
    assert semantic_block(parent, [parent, child])[0] == 'parent_task'
    child['근거ID목록'] = ['ev-2']
    assert semantic_block(parent, [child]) is None


def test_funding_missing_is_not_missing_in_document_when_source_has_labeled_rows():
    missing = obs('창호 시공', 'financial_plan', 1500, '백만원', 사업명='창호 시공', 계획구분='투자계획')
    total = obs('계', 'financial_plan', 1500, '백만원', 사업명='창호 시공 사업', 계획구분='투자계획', 재원구분='계')
    total['판독필드']['기간원문'] = '’30'
    assert semantic_block(missing, [missing]) is None
    assert semantic_block(missing, [total])[0] == 'funding_unresolved'
    total['판독필드']['사업명'] = '다른 사업'
    assert semantic_block(missing, [total]) is None


def test_metrics_not_collapsed_by_shared_project_id():
    rows = [obs(label, 'mitigation_projects', value, unit, 관리번호='B1-11', 사업명='창호 시공')
            for label, value, unit in [('공공주택',1000,'세대'), ('민간주택',1500,'세대'),
                                      ('평균면적',41.3,'㎡'), ('감축원단위',0.00648,'tCO2eq/㎡')]]
    result, audit = merge_fresh([rows[0]], rows[1:], 1, ['ev-1'])
    assert len(result) == 4
    assert all(x['status'] == 'added' for x in audit)


def test_same_project_code_with_conflicting_names_stays_held_not_fuzzy_repaired():
    a=obs('A1-1','mitigation_projects',None,'',사업명='건물 인증기준',관리번호='A1-1')
    b=obs('A1-1','mitigation_projects',None,'',사업명='건물 인센티브',관리번호='A1-1')
    idx=semantic_index([a,b])
    assert semantic_block(a,index=idx)[0] == 'project_name_conflict'
    assert semantic_block(b,[a,b]) == semantic_block(b,index=idx)


def test_indexed_semantic_decisions_equal_plain_decisions():
    rows=[obs('B1','mitigation_projects',None,''), obs('B1-1','mitigation_projects',None,''),
          obs('계','financial_plan',재원구분='계',사업명='시공'),obs('시공','financial_plan',사업명='시공')]
    idx=semantic_index(rows)
    assert [semantic_block(r,rows) for r in rows] == [semantic_block(r,index=idx) for r in rows]


def test_legacy_image_inline_merge_cannot_bypass_meaning_guard():
    from agents.image_agent import ImageAgent
    item={'항목':'소계','값':None,'연도':2030,'단위':'','fields':{'사업명':'소계'}}
    allowed,_,reasons=ImageAgent()._chart_merge_decision(
        {'type':'chart_table','chart_type':'표','table':[item],'confidence':'high'},item,'mitigation_projects')
    assert not allowed and any('G6 의미 분리' in r for r in reasons)


def test_performance_bands_are_distinct():
    a = obs('사업 수(24)', 'mitigation_projects', 4, '개', 달성도범위='60% 미만')
    b = obs('사업 수(24)', 'mitigation_projects', 2, '개', 달성도범위='60~80%')
    assert fact_identity(a) != fact_identity(b)


def test_exact_scope_period_funding_and_subsector_are_identity_not_wildcards():
    base = obs(시나리오='BAU', 부문='건물')
    for field,value in [('세부부문','가정'), ('시나리오','목표'), ('기간원문','2024~2033'), ('기준연도',2021)]:
        other=deepcopy(base)
        other['판독필드'][field]=value
        assert fact_identity(base) != fact_identity(other)
    other=deepcopy(base)
    other['근거ID목록']=['ev-2']
    assert duplicate_match(other,[base]) is None
    a=obs('사업', 'financial_plan', 재원구분='계')
    b=obs('사업', 'financial_plan', 재원구분='시비')
    assert fact_identity(a) != fact_identity(b)


def test_total_wording_and_scaled_units_can_be_duplicates_without_rewriting():
    a=obs('서울시 온실가스 배출량', value=47, 시나리오='BAU')
    b=obs('서울시 온실가스 배출량', value=47000, unit='tCO2eq', 시나리오='BAU', 부문='서울시 전체')
    assert fact_identity(a) == fact_identity(b)
    assert duplicate_match(b,[a])[0] == 'duplicate'
    b['원문값']=47001
    assert duplicate_match(b,[a])[0] == 'needs_review'
    assert not same_quantity(a,b)


def test_co2_is_not_co2eq_and_close_values_are_not_equal():
    assert not same_quantity(obs(value=14046),obs(value=14064))
    assert not same_quantity(obs(unit='tCO2'),obs(unit='tCO2eq'))


def test_real_organizer_and_excel_enforce_guards_without_recovery_flag(tmp_path):
    rows = [obs('서울시 온실가스 배출량', 시나리오='BAU'),
            obs('서울시 온실가스 배출량', 시나리오='BAU', 부문='서울시 전체'),
            obs('2021년 대비 증가량', 시나리오='BAU', 부문='서울시 전체'),
            obs('소계','mitigation_projects',None,''),
            obs('주관부서','mitigation_projects',None,''),
            obs('창호 시공','mitigation_projects',None,'',사업명='창호 시공'),
            obs('계','financial_plan',1500,'백만원',사업명='창호 시공 사업',계획구분='투자계획',재원구분='계'),
            obs('창호 시공','financial_plan',1500,'백만원',사업명='창호 시공',계획구분='투자계획')]
    # The legacy destination override must not bypass the forecast meaning gate.
    rerouted = obs('2021년 대비 증가량', 'emissions_regional', 시나리오='BAU')
    rerouted['제목'] = '서울시 BAU 전망'
    rows.append(rerouted)
    raw=source(rows)
    before=deepcopy(raw)
    with offline_guard(), settings(VISUAL_MERGE_LABELED_ENABLED=True, VISUAL_EVIDENCE_MERGE_ENABLED=True,
            READING_PIPELINE_ENABLED=True, REFERENCE_ENRICHMENT_ENABLED=False,
            SOURCE_VERIFICATION_ENABLED=False, CODEBOOK_SHEET_ENABLED=False):
        data,cells=save_and_verify(raw,tmp_path/'checked',False)
    assert len(data['emissions_forecast']) == 1
    assert len(data['mitigation_projects']) == 1
    assert len(data['financial_plan']) == 1
    assert data['chart_observations'][-1]['의미분리유형'] == 'non_total_measure'
    assert data['chart_observations'][-1]['대상시트'] == 'emissions_regional'
    assert len(data['chart_observations']) == len(rows)
    assert len(data['visual_semantic_audit']) >= 5
    assert cells['passed'] and cells['deterministic']
    # Organizer updates status on its input; raw fields/values must be intact.
    assert [r['판독필드'].get('사업명') for r in raw['chart_observations']] == [r['판독필드'].get('사업명') for r in before['chart_observations']]


def test_prompt_and_checkpoint_identity_include_semantic_rules():
    from agents.image_agent import CHART_TABLE_SYSTEM, ImageAgent
    from utils.reading_pipeline import instruction
    assert all(s in CHART_TABLE_SYSTEM for s in ('측정항목','기준연도','주관부서','재원구분'))
    assert 'chart_table_prompt_sha256' in ImageAgent._vision_descriptor([])
    assert '총량이 아닌 값' in instruction({'emissions_forecast'})
