from uocr_experiment.evaluation import character_accuracy, evaluate_engine
from uocr_experiment.models import ExperimentObject


def obj(engine: str, object_id: str) -> ExperimentObject:
    return ExperimentObject(
        object_id=object_id,
        engine=engine,
        page_number=1,
        object_type="table",
        sequence=1,
        rows=[["연도", "값"], ["2020", "100"]],
        bbox_norm=(100, 100, 900, 900),
    )


def test_perfect_evaluation() -> None:
    metrics, matches = evaluate_engine("test", [obj("test", "prediction")], [obj("golden", "gold")])
    assert metrics["object_recall"] == 1.0
    assert metrics["object_precision"] == 1.0
    assert metrics["table_cell_accuracy"] == 1.0
    assert matches[0]["bbox_iou"] == 1.0


def test_character_accuracy_handles_korean_unicode() -> None:
    assert character_accuracy("서울특별시", "서울특별시") == 1.0
