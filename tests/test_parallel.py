import unittest

import config
from utils.llm_client import LLMCallError, LLMQuotaExceededError
from utils.parallel import parallel_map_collect


class ParallelMapCollectTests(unittest.TestCase):
    def setUp(self):
        self._parallel_enabled = config.PARALLEL_PROCESSING_ENABLED

    def tearDown(self):
        config.PARALLEL_PROCESSING_ENABLED = self._parallel_enabled

    def test_collects_non_quota_failures_and_preserves_order(self):
        # Given: 한 항목만 일반 LLM 호출 실패를 내는 배치 목록
        config.PARALLEL_PROCESSING_ENABLED = True

        def worker(value):
            if value == 2:
                raise LLMCallError("boom")
            return value * 10

        # When: 수집형 병렬 실행을 수행하면
        results = parallel_map_collect(worker, [1, 2, 3], workers=3)

        # Then: 입력 순서를 보존하고 실패 항목만 예외로 남긴다.
        self.assertEqual(results[0], (10, None))
        self.assertIsNone(results[1][0])
        self.assertIsInstance(results[1][1], LLMCallError)
        self.assertEqual(results[2], (30, None))

    def test_quota_failure_is_collected_and_remaining_items_are_skipped(self):
        # Given: quota 실패를 내는 작업
        config.PARALLEL_PROCESSING_ENABLED = True

        def worker(value):
            if value == 2:
                raise LLMQuotaExceededError("quota")
            return value

        # When: quota가 발생하는 수집형 병렬 실행을 수행하면
        results = parallel_map_collect(worker, [1, 2, 3], workers=3)

        # Then: raise 없이 quota 항목과 미완료 항목을 실패로 남긴다.
        self.assertEqual(results[0], (1, None))
        self.assertIsNone(results[1][0])
        self.assertIsInstance(results[1][1], LLMQuotaExceededError)

    def test_sequential_fallback_returns_same_tuple_shape(self):
        # Given: 병렬 비활성 상태와 실패 항목
        config.PARALLEL_PROCESSING_ENABLED = False

        def worker(value):
            if value == 2:
                raise LLMCallError("boom")
            return value * 10

        # When: 순차 폴백으로 실행해도
        results = parallel_map_collect(worker, [1, 2, 3], workers=1)

        # Then: 병렬 수집과 같은 튜플 형태를 반환한다.
        self.assertEqual(results[0], (10, None))
        self.assertIsInstance(results[1][1], LLMCallError)
        self.assertEqual(results[2], (30, None))


if __name__ == "__main__":
    unittest.main()
