from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypedDict

import config


class _CacheFile(TypedDict):
    response: str


@dataclass(frozen=True, slots=True)
class LLMCacheRequest:
    call_kind: str
    provider: str
    model: str
    system: str
    prompt: str
    images_b64: tuple[str, ...] = ()


# 실행 단위 캐시 통계. Supervisor가 시작 시 reset_cache_stats()로 초기화하고
# 종료 시 get_cache_stats()로 hit/miss/write/disabled 요약을 출력한다.
_CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0, "write": 0, "disabled": 0}


def reset_cache_stats() -> None:
    for key in _CACHE_STATS:
        _CACHE_STATS[key] = 0


def get_cache_stats() -> dict[str, int]:
    return dict(_CACHE_STATS)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_root() -> Path:
    configured = getattr(config, "LLM_CACHE_DIR", ".cache/llm_responses") or ".cache/llm_responses"
    return Path(configured)


def _is_enabled() -> bool:
    return bool(getattr(config, "LLM_CACHE_ENABLED", True))


def _request_key(request: LLMCacheRequest) -> str:
    payload = {
        "version": getattr(config, "LLM_CACHE_VERSION", "carbon-report-llm-cache-v1"),
        "call_kind": request.call_kind,
        "provider": request.provider,
        "model": request.model,
        "system_sha256": _sha256_text(request.system),
        "prompt_sha256": _sha256_text(request.prompt),
        "image_sha256": [_sha256_text(image) for image in request.images_b64],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _cache_path(request: LLMCacheRequest) -> Path:
    key = _request_key(request)
    return _cache_root() / key[:2] / f"{key}.json"


def _read_response(request: LLMCacheRequest) -> str | None:
    path = _cache_path(request)
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    response = payload.get("response", "")
    if not isinstance(response, str):
        return None
    if not response.strip() or response.strip() == "{}":
        return None
    return response


def _write_response(request: LLMCacheRequest, response: str) -> None:
    if not response.strip() or response.strip() == "{}":
        return

    path = _cache_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: _CacheFile = {"response": response}
    encoded = json.dumps(payload, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(encoded)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def cached_response(request: LLMCacheRequest, producer: Callable[[], str]) -> str:
    if not _is_enabled():
        _CACHE_STATS["disabled"] += 1
        return producer()

    cached = _read_response(request)
    if cached is not None:
        _CACHE_STATS["hit"] += 1
        return cached

    _CACHE_STATS["miss"] += 1
    response = producer()
    try:
        _write_response(request, response)
        _CACHE_STATS["write"] += 1
    except OSError:
        return response
    return response
