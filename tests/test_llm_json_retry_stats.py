import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from utils import llm_client


class LLMJsonRetryStatsTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: getattr(llm_client.config, name)
            for name in (
                "LLM_PROVIDER",
                "CODEX_COMMAND",
                "LOCAL_AGENT_MODEL",
                "LOCAL_AGENT_TIMEOUT",
                "LLM_CACHE_ENABLED",
            )
        }
        llm_client.config.LLM_PROVIDER = "codex"
        llm_client.config.LOCAL_AGENT_MODEL = ""
        llm_client.config.LOCAL_AGENT_TIMEOUT = 5
        llm_client.config.LLM_CACHE_ENABLED = False
        llm_client.reset_llm_stats()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(llm_client.config, name, value)
        llm_client.reset_llm_stats()

    def _json_retry_command(self, tmpdir: Path) -> str:
        fake = tmpdir / "fake_json_retry.py"
        count_path = tmpdir / "count.txt"
        fake.write_text(
            textwrap.dedent(
                f"""
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                prompt = sys.stdin.read()
                output_path = args[args.index('--output-last-message') + 1]
                count_path = Path({str(count_path)!r})
                current = int(count_path.read_text(encoding='utf-8')) if count_path.exists() else 0
                count_path.write_text(str(current + 1), encoding='utf-8')
                with open(output_path, 'w', encoding='utf-8') as f:
                    if '[재요청]' in prompt:
                        json.dump({{'ok': True}}, f)
                    else:
                        f.write('이것은 JSON이 아님')
                """
            ),
            encoding="utf-8",
        )
        return f"{sys.executable} {fake}"

    def test_call_text_json_retries_once_after_parse_failure(self):
        # Given: 첫 응답은 JSON이 아니고 재요청 때만 JSON을 주는 로컬 에이전트
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            llm_client.config.CODEX_COMMAND = self._json_retry_command(tmpdir)

            # When: JSON 호출 헬퍼를 사용하면
            parsed, ok = llm_client.call_text_json('{"ok": true}', max_retries=1)
            count = (tmpdir / "count.txt").read_text(encoding="utf-8")

        # Then: 1회 재요청 후 성공하고 실제 호출 통계도 2회를 기록한다.
        self.assertTrue(ok)
        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(count, "2")
        self.assertEqual(llm_client.get_llm_stats()["total_calls"], 2)


if __name__ == "__main__":
    unittest.main()
