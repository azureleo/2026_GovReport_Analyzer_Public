import unittest
from copy import deepcopy
from utils.reading_position import coordinate_key, same_cell_key
from utils.reading_compatibility import convert


class PositionTests(unittest.TestCase):
    def test_hierarchy_and_types(self):
        self.assertNotEqual(coordinate_key(['건물', '가정']), coordinate_key(['건물', '상업']))
        self.assertNotEqual(coordinate_key(['a', 'b']), coordinate_key(['b', 'a']))
        self.assertNotEqual(coordinate_key(['a', 'b']), coordinate_key(['a > b']))
        self.assertNotEqual(coordinate_key(['2024']), coordinate_key('2024'))
        self.assertNotEqual(coordinate_key(2024), coordinate_key('2024'))
        hash(coordinate_key(['a', ['b', 'c']]))

    def test_repeat_labels_not_same_cell(self):
        pos = {'데이터행': ['건물', '가정'], '데이터열': ['2024'], 'case_id': 'a', 'PDF 실제 페이지': 1}
        before = deepcopy(pos)
        key = same_cell_key('O', pos)
        self.assertEqual(key, same_cell_key('O', deepcopy(pos)))
        for field, value in [('case_id', 'b'), ('PDF 실제 페이지', 2)]:
            other = dict(pos, **{field: value})
            self.assertNotEqual(key, same_cell_key('O', other))
        self.assertNotEqual(key, same_cell_key('OTHER', pos))
        self.assertIsNone(same_cell_key('O', {k:v for k,v in pos.items() if k != 'case_id'}))
        self.assertEqual(before, pos)

    def test_scalar_and_missing(self):
        self.assertEqual(same_cell_key('O', {'데이터행': 1, '데이터열': 2}),
                         same_cell_key('O', {'데이터행': 1, '데이터열': 2, 'case_id': 'ignored'}))
        self.assertIsNone(same_cell_key('O', {'데이터행': 1}))
        self.assertIsNone(same_cell_key('O', '문자열 원문'))

    def test_invalid_and_preservation(self):
        schema = {'SHEET_KEY_TO_NAME': {}, 'EXTRACTION_SHEETS': []}
        source = {'objects': [{'객체ID': 'O'}], 'records': [
            {'레코드ID': 'R', '객체ID': 'O', '객체 내 위치': {'데이터행': ['a'], '데이터열': ['b']}}], 'relations': []}
        before = deepcopy(source)
        out, report = convert(source, {'links': []}, schema)
        self.assertEqual(source, before)
        self.assertEqual(out, before)
        self.assertFalse(report['structural_issues'])
        for invalid in [[], {}, True, float('nan'), ['a', {}]]:
            source['records'][0]['객체 내 위치']['데이터행'] = invalid
            _, report = convert(source, {'links': []}, schema)
            self.assertEqual(report['structural_issues'][0]['code'], 'invalid_cell_position')
            self.assertEqual(report['structural_issues'][0]['record_id'], 'R')


if __name__ == '__main__':
    unittest.main()
