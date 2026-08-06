from __future__ import annotations

from utils.evidence_merge import build_evidence_catalog, match_evidence, normalize_evidence_ids


def _object(object_id: str, evidence_id: str, status: str = "extracted") -> dict:
    return {
        "object_id": object_id,
        "object_type": "chart",
        "metadata": {
            "evidence_id": evidence_id,
            "final_status": status,
        },
    }


def test_evidence_ids_are_normalized_without_reordering() -> None:
    assert normalize_evidence_ids(["ev-2", "ev-1"], "ev-2", "") == ["ev-2", "ev-1"]


def test_exact_match_requires_one_extracted_object() -> None:
    catalog = build_evidence_catalog([_object("obj-1", "ev-1")])

    result = match_evidence(["ev-1"], catalog)

    assert result.exact is True
    assert result.status == "exact"
    assert result.object_ids == ("obj-1",)


def test_missing_multiple_unknown_and_non_extracted_are_not_exact() -> None:
    catalog = build_evidence_catalog([
        _object("obj-1", "ev-1"),
        _object("obj-2", "ev-1"),
        _object("obj-3", "ev-review", "needs_review"),
    ])

    assert match_evidence([], catalog).status == "missing"
    assert match_evidence(["ev-1", "ev-2"], catalog).status == "multiple_ids"
    assert match_evidence(["ev-unknown"], catalog).status == "unknown"
    assert match_evidence(["ev-1"], catalog).status == "multiple_objects"
    assert match_evidence(["ev-review"], catalog).status == "object_not_extracted"


def test_catalog_deduplicates_same_object_from_object_and_triage_ledgers() -> None:
    catalog = build_evidence_catalog(
        [_object("obj-1", "ev-1")],
        [{
            "object_id": "obj-1",
            "evidence_id": "ev-1",
            "final_status": "needs_review",
        }],
    )

    assert match_evidence("ev-1", catalog).status == "exact"


def test_caption_proxy_and_image_with_same_evidence_are_one_physical_object() -> None:
    caption = _object("p10_chart_1", "ev-visual")
    caption.update({
        "page_number": 10,
        "object_type": "chart",
        "caption": "그림 2-1 배출 전망",
        "metadata": {
            **caption["metadata"],
            "caption_only": True,
        },
    })
    image = _object("p10_image_1", "ev-visual")
    image.update({
        "page_number": 10,
        "object_type": "image",
        "caption": "그림 2-1 배출 전망",
    })

    result = match_evidence("ev-visual", build_evidence_catalog([caption, image]))

    assert result.status == "exact"
    assert set(result.object_ids) == {"p10_chart_1", "p10_image_1"}
