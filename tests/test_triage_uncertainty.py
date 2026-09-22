"""판독 전 판단 보류는 비데이터 확정·자동 호출·추출 성공과 구분한다."""

import json

import pytest

import config
from agents.image_agent import ImageAgent
from agents.supervisor import Supervisor
from utils import llm_client
from utils.document_objects import DocumentObject
from utils.pdf_reader import PDFContent, PageContent
from utils.selective_ocr import apply_triage_metadata, build_triage_plan
from utils.source_verifier import build_source_object_inventory


def _image(**overrides):
    fields = dict(
        object_id="p1_image_1", object_type="image", page_number=1, sequence=1,
        bbox=(30, 30, 560, 800), metadata={"width": 900, "height": 1250},
    )
    fields.update(overrides)
    return DocumentObject(**fields)


def _decision(obj):
    return build_triage_plan([obj], backend="vlm", confidence_threshold=0.78)[0]


@pytest.mark.parametrize("caption,nearby", [
    ("", ""),
    ("그림 4-1 서울시 탄소중립·녹색성장 기본계획 비전 체계도", ""),
    ("그림 1-1", "행사 사진 참고 사례"),
    ("참고 사례", ""),
    ("공모전 모집 안내", ""),
    ("위원회 사진 및 설명", ""),
])
def test_weak_or_context_only_evidence_is_review_not_negative(caption, nearby):
    decision = _decision(_image(caption=caption, nearby_text=nearby))

    assert decision.action == "review_required"
    assert decision.final_status == "needs_review"
    assert decision.status == "deferred_classification"
    assert decision.attempt_count == 0
    assert decision.backend == ""
    assert "insufficient_evidence:visual_content_unknown" in decision.reasons
    assert not any(reason.startswith("explicit_non_data:") for reason in decision.reasons)


@pytest.mark.parametrize("caption", [
    "그림 1-1 위원회 사진", "[그림 1-2] 행사 사진", "기관 로고", "logo",
])
def test_explicit_local_non_data_labels_remain_excluded(caption):
    decision = _decision(_image(caption=caption))

    assert decision.action == "skip_non_data"
    assert decision.final_status == "not_relevant"
    assert any(reason.startswith("explicit_non_data:caption:") for reason in decision.reasons)


def test_data_signal_takes_precedence_over_explicit_photo_caption():
    decision = _decision(_image(caption="행사 사진", text="2030년 배출량 120 tCO2eq"))

    assert decision.action == "ocr_required"
    assert decision.final_status == "needs_review"
    assert decision.backend == "vlm"
    assert not any(reason.startswith("explicit_non_data:") for reason in decision.reasons)


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_empty_native_text_is_not_counted_as_extracted(text):
    obj = DocumentObject("p1_text_1", "text", 1, 1, text=text)
    decision = _decision(obj)

    assert decision.action == "review_required"
    assert decision.final_status == "needs_review"
    assert "insufficient_evidence:empty_native_text" in decision.reasons


def test_nonempty_text_and_render_proxy_keep_existing_routes():
    text = _decision(DocumentObject("p1_text_1", "text", 1, 1, text="기존 본문"))
    proxy = _decision(_image(metadata={"render_proxy": True}))

    assert text.action == "native_keep"
    assert text.final_status == "extracted"
    assert proxy.action == "transport_only"
    assert proxy.final_status == "not_relevant"
    assert not any(reason.startswith("explicit_non_data:") for reason in proxy.reasons)


def test_review_state_survives_serialization_and_metadata_binding():
    obj = _image()
    decision = _decision(obj)
    serialized = json.loads(json.dumps(decision.to_dict()))
    apply_triage_metadata([obj], [serialized])
    restored = DocumentObject.from_dict(json.loads(json.dumps(obj.to_dict())))

    assert restored.metadata["triage_action"] == "review_required"
    assert restored.metadata["final_status"] == "needs_review"
    assert restored.metadata["ocr_status"] == "deferred_classification"
    assert restored.metadata["attempt_count"] == 0
    assert restored.metadata["evidence_id"] == decision.evidence_id


def test_unknown_visual_is_kept_separate_from_confirmed_data_denominator():
    obj = _image()
    apply_triage_metadata([obj], [_decision(obj)])
    document = PDFContent(1, [PageContent(1, "", [], [])], "")

    report = build_source_object_inventory({"document_objects": [obj.to_dict()]}, document)

    assert report.total_objects == 1
    assert report.extraction_denominator_objects == 0
    assert report.excluded_candidate_objects == 0
    assert report.review_candidate_objects == 1
    assert report.triage_unresolved_objects == 1
    assert report.coverage_ratio is None
    assert report.rows[0].evaluation_target == "review"
    assert report.rows[0].triage_evaluation_status == "unresolved"
    assert report.rows[0].final_status == "needs_review"


@pytest.mark.parametrize("backend", ["vlm", "none"])
def test_image_agent_preserves_unknowns_without_new_model_calls(monkeypatch, capsys, backend):
    monkeypatch.setattr(config, "SELECTIVE_OCR_ENABLED", True)
    monkeypatch.setattr(config, "OCR_BACKEND", backend)
    monkeypatch.setattr(config, "OCR_RESULTS_DIR", "")

    def unexpected_call(*args, **kwargs):
        pytest.fail("판단 보류 분류만으로 모델을 호출하면 안 됩니다")

    for name in ("call_text", "call_vision_json", "call_vision_batch_json"):
        monkeypatch.setattr(llm_client, name, unexpected_call)

    page = PageContent(1, "", [], [{"width": 900, "height": 1250, "base64": "unused"}])
    document = PDFContent(1, [page], "")
    objects = [_image(), DocumentObject("p1_text_1", "text", 1, 1, text="")]
    agent = ImageAgent()
    result = agent.extract([page], {}, "알 수 없음", document=document, document_objects=objects)

    assert len(result["object_triage"]) == 2
    assert all(row["action"] == "review_required" for row in result["object_triage"])
    assert all(row["final_status"] == "needs_review" for row in result["object_triage"])
    assert all(row["attempt_count"] == 0 for row in result["object_triage"])
    assert all(row["metadata"]["triage_action"] == "review_required" for row in result["document_objects"])
    assert not result.get("chart_observations")
    assert agent.metrics()["object_candidates"] == 0
    assert agent.metrics()["review_required_visual_objects"] == 1
    assert agent.metrics()["empty_native_text_objects"] == 1
    assert agent.metrics()["explicit_non_data_objects"] == 0
    assert "판단 보류: 시각 객체 1개" in agent.report()
    assert "PyMuPDF 충분" not in capsys.readouterr().out


def test_supervisor_preserves_review_counters_for_manifest():
    supervisor = Supervisor()
    supervisor._add_vision_render_stats({
        "review_required_objects": 22,
        "review_required_visual_objects": 11,
        "empty_native_text_objects": 11,
        "explicit_non_data_objects": 0,
    })

    assert supervisor._vision_render_stats["review_required_visual_objects"] == 11
    assert supervisor._vision_render_stats["review_required_objects"] == 22
