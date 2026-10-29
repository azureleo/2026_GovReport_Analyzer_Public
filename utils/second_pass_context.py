"""저장 원문과 명시 사업 연결만으로 2차 예산 행의 분류를 보완한다.

사업명 키워드로 부문을 추측하거나 section/nearby_text를 절 제목으로 간주하지 않는다.
모든 판정은 필드별 근거와 경계를 남기며, 상충하는 값은 보류한다.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from copy import deepcopy
import re

from utils.source_coverage import canonical, ids, pages, token

VERSION = "budget-context-v1"
FIELDS = ("계획구분", "부문")
SECTORS = {"건물":"건물", "수송":"수송", "폐기물":"폐기물", "에너지생산":"에너지 생산",
           "탄소흡수":"흡수원", "흡수원":"흡수원", "시민협력":"시민협력", "산업":"산업",
           "전환":"전환", "농축산":"농축산", "기타":"기타"}
PLANS = {"감축":"온실가스감축대책", "온실가스감축대책":"온실가스감축대책",
         "부문별온실가스감축대책":"온실가스감축대책", "적응":"기후위기적응대책",
         "기후위기적응계획":"기후위기적응대책", "기후위기적응대책":"기후위기적응대책",
         "서울시기후위기적응계획":"기후위기적응대책",
         "대응기반강화":"기후위기대응기반강화", "기후위기대응기반강화":"기후위기대응기반강화",
         "서울시기후위기대응기반강화대책":"기후위기대응기반강화"}
PROJECT = re.compile(r"^([A-Z]{1,3}\d*(?:-\d+)+|[A-Z]{1,3}\d+)\s*\n([^\n]+(?:\n[^\n]+)?)$")
CHAPTER = re.compile(r"^(\d{2})\s*\n([가-힣A-Za-z][^\n]{3,80})$")


def name(value):
    return token(re.sub(r"\\+n", " ", str(value or "")))


def normalize(field, value):
    literal = token(value)
    return (PLANS if field == "계획구분" else SECTORS).get(literal)


def position(obj):
    box = obj.get("bbox")
    y = box[1] if isinstance(box, (list, tuple)) and len(box) == 4 else obj.get("sequence", 0)
    return (int(obj.get("page_number") or 0), float(y), int(obj.get("sequence") or 0))


def reference(obj):
    meta = obj.get("metadata") or {}
    return any(meta.get(key) for key in ("index_reference", "is_index_reference", "is_reference"))


def object_evidence(obj, relation):
    return {"relation":relation, "object_id":obj["object_id"],
            "evidence_id":(obj.get("metadata") or {}).get("evidence_id",obj["object_id"]),
            "page":obj.get("page_number"), "bbox":deepcopy(obj.get("bbox")),
            "literal":obj.get("text") or obj.get("caption") or ""}


class ContextResolver:
    def __init__(self, objects, data, municipality):
        self.data, self.city = data, municipality
        self.objects = {o["object_id"]:o for o in objects}
        self.events, self.projects, self.sectors = [], [], []
        for obj in sorted(objects,key=position):
            if obj.get("object_type") not in {"text","heading"} or reference(obj):
                continue
            engine = str((obj.get("metadata") or {}).get("engine") or "").lower()
            if engine and engine not in {"pymupdf","native","pdf","pypdf","fitz"}:
                continue  # 모델/OCR이 만든 제목을 독립 원문 근거로 승격하지 않는다.
            text = (obj.get("text") or "").strip()
            if len(text) > 180:
                continue
            chapter = CHAPTER.fullmatch(text)
            if chapter:
                title = chapter[2]
                plan = PLANS.get(token(title))
                # 해석할 수 없는 새 절도 경계다. 이전 계획분류가 넘어가지 않는다.
                self.events.append((position(obj),plan,obj))
            elif re.match(r"^\d[.．)]\s*",text):
                title = re.sub(r"^\d[.．)]\s*","",text)
                if token(title) in PLANS:
                    self.events.append((position(obj),PLANS[token(title)],obj))
            project = PROJECT.fullmatch(text)
            if project:
                code, title = project.groups()
                self.projects.append({"position":position(obj),"code":code,"title":title,
                                      "leaf":"-" in code,"object":obj})
            sector = re.fullmatch(r"(?:([A-Z]{1,3})\s*\n)?([^\n]{1,20}?)(?:\s*부문)?", text)
            if sector:
                prefix, value = sector.groups()
                value = token(value)
                if value in SECTORS and (prefix or text.endswith("부문")):
                    self.sectors.append((obj.get("page_number"), prefix, SECTORS[value],obj))
        self.project_positions = [p["position"] for p in self.projects]
        self.event_positions = [e[0] for e in self.events]

    def native_scope(self, candidate):
        source = candidate["source"]
        obj = self.objects.get(source["object_id"])
        if not obj or reference(obj):
            return {}, {"status":"held","reason":"source_object_missing_or_reference"}
        point = position(obj)
        i = bisect_right(self.project_positions, point)-1
        j = bisect_right(self.event_positions, point)-1
        chapter = self.events[j] if j >= 0 else None
        if i < 0:
            return {}, {"status":"missing","reason":"no_explicit_project_scope"}
        project = self.projects[i]
        if not project["leaf"] or point[0]-project["position"][0] > 8 or (
                chapter and chapter[0] > project["position"]):
            return {}, {"status":"missing","reason":"project_scope_ended_or_too_far"}
        code = str(candidate["identity"].get("code") or "").strip()
        if code and code != project["code"]:
            return {}, {"status":"held","reason":"project_code_conflict","candidate_code":code,
                        "scope_code":project["code"]}
        # 같은 사업 범위의 별도 참고자료 구역은 본문 분류를 승계하지 않는다.
        for other in self.objects.values():
            if project["position"] <= position(other) <= point and re.fullmatch(
                    r"[□■※\s]*(?:참고자료|참고사례|해외사례|국내외사례)\s*", (other.get("text") or "").strip()):
                return {}, {"status":"held","reason":"reference_scope_boundary"}
        anchor = object_evidence(project["object"],"explicit_project_scope")
        scope = {"status":"linked","code":project["code"],"title":project["title"],
                 "start":list(project["position"]),"next_project":self.projects[i+1]["code"] if i+1<len(self.projects) else None,
                 "anchor":anchor}
        values = {field:[] for field in FIELDS}
        if chapter and chapter[1] and chapter[0] <= project["position"]:
            values["계획구분"].append((chapter[1],{**object_evidence(chapter[2],"explicit_plan_heading"),
                "project_anchor":anchor}))
        prefix = re.match(r"[A-Z]+",project["code"])[0]
        for page, sector_prefix, value, heading in self.sectors:
            if page == project["position"][0] and (sector_prefix == prefix or (
                    sector_prefix is None and project["position"] <= position(heading) <= point)):
                values["부문"].append((value,{**object_evidence(heading,"explicit_sector_heading"),
                    "project_anchor":anchor}))
        return values, scope

    def resolve(self, candidate):
        native, scope = self.native_scope(candidate)
        if scope["status"] == "held":
            return {"version":VERSION,"source_object_id":candidate["source"]["object_id"],"scope":scope,
                    "fields":{field:{"status":"held","value":None,"reason":scope["reason"],"evidence":[],"rejected_evidence":[]} for field in FIELDS}}
        claims = {field:list(native.get(field,[])) for field in FIELDS}
        source = candidate["source"]
        source_obj = self.objects.get(source["object_id"],{})
        for field in FIELDS:
            value = candidate["identity"]["context"].get(field)
            if value:
                claims[field].append((value,{**object_evidence(source_obj,"explicit_candidate_context"),"original_value":value}))
            stored = (source_obj.get("metadata") or {}).get(field)
            if stored:
                claims[field].append((stored,{**object_evidence(source_obj,"explicit_source_object_metadata"),"original_value":stored}))
        # 사업목록/원래 재정 행은 이름·코드의 명시 대응과 출처 또는 객체 근거를 함께 요구한다.
        aliases = {name(candidate["identity"]["name"])}
        for key in ("사업명별칭","project_aliases"):
            declared = (source_obj.get("metadata") or {}).get(key,[])
            if isinstance(declared,list):
                aliases.update(name(v) for v in declared if isinstance(v,str))
        source_ids = set(source.get("aliases",[])) | {source["object_id"]}
        code = str(candidate["identity"].get("code") or scope.get("code") or "")
        for sheet in ("financial_plan","mitigation_projects"):
            for index,row in enumerate(self.data.get(sheet,[]),1):
                if row.get("지자체명") != self.city:
                    continue
                same_name = name(row.get("사업명")) in aliases
                row_code = str(row.get("관리번호") or "").strip()
                same_code = bool(code and row_code == code)
                anchored = source["page"] in pages(row.get("출처페이지")) or bool(ids(row) & source_ids)
                if not anchored or not (same_name or same_code):
                    continue
                # 동일 이름이라도 명시 관리번호가 서로 다르면 경쟁 사업으로 보존한다.
                if code and row_code and code != row_code:
                    for field in FIELDS:
                        if row.get(field):
                            claims[field].append(("__identity_conflict__",{"relation":"project_identity_conflict",
                                "source_sheet":sheet,"source_row":index,"code":row_code}))
                    continue
                for field in FIELDS:
                    if row.get(field):
                        claims[field].append((row[field],{"relation":"same_project_source_row","source_sheet":sheet,
                            "source_row":index,"project_name":row.get("사업명"),"code":row_code,
                            "page":pages(row.get("출처페이지")),"evidence_ids":sorted(ids(row)),
                            "original_value":row[field]}))
        fields = {}
        for field in FIELDS:
            evidence, rejected = [], []
            for value,proof in claims[field]:
                normalized = "__identity_conflict__" if value == "__identity_conflict__" else normalize(field,value)
                entry = {"value":normalized,"original_value":value,**proof}
                if normalized is None:
                    rejected.append({**entry,"reason":"unsupported_classification_label"})
                    continue
                if canonical(entry) not in {canonical(e) for e in evidence}:
                    evidence.append(entry)
            unique = {e["value"] for e in evidence}
            if len(unique) == 1 and "__identity_conflict__" not in unique:
                fields[field] = {"status":"resolved","value":next(iter(unique)),"reason":"unique_explicit_agreement","evidence":evidence,"rejected_evidence":rejected}
            else:
                fields[field] = {"status":"held" if unique else "missing","value":None,
                    "reason":"conflicting_context_values" if unique else "no_explicit_context_evidence","evidence":evidence,"rejected_evidence":rejected}
        return {"version":VERSION,"source_object_id":source["object_id"],"scope":scope,"fields":fields}


def summary(records):
    rows = [r for r in records.values() if "context_resolution" in r]
    return {"version":VERSION,"rows_checked":len(rows),
        "fully_classified":sum(not r.get("unfilled_context") for r in rows),
        "fully_resolved_without_conflict":sum(not r.get("unfilled_context") and
            all(v["status"] in {"resolved","preserved"} for v in r["context_resolution"]["fields"].values()) for r in rows),
        "rows_with_unfilled_context":sum(bool(r.get("unfilled_context")) for r in rows),
        "rows_with_conflict":sum(any(v["status"] in {"held","preserved_with_conflict"} for v in r["context_resolution"]["fields"].values()) for r in rows),
        "fields_filled_from_new_evidence":sum(v["status"]=="resolved" for r in rows for v in r["context_resolution"]["fields"].values()),
        "field_statuses":{f:dict(Counter(r["context_resolution"]["fields"][f]["status"] for r in rows)) for f in FIELDS},
        "classification_accuracy_evaluated":False}
