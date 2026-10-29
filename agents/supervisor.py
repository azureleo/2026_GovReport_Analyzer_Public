"""
에이전트 5: 감독·검수 에이전트

전체 파이프라인을 조율하고 결과의 품질을 검수합니다.
carbon_guideline.md 기반 16개 시트 구조에 맞춰 동작합니다.
"""

import json
import logging
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import config
from utils.pdf_reader import extract_pdf, PDFContent
from utils.hwp_reader import extract_hwp, is_hwp_file
from utils import llm_client
from agents.guideline_agent import GuidelineAgent
from agents.extractor_agent import ExtractorAgent
from agents.image_agent import ImageAgent
from agents.organizer_agent import OrganizerAgent
from agents.excel_agent import ExcelAgent
from utils.integration_audit import (audit_object_evidence, body_rows, document_hash, evidence_ids, object_inventory,
                                     ocr_directory_hash, persist_audit, save_json, source_validation)

logger = logging.getLogger(__name__)

QUALITY_SYSTEM = """당신은 저장 후 검사 결과를 설명하는 보조 검토자입니다.
검사에서 확인한 데이터 문제와 검증 근거의 한계를 구분하세요.
근거 없는 전체 등급이나 사실 정확도를 추정하지 말고 반드시 JSON만 반환하세요."""


def _quality_score(final_data: dict) -> tuple[float, list[str]]:
    """기존 재시도 조건용 최소 구조 검사. 최종 품질/정확도 점수가 아니다."""
    issues = []
    score = 100.0

    municipality = final_data.get("municipality_name", "알 수 없음")
    if not municipality or municipality == "알 수 없음":
        issues.append("지자체명 미확인")
        score -= 20

    # 핵심 시트 데이터 존재 확인
    emissions = final_data.get("emissions_regional", []) + final_data.get("emissions_management", [])
    if not emissions:
        issues.append("온실가스 배출 데이터 없음 (중요)")
        score -= 25

    targets = final_data.get("reduction_targets", [])
    if not targets:
        issues.append("감축목표 데이터 없음")
        score -= 15

    projects = final_data.get("mitigation_projects", [])
    if not projects:
        issues.append("감축사업 목록 없음")
        score -= 10

    forecast = final_data.get("emissions_forecast", [])
    if not forecast:
        issues.append("배출전망 데이터 없음")
        score -= 5

    regional = final_data.get("regional_conditions", [])
    if not regional:
        issues.append("지역여건 데이터 없음")
        score -= 5

    vision = final_data.get("vision_strategy", [])
    if not vision:
        issues.append("비전·전략 없음")
        score -= 5

    # 채움률 확인
    total_rows = sum(len(final_data.get(k, [])) for k in config.EXTRACTION_SHEETS)
    if total_rows < 10:
        issues.append(f"전체 추출 행 수 매우 적음 ({total_rows}행)")
        score -= 15

    return max(0.0, score), issues


class Supervisor:
    """에이전트 5: 감독·검수 에이전트"""

    QUALITY_THRESHOLD = 40.0

    def __init__(self):
        self._reports: list[str] = []
        self.last_outcome: dict = {}
        self._ocr_response_cache: dict = {}

    def _supplement_ocr(self, document, objects, raw_data, extractor, prompts, states):
        """Post-native extraction: only eligible changed OCR objects reach related sheets."""
        if not config.SELECTIVE_OCR_ENABLED:
            return objects
        from utils.selective_ocr import build_triage_plan, get_ocr_backend, merge_document_objects
        from utils.evidence_merge import merge_extraction_rows
        from utils.ocr_pipeline import select_supplement_objects

        audit = {"enabled": True, "backend": config.OCR_BACKEND, "loaded_objects": 0,
                 "reading_success_objects": 0, "extraction_success_objects": 0,
                 "body_reflected_objects": 0, "diagnostics": [], "review_objects": []}
        raw_data["ocr_import_audit"] = audit
        raw_data["ocr_reextraction_audit"] = []
        try:
            plan = build_triage_plan(objects, backend=config.OCR_BACKEND,
                                     confidence_threshold=config.OCR_NATIVE_CONFIDENCE_THRESHOLD,
                                     page_texts={p.page_number: p.text for p in document.pages})
            audit["triage"] = [decision.to_dict() for decision in plan]
            backend = get_ocr_backend(config.OCR_BACKEND, config.OCR_RESULTS_DIR)
            if getattr(backend, "uses_vision", False):
                audit["diagnostics"].append({"code": "directory_import_only", "message": "VLM remains on the existing ImageAgent path; no automatic OCR fallback."})
                return objects
            loaded = backend.load(document, plan)
            audit["loaded_objects"] = len(loaded)
            audit["diagnostics"].extend(getattr(backend, "diagnostics", []))
            audit["review_objects"] = getattr(backend, "review_objects", [])
            merged = merge_document_objects(objects, loaded)
            selected = select_supplement_objects(objects, merged)
            audit["reading_success_objects"] = len(selected)
            for obj in selected:
                state = {"status": "loaded", "reading_success": True, "business_extracted": False,
                         "body_reflected": False, "evidence_id": obj.metadata.get("evidence_id", obj.object_id)}
                states[obj.object_id] = state
                try:
                    fingerprint = obj.metadata.get("ocr_extraction_fingerprint", obj.object_id)
                    before = len(getattr(extractor, "extraction_records", []))
                    if fingerprint in self._ocr_response_cache:
                        rows = deepcopy(self._ocr_response_cache[fingerprint])
                        state["response_reused"] = True
                    else:
                        rows = extractor.extract_objects([obj], raw_data.get("municipality_name", ""), prompts)
                        errors = [r for r in extractor.extraction_records[before:]
                                  if r.get("status") not in {"ok", "skipped", "reused"}]
                        if errors:
                            state["failure_records"] = deepcopy(errors)
                        else:
                            self._ocr_response_cache[fingerprint] = deepcopy(rows)
                    keys = [k for k in config.EXTRACTION_SHEETS if rows.get(k)]
                    count = sum(len(rows.get(k, [])) for k in keys)
                    state.update(status="business_extracted" if count else "extraction_empty",
                                 business_extracted=bool(count), sheets=keys, candidate_rows=count)
                    if state.get("failure_records"):
                        state["status"] = "partial_extraction" if count else "reextraction_failed"
                    for key in keys:
                        raw_data[key] = merge_extraction_rows(key, raw_data.get(key, []), rows[key],
                                                             raw_data.setdefault("merge_conflicts", []))
                    if count:
                        audit["extraction_success_objects"] += 1
                except Exception as error:
                    state.update(status="reextraction_failed", error=str(error))
                    self._log(f"[OCR 경고] 객체 {obj.object_id} 재추출 실패: {error}")
                raw_data["ocr_reextraction_audit"].append({"object_id": obj.object_id,
                                                          "page": obj.page_number, **deepcopy(state)})
            self._log(f"[OCR 연결] 로드 {len(loaded)} / 변경 판독 {len(selected)} / 업무 추출 {audit['extraction_success_objects']}")
            return merged
        except Exception as error:
            audit["diagnostics"].append({"code": "ocr_import_failed", "message": str(error)})
            self._log(f"[OCR 경고] 기존 추출본 보존, OCR 연결 실패: {error}")
            return objects

    def _log(self, msg: str):
        print(msg)
        self._reports.append(msg)

    def _llm_quality_review(self, final_data: dict) -> dict:
        from utils.final_review import build_review_context, review_prompt
        municipality = final_data.get("municipality_name", "?")
        sheet_counts = {k: len(final_data.get(k, [])) for k in config.EXTRACTION_SHEETS if final_data.get(k)}
        review_context = getattr(self, "_final_review_context", None) or build_review_context(
            None, {"status": "unassessed", "reason": "저장 후 검사 정보 미제공"})
        prompt = review_prompt(municipality, sheet_counts, review_context)
        resp = llm_client.call_text(prompt, system=QUALITY_SYSTEM)
        return llm_client.parse_json(resp)

    def run(
        self,
        input_path: str | Path = None,
        output_path: str | Path = None,
        hwp_path: str | None = None,
        max_pipeline_retries: int = 2,
        include_images: bool = True,
        pdf_path: str | Path = None,
    ) -> Path:
        if input_path is None and pdf_path is not None:
            input_path = pdf_path
        if input_path is None:
            raise ValueError("입력 파일 경로를 지정해주세요.")

        input_path = Path(input_path)
        output_path = Path(output_path)
        self._ocr_response_cache = {}
        output_path.parent.mkdir(parents=True, exist_ok=True)

        file_type = "HWP" if is_hwp_file(input_path) else "PDF"

        self._log("=" * 60)
        self._log("[감독관] 파이프라인 시작 (가이드라인 기반 16개 시트)")
        self._log(f"  - 입력 {file_type}: {input_path}")
        self._log(f"  - 출력 경로: {output_path}")
        self._log("=" * 60)

        # STEP 0: 문서 파싱
        self._log(f"\n[감독관] STEP 0: {file_type} 파싱 중...")
        t0 = time.time()
        if is_hwp_file(input_path):
            pdf_content: PDFContent = extract_hwp(input_path)
            for page in pdf_content.pages:
                page.metadata["engine"] = "kordoc"
        else:
            pdf_content: PDFContent = extract_pdf(input_path, render_graph_pages=include_images)
        self._log(f"[감독관] {file_type} 파싱 완료: {pdf_content.total_pages}페이지 ({time.time()-t0:.1f}초)")
        from utils.document_objects import build_document_objects, consolidate_document_objects
        native_objects = build_document_objects(pdf_content.pages)
        if config.PHYSICAL_OBJECT_MERGE_ENABLED:
            native_objects = consolidate_document_objects(native_objects)
        states = {o.object_id: {"status": "native_parsed"} for o in native_objects}
        context = {"input_sha256": document_hash(input_path), "model_accuracy_evaluated": False,
                   "features": {"selective_ocr": config.SELECTIVE_OCR_ENABLED,
                                "object_batching": config.EXTRACTION_OBJECT_BATCHING_ENABLED,
                                "semantic_routing": config.SEMANTIC_ROUTING_ENABLED},
                   "warnings": []}
        coverage_capture = None
        coverage_attempts = []
        coverage_source_objects = None
        if config.SOURCE_COVERAGE_MODE == "audit":
            try:
                from utils.source_coverage import source_snapshot
                coverage_source_objects = source_snapshot(native_objects)
            except (OSError, TypeError, ValueError) as error:
                context["warnings"].append(f"원문 감사 파싱 스냅샷 실패: {error}")
            context["source_coverage"] = {"mode": "audit", "status": "pending",
                                          "native_object_count": len(native_objects)}

        # STEP 1: 가이드라인 (carbon_guideline.md 우선, HWP fallback)
        self._log("\n[감독관] STEP 1: 가이드라인 에이전트 실행...")
        guideline_agent = GuidelineAgent(hwp_path=hwp_path)
        extraction_prompts = guideline_agent.get_all_prompts()
        self._log(guideline_agent.report())
        run_state = None
        if config.RUN_STATE_ENABLED:
            from utils.run_state import RunState
            run_state = RunState.create(input_path=input_path, guideline_path=hwp_path,
                                        extraction_prompts=extraction_prompts,
                                        execution_info={"text_backend": config.LLM_PROVIDER,
                                                        "text_model": config.LOCAL_AGENT_MODEL or config.MODEL,
                                                        "ocr_enabled": config.SELECTIVE_OCR_ENABLED,
                                                        "ocr_backend": config.OCR_BACKEND,
                                                        "ocr_results_sha256": ocr_directory_hash(config.OCR_RESULTS_DIR) if config.SELECTIVE_OCR_ENABLED else "",
                                                        "native_confidence_threshold": config.OCR_NATIVE_CONFIDENCE_THRESHOLD},
                                        output_path=output_path, resume=config.EXTRACTION_RESUME,
                                        retry_failed_only=config.EXTRACTION_RETRY_FAILED_ONLY)

        # STEP 2~3: 추출·정제 (품질 미달 시 재시도)
        final_data = None
        for attempt in range(1, max_pipeline_retries + 1):
            self._log(f"\n[감독관] STEP 2~3: 추출·정제 시도 {attempt}/{max_pipeline_retries}")

            extractor = ExtractorAgent()
            extractor.run_state = run_state
            raw_data = extractor.extract(
                pages=pdf_content.pages,
                full_text=pdf_content.full_text,
                extraction_prompts=extraction_prompts,
            )
            self._log(extractor.report())
            objects = self._supplement_ocr(pdf_content, native_objects, raw_data, extractor,
                                           extraction_prompts, states)
            raw_data["document_objects"] = [asdict(obj) for obj in objects]
            raw_data["extraction_records"] = deepcopy(getattr(extractor, "extraction_records", []))
            raw_data["merge_conflicts"] = raw_data.get("merge_conflicts", []) + deepcopy(getattr(extractor, "merge_conflicts", []))

            municipality = raw_data.get("municipality_name", "알 수 없음")

            if include_images:
                image_agent = ImageAgent()
                raw_data = image_agent.extract(
                    pages=pdf_content.pages,
                    text_results=raw_data,
                    municipality=municipality,
                )
                self._log(image_agent.report())
            else:
                self._log("[에이전트2b 이미지분석] 비활성화 (--no-images)")

            # 감사는 입력 복사만 수행한다. 기존 추출/정제/재시도 조건에 관여하지 않는다.
            if config.SOURCE_COVERAGE_MODE == "audit":
                coverage_capture = None
                try:
                    from utils.source_coverage import snapshot
                    coverage_capture = {"raw": snapshot(raw_data), "attempt": attempt,
                                        "source_objects": coverage_source_objects,
                                        "source_snapshot_policy": "pre_extraction_native_tables_visual_placeholders_cover",
                                        "routes": deepcopy(getattr(extractor, "coverage_routes", []))}
                    coverage_attempts.append({"attempt": attempt,
                        "raw_rows": {key: len(rows) for key, rows in coverage_capture["raw"].items()}})
                except (TypeError, ValueError) as error:
                    context["warnings"].append(f"원문 감사 추출 스냅샷 실패: {error}")
            organizer = OrganizerAgent()
            final_data = organizer.organize(raw_data)
            # Internal ledgers are not business sheets; retain them for persistence.
            for key in config.INTERNAL_OBJECT_KEYS:
                if key in raw_data and key not in final_data:
                    final_data[key] = deepcopy(raw_data[key])
            if config.SEMANTIC_ROUTING_ENABLED:
                from utils.semantic_routing import validate_and_reclassify
                semantic = validate_and_reclassify(final_data, pdf_content,
                                                     auto_reclassify=config.SEMANTIC_AUTO_RECLASSIFY,
                                                     document_objects=objects)
                final_data["semantic_routing_report"] = semantic.metrics()
                final_data.setdefault("validation_report", []).extend(semantic.validation_issues(municipality))
            self._log(organizer.report())
            if coverage_capture is not None:
                try:
                    coverage_capture["cleaned"] = snapshot(final_data)
                except (TypeError, ValueError) as error:
                    context["warnings"].append(f"원문 감사 정제 스냅샷 실패: {error}")
                coverage_capture["attempt_history"] = deepcopy(coverage_attempts)
                final_data["source_coverage_snapshot"] = coverage_capture

            score, issues = _quality_score(final_data)
            self._log(f"\n[감독관] 최소 구조 검사 (재시도 조건): {score:.1f}/100; 정확도 아님")
            if issues:
                self._log(f"[감독관] 이슈: {', '.join(issues)}")

            if score >= self.QUALITY_THRESHOLD:
                self._log("[감독관] 최소 구조 기준 통과")
                break
            elif attempt < max_pipeline_retries:
                self._log("[감독관] 최소 구조 미달. 재시도합니다...")
            else:
                self._log("[감독관] 최대 재시도 도달. 현재 결과로 진행합니다.")

        # STEP 4: 엑셀 작성
        self._log("\n[감독관] STEP 4: 엑셀 작성 에이전트 실행...")
        excel_agent = ExcelAgent()
        reflected_ids = {eid for _, row in body_rows(final_data) for eid in evidence_ids(row)}
        for obj in objects:
            state = states.setdefault(obj.object_id, {"status": "native_parsed"})
            ids = {obj.object_id, str(obj.metadata.get("evidence_id", ""))}
            state["body_reflected"] = bool(ids & reflected_ids)
            if state["body_reflected"]:
                state["status"] = "body_reflected"
        final_data["object_processing_states"] = states
        if isinstance(final_data.get("ocr_import_audit"), dict):
            final_data["ocr_import_audit"]["body_reflected_objects"] = sum(
                bool(record.get("business_extracted") and states.get(record["object_id"], {}).get("body_reflected"))
                for record in final_data.get("ocr_reextraction_audit", []))
            for record in final_data.get("ocr_reextraction_audit", []):
                record.update(states.get(record["object_id"], {}))
            for decision in final_data["ocr_import_audit"].get("triage", []):
                # Triage's native "extracted" meant parser availability, not
                # business extraction/body reflection. Keep both meanings explicit.
                decision["selection_status"] = decision.pop("final_status", "planned")
                decision["final_status"] = states.get(decision["object_id"], {}).get("status", "not_processed")
        final_data["source_validation"] = source_validation(final_data, pdf_content)
        final_data["object_evidence_audit"] = audit_object_evidence(final_data, objects)
        validation = final_data.setdefault("validation_report", [])
        for held in final_data.get("_semantic_contract_quarantine", []):
            row = held.get("row", {})
            validation.append({"지자체명": municipality, "대상시트": held.get("sheet_key", ""),
                               "항목": "의미 계약 원행 격리", "사유": ",".join(held.get("reason_codes", [])),
                               "출처페이지": row.get("출처페이지"), "감사파일": str(output_path.with_name(output_path.stem + "_pipeline_audit.json"))})
        for conflict in final_data.get("merge_conflicts", []):
            validation.append({"지자체명": municipality, "대상시트": conflict.get("sheet_key", ""),
                               "항목": "동일 사실 값 충돌", "사유": "원행을 보존했습니다. 감사 원장의 충돌 값을 확인하세요."})
        failed_batches = [r for r in final_data.get("extraction_records", []) if r.get("status") not in {"ok", "skipped", "reused"}]
        for failed in failed_batches:
            validation.append({"지자체명": municipality, "대상시트": failed.get("sheet_key", ""),
                               "항목": "추출 배치 미완료", "사유": failed.get("error", failed.get("status", "")),
                               "출처페이지": failed.get("page_nums", [])})
        vision_failures = final_data.get("vision_execution", {}).get("failed_batches", [])
        for failed in vision_failures:
            validation.append({"지자체명": municipality, "대상시트": "chart_observations",
                               "항목": "시각 판독 배치 미완료", "사유": failed.get("error", ""),
                               "출처페이지": failed.get("pages", [])})
        existing_inventory = final_data.get("visual_inventory", [])
        final_data["visual_inventory"] = existing_inventory + object_inventory(objects, final_data, states)
        audit_path = output_path.with_name(output_path.stem + "_pipeline_audit.json")
        for row in final_data.get("validation_report", []):
            row.setdefault("감사파일", str(audit_path))
            row.setdefault("대상시트", row.get("대상시트키", "") or row.get("영역", ""))
            row.setdefault("사유", " / ".join(str(row.get(key)) for key in ("문제내용", "권장조치") if row.get(key))
                           or row.get("설명", "") or row.get("내용", ""))
        excel_data = {key: final_data.get(key, []) for key in config.SHEET_KEY_TO_NAME}
        result_path = excel_agent.write(excel_data, output_path)
        self._log(excel_agent.report())
        self.last_outcome = {"excel_saved": True, "excel_path": str(result_path), "audit_saved": False,
                             "model_accuracy_evaluated": False, "warnings": context["warnings"],
                             "failed_extraction_batches": len(failed_batches),
                             "failed_vision_batches": len(vision_failures),
                             "review_items": len(validation),
                             "ocr_summary": final_data.get("ocr_import_audit", {})}
        if config.SOURCE_COVERAGE_MODE == "audit":
            try:
                from utils.source_coverage import compare, persist, read_excel, sha256_file, VERSION
                coverage_started = time.perf_counter()
                capture = coverage_capture or {}
                from utils.run_state import _implementation_hash
                provenance = {"producer_run": result_path.stem, "auditor_rule_version": VERSION,
                              "producer_implementation_sha256": _implementation_hash(),
                              "auditor_module_sha256": sha256_file(Path(__file__).resolve().parents[1] / "utils" / "source_coverage.py"),
                              "excel_sha256": sha256_file(result_path),
                              "stage_attempt": capture.get("attempt"),
                              "source": "current_supervisor_snapshots"}
                journal, summary = compare(coverage_source_objects or objects, context["input_sha256"], read_excel(result_path),
                    raw=capture.get("raw"), cleaned=capture.get("cleaned"),
                    records=final_data.get("extraction_records", []), routes=capture.get("routes", []),
                    quarantine=final_data.get("_semantic_contract_quarantine", []), provenance=provenance)
                summary["audit_elapsed_seconds_before_persistence"] = round(time.perf_counter() - coverage_started, 6)
                saved = persist(result_path.with_suffix(""), journal, summary)
                context["source_coverage"] = {"mode": "audit", "status": "saved", **saved,
                                              "excel_sha256": provenance["excel_sha256"]}
                self._log(f"[원문 감사] 비교 {saved['comparable_facts']} / {saved['counts']}")
                if saved["comparable_facts"] == 0:
                    self._log("[원문 감사] 비교 가능한 항목이 없어 반영률·누락률은 미평가입니다.")
                self._log(f"[원문 감사 저장] {saved['coverage_summary_path']}")
            except (OSError, ValueError, TypeError, KeyError) as error:
                context["source_coverage"] = {"mode": "audit", "status": "failed", "error": str(error)}
                warning = f"Excel은 저장됐지만 원문 감사 저장/대조 실패: {error}"
                context["warnings"].append(warning)
                self._log(f"[경고] {warning}")
            self.last_outcome["source_coverage"] = context["source_coverage"]
        # 실제 저장된 16 업무 시트를 재조회한다. 진단 실패로 재추출하거나 업무 셀을 바꾸지 않는다.
        quality = None
        quality_state = {"status": "disabled"}
        if config.QUALITY_REPORT_ENABLED:
            try:
                quality_started = time.perf_counter()
                from utils.quality_evaluation import evaluate_saved_excel
                quality = evaluate_saved_excel(result_path, expected=excel_data, diagnostics=final_data,
                    document=pdf_content, objects=objects, municipality=municipality,
                    coverage=context.get("source_coverage"))
                quality["elapsed_seconds"] = round(time.perf_counter() - quality_started, 6)
                quality_state = {"status": "evaluated", "review_status": quality["review_status"],
                                 "issue_counts": quality["issue_counts"], "affected_rows": quality["affected_rows"]}
            except Exception as error:
                quality_state = {"status": "failed", "error": str(error)}
                warning = f"Excel은 저장됐지만 저장 후 품질 검사 실패: {error}"
                context["warnings"].append(warning)
                self._log(f"[경고] {warning}")
        from utils.benchmark_evaluation import evaluate_registered
        evaluation = evaluate_registered(result_path, input_path, manifest_path=config.EVALUATION_MANIFEST_PATH or None,
            mode=config.EVALUATION_MODE, dataset_id=config.EVALUATION_DATASET_ID or None,
            allow_draft=config.ALLOW_DRAFT_EVALUATION, allow_holdout=config.EVALUATE_HOLDOUT)
        if evaluation["status"] in {"failed", "required_unavailable"}:
            warning = f"Excel은 저장됐지만 요청한 골든 평가 미완료: {evaluation.get('reason')}"
            context["warnings"].append(warning)
            self._log(f"[경고] {warning}")
        # 큰 문제 목록은 별도 파일로 저장하고 원장에는 요약/경로/해시만 연결한다.
        evaluation_summary = {k: evaluation.get(k) for k in ("status", "reason", "contract_version", "dataset_id",
            "metrics", "counts", "scope", "review_status", "elapsed_seconds", "path", "sha256", "model_accuracy_evaluated") if k in evaluation}
        if evaluation.get("legacy_v1", {}).get("status") == "evaluated":
            evaluation_summary["legacy_v1"] = evaluation["legacy_v1"]
        self._saved_quality_summary = {"quality": quality_state, "evaluation": evaluation_summary,
                                      "metrics": quality["metrics"] if quality else {}}
        from utils.final_review import build_review_context, normalize_review, review_log_lines
        self._final_review_context = build_review_context(quality, evaluation_summary, quality_state)

        # 기존 1회 호출을 유지한다. 전체 등급 대신 검토할 항목의 참고 의견만 받는다.
        self._log("\n[감독관] LLM 보조 검토 의견 생성 중...")
        raw_review, review_error = None, None
        try:
            raw_review = self._llm_quality_review(final_data)
            if not isinstance(raw_review, dict):
                raise ValueError("LLM 보조 검수가 JSON 객체를 반환하지 않음")
        except Exception as error:
            review_error = str(error)
            context["warnings"].append(f"LLM 보조 의견 생성 실패 (규칙 진단 유지): {error}")
        review = normalize_review(raw_review, self._final_review_context, error=review_error)
        for line in review_log_lines(review, self._final_review_context):
            self._log(line)

        if quality is not None:
            quality["evaluation"] = evaluation_summary
            quality["llm_review"] = {"role": "diagnostic_only", "result": review}
            try:
                from utils.quality_evaluation import persist_quality_report
                quality_state = persist_quality_report(result_path, quality)
                self._log(f"[품질 보고서] {quality_state['paths']['markdown']}")
            except Exception as error:
                quality_state.update(status="failed", error=str(error))
                warning = f"Excel은 저장됐지만 품질 보고서 저장 실패: {error}"
                context["warnings"].append(warning)
                self._log(f"[경고] {warning}")
        context.update(quality=quality_state, evaluation=evaluation_summary,
                       llm_review={"role": "diagnostic_only", "result": review},
                       model_accuracy_evaluated=evaluation.get("model_accuracy_evaluated", False))
        self.last_outcome.update(quality=quality_state, evaluation=evaluation_summary,
            final_review={k: review[k] for k in ("schema_version", "rule_check", "verification_scope", "fact_accuracy", "llm_status")},
            evaluation_status=evaluation["status"], model_accuracy_evaluated=context["model_accuracy_evaluated"],
            run_status="no_data" if quality is not None and not quality["business_rows"] else "partial" if failed_batches or vision_failures else "completed",
            review_status="needs_review" if validation or (quality and (quality["unique_issues"] or not quality["business_rows"])) or context["warnings"]
                          else ("not_evaluated" if quality is None else "no_rule_findings"))
        try:
            persisted = persist_audit(result_path, input_path, final_data, context)
            self.last_outcome.update(audit_saved=True, audit_path=str(persisted))
            self._log(f"[감사 저장] {persisted}")
        except (OSError, TypeError, ValueError) as error:
            warning = f"Excel은 저장됐지만 감사 원장 저장 실패: {error}"
            context["warnings"].append(warning)
            self.last_outcome["review_status"] = "needs_review"
            self._log(f"[경고] {warning}")

        self._log("\n" + "=" * 60)
        self._log("[감독관] 파이프라인 완료")
        self._log(f"  출력 파일: {result_path}")
        self._log(f"  실행 상태: {self.last_outcome['run_status']} / 검토 상태: {self.last_outcome['review_status']}")
        self._log(f"  데이터 규칙: {review['rule_check']['label']} / 검증 범위: {review['verification_scope']['label']}")
        self._log(f"  규칙·근거 진단 건수: {quality_state.get('issue_counts', quality_state['status'])} (오답 행 수 아님)")
        if evaluation["status"] == "evaluated":
            metrics = evaluation["metrics"]
            pct = lambda value: f"{value * 100:.2f}%" if value is not None else "미평가"
            self._log(f"  골든 ({evaluation['contract_version']}): 셀 {pct(metrics['cell_accuracy'])} / 행 재현 {pct(metrics['row_recall'])} / 골든 매칭 정밀 {pct(metrics['row_precision'])}")
            self._log("  골든 밖 사실은 오답으로 확정하지 않습니다. 기존 v1 참조 점수는 평가 보고서에 별도 보존합니다.")
        elif evaluation["status"] == "disabled":
            self._log("  사실 정확도: 미평가 (골든 평가 OFF; 품질 감점 사유 아님)")
        else:
            self._log(f"  골든 정확도 미평가: {evaluation.get('reason')}")
        self._log("=" * 60)
        try:
            save_json(result_path.with_name(result_path.stem + "_run_outcome.json"), self.last_outcome)
            if run_state:
                from utils.run_state import sha256_json
                run_state.finalize(output_path=result_path, semantic_result_hash=sha256_json(final_data),
                                    extra={"audit_path": self.last_outcome.get("audit_path", "")})
        except OSError as error:
            self._log(f"[경고] 실행 기록 저장 실패 (Excel 보존): {error}")

        return result_path

    def get_report(self) -> str:
        return "\n".join(self._reports)
