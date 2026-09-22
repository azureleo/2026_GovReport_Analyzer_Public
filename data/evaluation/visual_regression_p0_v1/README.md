# P0 visual regression fixture

This locked fixture preserves the recurring visual misses on source pages 71, 89, 93, 414, and 441.

- `cases.jsonl`: reviewed object-level behavior contract.
- `manifest.json`: source, render, and artifact hashes.
- `objects.jsonl`: deterministic test inputs with rendered file locations.
- `annotations.jsonl`: reviewed triage, status, routing, and value anchors.
- `pages/`: full-page context renders using the original 1-based page number.
- `crops/`: reviewed object regions in PDF coordinates.
- `source_subset.pdf`: convenience copy; use the manifest page map because subset page numbers differ.

Rebuild:

```powershell
py scripts/build_visual_regression_fixture.py build `
  "서울특별시_탄소중립계획.pdf" `
  --cases "data/evaluation/visual_regression_p0_v1/cases.jsonl" `
  --dataset-id "seoul-visual-regression-p0-v1" `
  --output-dir "data/evaluation/visual_regression_p0_v1" `
  --dpi 108 `
  --force
```

Validate without any LLM call:

```powershell
py scripts/build_visual_regression_fixture.py validate `
  "data/evaluation/visual_regression_p0_v1/manifest.json" `
  --source "서울특별시_탄소중립계획.pdf"
```
