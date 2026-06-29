# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python CLI tool that extracts structured data (GHG emissions, energy use, vehicle stats, reduction strategies) from Korean municipal carbon-neutrality plan documents (PDF/HWP/HWPX) and outputs a guideline-based Excel file (17 always-present sheets — 16 data + 1 visual inventory — plus optional audit sheets). The default backend is the Gemini API (`GEMINI_API_KEY` required); local LLM agent CLIs (Codex or Claude Code) are used only when selected via `--agent`.

## Commands

### Run the pipeline
```bash
python main.py "input_document.pdf"
python main.py "input.pdf" -o "output.xlsx" --agent claude -v
python main.py "input.hwp" --guideline "가이드라인.hwp" --no-images
python main.py "input.pdf" --full-scan  # all pages, no routing filter
```

### Install dependencies
```bash
pip install -r requirements.txt   # Python: pymupdf, pandas, openpyxl, pillow, python-dotenv
npm install                        # Node.js: kordoc (for HWP/HWPX support)
```

### Run tests
```bash
python -m pytest tests/
python -m pytest tests/test_llm_client.py -v   # single test file
```

### Verify Excel output against source
```bash
python scripts/verify_excel_against_source.py
```

## Architecture

The pipeline is a sequential multi-agent system orchestrated by `Supervisor`, producing **17 Excel sheets** (16 data + 1 visual inventory) based on `carbon_guideline.md`:

```
main.py (CLI + env setup)
  → Supervisor.run()
    → STEP 0: Document parsing (pdf_reader / hwp_reader)
    → STEP 1: GuidelineAgent (loads carbon_guideline.md; HWP fallback)
    → STEP 2: ExtractorAgent (16-sheet extraction with per-sheet routing)
    → STEP 2b: ImageAgent (ChartQA-style triage → DePlot-style chart-to-table)
    → STEP 3: OrganizerAgent (deterministic Python normalization/dedup + validation pass)
    → STEP 3b: GapFillAgent (re-extract empty/sparse sheets; GAP_FILL_ENABLED)
    → STEP 3c/3d: HybridReviewAgent (optional; HYBRID_REVIEW_ENABLED)
    → STEP 4: ExcelAgent (openpyxl output; optional 17/18/19 sheets only when populated)
    → Quality scoring + LLM review
```

### Output sheets (carbon_guideline.md §5.2)

`00_문서메타`, `01_계획개요`, `02_지역여건`, `03_배출현황_지역`, `04_배출현황_관리권한`, `05_배출전망`, `06_감축목표`, `07_비전전략`, `08_감축사업목록`, `09_연차별이행계획`, `10_정량감축량`, `11_재정투자계획`, `12_대응기반강화`, `13_이행관리환류`, `14_점검실적`, `15_변경과제_조치`, `16_시각자료목록`

### Key design decisions

- **Guideline-driven**: The extraction schema, sheet structure, and field definitions all derive from `carbon_guideline.md` (pre-structured from the MoE HWP guideline). No HWP parsing needed at runtime.
- **Document routing**: Before extraction, pages are scored per-sheet (16 categories) with weighted keywords (`_ROUTE_CONFIGS` in `extractor_agent.py`). `FULL_DOCUMENT_SCAN=1` disables this.
- **Backend dispatch in `utils/llm_client.py`**: the Gemini (default) and OpenAI backends call cloud SDKs; the `codex`/`claude` backends shell out to `codex exec` / `claude -p` (subprocess, not SDK). JSON-only output is enforced via system prompt. Local-agent calls add quota/timeout resilience: on quota limits or repeated codex timeouts (treated as throttling), the call waits for reset and resumes instead of aborting (`LLM_QUOTA_WAIT_*`, `LLM_TIMEOUT_AS_QUOTA_THRESHOLD`).
- **Organizer is deterministic**: All normalization, dedup, type coercion, unit canonicalization, and the validation pass (reduction-rate recompute, sector/target-year coverage, unit-scale and financial-sum cross-checks → `19_검증리포트`) in `organizer_agent.py` are pure Python—no LLM calls. (GapFillAgent, a separate step, does make LLM calls.)
- **Image pipeline is conservative**: Only table-type charts with high confidence auto-merge; graph-estimated values stay in `16_시각자료목록`.

### LLM backend selection

Set via `LLM_PROVIDER` env var or `--agent` CLI flag:
- `gemini` (default): Gemini API, requires `GEMINI_API_KEY`
- `codex`: `codex exec --sandbox read-only`
- `claude`: `claude -p --output-format text`
- `openai`: OpenAI API, requires `OPENAI_API_KEY`
- `auto`: tries codex → claude → gemini

## Configuration

All tunable parameters are in `config.py` and overridable via environment variables. Key ones:
- `BATCH_SIZE` (default 15): pages per LLM call. Lower = more reliable JSON, more calls.
- `LOCAL_AGENT_TIMEOUT` (default 900s): per-call timeout for subprocess agents.
- `IMAGE_TRIAGE_MIN_SCORE` (default 5): Pillow-based heuristic score threshold.
- `GAP_FILL_ENABLED` / `FOCUSED_GAP_FILL_ENABLED`: toggle re-extraction passes.
- `DOCUMENT_ROUTE_MIN_SCORE` (default 3): page relevance threshold per sheet.

## Language

The codebase, prompts, comments, and variable names are in Korean. All LLM system prompts enforce JSON-only Korean output. Field names in data structures (시트 headers, dict keys) are Korean strings matching the Excel output columns defined in `config.EXCEL_HEADERS`.
