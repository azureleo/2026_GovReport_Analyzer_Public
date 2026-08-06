from __future__ import annotations

from utils.document_objects import DocumentObject
from utils.object_routing import (
    index_page_kind,
    is_index_page,
    prepare_object_inventory,
)


def test_index_page_detection_covers_three_report_layouts() -> None:
    seoul = "표 목차\n표 1-1 계획의 범위 ........ 28\n표 2-1 배출 현황 ........ 47\n" + "\n".join(
        f"표 3-{index} 항목 ........ {50 + index}" for index in range(1, 6)
    )
    gangwon_continuation = "목차｜\n" + "\n".join(
        f"<표 4-{index}> 연차별 계획 {100 + index}" for index in range(1, 9)
    )
    gyeonggi = "TABLES\n표목차\n" + "\n".join(
        f"[표 2-{index}] 지역 여건 ........ {20 + index}" for index in range(1, 6)
    )
    headerless_continuation = "보고서명\niv\n" + "\n".join(
        f"[표 5-{index}] 감축 계획\n{120 + index}" for index in range(1, 10)
    )
    headerless_contents = (
        "기본계획\nii\n중장기 감축목표\n159\n"
        "1. 감축목표 및 전략\n161\n2. 이행 로드맵\n165\n"
        "제7장 이행관리\n435\n참고문헌\n470\n부록\n482"
    )

    assert index_page_kind(seoul) == "table_index"
    assert index_page_kind(gangwon_continuation) == "table_index"
    assert index_page_kind(gyeonggi) == "table_index"
    assert index_page_kind(headerless_continuation) == "table_index"
    assert index_page_kind(headerless_contents) == "contents"
    assert not is_index_page("제2장 지역 여건\n본문에서는 보고서 목차 구성을 설명한다.")


def test_index_reference_is_resolved_to_body_object_and_not_counted() -> None:
    reference = DocumentObject(
        object_id="p7_table_1",
        object_type="table",
        page_number=7,
        sequence=1,
        number="표 2-4",
        caption="표 2-4 부문별 온실가스 배출량 전망",
    )
    body = DocumentObject(
        object_id="p141_table_1",
        object_type="table",
        page_number=141,
        sequence=1,
        number="표 2-4",
        caption="표 2-4 부문별 온실가스 배출량 전망",
        rows=[["부문", "2030"], ["건물", "100"]],
    )
    page_texts = {
        7: "표 목차\n표 2-4 부문별 온실가스 배출량 전망 ........ 141\n"
        + "\n".join(f"표 2-{i} 항목 ........ {130 + i}" for i in range(5, 10)),
        141: "제4장 배출 전망\n표 2-4 부문별 온실가스 배출량 전망",
    }

    prepared = prepare_object_inventory([reference, body], page_texts)

    assert [obj.object_id for obj in prepared.objects] == ["p141_table_1"]
    assert prepared.resolved_references == 1
    assert prepared.objects[0].metadata["index_reference_pages"] == [7]
    assert prepared.index_references[0].metadata["body_object_id"] == "p141_table_1"


def test_native_and_vlm_duplicate_share_one_inventory_object() -> None:
    native = DocumentObject(
        object_id="p88_chart_1",
        object_type="chart",
        page_number=88,
        sequence=1,
        number="그림 3-2",
        caption="그림 3-2 온실가스 감축 경로",
        metadata={"engine": "pymupdf", "caption_only": True},
    )
    vlm = DocumentObject(
        object_id="vision-p88-1",
        object_type="chart",
        page_number=88,
        sequence=2,
        number="그림 3-2",
        caption="그림 3-2 온실가스 감축 경로",
        rows=[["연도", "배출량"], ["2030", "100"]],
        metadata={"engine": "codex"},
    )

    prepared = prepare_object_inventory(
        [native, vlm],
        {88: "제3장 목표\n그림 3-2 온실가스 감축 경로"},
    )

    assert len(prepared.objects) == 1
    assert prepared.deduplicated_objects == 1
    assert prepared.objects[0].rows
    assert set(prepared.objects[0].metadata["source_object_ids"]) == {
        "p88_chart_1", "vision-p88-1",
    }
