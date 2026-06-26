import base64
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from utils import llm_client


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: getattr(llm_client.config, name)
            for name in (
                "LLM_PROVIDER",
                "CODEX_COMMAND",
                "CLAUDE_COMMAND",
                "LOCAL_AGENT_MODEL",
                "LOCAL_AGENT_TIMEOUT",
                "GEMINI_API_KEY",
            )
        }
        self._saved_optional = {
            name: getattr(llm_client.config, name, None)
            for name in (
                "LLM_CACHE_ENABLED",
                "LLM_CACHE_DIR",
                "LLM_CACHE_VERSION",
            )
        }
        llm_client.config.LLM_CACHE_ENABLED = False

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(llm_client.config, name, value)
        for name, value in self._saved_optional.items():
            if value is None and hasattr(llm_client.config, name):
                delattr(llm_client.config, name)
            elif value is not None:
                setattr(llm_client.config, name, value)

    def _fake_codex_command(self, tmpdir: Path) -> str:
        fake = tmpdir / "fake_codex.py"
        fake.write_text(
            textwrap.dedent(
                """
                import json
                import sys

                args = sys.argv[1:]
                prompt = sys.stdin.read()
                output_path = args[args.index('--output-last-message') + 1]
                payload = {
                    'provider': 'codex',
                    'saw_json_instruction': '유효한 JSON' in prompt,
                    'saw_system': '[시스템 지침]' in prompt,
                    'has_image': '--image' in args,
                    'image_count': args.count('--image'),
                    'has_ask_for_approval': '--ask-for-approval' in args,
                }
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False)
                print('ignored stdout')
                """
            ),
            encoding="utf-8",
        )
        return f"{sys.executable} {fake}"


    def _fake_counting_codex_command(self, tmpdir: Path) -> str:
        fake = tmpdir / "fake_counting_codex.py"
        count_path = tmpdir / "call_count.txt"
        fake.write_text(
            textwrap.dedent(
                f"""
                import json
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                sys.stdin.read()
                output_path = args[args.index('--output-last-message') + 1]
                count_path = Path({str(count_path)!r})
                current = int(count_path.read_text(encoding='utf-8')) if count_path.exists() else 0
                count_path.write_text(str(current + 1), encoding='utf-8')
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump({{'call_index': current + 1}}, f)
                """
            ),
            encoding="utf-8",
        )
        return f"{sys.executable} {fake}"

    def _fake_quota_command(self, tmpdir: Path) -> str:
        fake = tmpdir / "fake_quota.py"
        fake.write_text(
            textwrap.dedent(
                """
                import sys

                print("You've hit your session limit · resets 7:50pm (Asia/Seoul)")
                sys.exit(1)
                """
            ),
            encoding="utf-8",
        )
        return f"{sys.executable} {fake}"

    def test_call_text_uses_codex_cli_output_last_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""

            raw = llm_client.call_text('{"answer": true}', system="system", max_retries=1)
            parsed = llm_client.parse_json(raw)

        self.assertEqual(parsed["provider"], "codex")
        self.assertTrue(parsed["saw_json_instruction"])
        self.assertTrue(parsed["saw_system"])
        self.assertFalse(parsed["has_image"])
        self.assertFalse(parsed["has_ask_for_approval"])

    def test_call_vision_attaches_image_to_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""

            image_b64 = base64.b64encode(b"not really a png").decode("ascii")
            raw = llm_client.call_vision(image_b64, '{"image": true}', max_retries=1)
            parsed = llm_client.parse_json(raw)

        self.assertTrue(parsed["has_image"])

    def test_call_vision_batch_attaches_all_images_to_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""

            image_b64 = base64.b64encode(b"not really a png").decode("ascii")
            raw = llm_client.call_vision_batch([image_b64, image_b64], '{"images": true}', max_retries=1)
            parsed = llm_client.parse_json(raw)

        self.assertTrue(parsed["has_image"])
        self.assertEqual(parsed["image_count"], 2)


    def test_call_text_reuses_identical_successful_response_from_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_counting_codex_command(tmpdir)
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.LLM_CACHE_ENABLED = True
            llm_client.config.LLM_CACHE_DIR = str(tmpdir / "cache")
            llm_client.config.LLM_CACHE_VERSION = "test-v1"

            first = llm_client.parse_json(llm_client.call_text('{"answer": true}', system="system", max_retries=1))
            second = llm_client.parse_json(llm_client.call_text('{"answer": true}', system="system", max_retries=1))
            call_count = (tmpdir / "call_count.txt").read_text(encoding="utf-8")

        self.assertEqual(first, {"call_index": 1})
        self.assertEqual(second, {"call_index": 1})
        self.assertEqual(call_count, "1")

    def test_call_text_cache_key_includes_prompt_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_counting_codex_command(tmpdir)
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.LLM_CACHE_ENABLED = True
            llm_client.config.LLM_CACHE_DIR = str(tmpdir / "cache")
            llm_client.config.LLM_CACHE_VERSION = "test-v1"

            llm_client.call_text('{"answer": true}', system="system", max_retries=1)
            llm_client.call_text('{"answer": false}', system="system", max_retries=1)
            call_count = (tmpdir / "call_count.txt").read_text(encoding="utf-8")

        self.assertEqual(call_count, "2")

    def test_quota_errors_raise_instead_of_returning_empty_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_quota_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""

            with self.assertRaises(llm_client.LLMQuotaExceededError):
                llm_client.call_text('{"answer": true}', system="system", max_retries=3)

    def test_gemini_vision_batch_uses_batch_call(self):
        calls = []
        original = llm_client._call_gemini_vision_batch

        def fake_batch(images_b64, prompt, system="", max_retries=1):
            calls.append((images_b64, prompt, system, max_retries))
            return '{"ok": true}'

        try:
            llm_client.config.LLM_PROVIDER = "gemini"
            llm_client.config.GEMINI_API_KEY = "test-key"
            llm_client._call_gemini_vision_batch = fake_batch

            raw = llm_client.call_vision_batch(["a", "b"], "prompt", system="system", max_retries=1)
            parsed = llm_client.parse_json(raw)
        finally:
            llm_client._call_gemini_vision_batch = original

        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["a", "b"])

    def test_parse_json_recovers_wrapped_object_and_arrays(self):
        self.assertEqual(llm_client.parse_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(llm_client.parse_json('앞 설명 {"rows": [1, 2]} 뒤 설명'), {"rows": [1, 2]})
        self.assertEqual(llm_client.parse_json('[{"x": 1}]'), [{"x": 1}])

    def test_gemini_sdk_is_not_imported_for_local_provider(self):
        self.assertNotIn("google.genai", sys.modules)


if __name__ == "__main__":
    unittest.main()
