import json
from pathlib import Path
from copy import deepcopy
import unittest
from utils.reading_semantic_binding import bind_candidates


class SemanticBindingTests(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads((Path(__file__).resolve().parents[1]/'data/reading_mapping_rules_v1.json').read_text(encoding='utf8'))
        self.schema = {'EXTRACTION_SHEETS': ['emissions_regional'],
                       'SHEET_KEY_TO_NAME': {'emissions_regional': '03'},
                       'EXCEL_HEADERS': {'03': ['지자체명','인벤토리출처','배출유형','부문','세부부문','연도','배출량','단위']}}
        self.business = dict(zip(self.schema['EXCEL_HEADERS']['03'], ['서울시','출처','직접배출량','에너지','연료',2020,10,'천톤CO2eq']))
        self.raw = {'objects': [{'객체ID':'O','공통문맥':{},'PDF 실제 페이지':1}],
            'records': [{'레코드ID':'R','객체ID':'O','숫자값':10,'값원문':'10','연도':2020,
                         '단위원문':'천톤CO2eq','레코드분류':'measurement','레코드종류':'정량값','값상태':'명시값',
                         '문맥 구분':{'자료지역':'서울시','대상시트':'emissions_regional','업무필드':self.business}},
                        {'레코드ID':'M','객체ID':'O','문맥 구분':{}}], 'relations':[]}
        self.meta = {'links':[]}

    def link(self, field='연도', value=2020, role='연도', lid='L'):
        self.meta['links'].append(dict(link_id=lid,measurement_id='R',metadata_id='M',role=role,
            linked_value=value,target_field=field,evidence='명시 근거',scope='표전체'))
        self.raw['relations'].append({'관계ID':lid,'객체ID':'O','관계원문':'명시 메타데이터 연결','출발 레코드 또는 노드ID':'M',
                                     '도착 레코드 또는 노드ID':'R','근거 위치':'명시 근거'})

    def run_b(self):
        return bind_candidates(self.raw,self.meta,self.schema,self.rules,['R'])

    def test_precedence_and_no_mutation(self):
        before = deepcopy(self.raw)
        out = self.run_b()
        self.assertEqual(out['accepted_candidates'][0]['sheet_key'],'emissions_regional')
        self.assertEqual(before,self.raw)
        self.assertEqual(out,self.run_b())

    def test_fill_and_trace(self):
        del self.business['연도']; self.link()
        out = self.run_b()
        self.assertEqual(out['accepted_candidates'][0]['values']['연도'],2020)
        self.assertEqual(out['accepted_candidates'][0]['field_sources']['연도'],['M'])
        self.assertEqual(out['binding_log'][0]['status'],'filled')

    def test_conflict_does_not_overwrite(self):
        self.link(value=2021)
        self.assertFalse(self.run_b()['accepted_candidates'])
        self.assertEqual(self.business['연도'],2020)

    def test_link_link_conflict(self):
        del self.business['연도']; self.link(); self.link(value=2021,lid='L2')
        self.assertIn('field_conflict:연도',self.run_b()['adapter_reasons']['R'])

    def test_wrong_role_never_fills(self):
        del self.business['연도']; self.link(role='집계역할')
        self.assertFalse(self.run_b()['accepted_candidates'])
        self.assertEqual(self.run_b()['binding_log'][0]['status'],'preserved_unsupported_role')

    def test_invalid_endpoint(self):
        self.link(); self.raw['relations'][0]['도착 레코드 또는 노드ID']='M'
        self.assertFalse(self.run_b()['accepted_candidates'])

    def test_cross_object_cannot_fill(self):
        del self.business['연도']; self.link(); self.raw['records'][1]['객체ID']='OTHER'
        self.raw['objects'].append({'객체ID':'OTHER','공통문맥':{}})
        self.assertFalse(self.run_b()['accepted_candidates'])

    def test_explicit_hierarchy_confirmation(self):
        self.link(field='부문',role='부문',value='에너지 > 연료')
        out=self.run_b()
        self.assertEqual(out['accepted_candidates'][0]['values']['부문'],'에너지')
        self.assertEqual(out['binding_log'][0]['status'],'confirmed_explicit_hierarchy')

    def test_unit_and_numeric_guards(self):
        for field, value in [('단위','tCO2'),('배출량',None),('배출량',True),('배출량','10')]:
            original=self.business[field]; self.business[field]=value
            self.assertFalse(self.run_b()['accepted_candidates'])
            self.business[field]=original


if __name__ == '__main__':
    unittest.main()
