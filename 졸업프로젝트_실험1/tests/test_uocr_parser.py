from uocr_experiment.uocr_parser import markdown_table_rows, parse_detection, parse_uocr_text


def test_parse_detection_uses_literal_parser() -> None:
    label, bbox = parse_detection("['table', [[100, 200, 900, 500]]]")
    assert label == "table"
    assert bbox == (100.0, 200.0, 900.0, 500.0)


def test_parse_tagged_markdown_table() -> None:
    value = """<|ref|>| 연도 | 배출량 |
|---|---|
| 2020 | 100 |<|/ref|><|det|>['table', [100, 200, 900, 500]]<|/det|>"""
    objects = parse_uocr_text(value, page_number=10, pdf_width=600, pdf_height=800)
    assert len(objects) == 1
    assert objects[0].object_type == "table"
    assert objects[0].rows == [["연도", "배출량"], ["2020", "100"]]
    assert objects[0].bbox_pdf is not None


def test_markdown_table_rejects_plain_pipe_text() -> None:
    assert markdown_table_rows("문장 | 문장") == []


def test_parse_official_det_block_with_continuation_lines() -> None:
    value = """<|det|>table [[100, 200, 900, 500]]<|/det|>| 연도 | 값 |
|---|---|
| 2020 | 100 |
<|det|>image [[50, 520, 950, 980]]<|/det|>"""
    objects = parse_uocr_text(value, page_number=5, pdf_width=600, pdf_height=800)
    assert [item.object_type for item in objects] == ["table", "figure"]
    assert objects[0].rows[-1] == ["2020", "100"]
    assert objects[1].bbox_norm == (50.0, 520.0, 950.0, 980.0)
