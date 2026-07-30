from __future__ import annotations

import hashlib
import json
import tempfile
import threading
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
_CACHE_STATS: dict[str, int] = {
    "hit": 0,
    "miss": 0,
    "write": 0,
    "rejected": 0,
    "disabled": 0,
}
# 병렬 호출 시 여러 스레드가 통계를 증가시키므로 보호한다.
_CACHE_STATS_LOCK = threading.Lock()


def _bump_stat(key: str) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[key] += 1


def reset_cache_stats() -> None:
    with _CACHE_STATS_LOCK:
        for key in _CACHE_STATS:
            _CACHE_STATS[key] = 0


def get_cache_stats() -> dict[str, int]:
    with _CACHE_STATS_LOCK:
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
    if not _is_valid_json_response(response):
        _bump_stat("rejected")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return response


def _write_response(request: LLMCacheRequest, response: str) -> bool:
    if not _is_valid_json_response(response):
        _bump_stat("rejected")
        return False

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
    return True


def _is_valid_json_response(response: str) -> bool:
    """실패 문구나 잘린 응답이 다음 실행에서 정상 응답처럼 재사용되지 않게 한다."""
    stripped = (response or "").strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    # 공급자 fail-soft 경로는 일시 오류를 빈 객체로 반환한다. 이를 캐시하면
    # 다음 실행에서도 정상 호출 없이 데이터가 계속 누락되므로 저장하지 않는다.
    if parsed == {}:
        return False
    return isinstance(parsed, (dict, list))


def cached_response(request: LLMCacheRequest, producer: Callable[[], str]) -> str:
    if not _is_enabled():
        _bump_stat("disabled")
        return producer()

    cached = _read_response(request)
    if cached is not None:
        _bump_stat("hit")
        return cached

    _bump_stat("miss")
    response = producer()
    try:
        if _write_response(request, response):
            _bump_stat("write")
    except OSError:
        return response
    return response
