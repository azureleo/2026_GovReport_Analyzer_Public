import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config
from utils.llm_cache import (
    LLMCacheRequest,
    cached_response,
    get_cache_stats,
    reset_cache_stats,
)


def _request() -> LLMCacheRequest:
    return LLMCacheRequest(
        call_kind="text",
        provider="codex",
        model="test-model",
        system="system",
        prompt="same prompt",
    )


def test_singleflight_coalesces_concurrent_requests_with_disk_cache_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_CACHE_ENABLED", False)
    monkeypatch.setattr(config, "LLM_SINGLEFLIGHT_ENABLED", True)
    reset_cache_stats()
    workers = 6
    barrier = threading.Barrier(workers)
    call_count = 0
    call_lock = threading.Lock()

    def producer() -> str:
        nonlocal call_count
        with call_lock:
            call_count += 1
        time.sleep(0.08)
        return '{"ok": true}'

    def invoke() -> str:
        barrier.wait()
        return cached_response(_request(), producer)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        responses = list(executor.map(lambda _: invoke(), range(workers)))

    stats = get_cache_stats()
    assert responses == ['{"ok": true}'] * workers
    assert call_count == 1
    assert stats["disabled"] == workers
    assert stats["coalesced"] == workers - 1
    assert stats["singleflight_wait_seconds"] > 0


def test_singleflight_does_not_reuse_sequential_no_cache_requests(monkeypatch) -> None:
    monkeypatch.setattr(config, "LLM_CACHE_ENABLED", False)
    monkeypatch.setattr(config, "LLM_SINGLEFLIGHT_ENABLED", True)
    call_count = 0

    def producer() -> str:
        nonlocal call_count
        call_count += 1
        return '{"call": %d}' % call_count

    assert cached_response(_request(), producer) == '{"call": 1}'
    assert cached_response(_request(), producer) == '{"call": 2}'
    assert call_count == 2
