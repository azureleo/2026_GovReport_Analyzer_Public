# v8-1 선결 3건 원인진단 원자료 (2026-09-07, LLM 0)

- `v8_triage_count.py` — v8(67b9560) 워크트리 루트에서 `python v8_triage_count.py <pdf> <label>` : 판독 필요 객체·렌더 변형·해시 축소 후 vision 후보 수 집계(렌더·LLM 없음).
- `v7_triage_count.py` — v7(feat/v7-extraction-fidelity) 루트에서 실행: 기존 triage 통과 후보 수.
- `*_triage_서울.json` — 서울 PDF 결과. 요약과 해석은 `docs/specs/v8-1_통합선결3건_지시서.md` §0.3.
