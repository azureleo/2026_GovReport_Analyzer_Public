"""Freeze successful responses; replay target cleaning and retry only failed batches.

This is an incremental repair of a saved run, not a new full-document extraction.
The immutable baseline Excel (including Vision results) is never re-cleaned.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import config
from agents.extractor_agent import ExtractorAgent, PartialExtractionError, _build_page_text, _SHEET_CONFIGS
from agents.organizer_agent import OrganizerAgent
from agents.excel_agent import ExcelAgent
from utils.document_objects import DocumentObject
from utils.integration_audit import persist_audit, source_validation
from utils.pdf_reader import PDFContent, PageContent, _extract_tables_from_page
from utils.quality_adapters import excel_value
from utils.run_state import FAILED_BATCH_STATUSES, RunState, sha256_json
from utils.second_pass import (_read_workbook, digest, read_json, require, run_lock,
                               sha256_file, verify_schema, write_json, preserve_baseline_cell_types, runtime_hash)

VERSION = "saved-extraction-recovery-v1"
ROOT = Path(__file__).resolve().parents[1]


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _business_fields(sheet):
    return [key for key in config.EXCEL_HEADERS[config.SHEET_KEY_TO_NAME[sheet]]
            if key not in config.PROVENANCE_HEADERS]


def _payload(sheet, row):
    return {key: excel_value(row.get(key)) for key in _business_fields(sheet)}


def _saved_data(book):
    return {key: [dict(zip(book[name]["headers"], values)) for values in book[name]["values"]]
            for key, name in config.SHEET_KEY_TO_NAME.items()}


def _read_ledger(path):
    records = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            require(isinstance(record, dict) and record.get("batch_id"), "batch_id 없음")
            require(record.get("kind") == "sheet" and len(record.get("sheet_keys", [])) == 1,
                    "첫 버전은 시트별 텍스트 배치만 지원합니다.")
            require(record["sheet_keys"][0] in config.EXTRACTION_SHEETS, "알 수 없는 시트")
            require(record.get("status") in FAILED_BATCH_STATUSES | {"ok"}, "알 수 없는 배치 상태")
            require(isinstance(record.get("result"), list) and
                    all(isinstance(row, dict) for row in record["result"]), "행 배열 형식 오류")
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"원본 원장 {number}행 오류: {error}") from error
        # RunState is an append-only journal; the last record is authoritative.
        records[record["batch_id"]] = record
    require(bool(records), "빈 배치 원장")
    return records


def prepare(baseline_excel, *, pdf=None, state_dir=None, extraction_prompts=None):
    excel = Path(baseline_excel).resolve()
    manifest_path = excel.with_name(excel.stem + "_run_manifest.json")
    audit_path = excel.with_name(excel.stem + "_pipeline_audit.json")
    manifest, audit = read_json(manifest_path), read_json(audit_path)
    pdf = Path(pdf or manifest["input_path"]).resolve()
    state = Path(state_dir or manifest["state_directory"]).resolve()
    ledger_path = state / "ledger.jsonl"
    state_manifest = state / "run_manifest.json"
    require(sha256_file(excel) == manifest["output_sha256"], "기준 Excel 해시 불일치")
    require(sha256_file(pdf) == manifest["input_sha256"] == audit["context"]["input_sha256"], "PDF 해시 불일치")
    original_state = read_json(state_manifest)
    require(all(original_state.get(key) == manifest.get(key) for key in
                ("run_id", "input_sha256", "output_sha256", "prompt_sha256", "config", "execution_info")),
            "실행 매니페스트와 배치 원장 실행이 다릅니다.")
    if extraction_prompts is None:
        # Markdown loading is deterministic and makes no model call. HWP
        # regeneration is deliberately not attempted in a recovery run.
        from agents.guideline_agent import GuidelineAgent
        prompts = GuidelineAgent().get_all_prompts()
    else:
        prompts = extraction_prompts
    require(sha256_json(prompts) == manifest["prompt_sha256"],
            "기준 보조 지침 불일치: 당시 extraction_prompts JSON을 지정하세요.")
    records = _read_ledger(ledger_path)
    logged = {row["batch_id"]: row for row in audit["ledgers"]["extraction_records"]}
    require(set(records) == set(logged), "감사 파일과 원장 배치 목록 불일치")
    for bid, record in records.items():
        require(record["sheet_keys"] == [logged[bid]["sheet_key"]] and
                record["page_nums"] == logged[bid]["page_nums"] and
                record["status"] == logged[bid]["status"], "감사/원장 배치 대응 오류: " + bid)
    book = _read_workbook(excel)
    verify_schema(book)
    data = _saved_data(book)
    city = next(row["지자체명"] for row in data["document_meta"] if row.get("지자체명"))
    files = [excel, manifest_path, audit_path, pdf, ledger_path, state_manifest]
    plan = {"version": VERSION, "baseline_excel": str(excel), "pdf": str(pdf),
            "input_hashes": {str(path): sha256_file(path) for path in files},
            "baseline_run_id": manifest["run_id"], "successful_batches_frozen": sum(r["status"] == "ok" for r in records.values()),
            "failed_batch_ids": sorted(bid for bid, r in records.items() if r["status"] in FAILED_BATCH_STATUSES),
            "provider": manifest["execution_info"]["text_backend"],
            "model": manifest["execution_info"]["text_model"], "reasoning_override": False,
            "recovery_policy": "one_attempt_per_failed_batch; successful_text_and_vision_never_recalled",
            "scope": "append_verified_missing_facts; baseline_cells_preserved; no_full_raw_reconstruction"}
    plan["fingerprint"] = digest(plan)
    return {"plan": plan, "manifest": manifest, "audit": audit, "book": book, "data": data,
            "city": city, "records": records, "logged": logged, "prompts": prompts}


def verify_inputs(prepared):
    require(all(sha256_file(path) == value for path, value in prepared["plan"]["input_hashes"].items()),
            "복구 준비 후 기준 파일이 변경됐습니다.")


def _pages(pdf, numbers):
    import fitz
    output = []
    with fitz.open(pdf) as document:
        for number in sorted(set(numbers)):
            require(type(number) is int and 1 <= number <= len(document), "페이지 범위 오류")
            page = document[number - 1]
            output.append(PageContent(number, page.get_text("text", sort=True), _extract_tables_from_page(page), []))
        total = len(document)
    return PDFContent(total, output, "")


def batch_request(prepared, batch_id, content):
    record = prepared["records"][batch_id]
    audit = prepared["logged"][batch_id]
    sheet = record["sheet_keys"][0]
    pages = {p.page_number: p for p in content.pages}
    text = _build_page_text([pages[n] for n in record["page_nums"]])
    require(_hash_text(text) == audit.get("input_sha256"), "원본 배치 입력 텍스트 해시 불일치: " + batch_id)
    request = json.dumps({"prompt": prepared["prompts"].get(sheet, ""), "source": audit["source"],
        "municipality": prepared["city"], "schema_prompt": _SHEET_CONFIGS[sheet]["prompt"]}, ensure_ascii=False, sort_keys=True)
    fingerprint = _hash_text(sheet + text + request)
    # Recompute the old ID without opening or rewriting the old RunState.
    state = RunState.__new__(RunState)
    state.run_id = prepared["plan"]["baseline_run_id"]
    expected_id = state.batch_id(kind="sheet", sheet_keys=[sheet], page_nums=record["page_nums"],
                                 batch_text=text, request_fingerprint=fingerprint)
    require(expected_id == batch_id, "원본 배치 지문 불일치: 시트 지침·입력·지역·출처가 달라졌습니다.")
    obj = DocumentObject(audit["object_id"], "text", record["page_nums"][0], 1, text=text,
        metadata={"engine": audit.get("engine", "native"), "evidence_id": audit.get("evidence_id")})
    return sheet, text, obj


def _merge_new(data, sheet, rows, content, decisions):
    from utils.evidence_merge import ENTITY_FIELDS
    fields = ENTITY_FIELDS.get(sheet, ())
    for row, check in zip(rows, source_validation({sheet: rows}, content)):
        record = {"sheet": sheet, "row": deepcopy(row), "source_check": check}
        if sheet == "annual_implementation" and row.get("목표물량") is None:
            plan_text = str(row.get("연간계획") or "").strip()
            if plan_text.casefold() in {"", "-", "—", "미정", "없음", "n/a", "null"}:
                record.update(status="held", reason="no_measured_or_qualitative_plan")
                decisions.append(record)
                continue
            source_text = "".join(p.text for p in content.pages if p.page_number in check["pages"])
            if "".join(plan_text.split()) not in "".join(source_text.split()):
                record.update(status="held", reason="qualitative_plan_source_unconfirmed")
                decisions.append(record)
                continue
        # One unsupported number must not prevent a separately confirmed target
        # cell from surviving. Preserve the full response in the decision and
        # leave just the unconfirmed numeric fields blank in the appended row.
        if sheet == "reduction_targets" and check["status"] == "numeric_review":
            record["original_row"] = deepcopy(row)
            record["held_fields"] = {key: row[key] for key in check["unmatched_numeric_fields"]}
            row = deepcopy(row)
            for key in record["held_fields"]:
                row[key] = None
            record["row"] = row
            check = source_validation({sheet: [row]}, content)[0]
            if not any(row.get(key) is not None for key in ("기준배출량", "배출전망", "목표감축량", "목표배출량")):
                record.update(status="held", reason="no_confirmed_target_value")
                decisions.append(record)
                continue
        payload = _payload(sheet, row)
        duplicates = [n for n, old in enumerate(data[sheet], 2) if _payload(sheet, old) == payload]
        if duplicates:
            record.update(status="already_saved", excel_rows=duplicates)
        elif check["status"] != "source_present":
            record.update(status="held", reason="source_presence_unconfirmed")
        else:
            entity = lambda r: tuple(r.get(key) or None for key in fields)
            conflicts = [n for n, old in enumerate(data[sheet], 2) if fields and entity(old) == entity(row)]
            if conflicts:
                record.update(status="held", reason="existing_entity_payload_conflict", excel_rows=conflicts)
            else:
                data[sheet].append(deepcopy(row))
                record.update(status="added", excel_row=len(data[sheet]) + 1)
        decisions.append(record)


def execute(prepared, output_dir, *, recover_failed=False, reader=None, max_calls=1, timeout=300):
    """Successes are never called. Attempts and raw replies persist before parsing.

    reader(prompt, *, system, timeout) is the only external model boundary.
    Repeated executions reconstruct from the same immutable baseline and reuse
    saved replies. A crash during a call is held, never retried automatically.
    """
    require(max_calls >= 0 and timeout > 0, "호출 상한/타임아웃 오류")
    directory = Path(output_dir).resolve()
    require(directory != Path(prepared["plan"]["baseline_excel"]).parent, "전용 출력 폴더를 사용하세요.")
    directory.mkdir(parents=True, exist_ok=True)
    plan_path = directory / "plan.json"
    if plan_path.exists():
        require(read_json(plan_path)["fingerprint"] == prepared["plan"]["fingerprint"], "다른 기준 실행의 출력 폴더")
    else:
        require(not any(directory.iterdir()), "이미 파일이 있는 출력 폴더")
        write_json(plan_path, prepared["plan"])
    with run_lock(directory):
        verify_inputs(prepared)
        checkpoint = directory / "state.json"
        state = read_json(checkpoint) if checkpoint.exists() else {"attempts": {}}
        data = deepcopy(prepared["data"])
        data["municipality_name"] = prepared["city"]
        targets = [deepcopy(row) for r in prepared["records"].values()
                   if r["sheet_keys"] == ["reduction_targets"] for row in r["result"]]
        needed_pages = set()
        from utils.quality_adapters import pages as parse_pages
        for row in targets:
            numbers, error = parse_pages(row.get("출처페이지"))
            if not error:
                needed_pages.update(numbers)
        for bid in prepared["plan"]["failed_batch_ids"]:
            needed_pages.update(prepared["records"][bid]["page_nums"])
        content = _pages(prepared["plan"]["pdf"], needed_pages)
        decisions, batches, calls, reused = [], [], 0, 0
        clean = OrganizerAgent().organize({"municipality_name": prepared["city"], "reduction_targets": targets})
        _merge_new(data, "reduction_targets", clean["reduction_targets"], content, decisions)
        added_diagnostics = list(clean.get("validation_report", []))
        for bid in prepared["plan"]["failed_batch_ids"]:
            previous = prepared["records"][bid]
            batch = {"batch_id": bid, "sheet": previous["sheet_keys"][0], "pages": previous["page_nums"], "status": "deferred"}
            batches.append(batch)
            attempt = state["attempts"].get(bid)
            if not attempt and (not recover_failed or calls >= max_calls):
                continue
            sheet, text, obj = batch_request(prepared, bid, content)
            extractor = ExtractorAgent()
            reply = directory / (bid + "_response.json")
            def transport(prompt, *, system):
                nonlocal calls, reused
                binding = digest({"plan": prepared["plan"]["fingerprint"], "batch": bid, "prompt": prompt, "system": system})
                if attempt:
                    require(attempt.get("status") == "response_saved" and reply.is_file(), "기시도/중단 호출: 자동 재호출하지 않습니다.")
                    require(sha256_file(reply) == attempt["sha256"], "저장 응답 해시 불일치")
                    saved = read_json(reply)
                    require(saved["binding"] == binding, "저장 응답의 요청 지문 불일치")
                    reused += 1
                    return saved["text"]
                require(recover_failed and reader is not None, "실제 판독 경계가 없습니다.")
                state["attempts"][bid] = {"status": "attempting", "binding": binding, "timeout": timeout}
                write_json(checkpoint, state)
                calls += 1
                response = reader(prompt, system=system, timeout=timeout)
                require(isinstance(response, str), "문자열 응답이 아닙니다.")
                write_json(reply, {"binding": binding, "text": response})
                state["attempts"][bid].update(status="response_saved", sha256=sha256_file(reply))
                write_json(checkpoint, state)
                return response
            try:
                rows = extractor._extract_sheet(sheet, text, prepared["city"], prepared["prompts"].get(sheet, ""),
                                                strict=True, force=True, reader=transport)
                batch["status"] = "read_success"
            except PartialExtractionError as error:
                rows = error.rows
                batch.update(status="partial", error=str(error), invalid_rows=error.invalid_rows)
            except Exception as error:
                rows = []
                batch.update(status="failed", error=str(error))
            rows = extractor._attach_sources(rows, obj, prepared["logged"][bid]["source"], previous["page_nums"])
            # Keep valid partial rows from the original failed batch too.
            rows = extractor._merge_rows(sheet, previous["result"], rows)
            cleaned = OrganizerAgent().organize({"municipality_name": prepared["city"], sheet: rows})
            from utils.semantic_routing import validate_and_reclassify
            semantic = validate_and_reclassify(cleaned, content, auto_reclassify=False)
            added_diagnostics.extend(semantic.validation_issues(prepared["city"]))
            added_diagnostics.extend(cleaned.get("validation_report", []))
            start = len(decisions)
            _merge_new(data, sheet, cleaned[sheet], content, decisions)
            batch["rows_read"] = len(rows)
            batch["decisions"] = dict(Counter(r["status"] for r in decisions[start:]))
        for record in decisions:
            if record["status"] == "held" or record.get("held_fields"):
                added_diagnostics.append({"지자체명": prepared["city"], "대상시트": record["sheet"],
                    "항목": "저장 응답 복구 보류", "사유": record.get("reason", "unconfirmed_numeric_fields") +
                    (": " + json.dumps(record["held_fields"], ensure_ascii=False) if record.get("held_fields") else ""),
                    "출처페이지": record["row"].get("출처페이지")})
        data.setdefault("validation_report", []).extend(added_diagnostics)
        iteration = len(list(directory.glob("recovered_*.xlsx"))) + 1
        excel = directory / f"recovered_{iteration:03}.xlsx"
        verify_inputs(prepared)
        ExcelAgent().write(data, excel)
        preserve_baseline_cell_types(prepared["plan"]["baseline_excel"], excel)
        saved = _read_workbook(excel)
        for name, sheet in prepared["book"].items():
            require(saved[name]["values"][:len(sheet["values"])] == sheet["values"], "기준 셀 보존 실패: " + name)
        for record in decisions:
            if record["status"] != "added":
                continue
            sheet = record["sheet"]
            table = saved[config.SHEET_KEY_TO_NAME[sheet]]
            stored = dict(zip(table["headers"], table["values"][record["excel_row"] - 2]))
            require(_payload(sheet, stored) == _payload(sheet, record["row"]), "신규 업무 셀 저장 불일치")
            record["status"] = "saved_verified"
        summary = {"version": VERSION, "recovery_implementation_sha256": runtime_hash(),
            "baseline": prepared["plan"]["baseline_excel"],
            "excel_path": str(excel), "excel_sha256": sha256_file(excel), "baseline_cells_preserved": True,
            "successful_batches_frozen": prepared["plan"]["successful_batches_frozen"],
            "model_requests_this_execution": calls, "reused_recovery_responses": reused,
            "vision_requests": 0, "model": prepared["plan"]["model"], "reasoning_override": False,
            "batches": batches, "decision_counts": dict(Counter(r["status"] for r in decisions)),
            "decisions": decisions, "model_accuracy_evaluated": False,
            "evaluation": {"status": "unassessed", "reason": "저장 응답 연결·복구 범위 검증; 전체 정확도 재측정 아님"}}
        summary["exit_code"] = 2 if any(r["status"] in {"deferred", "partial", "failed"} for r in batches) or any(
            r["status"] == "held" or r.get("held_fields") for r in decisions) else 0
        # Original warning records stay as history; resolved failures are listed
        # separately rather than erasing the baseline validation sheet.
        summary["baseline_warnings_preserved_as_history"] = True
        for key, value in prepared["audit"]["ledgers"].items():
            if key in config.INTERNAL_OBJECT_KEYS:
                data[key] = deepcopy(value)
        summary["pipeline_audit_path"] = str(persist_audit(excel, prepared["plan"]["pdf"], data,
            {"input_sha256": prepared["manifest"]["input_sha256"], "saved_recovery": summary,
             "warnings": ["기준 실행의 내부 원장은 당시 기록이며, 복구 상태는 saved_recovery를 확인하세요."]}))
        outcome = excel.with_name(excel.stem + "_recovery.json")
        write_json(outcome, summary)
        verify_inputs(prepared)
        return summary


class CodexTextReader:
    """Same model/system/prompt/CLI reasoning defaults as the baseline; no retries."""
    def __init__(self, model, executable=None):
        from utils.second_pass_transport import _codex_command
        self.command = _codex_command(executable)
        self.model = model

    def __call__(self, prompt, *, system, timeout):
        import tempfile
        from utils.llm_client import _agent_prompt
        from utils.second_pass_transport import bounded_process
        with tempfile.TemporaryDirectory(prefix="saved-recovery-") as folder:
            last = Path(folder) / "last_message.txt"
            command = [*self.command, "exec", "--sandbox", "read-only", "--cd", str(ROOT), "--ephemeral",
                       "--color", "never", "--output-last-message", str(last), "--model", self.model, "-"]
            stdout = bounded_process(command, _agent_prompt(prompt, system), cwd=ROOT, timeout=timeout)
            return last.read_text(encoding="utf-8").strip() if last.is_file() else stdout
