"""New response contracts; no source-answer memorization or external calls."""
from copy import deepcopy
import pytest
import config
from utils.reading_pipeline import integrate_rows
from utils.reading_year import explicit_period

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(config,'READING_PIPELINE_ENABLED',True)

def budget():
    return {'지자체명':'서울시','사업명':'T-7','계획구분':None,'재원구분':'합계',
            '연도':2027,'예산액':123,'예산단위':'백만원','출처페이지':2,
            '_reading':{'레코드분류':'measurement','값상태':'명시값',
                '근거 문구':'시험 부문 연차별 재정투자 계획',
                '문맥 구분':{'현황전망목표구분':'계획','값의미':'재정투자 계획 예산'}}}

def run(key,row):
    before=deepcopy(row)
    result=integrate_rows(key,[row],'서울시')
    assert row==before
    return result

def test_amount_survives_unknown_policy_and_unverified_funding():
    rows,audit=run('financial_plan',budget())
    assert len(rows)==1 and rows[0]['예산액']==123
    assert rows[0]['계획구분'] is None and rows[0]['계획구분상태']=='미확정'
    assert rows[0]['재원구분'] is None and rows[0]['재원표기상태']=='근거미확인'
    assert rows[0]['관리번호']=='T-7'
    assert audit[0]['original']['재원구분']=='합계'

@pytest.mark.parametrize('field,value',[('예산액',None),('예산액',True),('예산단위','kg'),('연도',None)])
def test_missing_category_exception_does_not_waive_other_requirements(field,value):
    row=budget();row[field]=value
    assert not run('financial_plan',row)[0]

def test_missing_category_requires_evidence_and_identity():
    row=budget();row['_reading'].pop('근거 문구')
    assert not run('financial_plan',row)[0]
    row=budget();row['사업명']=None
    assert not run('financial_plan',row)[0]
    row['_reading']['문맥 구분']['집계범위']='시험 부문 계'
    result=run('financial_plan',row)[0][0]
    assert result['집계대상원문']=='시험 부문 계'

def test_actual_vs_planned_remains_conflict():
    row=budget();row['계획구분']='집행실적'
    assert not run('financial_plan',row)[0]

def test_verified_funding_total_not_cleared():
    row=budget()
    row['_reading']['메타데이터']=[{'역할':'재원구분','필드':'재원구분','값':'합계',
        '근거':'국비+시비 합계','적용범위':'이 예산행'}]
    assert run('financial_plan',row)[0][0]['재원구분']=='합계'

def total():
    row=budget();row.update(연도=None,기간시작=2027,기간종료=2036,시간기준='period_total')
    row['_reading']['메타데이터']=[{'역할':'연도','필드':'연도','값':'’27~’36',
        '근거':'연도 머리글 및 합계 열','적용범위':'이 합계행'}]
    return row

def test_period_link_confirms_bounds_without_becoming_year():
    rows,audit=run('financial_plan',total())
    assert rows[0]['연도'] is None and rows[0]['기간종료']==2036
    assert any(e.get('status')=='confirmed_explicit_period_not_year' for e in audit[0]['events'])

@pytest.mark.parametrize('value',['’28~’36','2027~2037','27~36',True])
def test_nonmatching_period_link_stays_held(value):
    row=total();row['_reading']['메타데이터'][0]['값']=value
    assert not run('financial_plan',row)[0]

def test_short_range_requires_full_endpoints():
    assert explicit_period('’27~’36') is None
    assert explicit_period('2027~2036')==(2027,2036)
    row=total();row['기간시작']=None
    assert not run('financial_plan',row)[0]

def test_nonbudget_merged_period_and_missing_scope():
    row=budget();row.update(예산액=None,정보유형='예산상태',값원문='비예산')
    row['_reading'].update(레코드분류='text_fact',값상태='정성표기')
    assert not run('financial_plan',row)[0]
    row.update(연도=None,기간시작=2027,기간종료=2036,기간원문='2027~2036',시간기준='period_status')
    rows,_=run('financial_plan',row)
    assert rows[0]['예산액'] is None and rows[0]['시간기준']=='period_status'
    row['연도']=2027
    assert not run('financial_plan',row)[0]
    row.pop('기간시작');row.pop('기간종료');row['시간기준']='annual'
    assert not run('financial_plan',row)[0]

def test_role_literal_prefix_only():
    row={'지자체명':'서울시','거버넌스기구':'환경과','역할':'이행관리를 모니터링하고 관리',
         '값원문':'이행관리를 모니터링하고 관리하는 주관부서의 역할을 수행하고 있음','정보유형':'조직기능',
         '_reading':{'레코드분류':'text_fact','근거문구':'환경과는 이행관리를 모니터링하고 관리하는 주관부서의 역할을 수행하고 있음','문맥 구분':{}}}
    rows,_=run('governance_feedback',row)
    assert rows[0]['역할']==row['값원문'] and rows[0]['역할요약원문']==row['역할']
    row['역할']='이행관리 책임이 없음'
    assert not run('governance_feedback',row)[0]
    row['역할']=row['값원문'];row['정보유형']='기관구성'
    assert not run('governance_feedback',row)[0]

def test_household_factor_is_separate_from_reduction():
    row={'지자체명':'서울시','사업명':'주택사업','정보유형':'감축원단위','감축원단위명':'가구당 감축량',
         '감축원단위값':0.4,'감축원단위단위':'tCO2eq/가구','예상감축량':None,
         '_reading':{'레코드분류':'measurement','값상태':'명시값','근거 문구':'0.4tCO2eq/가구',
                      '문맥 구분':{'값의미':'감축원단위'}}}
    rows,_=run('quantitative_reductions',row)
    assert rows[0]['감축원단위값']==0.4 and rows[0]['예상감축량'] is None
    row['예상감축량']=400
    assert not run('quantitative_reductions',row)[0]

def test_period_mean_context_survives():
    row={'지자체명':'서울시','지표명':'평균기온','연도':1990,'값':12.0,'단위':'℃',
         '_reading':{'기간원문':'1991~2000','문맥 구분':{'시간성격':'기간 평균'}}}
    rows,_=run('regional_conditions',row)
    assert (rows[0]['기간시작'],rows[0]['기간종료'],rows[0]['시간기준'])==(1991,2000,'period_mean')

def test_envelope_alias_conflicts_not_silently_resolved():
    row=budget();row['_reading']['근거문구']='서로 다른 문장'
    assert not run('financial_plan',row)[0]

def test_omitted_role_literal_only_filled_when_quoted():
    row={'지자체명':'서울시','거버넌스기구':'환경과','역할':'재활용 사업을 추진함',
         '_reading':{'레코드분류':'text_fact','근거 문구':'환경과는 재활용 사업을 추진함','문맥 구분':{}}}
    rows,audit=run('governance_feedback',row)
    assert rows[0]['값원문']==row['역할']
    assert any(e.get('reason')=='role_literal_present_in_record_evidence' for e in audit[0]['events'])
    row['역할']='사업 예산을 집행함'
    assert not run('governance_feedback',row)[0]

def test_explicit_parent_path_not_self_parent():
    row={'지자체명':'서울시','거버넌스기구':'환경분과','역할':'정책자문','값원문':'정책자문',
         '정보유형':'조직기능','_reading':{'문맥 구분':{'기관명':'환경분과','구성경로':'위원회 > 환경분과'}}}
    rows,_=run('governance_feedback',row)
    assert rows[0]['상위기관원문']=='위원회'
    assert rows[0]['구성경로원문']=='위원회 > 환경분과'
    row['_reading']['문맥 구분']['구성경로']='위원회 > 다른분과'
    assert not run('governance_feedback',row)[0]

@pytest.mark.parametrize('unit,item',[('개 과','과'),('개 팀','팀')])
def test_organization_count_unit_needs_matching_item(unit,item):
    row={'지자체명':'서울시','거버넌스기구':'환경본부','정보유형':'기관구성',
         '측정값':7,'측정단위':unit,'항목원문':item,'값원문':f'7{unit}',
         '_reading':{'문맥 구분':{'측정유형':'기관구성','구성구분':item}}}
    rows,_=run('governance_feedback',row)
    assert rows[0]['측정단위']=='개' and rows[0]['측정값']==7 and rows[0]['항목원문']==item
    row['항목원문']='다른 항목'
    assert not run('governance_feedback',row)[0]

def test_summary_label_and_scope_are_separate():
    row={'지자체명':'서울시','점검연도':2025,'정보유형':'성과집계','항목원문':'사업 수(9)',
         '측정값':9,'측정단위':'개','값원문':'9개 사업',
         '_reading':{'문맥 구분':{'측정유형':'사업 수','집계범위':'폐기물 분야 전체'}}}
    rows,_=run('monitoring_performance',row)
    assert rows[0]['항목원문']=='사업 수(9)' and rows[0]['집계범위원문']=='폐기물 분야 전체'
