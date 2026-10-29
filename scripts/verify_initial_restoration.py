"""Verify the recovered October 10 source/input contracts without model calls."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(ROOT))
from utils.run_state import _implementation_hash, sha256_json
from agents.guideline_agent import GuidelineAgent

EXPECTED_IMPLEMENTATION = '07669a4d6cf1a6ba21c14d51b1a1dfe5b45c39474de98a09854a6c62d34f1fc7'
EXPECTED_PROMPT = '3334767dd258e7c89670d5999131380df502d06f89df74ad537ff3a912de5b51'
EXPECTED_INPUT = '9d63dae845099ccb8349e7b370e314b02f6402a4fa712568f6af7d43f65ed207'

def main():
    implementation = _implementation_hash()
    prompt = sha256_json(GuidelineAgent().get_all_prompts())
    source = ROOT / '서울특별시_탄소중립계획.pdf'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
    result = {
        'historical_run': '서울_v9통합_20261010_011115',
        'base_main_commit': 'baceb021820df6c5b3952ce89ee9804af5b7f138',
        'v9_source_commit': 'a2cb6bc00da38ca59b44f64aa94de02999da7f15',
        'implementation_sha256': implementation,
        'expected_implementation_sha256': EXPECTED_IMPLEMENTATION,
        'implementation_matches': implementation == EXPECTED_IMPLEMENTATION,
        'prompt_sha256': prompt,
        'expected_prompt_sha256': EXPECTED_PROMPT,
        'prompt_matches': prompt == EXPECTED_PROMPT,
        'input_sha256': source_hash,
        'input_matches': source_hash == EXPECTED_INPUT,
        'model_calls': 0,
        'scope': 'implementation hash covers config.py/main.py and all agents/utils Python files; it does not cover .env, dependencies or server state',
    }
    result['passed'] = all(result[k] for k in ['implementation_matches', 'prompt_matches', 'input_matches'])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    out = ROOT / 'output/restoration_verification_20261020.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['passed'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
