"""외부 판독 경계만 대체하고 PDF·정제·의미검증·Excel·감사 저장은 실제 실행한다."""
from copy import deepcopy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import fitz
import openpyxl
import pytest

import config
from agents.excel_agent import ExcelAgent
from agents.organizer_agent import OrganizerAgent
from utils.document_objects import DocumentObject
from utils.source_coverage import audit_saved_run, read_excel, sha256_file
from utils.second_pass import (execute, prepare, prompt_for, read_json, validate_response,
                               write_json, _read_workbook)


@pytest.fixture
def inputs(tmp_path, request):
    source = tmp_path / "source.pdf"
    doc = fitz.open()
    objects = []
    for p in (1, 2):
        page = doc.new_page(width=600, height=800)
        if getattr(request,"param",None) in {"classification","classification_conflict"}:
            conflict=getattr(request,"param",None)=="classification_conflict"
            native = [(f"project-{p}",( ("M1-1\nTask1" if conflict else "B1-1\nTask1") if p==1 else "M1-2\nTask2"),60),
                      (f"sector-{p}",("B\n건물 부문" if p==1 and not conflict else "M\n수송 부문"),35)]
            if p==1:
                native.append(("plan-heading","01\n부문별 온실가스 감축대책",10))
            for oid,text,y in native:
                page.insert_text((30,y+7),text,fontsize=6,fontname="korea")
                objects.append(DocumentObject(oid,"text",p,y,text=text,bbox=(30,y,400,y+20),
                    metadata={"evidence_id":"ev-"+oid}).to_dict())
        rows = [["사업명", "연도", "재원구분", "예산액", "예산단위"],
                [f"Task{p}", "2024", "계", str(17001 + p), "백만원"],
                [f"Task{p}", "2025", "계", str(18001 + p), "백만원"]]
        xs = [30, 210, 290, 380, 470, 575]
        ys = [100, 140, 180, 220]
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                page.insert_text((xs[c] + 5, ys[r] + 23), text, fontname="korea", fontsize=10)
        objects.append(DocumentObject(f"p{p}_table_1", "table", p, 1, rows=rows,
            bbox=(30, 100, 575, 220), metadata={"evidence_id":f"ev-p{p}_table_1", "header_row_count":1}).to_dict())
    doc.save(source)
    doc.close()
    excel = tmp_path / "baseline.xlsx"
    old = {"municipality_name":"테스트시", "document_meta":[{"지자체명":"테스트시", "계획명":""}],
           "financial_plan":[{"지자체명":"테스트시", "사업명":"Task1", "연도":2023,
               "예산액":27.5,"예산단위":"백만원","재원구분":"합계","계획구분":"감축","부문":"건물","출처페이지":1}]}
    ExcelAgent().write(OrganizerAgent().organize(old), excel)
    audit = tmp_path / "baseline_pipeline_audit.json"
    write_json(audit, {"input_path":str(source), "excel_path":str(excel),
        "context":{"input_sha256":sha256_file(source)},"ledgers":{"document_objects":objects}})
    write_json(tmp_path / "baseline_run_manifest.json", {"input_sha256":sha256_file(source),
        "output_sha256":sha256_file(excel),"created_at":"2026-10-21T10:00:00+00:00",
        "completed_at":"2026-10-21T12:00:00+00:00",
        "execution_info":{"text_backend":"codex","text_model":"gpt-6-luna"}})
    summary = audit_saved_run(source, excel, audit, tmp_path / "audit")
    journal = [json.loads(v) for v in Path(summary["missing_candidates_path"]).read_text(encoding="utf-8").splitlines()]
    targets = [r for r in journal if r["status"] == "missing_candidate"]
    assert len(targets) == 4
    reviews = tmp_path / "review.json"
    write_json(reviews, {"records":[{"candidate_id":r["candidate_id"], "candidate":r,
        "review_verdict":"final_budget_cell_missing","sample_id":f"case-{i}"} for i,r in enumerate(targets)]})
    selection = tmp_path / "confirmed.json"
    write_json(selection, {"selection_status":"source_review_confirmed_missing", "second_pass_executed":False,
        "source_audit":summary["coverage_summary_path"], "source_excel_sha256":sha256_file(excel),
        "review_files":[str(reviews)], "target_count":len(targets),"targets":targets})
    return selection, source


def payload(prepared, group):
    return {"results":[{"candidate_id":cid,"status":"read","사업명":c["identity"]["name"],
        "연도":c["identity"]["year"],"재원구분":c["identity"]["context"]["재원구분"],
        "예산액":float(c["raw_value"].replace(",", "")),"예산단위":c["raw_unit"],
        "출처페이지":group["page"],"객체ID":group["object_id"],"근거ID":c["source"]["evidence_id"],
        "evidence_text":f"{c['identity']['name']} {c['identity']['year']} 계 {c['raw_value']} {c['raw_unit']}"}
        for cid in group["candidate_ids"] for c in [prepared["targets"][cid]]]}


def fake_reader(prepared, calls=None):
    def reader(prompt, image, timeout, group):
        assert Path(image).is_file() and timeout > 0
        if calls is not None:
            calls.append(group["group_id"])
        return json.dumps(payload(prepared, group), ensure_ascii=False)
    return reader


def test_plan_and_prompt_hide_expected_amounts(inputs):
    prepared = prepare(*inputs)
    plan = prepared["plan"]
    assert plan["target_count"] == 4 and plan["group_count"] == 2
    assert plan["model"] == "gpt-6-luna" and plan["reasoning_override"] is False
    assert plan["budget"]["max_seconds"] == 300
    for g in plan["groups"]:
        prompt = prompt_for(prepared, g)
        assert all(c["raw_value"] not in prompt for c in prepared["targets"].values())
        assert "review_verdict" not in prompt and "expected_value" not in prompt


def test_actual_organizer_semantic_excel_audit_and_resume(inputs, tmp_path):
    prepared = prepare(*inputs)
    before = {p:sha256_file(p) for p in prepared["plan"]["input_hashes"]}
    calls = []
    output = tmp_path / "run"
    result = execute(prepared, output, reader=fake_reader(prepared, calls), fixture=True)
    assert len(calls) == 2 and result["model_requests"] == 0 and result["simulated_requests"] == 2
    assert result["new_rows"] == 4 and result["status_counts"] == {"saved_verified":4}
    assert result["exit_code"] == 0 and result["audit_saved"]
    assert result["baseline_cells_preserved"] and not result["model_accuracy_evaluated"]
    assert result["baseline_empty_cell_types_restored"] > 0
    audit = read_json(result["pipeline_audit_path"])
    records = audit["context"]["second_pass"]["records"]
    assert all(v["saved_cells"] for v in records.values())
    assert len(read_excel(result["excel_path"])["financial_plan"]) == 5
    for name, old in prepared["baseline_book"].items():
        assert _read_workbook(result["excel_path"])[name]["values"][:len(old["values"])] == old["values"]
    second = execute(prepared, output, resume=True, reader=lambda *a:pytest.fail("no repeated calls"), fixture=True)
    assert second["reused_responses"] == 2 and second["model_requests"] == 0
    assert second["new_rows"] == 4  # 매번 불변 기준 Excel에서 다시 구성, 누적 중복 없음
    assert len(read_excel(second["excel_path"])["financial_plan"]) == 5
    assert second["excel_path"] != result["excel_path"]
    assert {p:sha256_file(p) for p in before} == before


def test_page_number_is_metadata_not_a_measured_value():
    from utils.integration_audit import source_validation
    from utils.pdf_reader import PDFContent, PageContent
    data = {"financial_plan":[{"사업명":"Budget", "연도":2024,"출처페이지":213,"예산액":25702.0}]}
    content = PDFContent(213,[PageContent(213,"Budget 2024 25,702 백만원",[],[])],"")
    assert source_validation(data,content)[0]["status"] == "source_present"
    data["financial_plan"][0]["예산액"] = 999
    assert source_validation(data,content)[0]["unmatched_numeric_fields"] == ["예산액"]


@pytest.mark.parametrize("field,value,reason", [
    ("사업명","Other","business_mismatch"),("연도",2026,"year_mismatch"),
    ("재원구분","국비","funding_mismatch"),("출처페이지",2,"page_mismatch"),
    ("객체ID","other","evidence_mismatch"),("근거ID","other","evidence_mismatch"),
    ("예산액",True,"invalid_amount"),("예산액",999,"source_value_conflict"),
    ("예산단위","억원","unit_or_scale_mismatch"),("evidence_text","missing","amount_quote_missing"),
    ("status","unreadable","model_unreadable"),
])
def test_conflicts_not_promoted(inputs, field, value, reason):
    prepared = prepare(*inputs)
    group = prepared["plan"]["groups"][0]
    reply = payload(prepared, group)
    reply["results"][0][field] = value
    records = validate_response(prepared, group, json.dumps(reply, ensure_ascii=False))
    assert records[group["candidate_ids"][0]]["reason"] == reason
    assert records[group["candidate_ids"][0]]["status"] == "held"
    assert records[group["candidate_ids"][1]]["status"] == "validated_reading"


def test_response_duplicates_and_missing(inputs):
    prepared = prepare(*inputs)
    group = prepared["plan"]["groups"][0]
    response = payload(prepared, group)
    response["results"].append(deepcopy(response["results"][0]))
    records = validate_response(prepared, group, json.dumps(response))
    assert all(r["status"] == "validated_reading" for r in records.values())
    response["results"][-1]["예산액"] = 17
    records = validate_response(prepared, group, json.dumps(response))
    assert records[group["candidate_ids"][0]]["reason"] == "conflicting_duplicate_response"
    assert all(r["status"] == "held" for r in validate_response(prepared, group, '{"results":[]}').values())


def test_budget_partial_response_resume_only_unattempted(inputs, tmp_path):
    first = prepare(*inputs, max_calls=1)
    calls = []
    out = tmp_path / "limited"
    result = execute(first, out, reader=fake_reader(first, calls), fixture=True)
    assert len(calls) == 1 and result["new_rows"] == 2 and result["exit_code"] == 2
    assert result["status_counts"] == {"saved_verified":2,"deferred":2}
    expanded = prepare(*inputs, max_calls=2)
    result2 = execute(expanded, out, reader=fake_reader(expanded, calls), resume=True, fixture=True)
    assert len(calls) == 2 and result2["reused_responses"] == 1
    assert result2["new_rows"] == 4 and result2["exit_code"] == 0


def test_expired_cumulative_deadline_makes_no_calls(inputs, tmp_path):
    prepared = prepare(*inputs, max_seconds=1)
    ticks = iter(range(100))
    output = tmp_path / "deadline"
    result = execute(prepared, output, reader=lambda *a:pytest.fail("time budget exceeded"),
                     fixture=True, clock=lambda:next(ticks))
    assert result["model_requests"] == result["simulated_requests"] == result["new_rows"] == 0
    assert result["status_counts"] == {"deferred":4}
    resumed = execute(prepared, output, reader=lambda *a:pytest.fail("cumulative time budget exceeded"),
                      fixture=True, resume=True, clock=lambda:next(ticks))
    assert resumed["status_counts"] == {"deferred":4}


def test_interruption_retains_success_and_does_not_repeat_uncertain_call(inputs, tmp_path):
    prepared = prepare(*inputs)
    def reader(prompt, image, timeout, group):
        if group["page"] == 2:
            raise KeyboardInterrupt()
        return fake_reader(prepared)(prompt, image, timeout, group)
    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        execute(prepared, output, reader=reader, fixture=True)
    assert not (output / ".run.lock").exists()
    resumed = execute(prepared, output, reader=lambda *a:pytest.fail("no repeat after interruption"), fixture=True, resume=True)
    assert resumed["new_rows"] == 2 and resumed["reused_responses"] == 1
    assert resumed["status_counts"] == {"saved_verified":2,"held":2}


def test_conflicting_response_excluded_from_actual_excel(inputs, tmp_path):
    prepared = prepare(*inputs)
    def reader(prompt, image, timeout, group):
        response = payload(prepared, group)
        if group["page"] == 1:
            response["results"][0]["예산액"] = 99999
        response["results"].append(deepcopy(response["results"][-1]))  # 동일 근거 재반환은 한 행
        response["results"].append({"candidate_id":"not_selected","예산액":99999})
        return json.dumps(response)
    result = execute(prepared, tmp_path / "conflict", reader=reader, fixture=True)
    assert result["new_rows"] == 3 and result["status_counts"] == {"held":1,"saved_verified":3}
    assert all(row.get("예산액") != 99999 for row in read_excel(result["excel_path"])["financial_plan"])
    review = _read_workbook(result["excel_path"])[config.SHEET_KEY_TO_NAME["validation_report"]]
    assert any("source_value_conflict" in str(value) for row in review["values"] for value in row)


def test_review_version_metadata_compatible_but_changed_fact_rejected(inputs):
    manifest = read_json(inputs[0])
    review = Path(manifest["review_files"][0])
    data = read_json(review)
    data["records"][0]["candidate"]["schema_version"] = "older-review-rules"
    data["records"][0]["candidate"]["source"]["unit_origin"] = "older-explanation"
    write_json(review, data)
    assert prepare(*inputs)["plan"]["target_count"] == 4
    data["records"][0]["candidate"]["source"]["column"] += 1
    write_json(review, data)
    with pytest.raises(ValueError, match="검토 레코드"):
        prepare(*inputs)


def test_input_changed_after_plan_prevents_reading(inputs, tmp_path):
    prepared = prepare(*inputs)
    inputs[1].write_bytes(inputs[1].read_bytes() + b" ")
    with pytest.raises(ValueError, match="입력 파일 변경"):
        execute(prepared, tmp_path / "changed", reader=lambda *a:pytest.fail("input changed"), fixture=True)


def test_failed_group_not_retried_and_other_success_preserved(inputs, tmp_path):
    prepared = prepare(*inputs)
    def reader(prompt, image, timeout, group):
        if group["page"] == 1:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return fake_reader(prepared)(prompt, image, timeout, group)
    result = execute(prepared, tmp_path / "timeout", reader=reader, fixture=True)
    assert result["new_rows"] == 2 and result["exit_code"] == 2
    second = execute(prepared, tmp_path / "timeout", reader=lambda *a:pytest.fail("must not retry"), fixture=True, resume=True)
    assert second["new_rows"] == 2 and second["reused_responses"] == 1


def test_unapproved_or_tampered_targets_rejected(inputs):
    target_path, pdf = inputs
    original = read_json(target_path)
    bad = deepcopy(original)
    bad["targets"][0]["raw_value"] = "999"
    write_json(target_path, bad)
    with pytest.raises(ValueError, match="원장 대응 불일치"):
        prepare(target_path, pdf)
    write_json(target_path, original)
    review = Path(original["review_files"][0])
    contents = read_json(review)
    contents["records"][0]["review_verdict"] = "pending"
    write_json(review, contents)
    with pytest.raises(ValueError, match="미승인"):
        prepare(target_path, pdf)


@pytest.mark.parametrize("artifact", ["pdf", "excel", "audit", "journal"])
def test_bound_artifact_changes_rejected(inputs, artifact):
    selection, source = inputs
    summary = read_json(read_json(selection)["source_audit"])
    path = {"pdf":source,"excel":Path(summary["provenance"]["source_excel_path"]),
            "audit":Path(summary["provenance"]["source_pipeline_audit_path"]),
            "journal":Path(summary["missing_candidates_path"])}[artifact]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="해시 불일치"):
        prepare(*inputs)


def test_early_schema_check_before_any_model_call(inputs):
    selection, source = inputs
    original = read_json(selection)
    summary = read_json(original["source_audit"])
    excel = Path(summary["provenance"]["source_excel_path"])
    workbook = openpyxl.load_workbook(excel)
    workbook[config.SHEET_KEY_TO_NAME["document_meta"]].cell(1, 1, "wrong_header")
    workbook.save(excel)
    workbook.close()
    # 해시 검사도 먼저 차단한다. 저장 스키마 함수는 별도 실제 워크북으로 확인.
    from utils.second_pass import verify_schema
    with pytest.raises(ValueError, match="스키마 차이"):
        verify_schema(_read_workbook(excel))


def test_cleaner_removed_first_row_does_not_misassign_next(inputs, tmp_path, monkeypatch):
    prepared = prepare(*inputs)
    original = OrganizerAgent.organize
    def drop_first(self, data):
        result = original(self, data)
        result["financial_plan"] = result["financial_plan"][1:]
        return result
    monkeypatch.setattr(OrganizerAgent, "organize", drop_first)
    result = execute(prepared, tmp_path / "dropped", reader=fake_reader(prepared), fixture=True)
    assert result["new_rows"] == 3
    first = prepared["plan"]["groups"][0]["candidate_ids"][0]
    assert result["records"][first]["reason"] == "downstream_row_removed"
    assert result["status_counts"] == {"held":1,"saved_verified":3}


def test_audit_storage_failure_keeps_verified_excel(inputs, tmp_path, monkeypatch):
    prepared = prepare(*inputs)
    import utils.integration_audit
    monkeypatch.setattr(utils.integration_audit, "persist_audit", lambda *a:(_ for _ in ()).throw(OSError("fixture disk error")))
    result = execute(prepared, tmp_path / "disk", reader=fake_reader(prepared), fixture=True)
    assert Path(result["excel_path"]).is_file() and result["baseline_cells_preserved"]
    assert result["new_rows"] == 4 and result["exit_code"] == 2 and not result["audit_saved"]
    assert "감사 저장 실패" in result["warnings"][0]
    resumed = execute(prepared, tmp_path / "disk", reader=lambda *a:pytest.fail("no calls"), fixture=True, resume=True)
    assert resumed["excel_path"] != result["excel_path"]


def test_response_hash_corruption_and_mode_change_rejected(inputs, tmp_path):
    prepared = prepare(*inputs)
    output = tmp_path / "checkpoint"
    execute(prepared, output, reader=fake_reader(prepared), fixture=True)
    with pytest.raises(ValueError, match="모드 불일치"):
        execute(prepared, output, resume=True, reader=fake_reader(prepared), fixture=False)
    next((output / "responses").glob("*.json")).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="체크포인트 손상"):
        execute(prepared, output, resume=True, reader=fake_reader(prepared), fixture=True)


def test_cli_default_dry_run_no_call(inputs, tmp_path):
    command = [sys.executable, "-B", "scripts/run_second_pass.py", "--targets", str(inputs[0]),
               "--input", str(inputs[1]), "--output-dir", str(tmp_path / "cli")]
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    assert "모델 호출 0회" in result.stdout
    assert not (tmp_path / "cli/state.json").exists()


def test_transport_timeout_and_process_termination(tmp_path):
    from utils.second_pass_transport import bounded_process
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_process([sys.executable, "-c", "import time; time.sleep(30)"], "", cwd=tmp_path, timeout=.2)
    assert time.monotonic() - started < 10


def test_timeout_terminates_descendant_process(tmp_path):
    from utils.second_pass_transport import bounded_process
    pidfile = tmp_path / "child_pid.txt"
    helper = ("import subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              f"Path({str(pidfile)!r}).write_text(str(child.pid)); time.sleep(30)")
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_process([sys.executable, "-c", helper], "", cwd=tmp_path, timeout=1)
    assert pidfile.is_file(), "timeout fixture did not start descendant"
    pid = int(pidfile.read_text())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            assert code.value != 259, "descendant process still active"
    else:
        # 같은 프로세스 그룹으로 시작된 실제 자식도 종료됐는지 /proc에서 확인.
        status = Path(f"/proc/{pid}/stat")
        assert not status.exists() or status.read_text().split()[2] == "Z"


def test_codex_transport_isolated_prompt_and_no_model_overrides(tmp_path, monkeypatch):
    from utils import second_pass_transport as transport
    monkeypatch.setattr(transport.tempfile,"tempdir",str(tmp_path))
    monkeypatch.setattr(transport, "_codex_command", lambda value:["codex"])
    image = tmp_path / "table.png"
    image.write_bytes(b"fixture image")
    calls = []
    def run(command, prompt, cwd, timeout):
        calls.append((command, prompt, cwd))
        assert set(p.name for p in Path(cwd).iterdir()) == {"source_table.png"}
        return '{"results":[]}'
    monkeypatch.setattr(transport, "bounded_process", run)
    assert transport.CodexReader("gpt-6-luna", tmp_path)("prompt", image, 1, {}) == '{"results":[]}'
    command = calls[0][0]
    assert command[command.index("--model") + 1] == "gpt-6-luna"
    assert not any("reasoning" in item or "service_tier" in item for item in command)
    assert Path(calls[0][2]) != tmp_path


@pytest.mark.parametrize("inputs",["classification"],indirect=True)
def test_context_fields_and_evidence_reach_actual_excel(inputs,tmp_path):
    prepared=prepare(*inputs)
    result=execute(prepared,tmp_path/"context",reader=fake_reader(prepared),fixture=True)
    assert result["new_rows"]==4 and result["context_summary"]["fully_classified"]==4
    assert all(r["classification_saved_verified"] for r in result["records"].values())
    book=_read_workbook(result["excel_path"])[config.SHEET_KEY_TO_NAME["financial_plan"]]
    rows=[dict(zip(book["headers"],v)) for v in book["values"]]
    assert all(row["부문"]=="수송" and row["계획구분"]=="온실가스감축대책" for row in rows if row["사업명"]=="Task2")
    assert all("context_field" in row["출처계보"] for row in rows[1:])
    ledger=read_json(result["context_audit_path"])
    assert ledger["summary"]["fully_classified"]==4


@pytest.mark.parametrize("inputs",["classification_conflict"],indirect=True)
def test_preexisting_context_is_preserved_and_conflict_is_explicit(inputs,tmp_path):
    prepared=prepare(*inputs)
    result=execute(prepared,tmp_path/"context_conflict",reader=fake_reader(prepared),fixture=True)
    first=prepared["plan"]["groups"][0]["candidate_ids"]
    assert all(result["records"][cid]["context_resolution"]["fields"]["부문"]["status"]=="preserved_with_conflict" for cid in first)
    book=_read_workbook(result["excel_path"])[config.SHEET_KEY_TO_NAME["financial_plan"]]
    rows=[dict(zip(book["headers"],v)) for v in book["values"]]
    assert all(row["부문"]=="건물" for row in rows if row["사업명"]=="Task1")
    assert result["context_summary"]["rows_with_conflict"]==2


def legacy_run(prepared,directory):
    """현재 fixture를 과거 지문 계약으로 고정. 분류 코드 변경 뒤 응답 재사용 재현."""
    from utils.second_pass import digest
    execute(prepared,directory,reader=fake_reader(prepared),fixture=True)
    plan=read_json(directory/"plan.json"); plan["version"]="confirmed-second-pass-v1"; plan["runtime_sha256"]="older-implementation"
    plan["fingerprint"]=digest({k:v for k,v in plan.items() if k not in {"budget","fingerprint"}})
    state=read_json(directory/"state.json"); state["fingerprint"]=plan["fingerprint"]
    for group in plan["groups"]:
        response_path=directory/"responses"/(group["group_id"]+".json")
        response=read_json(response_path); response["fingerprint"]=plan["fingerprint"]
        write_json(response_path,response)
        state["attempts"][group["group_id"]]["sha256"]=sha256_file(response_path)
    write_json(directory/"plan.json",plan); write_json(directory/"state.json",state)
    return directory


@pytest.mark.parametrize("inputs",["classification"],indirect=True)
def test_explicit_legacy_response_replay_no_new_calls_and_no_source_writes(inputs,tmp_path):
    from utils.second_pass import replay_saved
    prepared=prepare(*inputs); source=legacy_run(prepared,tmp_path/"source")
    hashes={str(p):sha256_file(p) for p in source.rglob('*') if p.is_file()}
    result=replay_saved(prepared,source,tmp_path/"replay")
    assert result["model_requests"]==0 and result["reused_responses"]==2 and result["new_rows"]==4
    assert result["context_summary"]["fully_classified"]==4 and result["execution_mode"]=="saved_response_replay"
    assert hashes=={str(p):sha256_file(p) for p in source.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match="전용 폴더"):
        execute(prepared,tmp_path/"replay",reader=lambda *a:pytest.fail("no calls"),fixture=True,resume=True)


@pytest.mark.parametrize("artifact",["response","image","prompt","plan","input"])
def test_replay_rejects_tampering_or_changed_contract(inputs,tmp_path,artifact):
    from utils.second_pass import replay_saved,digest
    prepared=prepare(*inputs); source=legacy_run(prepared,tmp_path/"source")
    gid=prepared["plan"]["groups"][0]["group_id"]
    if artifact in {"response","image"}:
        path=source/("responses" if artifact=="response" else "images")/(gid+(".json" if artifact=="response" else ".png"))
        path.write_bytes(path.read_bytes()+b" ")
    elif artifact=="prompt":
        state=read_json(source/"state.json"); state["attempts"][gid]["prompt_sha256"]="wrong"; write_json(source/"state.json",state)
    elif artifact=="plan":
        plan=read_json(source/"plan.json"); plan["model"]="another-model"; write_json(source/"plan.json",plan)
    else:
        inputs[1].write_bytes(inputs[1].read_bytes()+b" ")
    with pytest.raises(ValueError):replay_saved(prepared,source,tmp_path/"blocked")


def test_replay_requires_distinct_output_folder(inputs,tmp_path):
    from utils.second_pass import replay_saved
    prepared=prepare(*inputs); source=legacy_run(prepared,tmp_path/"source")
    with pytest.raises(ValueError,match="기존 실행 폴더"):
        replay_saved(prepared,source,source/"child")
