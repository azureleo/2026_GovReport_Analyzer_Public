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

    def tearDown(self):
        for name, value in self._saved.items():
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

    def test_parse_json_recovers_wrapped_object_and_arrays(self):
        self.assertEqual(llm_client.parse_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(llm_client.parse_json('앞 설명 {"rows": [1, 2]} 뒤 설명'), {"rows": [1, 2]})
        self.assertEqual(llm_client.parse_json('[{"x": 1}]'), [{"x": 1}])

    def test_gemini_sdk_is_not_imported_for_local_provider(self):
        self.assertNotIn("google.genai", sys.modules)


if __name__ == "__main__":
    unittest.main()
