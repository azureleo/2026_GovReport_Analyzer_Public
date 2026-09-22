import unittest
from copy import deepcopy
from utils.reading_financial_period import apply_contract
from utils.reading_compatibility import convert
from tests import test_reading_semantic_binding as base


class FinancialPeriodTests(unittest.TestCase):
    run_b = base.SemanticBindingTests.run_b
    def setUp(self):
        base.SemanticBindingTests.setUp(self)
        self.schema = {'EXTRACTION_SHEETS':['financial_plan'], 'SHEET_KEY_TO_NAME':{'financial_plan':'11'},
                       'EXCEL_HEADERS':{'11':['지자체명','계획구분','부문','사업명','재원구분','연도','예산액','예산단위']}}
        self.business = {'지자체명':'서울시','계획구분':'온실가스감축대책','부문':'폐기물','연도':2024,'예산액':10,'예산단위':'백만원'}
        self.raw['records'][0].update({'단위원문':'백만원','연도':2024,'행머리글 경로':['C1','C1-1'], '항목원문':'C1-1',
            '문맥 구분':{'자료지역':'서울시','대상시트':'financial_plan','업무필드':self.business}})

    def test_funding_missing(self):
        before=deepcopy(self.raw)
        result=self.run_b()['accepted_candidates'][0]['values']
        self.assertEqual(result['재원표기상태'],'미표기')
        self.assertIsNone(result.get('재원구분'))
        self.assertEqual(result['관리번호'],'C1-1')
        self.assertEqual(self.raw,before)

    def test_total_and_no_fake_year(self):
        self.business.pop('연도')
        self.raw['records'][0].update({'연도':None,'기간원문':'2024-2033'})
        self.raw['records'][0]['문맥 구분']['시간성격']='2024~2033 합계'
        result=self.run_b()['accepted_candidates'][0]['values']
        self.assertEqual((result['기간시작'],result['기간종료']),(2024,2033))
        self.assertIsNone(result.get('연도'))
        self.assertEqual(result['시간기준'],'period_total')
        self.business['연도']=2033
        self.assertFalse(self.run_b()['accepted_candidates'])

    def test_no_period_inference(self):
        self.business.pop('연도')
        self.raw['records'][0]['연도']=None
        self.assertFalse(self.run_b()['accepted_candidates'])
        self.raw['records'][0]['문맥 구분']['시간성격']='2024~2033 합계'
        self.assertFalse(self.run_b()['accepted_candidates'])

    def test_blank_not_zero(self):
        self.raw['records'][0].update({'숫자값':None,'값상태':'빈칸'})
        self.business['예산액']=None
        self.assertFalse(self.run_b()['accepted_candidates'])
        self.raw['records'][0].update({'숫자값':0,'값상태':'명시값'})
        self.business['예산액']=0
        self.assertEqual(self.run_b()['accepted_candidates'][0]['values']['예산액'],0)

    def test_units_same_scale_only(self):
        self.raw['records'][0]['단위원문']='백만 원'
        self.business['예산단위']='백만 원'
        before=deepcopy(self.raw)
        out,report=convert(self.raw,self.meta,self.schema)
        self.assertEqual(out['records'][0]['단위원문'],'백만원')
        self.assertEqual(out['records'][0]['숫자값'],10)
        self.assertEqual(self.raw,before)

    def test_period_conflicts(self):
        record={'기간원문':'2024-2033','문맥 구분':{'시간성격':'2025~2033 합계'}}
        values={'사업명':'사업','시간기준':'합계','기간시작':2020,'기간종료':2033}
        _,issues,_=apply_contract('quantitative_reductions',record,values)
        self.assertIn('field_conflict:기간시작',issues)
        self.assertIn('field_conflict:기간',issues)


    def test_distinct_budget_codes_not_merged(self):
        other=deepcopy(self.raw['records'][0])
        other.update({'레코드ID':'R2','항목원문':'C1-2','행머리글 경로':['C1','C1-2']})
        self.raw['records'].append(other)
        from utils.reading_semantic_binding import bind_candidates
        result=bind_candidates(self.raw,self.meta,self.schema,self.rules,['R','R2'])
        self.assertEqual(len(result['accepted_candidates']),2)

    def test_distinct_total_periods_not_merged(self):
        first=self.raw['records'][0]
        first.update({'연도':None,'기간원문':'2024-2033'})
        self.business.pop('연도')
        first['문맥 구분']['시간성격']='2024~2033 합계'
        other=deepcopy(first)
        other.update({'레코드ID':'R2','기간원문':'2034-2043'})
        other['문맥 구분']['시간성격']='2034~2043 합계'
        self.raw['records'].append(other)
        from utils.reading_semantic_binding import bind_candidates
        result=bind_candidates(self.raw,self.meta,self.schema,self.rules,['R','R2'])
        self.assertEqual(len(result['accepted_candidates']),2)

    def plan_context(self):
        record = self.raw['records'][0]
        record['문맥 구분'].update({'현황전망목표구분':'계획', '값의미':'재정투자 계획 예산'})
        record['근거 문구'] = '[표 8-6] 탄소흡수 부문 연차별 재정투자 계획 / C1-1 / 2024 / 10'
        return record

    def test_explicit_plan_context_preserves_policy_with_trace_without_mutating_input(self):
        record = self.plan_context()
        before = deepcopy(self.raw)
        result = self.run_b()
        candidate = result['accepted_candidates'][0]
        self.assertEqual(candidate['values']['계획구분'], '온실가스감축대책')
        self.assertEqual(candidate['field_sources']['계획구분'], ['R'])
        event = next(e for e in result['binding_log'] if e.get('field') == '재정자료성격')
        self.assertTrue(event['audit_only'])
        self.assertNotIn('재정자료성격', candidate['values'])
        self.assertEqual(event['reason'], 'explicit_financial_plan_context')
        self.assertEqual(event['evidence'], record['근거 문구'])
        self.assertIn('문맥 구분.현황전망목표구분', event['source_fields'])
        self.assertTrue(event['candidate_accepted'])
        self.assertEqual(self.raw, before)
        self.assertEqual(self.run_b(), result)

    def test_plan_mapping_requires_both_explicit_labels_and_evidence(self):
        self.business.pop('계획구분')
        for field, value in [('현황전망목표구분', None), ('현황전망목표구분','실적'),
                             ('값의미',None), ('값의미','예산집행액')]:
            with self.subTest(field=field, value=value):
                record = self.plan_context()
                record['문맥 구분'][field] = value
                self.assertFalse(self.run_b()['accepted_candidates'])
        record = self.plan_context()
        record['근거 문구'] = '   '
        self.raw['objects'][0]['제목'] = '서울시 재정투자 계획'
        self.assertFalse(self.run_b()['accepted_candidates'])

    def test_plan_conflict_is_held_not_overwritten(self):
        self.plan_context()
        self.business['계획구분'] = '집행실적'
        result = self.run_b()
        self.assertFalse(result['accepted_candidates'])
        self.assertIn('field_conflict:계획구분', result['adapter_reasons']['R'])
        self.assertEqual(self.business['계획구분'], '집행실적')

    def test_metadata_plan_conflict_is_held(self):
        self.plan_context()
        self.business.pop('계획구분')
        base.SemanticBindingTests.link(self, field='계획구분', value='집행실적', role='자료구분')
        result = self.run_b()
        self.assertFalse(result['accepted_candidates'])
        self.assertIn('field_conflict:계획구분', result['adapter_reasons']['R'])

    def test_plan_mapping_preserves_explicit_period_total(self):
        record = self.plan_context()
        self.business.pop('연도')
        self.business.update({'기간시작':2024, '기간종료':2033, '시간기준':'period_total'})
        record['연도'] = None
        result = self.run_b()['accepted_candidates'][0]['values']
        self.assertEqual(result['계획구분'], '온실가스감축대책')
        self.assertEqual(result['예산액'], 10)
        self.assertEqual((result['기간시작'],result['기간종료']), (2024,2033))
        self.assertIsNone(result.get('연도'))

    def test_plan_context_does_not_promote_blank_or_text_to_amount(self):
        record = self.plan_context()
        self.business.pop('계획구분')
        self.business.pop('예산액')
        record.update({'숫자값':None, '값상태':'빈칸', '레코드분류':'unresolved'})
        self.assertFalse(self.run_b()['accepted_candidates'])
        record.update({'값상태':'정성상태', '레코드분류':'text_fact', '값원문':'비예산', '기간원문':'2024'})
        result = self.run_b()['accepted_candidates'][0]['values']
        self.assertEqual(result['값원문'], '비예산')
        self.assertIsNone(result.get('예산액'))
        self.assertIsNone(result.get('계획구분'))

    def test_plan_mapping_is_not_applied_to_reduction_contract(self):
        record = self.plan_context()
        values = {'사업명':'시험'}
        apply_contract('quantitative_reductions', record, values)
        self.assertNotIn('계획구분', values)

    def test_missing_or_legacy_policy_category_preserves_amount_without_guess(self):
        self.plan_context()
        for value in (None, '', '투자계획', '재정투자계획'):
            self.business['계획구분'] = value
            result = self.run_b()
            values = result['accepted_candidates'][0]['values']
            self.assertIn(values.get('계획구분'), (None, ''))
            self.assertEqual(values['계획구분상태'], '미확정')
            self.assertEqual(values['예산액'], 10)

    def test_financial_kind_metadata_does_not_conflict_with_policy(self):
        self.plan_context()
        base.SemanticBindingTests.link(self, field='계획구분', value='투자계획', role='자료구분')
        result = self.run_b()
        self.assertEqual(result['accepted_candidates'][0]['values']['계획구분'], '온실가스감축대책')
        self.assertIn('preserved_financial_kind_not_policy_category', [e['status'] for e in result['binding_log']])
