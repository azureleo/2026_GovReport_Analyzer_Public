"""Production integration tests. Model responses are fixed; no network calls."""
from copy import deepcopy
import json

import pytest

import config
from agents.organizer_agent import OrganizerAgent
from utils.reading_pipeline import integrate_rows, instruction


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', True)
    for name in ('REFERENCE_ENRICHMENT_ENABLED', 'CODEBOOK_SHEET_ENABLED',
                 'GAP_FILL_ENABLED', 'HYBRID_REVIEW_ENABLED', 'RUN_STATE_ENABLED',
                 'SOURCE_VERIFICATION_ENABLED', 'SOURCE_OBJECT_INVENTORY_ENABLED',
                 'VISUAL_MERGE_LABELED_ENABLED', 'SHEET_CLOSED_LOOP_ENABLED'):
        monkeypatch.setattr(config, name, False)


def budget():
    return {'지자체명': '서울시', '관리번호': 'F1-1', '부문': '탄소흡수', '계획구분': '온실가스감축대책',
            '연도': 2024, '예산액': 36819, '예산단위': '백만 원',
            '근거ID': 'ORIGINAL', '출처페이지': 475,
            '_reading': {'근거 문구': '재정투자 계획 예산 F1-1 2024 36,819',
                         '문맥 구분': {'현황전망목표구분': '계획', '값의미': '재정투자 계획 예산'}}}


def prepare(row, key='financial_plan', region='서울시'):
    return integrate_rows(key, [row], region)


def test_finance_rules_are_reused_without_changing_amount_or_provenance():
    row = budget(); before = deepcopy(row)
    rows, audit = prepare(row)
    assert len(rows) == 1
    out = rows[0]
    assert (out['예산액'], out['예산단위'], out['계획구분']) == (36819, '백만원', '온실가스감축대책')
    assert out['재원표기상태'] == '미표기' and not out.get('재원구분')
    assert (out['근거ID'], out['출처페이지']) == ('ORIGINAL', 475)
    assert row == before and audit[0]['status'] == 'applied'
    assert prepare(row) == (rows, audit)


def test_total_is_not_an_annual_amount():
    row = budget(); row['연도'] = None; row['예산액'] = 295274
    row['_reading']['기간원문'] = '2024~2033'
    row['_reading']['문맥 구분']['시간성격'] = '2024~2033 합계'
    out = prepare(row)[0][0]
    assert (out['기간시작'], out['기간종료'], out['시간기준'], out['연도']) == (2024, 2033, 'period_total', None)


@pytest.mark.parametrize('field,value', [('계획구분', '집행실적'), ('지자체명', '경기도')])
def test_conflicts_are_held_with_original_row(field, value):
    row = budget(); row[field] = value
    rows, audit = prepare(row)
    assert rows == [] and audit[0]['status'] == 'held'
    assert audit[0]['original'] == row


def test_no_document_region_fallback_but_object_evidence_is_supported():
    row = budget(); row.pop('지자체명')
    assert not prepare(row)[0]
    row['_reading']['객체문맥'] = {'자료지역': '서울특별시', '지역근거': '서울특별시 투자계획', '적용범위': '표 8-6 전체'}
    assert prepare(row)[0][0]['지자체명'] == '서울특별시'
    row['_reading']['문맥 구분']['자료구분'] = '전국 참고자료'
    assert not prepare(row)[0]


def test_conflicting_object_and_record_region_is_held():
    row = budget()
    row['_reading']['객체문맥'] = {'자료지역': '경기도', '지역근거': '경기도 투자계획', '적용범위': '현재 표 전체'}
    rows, audit = prepare(row)
    assert not rows and audit[0]['issues'] == ['record_object_region_conflict']


@pytest.mark.parametrize('region', ['경기도', '강원특별자치도'])
def test_region_contract_is_not_hardcoded_to_seoul(region):
    row = budget(); row['지자체명'] = region
    assert prepare(row, region=region)[0][0]['지자체명'] == region


def test_explicit_metadata_role_fills_year_but_conflict_is_held():
    row = budget(); row.pop('연도')
    row['_reading']['메타데이터'] = [{'역할': '연도', '필드': '연도', '값': 2024,
                                         '근거': '2024년', '적용범위': '현재 행의 열머리글'}]
    assert prepare(row)[0][0]['연도'] == 2024
    row['연도'] = 2025
    assert not prepare(row)[0]


@pytest.mark.parametrize('bad', ['문자열', [], {'문맥 구분': []}, {'메타데이터': '잘못된 연결'}])
def test_malformed_context_is_reported_not_crashed(bad):
    row = budget(); row['_reading'] = bad
    rows, audit = prepare(row)
    assert not rows and audit[0]['issues']


def test_legacy_rows_are_not_forced_through_stricter_experimental_requirements():
    row = {'사업명': '기존 사업', '예산액': 0, '예산단위': '억 원'}
    out = prepare(row)[0][0]
    assert out['예산액'] == 0 and out['예산단위'] == '억원'
    assert '계획구분' not in out and '지자체명' not in out
    assert out['재원표기상태'] == '미표기'


def test_co2_is_not_relabelled_as_co2_equivalent():
    row = {'사업명': '기존', '예상감축량': 1, '단위': 'tCO2'}
    assert prepare(row, 'quantitative_reductions')[0][0]['단위'] == 'tCO2'


def test_bad_native_total_is_held_not_assigned_a_year():
    row = {'사업명': '사업', '예산액': 10, '시간기준': 'period_total', '연도': 2033,
           '기간시작': 2024, '기간종료': 2033}
    assert not prepare(row)[0]


def test_typed_summary_survives_real_organizer():
    row = {'지자체명': '서울시', '점검연도': 2023, '정보유형': '성과집계',
           '항목원문': '60% 미만', '측정값': 0, '값원문': '0', '_reading': {}}
    out = OrganizerAgent().organize({'municipality_name': '서울시', 'monitoring_performance': [row]})
    saved = out['monitoring_performance'][0]
    assert saved['측정값'] == 0 and not saved.get('사업명')


def test_factor_and_qualitative_budget_are_separate_from_measurements():
    factor = {'지자체명': '서울시', '사업명': '창호교체', '정보유형': '감축원단위',
              '감축원단위값': .00648, '감축원단위단위': 'tCO2eq/㎡', '감축원단위명': '창호 교체', '_reading': {}}
    out = prepare(factor, 'quantitative_reductions')[0][0]
    assert out['감축원단위값'] == .00648 and out.get('예상감축량') is None
    text = {'지자체명': '서울시', '관리번호': 'F1-2', '집계대상원문': 'F1-2',
            '정보유형': '예산상태', '값원문': '비예산', '_reading': {}}
    out = prepare(text)[0][0]
    assert out['값원문'] == '비예산' and out.get('예산액') is None


def test_explicit_target_cannot_be_routed_to_wrong_sheet():
    row = budget(); row['_reading']['문맥 구분']['대상시트'] = 'regional_conditions'
    assert prepare(row)[1][0]['issues'] == ['explicit_sheet_conflict']


def test_switch_disables_bridge_without_changing_rows(monkeypatch):
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', False)
    row = budget(); rows, audit = prepare(row)
    assert rows == [row] and not audit


def test_text_prompt_and_checkpoint_include_contract(monkeypatch):
    from agents.extractor_agent import ExtractorAgent, _attach_row_context
    from utils import llm_client
    captured = []
    def response(prompt, **kwargs):
        captured.append(prompt)
        row = budget(); row.pop('지자체명')
        return {'financial_plan': [row]}, True
    monkeypatch.setattr(llm_client, 'call_text_json', response)
    agent = ExtractorAgent()
    result = agent._extract_sheet('financial_plan', '=== 페이지 475 ===\n재정투자 계획', '서울시')
    assert '_reading' in captured[0] and not result[0].get('지자체명')
    task = {'sheet_key': 'financial_plan'}
    on = agent._prompt_contract_fingerprint(task, 'sheet')
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', False)
    assert on != agent._prompt_contract_fingerprint(task, 'sheet')
    assert '_reading' not in instruction(['document_meta'])


@pytest.mark.parametrize('entrypoint', ['supervisor', 'main'])
def test_supervisor_writes_excel_and_audit_without_model_calls(monkeypatch, tmp_path, entrypoint):
    from agents import supervisor as module
    from utils.pdf_reader import PDFContent, PageContent
    from utils import llm_client
    from openpyxl import load_workbook
    pdf = PDFContent(1, [PageContent(1, '서울시 투자계획 ' * 100, [], [])], '서울시 투자계획 ' * 100)
    monkeypatch.setattr(module, 'extract_pdf', lambda *a, **k: pdf)
    monkeypatch.setattr(module.GuidelineAgent, 'get_all_prompts', lambda self: {})
    monkeypatch.setattr(module.GuidelineAgent, 'report', lambda self: '고정 입력')
    monkeypatch.setattr(module.ExtractorAgent, 'extract', lambda *a, **k: {
        'municipality_name': '서울시', 'financial_plan': [budget()],
        'monitoring_performance': [{'지자체명': '서울시', '점검연도': 2023, '정보유형': '성과집계',
                                   '항목원문': '60% 미만', '측정값': 0, '값원문': '0', '_reading': {}}],
        'governance_feedback': [{'지자체명': '서울시', '거버넌스기구': '위원회', '정보유형': '기관구성',
                                '항목원문': '위원', '측정값': 20, '측정단위': '명', '_reading': {}}],
        'quantitative_reductions': [{'지자체명': '서울시', '사업명': '창호교체', '정보유형': '감축원단위',
                                    '감축원단위값': .00648, '감축원단위단위': 'tCO2eq/㎡',
                                    '감축원단위명': '창호 교체', '_reading': {}}]})
    monkeypatch.setattr(module.Supervisor, '_llm_quality_review', lambda *a: {'quality_level': '검증용'})
    def forbidden(*a, **k):
        pytest.fail('Unexpected live model call')
    monkeypatch.setattr(llm_client, 'call_text', forbidden)
    monkeypatch.setattr(llm_client, 'call_text_json', forbidden)
    monkeypatch.setattr(llm_client, 'call_vision_json', forbidden)
    path = tmp_path / 'out.xlsx'
    if entrypoint == 'main':
        import sys
        import main
        source = tmp_path / 'fixture.pdf'
        source.touch()  # The PDF reader is mocked; no source PDF is read.
        monkeypatch.setattr(config, 'LLM_PROVIDER', 'codex')
        monkeypatch.setattr(config, 'SELECTIVE_OCR_ENABLED', False)
        monkeypatch.setattr(main, 'check_dependencies', lambda: None)
        monkeypatch.setattr(main, '_configure_stdio_errors', lambda: None)
        monkeypatch.setattr(sys, 'argv', ['main.py', str(source), '--output', str(path), '--no-images', '--retries', '1'])
        # Saving succeeds, but this deliberately incomplete offline sample needs review.
        assert main.main() == 3
    else:
        module.Supervisor().run(input_path=tmp_path / 'fixture.pdf', output_path=path,
                                max_pipeline_retries=1, include_images=False)
    wb = load_workbook(path, read_only=True)
    values = list(wb['11_재정투자계획'].values)
    row = dict(zip(values[0], values[1]))
    assert row['예산액'] == 36819 and row['계획구분'] == '온실가스감축대책'
    for sheet, field, expected in [('14_점검실적', '측정값', 0), ('13_이행관리환류', '측정값', 20),
                                   ('10_정량감축량', '감축원단위값', .00648)]:
        cells = list(wb[sheet].values)
        saved = dict(zip(cells[0], cells[1]))
        assert saved[field] == expected
    wb.close()
    outcome = json.loads(path.with_name('out_run_outcome.json').read_text(encoding='utf8'))
    assert outcome['file_saved'] and outcome['status'] == 'needs_review'
    audit = json.loads(path.with_name('out_reading_pipeline.json').read_text(encoding='utf8'))
    assert audit['enabled'] and audit['applied_rows'] >= 1 and audit['held_rows'] == 0


def test_closed_loop_does_not_reinterpret_bound_envelope():
    row = budget()
    agent = OrganizerAgent()
    first = agent.organize_sheet('financial_plan', [row], '서울시')
    assert '_reading' not in first[0]
    cleaned = agent.organize({'municipality_name': '서울시', 'financial_plan': first,
                             'reading_pipeline_audit': deepcopy(agent.reading_pipeline_audit)})
    assert cleaned['financial_plan'][0]['예산액'] == 36819
    assert len(cleaned['reading_pipeline_audit']) == 1
    assert cleaned['reading_pipeline_audit'][0]['original']['_reading'] == row['_reading']


def test_extractor_preserves_context_conflicts_until_binding():
    from agents.extractor_agent import ExtractorAgent
    first = budget(); second = budget()
    second['_reading']['문맥 구분']['자료구분'] = '참고자료'
    pending = ExtractorAgent()._merge_rows('financial_plan', [first], [second, first])
    assert len(pending) == 2
    rows, audit = integrate_rows('financial_plan', pending, '서울시')
    assert len(rows) == 1 and sum(r['status'] == 'held' for r in audit) == 1


def test_conflicting_explicit_amounts_are_held_not_silently_deduplicated():
    a = budget(); b = budget(); b['예산액'] = 36999
    rows, audit = integrate_rows('financial_plan', [a, b], '서울시')
    assert not rows and len(audit) == 2
    assert all(r['issues'] == ['conflicting_duplicate_business_identity'] for r in audit)


@pytest.mark.parametrize('period_total', [False, True])
@pytest.mark.parametrize('blocker', ['', 'missing_evidence', 'low_confidence', 'reference', 'estimated'])
def test_vision_budget_reaches_organizer_without_bypassing_safety_gates(monkeypatch, period_total, blocker):
    from agents.image_agent import ImageAgent
    from utils.evidence_merge import build_evidence_catalog
    monkeypatch.setattr(config, 'VISUAL_MERGE_LABELED_ENABLED', True)
    monkeypatch.setattr(config, 'VISUAL_EVIDENCE_MERGE_ENABLED', True)
    row = budget()
    if period_total:
        row['연도'] = None
        row['_reading']['기간원문'] = '2024~2033'
        row['_reading']['문맥 구분']['시간성격'] = '2024~2033 합계'
    if blocker == 'reference':
        row['_reading']['문맥 구분']['자료구분'] = '전국 참고자료'
    if blocker == 'estimated':
        row['estimated'] = True
    objects = [{'object_id': 'o-1', 'object_type': 'table', 'page_number': 475,
                'metadata': {'evidence_id': 'ev-1', 'final_status': 'extracted'}}]
    analysis = {'type': 'chart_table', 'target_sheet': 'financial_plan', 'chart_type': '표',
                'title': '재정투자 계획', 'unit': '백만 원', 'page_number': 475, 'municipality': '서울시',
                'confidence': 'low' if blocker == 'low_confidence' else 'high',
                'source_evidence_ids': [] if blocker == 'missing_evidence' else ['ev-1'],
                'table': [{'항목': 'F1-1', '연도': row['연도'], '값': row['예산액'], '단위': row['예산단위'],
                           'fields': row}]}
    raw = ImageAgent()._merge_image_results(
        {'municipality_name': '서울시', 'document_objects': objects, 'object_triage': []},
        [analysis], '서울시', evidence_catalog=build_evidence_catalog(objects))
    assert not raw['financial_plan']
    result = OrganizerAgent().organize(raw)
    if blocker:
        assert not result['financial_plan']
        assert result['chart_observations'][0]['병합차단사유']
    else:
        out = result['financial_plan'][0]
        assert out['예산액'] == 36819 and out['예산단위'] == '백만원'
        assert out['계획구분'] == '온실가스감축대책' and out['재원표기상태'] == '미표기'
        assert out['근거ID'] == 'ev-1' and '_reading' not in out
        if period_total:
            assert out['연도'] is None and out['시간기준'] == 'period_total'
            assert (out['기간시작'], out['기간종료']) == (2024, 2033)


def test_vision_prompt_and_checkpoint_include_contract(monkeypatch):
    from agents.image_agent import ImageAgent
    from utils import llm_client
    prompts = []
    def response(image, prompt, **kwargs):
        prompts.append(prompt)
        return {'type': 'chart_table', 'target_sheet': 'financial_plan', 'chart_type': '표',
                'table': [{'연도': 2024, '값': 1, 'fields': {'_reading': {}}}]}, True
    monkeypatch.setattr(llm_client, 'call_vision_json', response)
    monkeypatch.setattr(ImageAgent, '_revalidate_negative_result', lambda self, image, page, municipality, result: result)
    ImageAgent()._chart_to_table({'base64': 'fixed-mock-image'}, 475, '서울시')
    assert len(prompts) == 1 and '_reading' in prompts[0]
    on = ImageAgent._vision_descriptor([])
    monkeypatch.setattr(config, 'READING_PIPELINE_ENABLED', False)
    assert on != ImageAgent._vision_descriptor([])
