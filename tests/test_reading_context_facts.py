import unittest
from copy import deepcopy
from tests import test_reading_semantic_binding as base
from utils.reading_context_facts import resolve_region, typed_fact


class ContextFactTests(unittest.TestCase):
    def setUp(self):
        helper=base.SemanticBindingTests(); helper.setUp()
        self.rules=helper.rules
        self.obj={'객체ID':'O','공통문맥':{}}
        self.row={'문맥 구분':{}}

    def test_no_global_guess(self):
        self.obj['제목']='서울 보고서에 실린 표'
        # Existing legacy title-based own detection is retained.
        self.obj['제목']='위원회 현황'
        self.assertIsNone(resolve_region(self.row,self.obj,self.rules)[0])
        ctx={'objects':[{'object_id':'OTHER','region':'서울시','evidence':'표 제목','scope':'그 표만'}]}
        self.assertIsNone(resolve_region(self.row,self.obj,self.rules,ctx)[0])

    def test_evidence_and_conflict(self):
        ctx={'objects':[{'object_id':'O','region':'서울시','evidence':'서울시 위원회 표 제목','scope':'O의 전체 행'}]}
        before=deepcopy(self.row)
        self.assertEqual(resolve_region(self.row,self.obj,self.rules,ctx)[0],'서울시')
        self.assertEqual(self.row,before)
        self.row['문맥 구분']['자료지역']='경기도'
        self.assertIsNone(resolve_region(self.row,self.obj,self.rules,ctx)[0])
        self.row['문맥 구분']={'자료구분':'전국 참고자료'}
        self.assertIsNone(resolve_region(self.row,self.obj,self.rules,ctx)[0])

    def test_object_evidence(self):
        self.obj['공통문맥']={'지역':'서울시'}
        self.assertIsNone(resolve_region(self.row,self.obj,self.rules)[0])
        self.obj['공통문맥 근거']={'지역':'표 제목에 서울시 명시'}
        self.assertEqual(resolve_region(self.row,self.obj,self.rules)[0],'서울시')

    def test_org_zero_not_activity(self):
        r={'레코드분류':'measurement','값상태':'명시값','숫자값':0,'단위원문':'명','값원문':'0명',
           '문맥 구분':{'측정유형':'기관구성','구성구분':'위원수 > 위촉'}}
        v={'지자체명':'서울시','거버넌스기구':'위원회'}
        issues,_=typed_fact('governance_feedback',r,v)
        self.assertFalse(issues); self.assertEqual(v['측정값'],0)
        self.assertNotIn('활동량',v)
        r['숫자값']=True
        self.assertIsNone(typed_fact('governance_feedback',r,{}))

    def test_factor_is_not_reduction(self):
        r={'레코드분류':'measurement','값상태':'명시값','숫자값':.00648,'단위원문':'tCO2eq/㎡','항목원문':'창호교체'}
        v={'지자체명':'서울시','사업명':'창호사업'}
        self.assertFalse(typed_fact('quantitative_reductions',r,v)[0])
        self.assertEqual(v['감축원단위값'],.00648)
        self.assertNotIn('예상감축량',v)
        v['예상감축량']=100
        self.assertIn('mixed_factor_and_measurement',typed_fact('quantitative_reductions',r,v)[0])

    def test_icon_requires_legend(self):
        r={'레코드분류':'text_fact','값상태':'아이콘범위','값원문':'녹색 아이콘','문맥 구분':{'달성도범위':'90% 이상'}}
        v={'지자체명':'서울시','점검연도':2023,'사업명':'사업','이행실적':'90% 이상'}
        self.assertIn('missing_icon_legend',typed_fact('monitoring_performance',r,v)[0])
        r['범례원문']='녹색 = 90% 이상'
        self.assertFalse(typed_fact('monitoring_performance',r,v)[0])
        self.assertNotIn('측정값',v)

    def test_budget_text_not_zero(self):
        r={'레코드분류':'text_fact','값상태':'정성상태','숫자값':None,'값원문':'전액민자'}
        v={'지자체명':'서울시','집계대상원문':'["E1-5"]'}
        self.assertFalse(typed_fact('financial_plan',r,v)[0])
        self.assertNotIn('예산액',v)
        v['예산액']=0
        self.assertIn('mixed_text_and_budget_amount',typed_fact('financial_plan',r,v)[0])
