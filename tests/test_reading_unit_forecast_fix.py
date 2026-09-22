import unittest
from copy import deepcopy
from unittest.mock import patch
from utils.reading_compatibility import convert, UNIT_ALIASES
from agents.organizer_agent import OrganizerAgent


class UnitForecastFixTests(unittest.TestCase):
    def test_exact_unit_aliases_preserve_values_and_input(self):
        for unit in ('tCO₂eq.', 'tCO₂eq', 'tCO2eq.'):
            raw = {'objects': [{'객체ID':'O','공통문맥':{}}], 'records':[
                {'레코드ID':'R','객체ID':'O','숫자값':3516,'단위원문':unit,
                 '문맥 구분':{'업무필드':{'단위':unit,'예상감축량':3516}}}], 'relations':[]}
            before=deepcopy(raw)
            converted, report=convert(raw, {'links':[]}, {'SHEET_KEY_TO_NAME':{},'EXTRACTION_SHEETS':[]})
            self.assertEqual(raw,before)
            self.assertEqual(converted['records'][0]['단위원문'],'tCO2eq')
            self.assertEqual(converted['records'][0]['숫자값'],3516)
            self.assertEqual(len(report['changes']),2)
            self.assertFalse(report['structural_issues'])

    def test_no_co2_or_scale_conversion(self):
        for unit in ('tCO2','tCO₂','kgCO2eq','천tCO2eq','tCO2eq/대'):
            self.assertNotIn(unit,UNIT_ALIASES)

    def clean(self, detail2='가정', value2=6376):
        base={'지자체명':'서울특별시','시나리오':'BAU','부문':'에너지','연도':2024,
              '단위':'천톤CO2eq'}
        rows=[dict(base,세부부문='도로수송',전망값=7489,근거ID='R1'),
              dict(base,세부부문=detail2,전망값=value2,근거ID='R2')]
        with patch('config.RUN_STATE_ENABLED',False):
            return OrganizerAgent().organize_sheet('emissions_forecast',rows,'서울특별시')

    def test_distinct_subsectors_not_conflicting(self):
        rows=self.clean()
        self.assertEqual(len(rows),2)
        self.assertEqual({r['전망값'] for r in rows},{7489,6376})
        self.assertTrue(all(r.get('데이터상태')!='conflicting' for r in rows))

    def test_same_subsector_conflict_still_detected(self):
        rows=self.clean('도로수송')
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r.get('데이터상태')=='conflicting' for r in rows))
