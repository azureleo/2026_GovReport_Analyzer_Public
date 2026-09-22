"""
독립적인 LLM 호출을 동시에 실행하기 위한 작은 헬퍼.

핵심 불변식: 입력 리스트 순서를 그대로 보존해 결과를 돌려준다. 따라서 호출을
병렬화해도 누적 순서(추출 행 순서)는 순차 실행과 동일하다 → 출력 결정성 유지.
프롬프트·응답 자체는 바뀌지 않으므로 추출 품질·토큰량은 변하지 않고, 줄어드는 것은
오직 벽시계 대기 시간뿐이다.

config.PARALLEL_PROCESSING_ENABLED=False 이거나 workers<=1, 항목이 1개면
순차 실행으로 자동 폴백한다(동작 동일).
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from typing import Any, Callable, Sequence, TypeVar

import config
from utils.llm_client import LLMQuotaExceededError

T = TypeVar("T")
R = TypeVar("R")


_STATS_LOCK = threading.Lock()
_PARALLEL_STATS: dict[str, dict[str, float | int]] = {}


def reset_parallel_stats() -> None:
    """현재 파이프라인 실행의 작업 큐 계측을 초기화한다."""
    with _STATS_LOCK:
        _PARALLEL_STATS.clear()


def get_parallel_stats() -> dict[str, dict[str, float | int]]:
    """단계별 제출·시작·완료 수와 큐 대기시간을 반환한다."""
    with _STATS_LOCK:
        return {
            label: {
                key: round(float(value), 3) if key.endswith("_seconds") else int(value)
                for key, value in values.items()
            }
            for label, values in _PARALLEL_STATS.items()
        }


def _add_stat(label: str | None, key: str, amount: float | int = 1) -> None:
    if not label:
        return
    with _STATS_LOCK:
        stats = _PARALLEL_STATS.setdefault(label, {
            "submitted": 0,
            "started": 0,
            "completed": 0,
            "queue_wait_seconds": 0.0,
            "max_queue_wait_seconds": 0.0,
            "execution_seconds": 0.0,
        })
        if key == "max_queue_wait_seconds":
            stats[key] = max(float(stats[key]), float(amount))
        else:
            stats[key] = float(stats[key]) + float(amount)


def _run_timed(
    fn: Callable[[T], R],
    item: T,
    submitted_at: float,
    stats_label: str | None,
) -> R:
    started_at = time.perf_counter()
    wait_seconds = max(0.0, started_at - submitted_at)
    _add_stat(stats_label, "started")
    _add_stat(stats_label, "queue_wait_seconds", wait_seconds)
    _add_stat(stats_label, "max_queue_wait_seconds", wait_seconds)
    try:
        return fn(item)
    finally:
        _add_stat(stats_label, "execution_seconds", time.perf_counter() - started_at)
        _add_stat(stats_label, "completed")


def parallel_map(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int,
    stats_label: str | None = None,
) -> list[R]:
    """
    items의 각 원소에 fn을 적용한 결과를 입력 순서대로 반환한다.

    - 병렬 비활성/단일 항목이면 순차 실행.
    - fn에서 발생한 예외는 그대로 전파한다(순차 실행과 동일하게 첫 실패에서 중단).
      이미 제출된 다른 작업은 executor 종료 시 마무리되지만, 결과는 버려진다.
    """
    n = len(items)
    if n == 0:
        return []
    enabled = getattr(config, "PARALLEL_PROCESSING_ENABLED", True)
    if not enabled or workers <= 1 or n == 1:
        results: list[R] = []
        for item in items:
            submitted_at = time.perf_counter()
            _add_stat(stats_label, "submitted")
            results.append(_run_timed(fn, item, submitted_at, stats_label))
        return results

    results: list[R] = [None] * n  # type: ignore[list-item]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_index = {}
        for i, item in enumerate(items):
            submitted_at = time.perf_counter()
            _add_stat(stats_label, "submitted")
            future = executor.submit(_run_timed, fn, item, submitted_at, stats_label)
            future_to_index[future] = i
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()  # 예외는 여기서 재발생
    except BaseException:
        # 한 작업이 실패(예: quota 상한)하면 아직 시작 안 한 대기 작업은 즉시 취소한다.
        # 안 그러면 로컬 에이전트(codex/claude) 모드에서 대기열의 900초 타임아웃 호출들이
        # 모두 끝날 때까지 예외 전파가 지연된다(이미 실행 중인 호출은 취소 불가).
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    return results


def parallel_map_collect(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int,
    stats_label: str | None = None,
) -> list[tuple[R | None, Exception | None]]:
    """
    items의 각 원소에 fn을 적용하되, 일반 예외는 항목별 실패로 수집한다.

    반환값은 입력 순서를 보존하는 (결과, None) 또는 (None, 예외) 튜플 목록이다.
    LLMQuotaExceededError는 해당 항목부터 남은 미완료 항목을 실패로 채워 반환한다.
    quota 이후 이미 완료된 작업은 보존하고, 실행 중이던 작업은 기다리지 않고 건너뜀으로
    기록한다.
    """
    n = len(items)
    if n == 0:
        return []
    enabled = getattr(config, "PARALLEL_PROCESSING_ENABLED", True)
    if not enabled or workers <= 1 or n == 1:
        sequential: list[tuple[R | None, Exception | None]] = []
        for index, item in enumerate(items):
            submitted_at = time.perf_counter()
            _add_stat(stats_label, "submitted")
            try:
                sequential.append((_run_timed(fn, item, submitted_at, stats_label), None))
            except LLMQuotaExceededError as exc:
                sequential.append((None, exc))
                skip = LLMQuotaExceededError("선행 배치 quota로 건너뜀")
                sequential.extend((None, skip) for _ in items[index + 1:])
                return sequential
            except Exception as exc:  # noqa: BLE001 - 항목 실패를 원장에 남기기 위한 경계
                sequential.append((None, exc))
        return sequential

    results: list[tuple[R | None, Exception | None] | None] = [None] * n
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    future_to_index = {}
    for i, item in enumerate(items):
        submitted_at = time.perf_counter()
        _add_stat(stats_label, "submitted")
        future = executor.submit(_run_timed, fn, item, submitted_at, stats_label)
        future_to_index[future] = i
    quota_error: LLMQuotaExceededError | None = None
    for future in concurrent.futures.as_completed(future_to_index):
        index = future_to_index[future]
        try:
            results[index] = (future.result(), None)
        except LLMQuotaExceededError as exc:
            results[index] = (None, exc)
            quota_error = exc
            executor.shutdown(wait=False, cancel_futures=True)
            break
        except Exception as exc:  # noqa: BLE001 - 항목별 실패 수집 API
            results[index] = (None, exc)

    if quota_error is None:
        executor.shutdown(wait=True)
    else:
        skip = LLMQuotaExceededError("선행 배치 quota로 건너뜀")
        for future, index in future_to_index.items():
            if results[index] is not None:
                continue
            if future.cancelled() or not future.done():
                results[index] = (None, skip)
                continue
            try:
                results[index] = (future.result(), None)
            except LLMQuotaExceededError as exc:
                results[index] = (None, exc)
            except Exception as exc:  # noqa: BLE001 - 항목별 실패 수집 API
                results[index] = (None, exc)

    return [item if item is not None else (None, RuntimeError("작업 결과 누락")) for item in results]
