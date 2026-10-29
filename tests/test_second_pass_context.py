"""명시 분류 연결·경계·충돌 회귀. 표본의 사업명이나 페이지를 규칙에 넣지 않는다."""
from copy import deepcopy
import pytest

from utils.second_pass_context import ContextResolver


def obj(key,text,page,y,**metadata):
    return {"object_id":key,"object_type":"text","page_number":page,"sequence":int(y),
            "bbox":[0,y,500,y+20],"text":text,"metadata":{"evidence_id":"ev-"+key,**metadata}}


def fixture():
    objects = [obj("plan","01\n부문별 온실가스 감축대책",1,30),
               obj("sector","M\n수송 부문",2,80),obj("project","M1-1\nAlpha",2,100),
               {**obj("table","",3,300),"object_type":"table"}]
    candidate = {"source":{"object_id":"table","aliases":["table","ev-table"],"page":3},
                 "identity":{"name":"Alpha","code":"","context":{}}}
    return objects,candidate


def resolve(objects,candidate,data=None):
    return ContextResolver(objects,data or {},"테스트시").resolve(candidate)


def row(**kwargs):
    return {"지자체명":"테스트시","사업명":"Alpha","출처페이지":3,**kwargs}


def test_native_project_scope_continuation_and_exact_heading_evidence():
    objects,c = fixture(); r=resolve(objects,c)
    assert r["scope"]["code"] == "M1-1"
    assert r["fields"]["계획구분"]["value"] == "온실가스감축대책"
    assert r["fields"]["부문"]["value"] == "수송"
    proof=r["fields"]["부문"]["evidence"][0]
    assert proof["object_id"]=="sector" and proof["project_anchor"]["object_id"]=="project"


def test_business_words_and_polluted_section_are_not_classification():
    objects,c=fixture(); objects=objects[2:]; objects[-1]["section"]="건물 부문 감축대책 버스"
    c["identity"]["name"]="건물 에너지 버스 보급"
    assert all(v["status"]=="missing" for v in resolve(objects,c)["fields"].values())


def test_unknown_major_section_stops_inheritance():
    objects,c=fixture(); objects.append(obj("new-chapter","02\n다른 검토 과제",3,20))
    assert all(v["value"] is None for v in resolve(objects,c)["fields"].values())


def test_subsection_does_not_reset_real_plan_heading():
    objects,c=fixture(); objects.append(obj("subsection","1) 새로운 소제목",2,40))
    assert resolve(objects,c)["fields"]["계획구분"]["value"]=="온실가스감축대책"


def test_next_project_controls_scope_instead_of_previous_sector():
    objects,c=fixture(); objects.extend([obj("next","B1-1\nBeta",3,40),obj("next-sector","B\n건물 부문",3,60)])
    r=resolve(objects,c)
    assert r["scope"]["code"]=="B1-1" and r["fields"]["부문"]["value"]=="건물"
    c["identity"]["code"]="M1-1"
    assert all(v["status"]=="held" for v in resolve(objects,c)["fields"].values())


def test_parent_heading_or_large_gap_does_not_leak_previous_leaf():
    objects,c=fixture(); objects.append(obj("parent","M2\n다음 사업군",3,40))
    assert resolve(objects,c)["scope"]["reason"]=="project_scope_ended_or_too_far"
    objects=[o for o in objects if o["object_id"]!="parent"]
    objects[-1]["page_number"]=20; c["source"]["page"]=20
    assert resolve(objects,c)["fields"]["부문"]["value"] is None


@pytest.mark.parametrize("mode",["source_reference","reference_subsection"])
def test_reference_scopes_never_inherit(mode):
    objects,c=fixture()
    if mode=="source_reference":objects[-1]["metadata"]["index_reference"]=True
    else:objects.append(obj("reference","□ 해외사례",3,20))
    r=resolve(objects,c,{"financial_plan":[row(부문="수송")]})
    assert all(v["status"]=="held" and v["value"] is None for v in r["fields"].values())


@pytest.mark.parametrize("changed",[{"지자체명":"다른시"},{"출처페이지":9},{"사업명":"Alpha(동부)"}])
def test_name_city_and_anchor_are_all_required(changed):
    objects,c=fixture(); objects=objects[-1:]
    assert resolve(objects,c,{"mitigation_projects":[row(부문="건물",**changed)]})["fields"]["부문"]["value"] is None


def test_explicit_code_can_join_different_display_name_with_source_anchor():
    objects,c=fixture()
    r=resolve(objects,c,{"mitigation_projects":[row(사업명="다른 표시명",관리번호="M1-1",부문="수송")]})
    assert len(r["fields"]["부문"]["evidence"])==2


def test_competing_management_code_is_held():
    objects,c=fixture()
    r=resolve(objects,c,{"mitigation_projects":[row(관리번호="B1-2",부문="수송")]})
    assert r["fields"]["부문"]["status"]=="held"


def test_conflicting_valid_classifications_are_not_overwritten():
    objects,c=fixture()
    r=resolve(objects,c,{"mitigation_projects":[row(부문="건물")]})
    assert r["fields"]["부문"]["reason"]=="conflicting_context_values"
    assert r["fields"]["부문"]["value"] is None


def test_malformed_label_is_audited_not_treated_as_valid_sector():
    objects,c=fixture()
    r=resolve(objects,c,{"mitigation_projects":[row(부문="버스 사업 시비")]})
    d=r["fields"]["부문"]
    assert d["value"]=="수송" and d["rejected_evidence"][0]["original_value"]=="버스 사업 시비"


def test_plan_conflict_is_independent_of_sector():
    objects,c=fixture(); c["identity"]["context"]["계획구분"]="기후위기적응대책"
    r=resolve(objects,c)
    assert r["fields"]["계획구분"]["status"]=="held" and r["fields"]["부문"]["value"]=="수송"


def test_explicit_alias_only_and_no_parenthesis_stripping():
    objects,c=fixture(); objects=objects[-1:]
    data={"mitigation_projects":[row(사업명="Alpha(지역A)",부문="건물")]}
    assert resolve(objects,c,data)["fields"]["부문"]["value"] is None
    objects[0]["metadata"]["project_aliases"]=["Alpha(지역A)"]
    assert resolve(objects,c,data)["fields"]["부문"]["value"]=="건물"


def test_native_sector_alias_retains_original_literal():
    objects,c=fixture(); objects[1]["text"]="F\n탄소 흡수"; objects[2]["text"]="F1-1\nAlpha"
    r=resolve(objects,c)
    assert r["fields"]["부문"]["value"]=="흡수원"
    assert "탄소 흡수" in r["fields"]["부문"]["evidence"][0]["literal"]


def test_explicit_source_metadata_and_partial_completion():
    objects,c=fixture(); objects=objects[-1:]; objects[0]["metadata"]["부문"]="폐기물"
    r=resolve(objects,c)
    assert r["fields"]["부문"]["value"]=="폐기물" and r["fields"]["계획구분"]["status"]=="missing"


def test_other_sector_on_same_project_page_is_not_borrowed():
    objects,c=fixture(); objects.insert(2,obj("other-sector","B\n건물 부문",2,90))
    assert resolve(objects,c)["fields"]["부문"]["value"]=="수송"


def test_model_generated_heading_not_promoted_to_native_evidence():
    objects,c=fixture(); objects[0]["metadata"]["engine"]="vlm"; objects[1]["metadata"]["engine"]="vlm"
    assert all(v["value"] is None for v in resolve(objects,c)["fields"].values())
