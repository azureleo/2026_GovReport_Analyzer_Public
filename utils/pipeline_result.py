"""CLI outcome sidecars distinguish saved review artifacts from runtime failure."""
import json
from pathlib import Path


def load_outcome(workbook):
    path = Path(workbook)
    sidecar = path.with_name(f"{path.stem}_run_outcome.json")
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def saved_review_result(exit_code, workbook):
    outcome = load_outcome(workbook)
    return (exit_code == 3 and Path(workbook).is_file()
            and outcome.get("file_saved") is True
            and outcome.get("status") == "needs_review"
            and outcome.get("exit_code") == 3)
