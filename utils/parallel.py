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
from typing import Callable, Sequence, TypeVar

import config

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    workers: int,
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
        return [fn(item) for item in items]

    results: list[R] = [None] * n  # type: ignore[list-item]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        future_to_index = {executor.submit(fn, item): i for i, item in enumerate(items)}
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
