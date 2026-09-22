import unittest
from copy import deepcopy
from utils.reading_compatibility import convert


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.schema = {'SHEET_KEY_TO_NAME': {'financial_plan': '11_재정투자계획'}, 'EXTRACTION_SHEETS': ['financial_plan']}
        self.source = {'objects': [{'객체ID': 'O', '공통문맥': '서울이라는 원문'}], 'records': [
            {'레코드ID': 'R', '객체ID': 'O', '숫자값': 1, '단위원문': '천 톤CO₂eq.',
             '값상태': '명시', '문맥 구분': {'대상시트': '11_재정투자계획', '업무필드': {}}}], 'relations': [], 'run': {}}

    def test_lossless_aliases_and_no_inference(self):
        before = deepcopy(self.source)
        out, report = convert(self.source, {'links': []}, self.schema)
        self.assertEqual(self.source, before)
        self.assertEqual(out['objects'][0]['공통문맥'], {'원문텍스트': '서울이라는 원문'})
        row = out['records'][0]
        self.assertEqual(row['숫자값'], 1)
        self.assertEqual(row['단위원문'], '천톤CO2eq')
        self.assertEqual(row['문맥 구분']['대상시트'], 'financial_plan')
        self.assertEqual(row['문맥 구분']['업무필드'], {})
        self.assertEqual(row['값상태'], '명시값')
        self.assertFalse(report['structural_issues'])
        self.assertEqual(convert(self.source, {'links': []}, self.schema), (out, report))

    def test_null_and_co2_preserved(self):
        self.source['records'][0].update({'숫자값': None, '단위원문': 'tCO2', '값상태': '미표기'})
        out, _ = convert(self.source, {'links': []}, self.schema)
        self.assertIsNone(out['records'][0]['숫자값'])
        self.assertEqual(out['records'][0]['단위원문'], 'tCO2')
        self.assertEqual(out['records'][0]['값상태'], '미표기')

    def test_collect_errors(self):
        self.source['objects'][0]['공통문맥'] = []
        self.source['records'][0]['객체ID'] = 'missing'
        self.source['records'][0]['숫자값'] = True
        _, report = convert(self.source, {'links': []}, self.schema)
        self.assertEqual(len(report['structural_issues']), 3)

    def test_field_competition_not_resolved(self):
        links = [dict(link_id=str(i), measurement_id='R', metadata_id='R', target_field='계획구분', linked_value=v) for i,v in enumerate(['투자계획','합계'])]
        out, report = convert(self.source, {'links': links}, self.schema)
        self.assertEqual(len(report['field_conflicts']), 1)
        self.assertEqual(out['records'][0]['문맥 구분']['업무필드'], {})


if __name__ == '__main__':
    unittest.main()
