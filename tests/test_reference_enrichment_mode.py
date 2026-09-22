import unittest
from copy import deepcopy
from unittest.mock import patch
import agents.organizer_agent as organizer
from scripts.verify_reading_excel_a import PROFILE


class ReferenceModeTests(unittest.TestCase):
    def test_verification_disables_references(self):
        self.assertEqual(PROFILE['REFERENCE_ENRICHMENT_ENABLED'], '0')

    def test_disabled_preserves_rows_without_lookup(self):
        row={'사업명':'시험사업', '예상감축량':3516, '감축원단위ID':'existing'}
        before=deepcopy(row)
        with patch('config.REFERENCE_ENRICHMENT_ENABLED', False), \
             patch.object(organizer, 'match_appendix3_unit', side_effect=AssertionError('lookup')), \
             patch.object(organizer, 'find_appendix3_unit_by_id', side_effect=AssertionError('lookup')), \
             patch.object(organizer, 'match_appendix4_project', side_effect=AssertionError('lookup')):
            organizer._apply_appendix3_unit_match(row,'서울특별시',1)
            organizer._apply_appendix4_project_match(row,'서울특별시',1)
        self.assertEqual(row,before)

    def test_enabled_retains_lookup_path(self):
        with patch('config.REFERENCE_ENRICHMENT_ENABLED', True), \
             patch.object(organizer,'match_appendix3_unit',return_value=None) as match3, \
             patch.object(organizer,'match_appendix4_project',return_value=None) as match4:
            organizer._apply_appendix3_unit_match({'사업명':'시험'},'서울특별시',1)
            organizer._apply_appendix4_project_match({'사업명':'시험'},'서울특별시',1)
        match3.assert_called_once()
        match4.assert_called_once()
