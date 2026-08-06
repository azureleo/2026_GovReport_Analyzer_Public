#!/usr/bin/env bash
set -euo pipefail

OUTPUT_PATH="${1:-processor.txt}"
python - "$OUTPUT_PATH" <<'PY'
from pathlib import Path
import sys

from sglang.srt.sampling.custom_logit_processor import DeepseekOCRNoRepeatNGramLogitProcessor

Path(sys.argv[1]).write_text(
    DeepseekOCRNoRepeatNGramLogitProcessor.to_str(),
    encoding="utf-8",
)
print(Path(sys.argv[1]).resolve())
PY
