from copy import deepcopy

import pytest

import config
from utils.reading_pipeline import integrate_rows, instruction
from utils.reading_context_facts import scoped_municipality
from utils.visual_reading_context import adapt_observation, adapt_observations
from utils.visual_fact_identity import fact_identity
from utils.vision_recovery_runtime import offline_guard, save_and_verify


@pytest.fixture(autouse=True)
def local_config(monkeypatch):
    for flag in ('READING_PIPELINE_ENABLED', 'VISUAL_MERGE_LABELED_ENABLED', 'VISUAL_EVIDENCE_MERGE_ENABLED'):
        monkeypatch.setattr(config, flag, True)
    for flag in ('REFERENCE_ENRICHMENT_ENABLED', 'SOURCE_VERIFICATION_ENABLED', 'CODEBOOK_SHEET_ENABLED'):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, 'VISION_RECOVERY_BINDINGS_ENABLED', False, raising=False)


def envelope(region='서울시'):
    return {'객체문맥': {'자료지역': region, '지역근거': region + ' 위원회/사업 표', '적용범위': '현재 객체의 해당 행'}}


def observation(target='other', item='탄소중립위원회', value=40, unit='명', **fields):
    return {'_recovery_id': 'R1', '페이지': 1, '대상시트': target, '항목': item,
            '값': value, '원문값': value, '단위': unit, '원문단위': unit,
            '연도': None, '값근거': 'table_cell', '그래프유형': '표', '신뢰도': 'high',
            '근거ID목록': ['ev-1'], '근거ID': 'ev-1', '원본객체ID목록': ['obj-1'],
            '반영여부': '검토', '판독필드': fields,
            '자동병합정책': 'block_unsupported_target' if target == 'other' else 'standard',
            '병합차단사유': '기타 대상 시트 자동 병합 금지; 연도 없음/비숫자' if target == 'other' else ''}


def raw(rows):
    return {'municipality_name': '알 수 없음', 'chart_observations': rows,
            'document_objects': [{'object_id': 'obj-1', 'object_type': 'image', 'page_number': 1,
                                  'metadata': {'evidence_id': 'ev-1', 'final_status': 'extracted'}}]}


def verify(rows, tmp_path):
    with offline_guard():
        return save_and_verify(raw(rows), tmp_path / 'result', False)


def test_explicit_project_contract_does_not_require_numeric_measurement():
    row = {'사업명': '창호 교체', '관리번호': 'B1-11', '_reading': envelope(), '근거ID': 'ORIGINAL'}
    before = deepcopy(row)
    rows, audit = integrate_rows('mitigation_projects', [row], '알 수 없음')
    assert rows[0]['사업명'] == '창호 교체' and rows[0]['지자체명'] == '서울시'
    assert rows[0]['근거ID'] == 'ORIGINAL' and row == before
    assert audit[0]['status'] == 'applied'
    assert not rows[0].get('측정값')


@pytest.mark.parametrize('label', ['소계', '주관부서', '사업 수(24)'])
def test_explicit_project_contract_keeps_header_guard(label):
    rows, audit = integrate_rows('mitigation_projects', [{'사업명': label, '_reading': envelope()}], '알 수 없음')
    assert not rows and 'project_identity:' in str(audit)


def test_missing_project_name_is_not_filled_with_numeric_value():
    rows, audit = integrate_rows('mitigation_projects', [{'관리번호': 'B1-1', '_reading': envelope()}], '알 수 없음')
    assert not rows and 'missing_explicit_project_name' in str(audit)


@pytest.mark.parametrize('uncertain', [False, True])
def test_region_role_link_is_checked_before_missing_scope_rejection(uncertain):
    row = {'사업명': '창호 교체', '_reading': {'메타데이터': [
        {'역할': '지역', '필드': '지자체명', '값': '서울시', '근거': '서울시 사업 표',
         '적용범위': '현재 행', '불확실성': uncertain}]}}
    rows, audit = integrate_rows('mitigation_projects', [row], '서울특별시')
    assert bool(rows) is not uncertain
    if rows:
        assert rows[0]['지자체명'] == '서울시'
    else:
        assert 'unverified_link' in str(audit)


def test_unknown_region_requires_evidence_not_filename_or_title():
    row = {'사업명': '창호 교체', '_reading': {}}
    assert not integrate_rows('mitigation_projects', [row], '서울시')[0]
    assert scoped_municipality({'_reading': {'객체문맥': {'자료지역': '서울시'}}}, '알 수 없음') == '알 수 없음'


@pytest.mark.parametrize('ctx', [{'자료구분': '전국 참고자료'}, {'자료지역': '경기도'}])
def test_conflicting_or_reference_region_is_not_inherited(ctx):
    env = envelope(); env['문맥 구분'] = ctx
    assert not integrate_rows('mitigation_projects', [{'사업명': '창호 교체', '_reading': env}], '알 수 없음')[0]


def test_count_and_function_reach_real_excel_without_recovery_flag(tmp_path):
    count = observation(지표명='위원수', 구성구분='위촉', _reading=envelope())
    function = observation(item='전체회의', value=None, unit='', 구조역할='주체', 상위항목='탄소중립위원회',
                           설명='정책 심의', _reading=envelope())
    function['_recovery_id'] = 'R2'
    function['병합차단사유'] += '; 값 없음; 판독값 전부 null'
    before = deepcopy([count, function])
    result, cells = verify([count, function], tmp_path)
    assert cells['passed'] and cells['deterministic']
    rows = result['governance_feedback']
    assert len(rows) == 2, result.get('chart_observations')
    assert {r['정보유형'] for r in rows} == {'기관구성', '조직기능'}
    assert next(r for r in rows if r['정보유형'] == '기관구성')['측정값'] == 40
    assert next(r for r in rows if r['정보유형'] == '조직기능')['역할'] == '정책 심의'
    assert [count, function] == before
    assert len(result['visual_context_audit']) == 2


@pytest.mark.parametrize('problem', ['scope', 'confidence', 'evidence', 'estimated', 'reference', 'conflict'])
def test_org_projection_never_bypasses_other_gates(tmp_path, problem):
    row = observation(지표명='위원수', 구성구분='위촉', _reading=envelope())
    if problem == 'scope': row['판독필드']['_reading'] = {}
    if problem == 'confidence': row['신뢰도'] = 'low'
    if problem == 'evidence': row['근거ID목록'] = ['wrong']; row['원본객체ID목록'] = []; row['근거ID'] = 'wrong'
    if problem == 'estimated': row['판독필드']['estimated'] = True
    if problem == 'reference': row['참고자료여부'] = True
    if problem == 'conflict': row['판독필드']['측정값'] = 41
    result, cells = verify([row], tmp_path)
    assert cells['passed'] and not result.get('governance_feedback')


def test_organization_roles_and_subgroups_have_distinct_identities():
    a, _ = adapt_observation(observation(지표명='위원수', 구성구분='위촉', _reading=envelope()))
    b, _ = adapt_observation(observation(지표명='위원수', 구성구분='당연', _reading=envelope()))
    assert fact_identity(a) != fact_identity(b)


@pytest.mark.parametrize('count', [-1, 1.5, True])
def test_organization_count_is_not_negative_fraction_or_boolean(tmp_path, count):
    row = observation(value=count, 지표명='위원수', _reading=envelope())
    result, _ = verify([row], tmp_path)
    assert not result.get('governance_feedback')


def test_multiple_explicit_org_functions_are_not_collapsed(tmp_path):
    a = observation(item='전체회의', value=None, unit='', 구조역할='주체', 상위항목='위원회',
                    설명='정책 심의', _reading=envelope())
    b = deepcopy(a); b['판독필드']['설명'] = '이행 평가'; b['_recovery_id'] = 'R2'
    result, cells = verify([a, b], tmp_path)
    assert cells['passed'] and {r['역할'] for r in result['governance_feedback']} == {'정책 심의', '이행 평가'}


def test_count_value_conflict_is_held_not_first_wins(tmp_path):
    a = observation(지표명='위원수', 구성구분='위촉', _reading=envelope())
    b = deepcopy(a); b['값'] = b['원문값'] = 41; b['_recovery_id'] = 'R2'
    result, _ = verify([a, b], tmp_path)
    assert not result.get('governance_feedback')


def test_project_observed_name_with_code_survives_without_measurement_coercion(tmp_path):
    row = observation('mitigation_projects', '창호 교체', None, '%', 관리번호='B1-11', _reading=envelope())
    result, cells = verify([row], tmp_path)
    assert cells['passed'] and result['mitigation_projects'][0]['사업명'] == '창호 교체'
    assert result['chart_observations'][0]['값'] is None


def test_other_without_organization_contract_is_not_guessed():
    row = observation(item='기타 도표', 정보유형='미분류')
    row['제목'] = '서울시 탄소중립위원회'
    adapted, audit = adapt_observation(row)
    assert adapted == row and audit is None


def test_prompts_offer_new_contract_on_project_only_calls():
    from agents.image_agent import CHART_TABLE_SYSTEM
    from utils.visual_contract import VISUAL_TARGET_SHEETS
    assert '_reading' in instruction(['mitigation_projects'])
    assert 'governance_feedback' in VISUAL_TARGET_SHEETS and '조직기능' in CHART_TABLE_SYSTEM


def table_row(table='표 7-1', region=None, **fields):
    env = {'객체문맥': {'자료지역': region, '지역근거': region + ' 위원회 표', '적용범위': table}} if region else {}
    return observation('governance_feedback', 정보유형='기관구성', 거버넌스기구='위원회',
                       항목원문='위원수', 표ID=table, _reading=env, **fields)


def test_table_region_is_scoped_and_audited_without_mutation():
    source = [table_row(region='서울시'), table_row(), table_row('표 7-2')]
    before = deepcopy(source)
    result, audit = adapt_observations(source)
    assert result[1]['판독필드']['_reading']['객체문맥']['자료지역'] == '서울시'
    assert '객체문맥' not in result[2]['판독필드']['_reading']
    event = next(e for e in audit if e['observation_index'] == 1)
    assert event['original'] == before[1]
    assert event['changes'][0]['source_observation_indices'] == [0]
    assert source == before


@pytest.mark.parametrize('problem', ['page', 'object', 'evidence', 'multi_object', 'missing_table',
    'title_only', 'table_conflict', 'reference', 'other_region', 'uncertain'])
def test_table_region_never_crosses_unproved_boundaries(problem):
    donor, receiver = table_row(region='서울시'), table_row()
    f = receiver['판독필드']
    if problem == 'page': receiver['페이지'] = 2
    if problem == 'object': receiver['원본객체ID목록'] = ['obj-2']
    if problem == 'evidence': receiver['근거ID목록'] = ['ev-2']
    if problem == 'multi_object': receiver['원본객체ID목록'] = ['obj-1', 'obj-2']
    if problem in ('missing_table', 'title_only'): f.pop('표ID')
    if problem == 'title_only': receiver['제목'] = '표 7-1 서울시 위원회'
    if problem == 'table_conflict': f['합계그룹'] = '표 7-2 / 위원회'
    if problem == 'reference': receiver['참고자료여부'] = True
    if problem == 'other_region': f['_reading']['문맥 구분'] = {'자료지역': '경기도'}
    if problem == 'uncertain': f['_reading']['불확실성'] = True
    result, _ = adapt_observations([donor, receiver])
    assert '객체문맥' not in result[1]['판독필드']['_reading']


@pytest.mark.parametrize('problem', ['low_confidence', 'unknown_confidence', 'missing_evidence', 'reference', 'scope', 'uncertain', 'contradiction'])
def test_untrusted_region_donor_cannot_fill_other_rows(problem):
    donor, receiver = table_row(region='서울시'), table_row()
    env = donor['판독필드']['_reading']
    if problem == 'low_confidence': donor['신뢰도'] = 'low'
    if problem == 'unknown_confidence': donor.pop('신뢰도')
    if problem == 'missing_evidence': env['객체문맥']['지역근거'] = ''
    if problem == 'reference': donor['참고자료여부'] = True
    if problem == 'scope': env['객체문맥']['적용범위'] = '현재 행'
    if problem == 'uncertain': env['객체문맥']['불확실성'] = True
    if problem == 'contradiction': env['문맥 구분'] = {'자료지역': '경기도'}
    result, _ = adapt_observations([donor, receiver])
    assert '객체문맥' not in result[1]['판독필드']['_reading']


def test_conflicting_table_donors_block_even_anchor_rows():
    rows = [table_row(region='서울시'), table_row(region='경기도'), table_row()]
    result, audit = adapt_observations(rows)
    assert all('explicit_table_region_conflict' in r['병합차단사유'] for r in result)
    assert all('explicit_table_region_conflict' in e['issues'] for e in audit)


def test_exact_aggregate_group_can_identify_table_but_not_arbitrary_text():
    receiver = table_row(); receiver['판독필드'].pop('표ID')
    receiver['판독필드']['합계그룹'] = '표 7-1 / 위원회 / 위원수'
    result, _ = adapt_observations([table_row(region='서울시'), receiver])
    assert result[1]['판독필드']['_reading']['객체문맥']['자료지역'] == '서울시'


def test_generated_aggregate_title_is_not_an_explicit_table_reference():
    from utils.visual_contract import normalize_visual_table_rows
    receiver = table_row(); receiver['판독필드'].pop('표ID')
    normalized = normalize_visual_table_rows([{'항목': '위원회', '값': 40, '단위': '명',
        'fields': receiver['판독필드']}], chart_type='표', title='표 7-1')[0]
    receiver['판독필드'] = normalized['fields']
    result, _ = adapt_observations([table_row(region='서울시'), receiver])
    assert '객체문맥' not in result[1]['판독필드']['_reading']


def test_live_shape_function_label_is_preserved_separately(tmp_path):
    rows = [observation('governance_feedback', item='전체회의', value=None, unit='',
                        거버넌스기구='탄소중립위원회', 정보유형='조직기능', 역할='전체회의',
                        항목원문='담당 업무(기능)', 값원문=text, _reading=envelope())
            for text in ('정책 심의', '계획 수립 심의', '점검평가')]
    before = deepcopy(rows)
    result, cells = verify(rows, tmp_path)
    assert cells['passed'] and len(result['governance_feedback']) == 3
    assert {r['역할'] for r in result['governance_feedback']} == {'정책 심의', '계획 수립 심의', '점검평가'}
    assert all('전체회의' in r['구성경로원문'] for r in result['governance_feedback'])
    assert rows == before


@pytest.mark.parametrize('problem', ['function_text', 'envelope_text', 'component_path', 'department'])
def test_real_conflicts_stay_blocked_after_alias_projection(tmp_path, problem):
    row = observation('governance_feedback', item='전체회의', value=None, unit='',
                      거버넌스기구='위원회', 정보유형='조직기능', 역할='전체회의',
                      값원문='정책 심의', _reading=envelope())
    f = row['판독필드']
    if problem == 'function_text': f['역할'] = '상이한 업무'
    if problem == 'envelope_text': f['_reading']['값원문'] = '다른 기능'
    if problem == 'component_path': f['_reading']['문맥 구분'] = {'구성경로': ['다른 회의']}
    if problem == 'department': f.update({'담당부서': '부서A', '담당 부서': '부서B'})
    result, cells = verify([row], tmp_path)
    assert cells['passed'] and not result.get('governance_feedback')


def test_department_alias_reaches_excel_and_keeps_original(tmp_path):
    row = table_row(region='서울시', **{'담당 부서': '기후환경정책과'})
    result, cells = verify([row], tmp_path)
    assert cells['passed'] and result['governance_feedback'][0]['담당부서'] == '기후환경정책과'
    assert result['visual_context_audit'][0]['original']['판독필드']['담당 부서'] == '기후환경정책과'


def test_new_prompt_separates_table_component_and_function():
    prompt = instruction(['governance_feedback'])
    assert '표ID' in prompt and '구성요소원문' in prompt and '실제 업무/기능' in prompt
