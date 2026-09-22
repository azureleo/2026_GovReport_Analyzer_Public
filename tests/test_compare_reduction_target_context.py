"""감축목표 문맥 A/B 리포트 집계 테스트."""

from scripts.compare_reduction_target_context import summarize_decisions


def test_판정과_재태깅방향을_결정적으로_집계한다() -> None:
    summary = summarize_decisions({
        "reduction_targets": [
            {"목표수준": "부문"},
            {"목표수준": "세부사업"},
        ],
        "reduction_target_context": [
            {
                "original_level": "부문",
                "suggested_level": "부문",
                "status": "keep",
                "confidence": "high",
            },
            {
                "original_level": "총괄",
                "suggested_level": "세부사업",
                "status": "retag",
                "confidence": "medium",
            },
            {
                "original_level": "연차경로",
                "suggested_level": "연차경로",
                "status": "needs_review",
                "confidence": "low",
            },
        ],
    })

    assert summary["input_rows"] == 3
    assert summary["output_rows"] == 2
    assert summary["decision_counts"] == {
        "keep": 1,
        "needs_review": 1,
        "retag": 1,
    }
    assert summary["retag_directions"] == {"총괄->세부사업": 1}
    assert summary["needs_review_original_levels"] == {"연차경로": 1}
