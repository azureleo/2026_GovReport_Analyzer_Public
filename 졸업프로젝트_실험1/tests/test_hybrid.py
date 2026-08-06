from uocr_experiment.hybrid import merge_objects
from uocr_experiment.models import ExperimentObject


def table(engine: str, object_id: str, rows: list[list[str]]) -> ExperimentObject:
    return ExperimentObject(
        object_id=object_id,
        engine=engine,
        page_number=10,
        object_type="table",
        sequence=1,
        rows=rows,
        bbox_norm=(100, 100, 900, 900),
    )


def test_hybrid_replaces_sparse_table_on_hard_page() -> None:
    baseline = [table("pymupdf", "base", [["연도", "값"], ["2020", ""]])]
    uocr = [table("unlimited_ocr", "ocr", [
        ["연도", "값", "단위"],
        ["2020", "100", "tCO2eq"],
        ["2021", "90", "tCO2eq"],
    ])]
    manifest = {"pages": [{"page_number": 10, "categories": ["unconfirmed"]}]}
    objects, decisions = merge_objects(baseline, uocr, manifest)
    assert [item.object_id for item in objects] == ["ocr"]
    assert decisions[0]["action"] == "replace"
