"""No model calls: TOC audit parity, conservative gating and pipeline wiring."""
from copy import deepcopy
import json

import pytest
import config
from agents.image_agent import ImageAgent
from utils.document_objects import DocumentObject, build_document_objects
from utils.pdf_reader import PageContent, PDFContent
from utils.selective_ocr import apply_triage_metadata, build_triage_plan
from utils.vision_review import promote_review_candidates
from utils.vision_toc import TocPolicy, analyze_page, policy_snapshot


def index_page(number=41, count=8, heading=True):
    blocks = ([dict(text='표 목차\niii', bbox=[0, 0, 100, 15])] if heading else [])
    for n in range(count):
        blocks.append(dict(text=f'[표 2-{n+1}] 서울시 배출량 현황\n{100+n}',
                           bbox=[10, 30+n*20, 500, 45+n*20]))
    return PageContent(number, '\n'.join(b['text'] for b in blocks), [], [],
                       text_blocks=blocks, width=600, height=900)


def decide(page, mode, monkeypatch, extra=()):
    monkeypatch.setattr(config, 'VISION_TOC_MODE', mode)
    objs = build_document_objects([page]) + list(extra)
    rows = build_triage_plan(objs, backend='vlm', confidence_threshold=0.78)
    policy = TocPolicy([page])
    policy.apply(objs, rows)
    return policy, objs, rows


@pytest.mark.parametrize('mode', ['off', 'audit', 'exclude'])
def test_modes_and_audit_evidence(monkeypatch, mode):
    page = index_page()
    original = deepcopy(page)
    policy, objects, rows = decide(page, mode, monkeypatch)
    table = [r for r in rows if r.object_type == 'table']
    assert len(table) == 8
    assert {r.action for r in table} == ({'skip_non_data'} if mode == 'exclude' else {'ocr_required'})
    assert len(policy.audit()['objects']) == (0 if mode == 'off' else 8)
    assert page == original
    if mode != 'off':
        assert table[0].toc['evidence']['printed_page_reference'] == 100
        assert table[0].toc['evidence']['physical_page_resolved'] is False
    if mode == 'exclude':
        assert all(r.attempt_count == 0 and not r.backend and r.context_only for r in table)
    apply_triage_metadata(objects, rows)
    loaded = [DocumentObject.from_dict(x.to_dict()) for x in objects]
    if mode != 'off':
        assert any(o.metadata.get('toc') for o in loaded)
    json.dumps(policy.audit(), ensure_ascii=False)


def test_dense_continuation_requires_no_page_range_or_heading():
    assert analyze_page(index_page(333, heading=False))['confirmed']
    assert not analyze_page(index_page(1, count=2, heading=False))['confirmed']


@pytest.mark.parametrize('text', ['', '목차', '보고서의 목차를 참고하세요.', '[표 1-1] 연간 예산\n2024\n2025\n565\n500',
                                  '표 목차\n[표 1-1] 배출량\n12.5 tCO2eq'])
def test_insufficient_evidence_keeps_existing_route(text, monkeypatch):
    page = PageContent(5, text, [], [])
    policy, _, rows = decide(page, 'exclude', monkeypatch)
    assert not policy.audit()['objects']
    assert not any(r.status == 'toc_excluded' for r in rows)


def test_split_reference_and_text_only_fallback():
    page = index_page(count=3)
    page.text_blocks[1]['text'] = '[표 2-1] 연간 계획\n1\n3\n5'
    page.text = '\n'.join(b['text'] for b in page.text_blocks)
    assert analyze_page(page)['entries'][0]['printed_page_reference'] == 135
    page.text_blocks = []
    assert analyze_page(page)['index_entry_count'] == 3


def test_generic_leader_index_preserves_unresolved_navigation():
    page = PageContent(100, '목차\n1. 사업 계획 .... 12\n2. 감축 현황 .... 35\n3. 재정 투자 .... 60', [], [])
    result = analyze_page(page)
    assert result['confirmed']
    assert [e['printed_page_reference'] for e in result['entries']] == [12, 35, 60]


def test_mixed_page_real_table_and_image_are_preserved(monkeypatch):
    page = index_page()
    page.tables = ['<table><tr><td>2024</td><td>565</td></tr></table>']
    page.images = [dict(width=600, height=900, base64='unused')]
    actual = DocumentObject('actual', 'table', 41, 9, number='표 2-1',
                            caption='[표 2-1] 서울시 배출량 현황', rows=[['2024', '565']],
                            bbox=(10, 300, 500, 500))
    unknown = DocumentObject('unknown', 'image', 41, 10, bbox=(0, 0, 600, 900))
    policy, _, rows = decide(page, 'exclude', monkeypatch, (actual, unknown))
    assert policy.pages[41]['role'] == 'mixed_toc'
    assert next(r for r in rows if r.object_id == 'actual').status != 'toc_excluded'
    assert next(r for r in rows if r.object_id == 'unknown').action == 'review_required'
    # Even a whole-page render must remain on a mixed/embedded-image page.
    image = dict(source_kind='page_render')
    assert policy.filter_images([(page, image)]) == [(page, image)]


def test_same_caption_outside_index_rectangle_is_not_excluded(monkeypatch):
    page = index_page()
    actual = DocumentObject('elsewhere', 'table', 41, 10, number='표 2-1',
                            caption='[표 2-1] 서울시 배출량 현황', bbox=(10, 700, 500, 720),
                            metadata={'missing_native': True})
    _, _, rows = decide(page, 'exclude', monkeypatch, (actual,))
    assert next(r for r in rows if r.object_id == 'elsewhere').action == 'ocr_required'


def test_excluded_cannot_be_promoted_back_and_keeps_native_text(monkeypatch):
    page = index_page()
    policy, objects, rows = decide(page, 'exclude', monkeypatch)
    monkeypatch.setattr(config, 'VISION_REVIEW_ENABLED', True)
    promote_review_candidates(rows, objects, [page])
    assert all(r.action == 'skip_non_data' for r in rows if r.object_type == 'table')
    assert all(r.action == 'native_keep' for r in rows if r.object_type == 'text')
    excluded = policy.objects[0]['object_id']
    assert not policy.filter_images([(page, {'source_object_ids': [excluded]})])
    assert policy.filter_images([(page, {'source_object_ids': [excluded, 'unknown']})])


@pytest.mark.parametrize('mode,expected', [('off', 1), ('audit', 1), ('exclude', 0)])
def test_legacy_full_page_render_gate(mode, expected, monkeypatch):
    page = index_page()
    policy, _, _ = decide(page, mode, monkeypatch)
    assert len(policy.filter_images([(page, dict(source_kind='page_render'))])) == expected


def test_real_agent_excludes_before_render_batch_and_model(monkeypatch):
    import agents.image_agent as module
    from utils import llm_client
    monkeypatch.setattr(config, 'VISION_TOC_MODE', 'exclude')
    monkeypatch.setattr(config, 'SELECTIVE_OCR_ENABLED', True)
    monkeypatch.setattr(config, 'OCR_BACKEND', 'vlm')
    monkeypatch.setattr(config, 'VISION_REVIEW_ENABLED', True)
    page = index_page()
    monkeypatch.setattr(config, 'PHYSICAL_OBJECT_MERGE_ENABLED', False)
    document = PDFContent(1, [page], page.text)
    renders = []
    def render(document, objects, required, **kwargs):
        renders.extend(required)
        assert not required
        return []
    monkeypatch.setattr(module, 'render_ocr_candidate_images', render)
    def forbidden(*args, **kwargs):
        pytest.fail('TOC must not reach model calls or retries')
    for name in ('call_text_json', 'call_vision_json', 'call_vision_batch_json'):
        monkeypatch.setattr(llm_client, name, forbidden)
    agent = ImageAgent()
    result = agent.extract([page], {}, '서울특별시', document=document)
    assert not renders
    assert agent.preflight['input_count'] == 0
    assert agent.preflight['toc_audit']['summary']['toc_excluded_ocr_objects'] == 8
    assert sum(r['status'] == 'toc_excluded' for r in result['object_triage']) == 8


def test_invalid_mode_fails_closed_to_configuration_error(monkeypatch):
    monkeypatch.setattr(config, 'VISION_TOC_MODE', 'typo')
    with pytest.raises(ValueError, match='VISION_TOC_MODE'):
        policy_snapshot()


def test_uncertain_member_of_merged_object_blocks_exclusion(monkeypatch):
    page = index_page()
    monkeypatch.setattr(config, 'VISION_TOC_MODE', 'exclude')
    objects = build_document_objects([page])
    rows = build_triage_plan(objects, backend='vlm', confidence_threshold=0.78)
    row = next(r for r in rows if r.object_type == 'table')
    row.alias_object_ids.append('unseen_body_chart')
    policy = TocPolicy([page])
    policy.apply(objects, rows)
    assert row.action == 'ocr_required'
    assert len(policy.objects) == 7


def test_body_caption_prevents_full_page_exclusion(monkeypatch):
    page = index_page()
    block = dict(text='[그림 9-1] 본문의 실제 그래프', bbox=[0, 700, 500, 720])
    page.text_blocks.append(block)
    page.text += '\n'+block['text']
    policy, _, _ = decide(page, 'exclude', monkeypatch)
    assert policy.pages[41]['role'] == 'mixed_toc'
    assert policy.filter_images([(page, dict(source_kind='page_render'))])


def test_audit_has_exact_same_preflight_inputs_as_off(monkeypatch):
    import agents.image_agent as module
    monkeypatch.setattr(config, 'SELECTIVE_OCR_ENABLED', True)
    monkeypatch.setattr(config, 'OCR_BACKEND', 'vlm')
    monkeypatch.setattr(config, 'PHYSICAL_OBJECT_MERGE_ENABLED', False)
    monkeypatch.setattr(config, 'IMAGE_TRIAGE_ENABLED', False)
    page = index_page()
    doc = PDFContent(1, [page], page.text)
    def render(document, objects, rows, **kwargs):
        return [(page, dict(base64=r.object_id, width=600, height=900,
                            source_kind='ocr_candidate', source_object_ids=[r.object_id],
                            source_evidence_ids=[r.evidence_id], ocr_required=True)) for r in rows]
    monkeypatch.setattr(module, 'render_ocr_candidate_images', render)
    plans = {}
    for mode in ('off', 'audit', 'exclude'):
        monkeypatch.setattr(config, 'VISION_TOC_MODE', mode)
        plans[mode] = ImageAgent().extract([page], {}, '서울', document=doc, preflight_only=True)['vision_preflight']
    assert plans['off']['inputs'] == plans['audit']['inputs']
    assert plans['off']['candidate_sha256'] == plans['audit']['candidate_sha256']
    assert plans['exclude']['input_count'] == 0


def test_mode_changes_run_fingerprint(monkeypatch, tmp_path):
    from utils.run_state import RunState
    source = tmp_path/'source.pdf'
    source.write_bytes(b'test')
    monkeypatch.setattr(config, 'RUN_STATE_DIR', str(tmp_path/'states'))
    runs = []
    for mode in ('off', 'audit', 'exclude'):
        monkeypatch.setattr(config, 'VISION_TOC_MODE', mode)
        state = RunState.create(input_path=source, guideline_path=None, extraction_prompts={},
                                execution_info={}, output_path=tmp_path/'out.xlsx')
        runs.append(state.run_id)
        assert state.manifest['config']['vision_toc_policy']['mode'] == mode
    assert len(set(runs)) == 3


def test_preflight_cli_mode_is_scoped_and_writes_audit(monkeypatch, tmp_path):
    from scripts.preflight_vision import main
    from utils import pdf_reader
    import agents.image_agent as module
    monkeypatch.setattr(config, 'VISION_TOC_MODE', 'audit')
    monkeypatch.setattr(config, 'SELECTIVE_OCR_ENABLED', True)
    monkeypatch.setattr(config, 'OCR_BACKEND', 'vlm')
    monkeypatch.setattr(config, 'PHYSICAL_OBJECT_MERGE_ENABLED', False)
    page = index_page()
    monkeypatch.setattr(pdf_reader, 'extract_pdf', lambda *a, **k: PDFContent(1, [page], page.text))
    monkeypatch.setattr(module, 'render_ocr_candidate_images', lambda *a, **k: [])
    source = tmp_path/'source.pdf'
    source.write_bytes(b'fixture')
    output = tmp_path/'preflight.json'
    assert main([str(source), '--output', str(output), '--toc-mode', 'exclude']) == 2
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result['toc_audit']['summary']['toc_excluded_ocr_objects'] == 8
    assert result['model_calls'] == 0
    assert config.VISION_TOC_MODE == 'audit'
    with pytest.raises(SystemExit):
        main([str(source), '--output', str(output)])


def test_toc_exclusion_is_not_counted_as_data_extraction(monkeypatch):
    from utils.source_verifier import build_source_object_inventory
    page = index_page()
    policy, objects, rows = decide(page, 'exclude', monkeypatch)
    apply_triage_metadata(objects, rows)
    report = build_source_object_inventory({}, PDFContent(1, [page], page.text), document_objects=objects)
    # Source inventory already omits navigation/context proxies before its
    # denominator. The separate TOC audit retains their explicit exclusion.
    assert policy.metrics()['toc_excluded_objects'] == 8
    assert report.extraction_denominator_objects == 0
    assert report.coverage_ratio is None
