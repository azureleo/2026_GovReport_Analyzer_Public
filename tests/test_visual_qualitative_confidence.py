from copy import deepcopy
import json

import pytest
import config
from agents.image_agent import ImageAgent, _has_explicit_function_text, _is_reference_chart
from agents.organizer_agent import _is_reference_visual_observation
from utils.visual_reference import reference_keyword_hits, observation_reference_text
from utils.vision_recovery_runtime import offline_guard, observations_from_response, save_and_verify


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for flag in ('READING_PIPELINE_ENABLED', 'VISUAL_MERGE_LABELED_ENABLED', 'VISUAL_EVIDENCE_MERGE_ENABLED'):
        monkeypatch.setattr(config, flag, True)
    for flag in ('REFERENCE_ENRICHMENT_ENABLED', 'SOURCE_VERIFICATION_ENABLED', 'CODEBOOK_SHEET_ENABLED'):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, 'IMAGE_CHART_MERGE_MIN_CONFIDENCE', 'medium')


def fixture():
    row = {'항목': '전체회의', '값': None, '단위': None, '연도': None,
           'fields': {'정보유형': '조직기능', '거버넌스기구': '탄소중립위원회',
                      '역할': '전체회의', '값원문': '탄소중립 정책 심의·의결',
                      '값근거': '표셀', '_reading': {'레코드분류': 'text_fact',
                      '객체문맥': {'자료지역': '서울시', '지역근거': '서울시 위원회', '적용범위': '표 7-2'}}}}
    task = {'page': 1, 'object_ids': ['obj-1'], 'evidence_ids': ['ev-1'], 'bbox': [0,0,100,100]}
    parsed = {'type': 'chart_table', 'target_sheet': 'governance_feedback', 'chart_type': '표',
              'title': '조직표', 'confidence': 'high', 'page_number': 1,
              'source_object_ids': task['object_ids'], 'source_evidence_ids': task['evidence_ids'],
              'source_bbox': task['bbox'], 'table': [row]}
    return row, parsed, task


@pytest.mark.parametrize('confidence,expected', [('high','high'),('medium','medium'),('low','low')])
def test_function_keeps_original_confidence_without_numeric_penalty(confidence, expected):
    row, parsed, _ = fixture(); parsed['confidence'] = confidence
    accepted, final, issues = ImageAgent()._chart_merge_decision(parsed, row, 'governance_feedback')
    assert final == expected and accepted == (confidence != 'low')
    assert '값 없음' not in issues and '연도 없음/비숫자' not in issues
    if confidence == 'low': assert '신뢰도 기준 미달' in issues


@pytest.mark.parametrize('text', [None, '', ' ', '–', '미표기', '판독 불가', '해당없음', '전체회의'])
def test_missing_or_label_only_function_is_not_a_value(text):
    row, parsed, _ = fixture(); row['fields']['값원문'] = text
    assert not _has_explicit_function_text(row, 'governance_feedback')
    assert not ImageAgent()._chart_merge_decision(parsed, row, 'governance_feedback')[0]


@pytest.mark.parametrize('problem', ['wrong_target','missing_org','mixed_number','mixed_field',
    'invalid_envelope','measurement_kind','uncertain_status','conflicting_text','disabled'])
def test_invalid_function_contract_keeps_numeric_failure(problem, monkeypatch):
    row, _, _ = fixture(); fields = row['fields']; target = 'governance_feedback'
    if problem == 'wrong_target': target = 'emissions_forecast'
    if problem == 'missing_org': fields.pop('거버넌스기구')
    if problem == 'mixed_number': row['값'] = 1
    if problem == 'mixed_field': fields['측정값'] = 1
    if problem == 'invalid_envelope': fields['_reading'] = []
    if problem == 'measurement_kind': fields['_reading']['레코드분류'] = 'measurement'
    if problem == 'uncertain_status': fields['_reading']['값상태'] = '미확인'
    if problem == 'conflicting_text': fields['_reading']['값원문'] = '다른 업무'
    if problem == 'disabled': monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', False)
    assert not _has_explicit_function_text(row, target)


@pytest.mark.parametrize('problem', ['model_low','row_low','estimated','derived','reference','year'])
def test_function_does_not_bypass_protective_gates(problem):
    row, parsed, _ = fixture()
    if problem == 'model_low': parsed['confidence'] = 'low'
    if problem == 'row_low': row['confidence'] = 'low'
    if problem == 'estimated': row['fields']['estimated'] = True
    if problem == 'derived': row['fields']['값근거'] = 'derived'
    if problem == 'reference': parsed['reference_context'] = True
    if problem == 'year': row['연도'] = 1800
    accepted, confidence, reasons = ImageAgent()._chart_merge_decision(parsed, row, 'governance_feedback')
    assert not accepted and confidence == 'low' and reasons


@pytest.mark.parametrize('has_region', [True, False])
def test_real_image_organizer_excel_path_for_null_function(tmp_path, has_region):
    row, parsed, task = fixture()
    parsed['unit'] = '명, 개 분과'  # Page-wide count units do not describe a function.
    if not has_region: row['fields']['_reading'].pop('객체문맥')
    before = deepcopy(parsed)
    with offline_guard():
        observations = observations_from_response(parsed, task)
        candidate = observations[0]
        assert candidate['신뢰도'] == 'high'
        assert candidate['단위'] == '' and candidate['원문단위'] == ''
        assert '값 없음' not in candidate['병합차단사유']
        assert '판독값 전부 null' not in candidate['병합차단사유']
        result, cells = save_and_verify({'municipality_name': '알 수 없음',
            'chart_observations': observations, 'document_objects': [{
                'object_id': 'obj-1', 'object_type': 'image', 'page_number': 1,
                'metadata': {'evidence_id': 'ev-1', 'final_status': 'extracted'}}]}, tmp_path/'saved', False)
    assert cells['passed'] and cells['deterministic'] and parsed == before
    orgs = result['governance_feedback']
    assert len(orgs) == int(has_region)
    if orgs:
        assert orgs[0]['역할'] == row['fields']['값원문']
        assert orgs[0].get('측정값') is None
        assert orgs[0].get('측정단위') in (None, '')
        assert '전체회의' in orgs[0]['구성경로원문']


@pytest.mark.parametrize('text', ['unknown_unit', 'unit', 'community', 'unknown', 'EURO', 'COPY', 'unit UNknown'])
def test_acronyms_do_not_match_internal_or_longer_words(text):
    assert not reference_keyword_hits(text, ['UN','EU','COP'])


@pytest.mark.parametrize('text,word', [('UN 자료','UN'),('UN의 통계','UN'),('un 자료','UN'),
    ('EU(유럽연합)','EU'),('COP28 자료','COP'),('IPCC의 보고서','IPCC'),('OECD 통계','OECD'),('런던 사례','런던')])
def test_real_reference_names_still_match(text, word):
    assert word in reference_keyword_hits(text, config.IMAGE_CHART_REFERENCE_KEYWORDS)
    assert _is_reference_chart({'title': text})
    assert _is_reference_visual_observation({'제목': text, '참고자료여부': False}, {}, 'governance_feedback')


def test_json_state_and_keys_are_excluded_but_source_text_is_retained():
    fields = {'단위정규화상태': 'unknown_unit', 'UN': '내부 키', '_reading': {'값상태': 'UN'}, '설명': '분과 구성'}
    observation = {'제목': '위원회 현황', '판독필드': fields, '근거': json.dumps(fields,ensure_ascii=False),
                   '참고자료여부': False}
    assert 'unknown_unit' not in observation_reference_text(observation)
    assert not _is_reference_visual_observation(observation, fields, 'governance_feedback')
    fields['_reading']['근거 문구'] = 'UN의 자료'
    assert _is_reference_visual_observation(observation, fields, 'governance_feedback')


@pytest.mark.parametrize('source', ['UN의 보고서', json.dumps({'출처':'UN 자료'}),
    json.dumps({'_reading':{'객체문맥':{'지역근거':'UN 자료'}}}) + ' | unknown_unit'])
def test_legacy_and_nested_source_reference_evidence_is_not_lost(source):
    assert _is_reference_visual_observation({'근거': source}, {}, 'governance_feedback')


@pytest.mark.parametrize('extra', [{'참고자료여부':True}, {'자동병합정책':'block_auto_merge'}])
def test_explicit_reference_block_remains(extra):
    assert _is_reference_visual_observation({'근거':'unknown_unit', **extra}, {}, 'governance_feedback')
