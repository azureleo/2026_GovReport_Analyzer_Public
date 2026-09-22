import json
from pathlib import Path

from scripts.run_text_optimization_ab import _build_command, _manifest_metrics, _speedup


def test_text_ab_command_always_disables_vision(tmp_path: Path) -> None:
    command = _build_command(
        tmp_path / "source.pdf",
        tmp_path / "result.xlsx",
        retries=1,
    )

    assert "--no-images" in command
    assert command[command.index("--retries") + 1] == "1"


def test_text_ab_manifest_summary_reports_speed_and_calls(tmp_path: Path) -> None:
    manifest = tmp_path / "run_manifest.json"
    manifest.write_text(json.dumps({
        "status": "complete",
        "quality_score": 91.5,
        "timings_seconds": {"텍스트 추출": 120.0},
        "llm_call_stats": {
            "total_calls": 40,
            "input_chars": {"text:codex:text:model": 1234},
        },
    }, ensure_ascii=False), encoding="utf-8")

    baseline = _manifest_metrics(manifest)
    clustered = {**baseline, "text_seconds": 60.0}

    assert baseline["total_calls"] == 40
    assert baseline["input_chars"] == 1234
    assert _speedup(baseline, clustered) == 2.0
