"""Typed performance counts must survive the ordinary sheet cleaner."""
from copy import deepcopy
import unittest

from agents.organizer_agent import OrganizerAgent
from utils.run_state import stable_entity_id


class MonitoringSummaryTests(unittest.TestCase):
    def summary(self, label='60% 미만', value=1):
        return {'지자체명': '서울시', '점검연도': 2023, '부문': '폐기물',
                '정보유형': '성과집계', '항목원문': label, '측정값': value,
                '값원문': str(value), '근거ID': label, '출처페이지': 2}

    def clean(self, rows):
        return OrganizerAgent().organize_sheet('monitoring_performance', rows, '서울시')

    def test_distinct_count_buckets_survive_without_fake_project(self):
        rows = [self.summary(), self.summary('60% 이상 80% 미만')]
        before = deepcopy(rows)
        actual = self.clean(rows)
        self.assertEqual(len(actual), 2)
        self.assertEqual(rows, before)
        for source, result in zip(rows, actual):
            for key in ('항목원문', '측정값', '값원문', '근거ID', '정보유형'):
                self.assertEqual(result[key], source[key])
            for key in ('사업명', '이행실적', '측정단위'):
                self.assertIsNone(result.get(key))
        self.assertNotEqual(*(stable_entity_id('monitoring_performance', r) for r in actual))

    def test_zero_is_a_valid_count(self):
        self.assertEqual(self.clean([self.summary(value=0)])[0]['측정값'], 0)

    def test_invalid_counts_do_not_bypass_filter(self):
        for value in (None, '', '1', True, False, -1, 0.5, float('nan'), float('inf')):
            with self.subTest(value=value):
                row = self.summary(value=value)
                row['사업명'] = '집계에는 일반 사업명 필터를 적용하지 않음'
                self.assertEqual(self.clean([row]), [])

    def test_missing_summary_identity_stays_out(self):
        for field in ('항목원문', '점검연도'):
            with self.subTest(field=field):
                row = self.summary()
                row.pop(field)
                self.assertEqual(self.clean([row]), [])
        for label in ('', '   ', [], {}):
            row = self.summary(label=label)
            self.assertEqual(self.clean([row]), [])

    def test_legacy_rows_keep_the_same_filter(self):
        rows = [{'사업명': '기존 사업'}, {'이행실적': '정성사업'}, {},
                {'측정값': 1, '항목원문': '분류 미표기'}]
        actual = self.clean(rows)
        self.assertEqual(len(actual), 2)
        self.assertEqual(actual[0]['사업명'], '기존 사업')
        self.assertEqual(actual[1]['이행실적'], '정성사업')


if __name__ == '__main__':
    unittest.main()
