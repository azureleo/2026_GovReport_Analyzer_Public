"""M2-3 vision 벤치마크 공통 타입과 후보 환경 구성."""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

TEXT_PROVIDER: Final = "gemini"
TEXT_MODEL: Final = "gemini-2.5-flash-lite"
DEFAULT_CACHE_DIR: Final = Path(".cache/llm_responses_gemini_v5")
DEFAULT_SOURCE: Final = Path("서울특별시_탄소중립계획.pdf")
DEFAULT_INVENTORY: Final = Path("data/golden/서울_시각요소_인벤토리_v1.xlsx")
DEFAULT_GOLDEN: Final = Path("data/golden/서울특별시_골든셋_v1_초벌.xlsx")
STAGE_OVERRIDE_KEYS: Final = (
    "STAGE_PROVIDER_EXTRACTION", "STAGE_MODEL_EXTRACTION",
    "STAGE_PROVIDER_GAP_FILL", "STAGE_MODEL_GAP_FILL",
    "STAGE_PROVIDER_REVIEW", "STAGE_MODEL_REVIEW",
    "STAGE_PROVIDER_VISION", "STAGE_MODEL_VISION",
)
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    source: Path
    inventory: Path
    golden: Path | None
    output_dir: Path
    cache_dir: Path
    command: str
    candidates: tuple[Candidate, ...]
    skip_run: tuple[str, ...]
    sample_size: int
    baseline_recall: float
    cache_only: bool
    force_run: bool
    env_file: Path


@dataclass(frozen=True, slots=True)
class CommandStats:
    exit_code: int
    log_path: Path
    elapsed_seconds: float
    cache_hit: int | None
    cache_miss: int | None
    cache_write: int | None
    llm_total_calls: int | None


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate: Candidate
    output_xlsx: Path
    run: CommandStats
    audit_json: Path | None
    score_json: Path | None
    workbook_metrics: JsonObject
    environment: JsonObject
    skip_reason: str
    reused: bool


CANDIDATE_ALIASES: Final = {
    "flashlite": Candidate("flashlite", "gemini", "gemini-2.5-flash-lite"),
    "flash": Candidate("flash", "gemini", "gemini-2.5-flash"),
    "pro": Candidate("pro", "gemini", "gemini-2.5-pro"),
    "claude": Candidate("claude", "claude", ""),
    "codex": Candidate("codex", "codex", ""),
    "openai": Candidate("openai", "openai", "gpt-5.4-mini"),
}


def resolve_candidate(raw: str) -> Candidate:
    token = raw.strip().lower()
    if token in CANDIDATE_ALIASES:
        return CANDIDATE_ALIASES[token]
    if ":" in token:
        provider, model = token.split(":", 1)
        name = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", token).strip("_")
        return Candidate(name or provider, provider, model)
    raise argparse.ArgumentTypeError(f"알 수 없는 후보입니다: {raw}")


def candidate_list(raw: str) -> tuple[Candidate, ...]:
    tokens = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if not tokens:
        raise argparse.ArgumentTypeError("후보를 1개 이상 지정해야 합니다")
    return tuple(resolve_candidate(token) for token in tokens)


def tag_list(raw: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in re.split(r"[\s,]+", raw.strip()) if part.strip())


def build_variant_env(base_env: dict[str, str], candidate: Candidate, cache_dir: Path) -> dict[str, str]:
    env = dict(base_env)
    for key in STAGE_OVERRIDE_KEYS:
        env.pop(key, None)
    env["LLM_PROVIDER"] = TEXT_PROVIDER
    env["GEMINI_MODEL"] = TEXT_MODEL
    env["LLM_CACHE_ENABLED"] = "1"
    # 환경 스냅샷과 테스트 결과가 OS 경로 구분자에 따라 달라지지 않게 한다.
    env["LLM_CACHE_DIR"] = cache_dir.as_posix()
    env["STAGE_PROVIDER_VISION"] = candidate.provider
    if candidate.model:
        env["STAGE_MODEL_VISION"] = candidate.model
    return env


def has_openai_key(env: dict[str, str], env_file: Path) -> bool:
    if env.get("OPENAI_API_KEY", "").strip():
        return True
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        if key.strip() == "OPENAI_API_KEY" and _clean_env_value(raw_value):
            return True
    return False


def should_skip_candidate(candidate: Candidate, config: BenchmarkConfig, env: dict[str, str] | None = None) -> str:
    if candidate.name in config.skip_run:
        return "사용자 skip-run"
    active_env = os.environ if env is None else env
    if candidate.provider == "openai" and not has_openai_key(dict(active_env), config.env_file):
        return "OPENAI_API_KEY 부재"
    return ""


def _clean_env_value(raw: str) -> str:
    return raw.split("#", 1)[0].strip().strip('"').strip("'")
