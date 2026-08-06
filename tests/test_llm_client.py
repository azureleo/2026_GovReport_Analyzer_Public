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
                "CODEX_TEXT_MODEL",
                "CODEX_VISION_MODEL",
                "LOCAL_AGENT_TIMEOUT",
                "GEMINI_API_KEY",
                "OPENAI_API_KEY",
                "OPENAI_MODEL",
                "OPENAI_MAX_RETRIES",
                "OPENAI_RETRY_BASE_SECONDS",
                "OPENAI_RETRY_MAX_SECONDS",
                "OPENAI_FAIL_SOFT_ON_TRANSIENT",
                "LLM_QUOTA_WAIT_ENABLED",
                "STAGE_PROVIDERS",
                "STAGE_MODELS",
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
                model = args[args.index('--model') + 1] if '--model' in args else ''
                payload = {
                    'provider': 'codex',
                    'model': model,
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

    def test_auto_provider_falls_back_to_openai_when_only_openai_key_exists(self):
        llm_client.config.LLM_PROVIDER = "auto"
        llm_client.config.CODEX_COMMAND = "__missing_codex_for_test__"
        llm_client.config.CLAUDE_COMMAND = "__missing_claude_for_test__"
        llm_client.config.GEMINI_API_KEY = ""
        llm_client.config.OPENAI_API_KEY = "openai-key"

        provider = llm_client._resolve_provider()

        self.assertEqual(provider, "openai")

    def test_openai_requires_api_key_with_korean_message(self):
        llm_client.config.LLM_PROVIDER = "openai"
        llm_client.config.OPENAI_API_KEY = ""

        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            llm_client._resolve_provider()

    def test_openai_stage_model_is_used_for_call_and_cache_key(self):
        calls = []

        class FakeResponses:
            def create(self, **kwargs):
                calls.append(kwargs)
                return type("Response", (), {"output_text": f'{{"model": "{kwargs["model"]}", "call": {len(calls)}}}'})()

        class FakeOpenAI:
            def __init__(self, api_key):
                self.api_key = api_key
                self.responses = FakeResponses()

        original_client_class = llm_client._get_openai_client_class

        try:
            with tempfile.TemporaryDirectory() as tmp:
                llm_client.config.LLM_PROVIDER = "openai"
                llm_client.config.OPENAI_API_KEY = "openai-key"
                llm_client.config.OPENAI_MODEL = "global-openai"
                llm_client.config.STAGE_MODELS = {"review": "review-openai-a"}
                llm_client.config.LLM_CACHE_ENABLED = True
                llm_client.config.LLM_CACHE_DIR = str(Path(tmp) / "cache")
                llm_client.config.LLM_CACHE_VERSION = "openai-stage-cache"
                llm_client._get_openai_client_class = lambda: FakeOpenAI

                first = llm_client.parse_json(llm_client.call_text('{"answer": true}', stage="review"))
                cached = llm_client.parse_json(llm_client.call_text('{"answer": true}', stage="review"))

                llm_client.config.STAGE_MODELS = {"review": "review-openai-b"}
                changed_model = llm_client.parse_json(llm_client.call_text('{"answer": true}', stage="review"))
        finally:
            llm_client._get_openai_client_class = original_client_class

        self.assertEqual(first, {"model": "review-openai-a", "call": 1})
        self.assertEqual(cached, first)
        self.assertEqual(changed_model, {"model": "review-openai-b", "call": 2})
        self.assertEqual([call["model"] for call in calls], ["review-openai-a", "review-openai-b"])

    def test_openai_quota_marker_raises_quota_error(self):
        class FakeResponses:
            def create(self, **kwargs):
                raise RuntimeError("insufficient_quota: billing hard limit")

        class FakeOpenAI:
            def __init__(self, api_key):
                self.api_key = api_key
                self.responses = FakeResponses()

        original_client_class = llm_client._get_openai_client_class
        original_sleep = llm_client.time.sleep

        try:
            llm_client.config.LLM_PROVIDER = "openai"
            llm_client.config.OPENAI_API_KEY = "openai-key"
            llm_client.config.OPENAI_MODEL = "global-openai"
            llm_client.config.OPENAI_MAX_RETRIES = 3
            llm_client.config.LLM_CACHE_ENABLED = False
            llm_client._get_openai_client_class = lambda: FakeOpenAI
            llm_client.time.sleep = lambda seconds: None

            with self.assertRaises(llm_client.LLMQuotaExceededError):
                llm_client.call_text('{"answer": true}', max_retries=1)
        finally:
            llm_client._get_openai_client_class = original_client_class
            llm_client.time.sleep = original_sleep

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

    def test_codex_vision은_기본으로_CODEX_VISION_MODEL을_지정한다(self):
        # 벤치마크 채택(2026-07-10): 에이전트 모드 vision 기본 = gpt-5.6-luna
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.CODEX_VISION_MODEL = "gpt-5.6-luna"
            llm_client.config.STAGE_MODELS = {
                **llm_client.config.STAGE_MODELS,
                "vision": "",
            }

            image_b64 = base64.b64encode(b"not really a png").decode("ascii")
            raw = llm_client.call_vision(image_b64, '{"image": true}', max_retries=1, stage="vision")
            parsed = llm_client.parse_json(raw)

        self.assertEqual(parsed["model"], "gpt-5.6-luna")

    def test_codex_vision은_STAGE_MODEL_VISION_오버라이드가_기본값을_이긴다(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.STAGE_MODELS = {**llm_client.config.STAGE_MODELS, "vision": "gpt-테스트모델"}

            image_b64 = base64.b64encode(b"not really a png").decode("ascii")
            raw = llm_client.call_vision(image_b64, '{"image": true}', max_retries=1, stage="vision")
            parsed = llm_client.parse_json(raw)

        self.assertEqual(parsed["model"], "gpt-테스트모델")

    def test_codex_텍스트호출은_텍스트_기본모델을_지정한다(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.CODEX_TEXT_MODEL = "gpt-5.6-luna"

            raw = llm_client.call_text('{"answer": true}', system="system", max_retries=1, stage="extraction")
            parsed = llm_client.parse_json(raw)

        self.assertEqual(parsed["model"], "gpt-5.6-luna")


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

    def test_stage_provider_and_model_override_route_without_changing_global_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "gemini"
            llm_client.config.GEMINI_API_KEY = ""
            llm_client.config.CODEX_COMMAND = self._fake_codex_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = "global-model"
            llm_client.config.STAGE_PROVIDERS = {"review": "codex"}
            llm_client.config.STAGE_MODELS = {"review": "stage-model"}

            raw = llm_client.call_text('{"answer": true}', system="system", max_retries=1, stage="review")
            parsed = llm_client.parse_json(raw)

        self.assertEqual(parsed["provider"], "codex")
        self.assertEqual(parsed["model"], "stage-model")

    def test_quota_errors_raise_instead_of_returning_empty_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm_client.config.LLM_PROVIDER = "codex"
            llm_client.config.CODEX_COMMAND = self._fake_quota_command(Path(tmp))
            llm_client.config.LOCAL_AGENT_TIMEOUT = 5
            llm_client.config.LOCAL_AGENT_MODEL = ""
            llm_client.config.LLM_QUOTA_WAIT_ENABLED = False

            with self.assertRaises(llm_client.LLMQuotaExceededError):
                llm_client.call_text('{"answer": true}', system="system", max_retries=3)

    def test_local_agent_cleanup_race_keeps_successful_response_without_retry(self):
        calls = {"run": 0, "sleep": 0}

        class RacingTemporaryDirectory:
            def __init__(self, prefix="", ignore_cleanup_errors=False):
                self.ignore_cleanup_errors = ignore_cleanup_errors
                self.path = Path(tempfile.mkdtemp(prefix=prefix))

            def __enter__(self):
                return str(self.path)

            def __exit__(self, exc_type, exc, traceback):
                if self.ignore_cleanup_errors:
                    return False
                raise OSError("Directory not empty")

        original_tempdir = llm_client.tempfile.TemporaryDirectory
        original_run_codex = llm_client._run_codex
        original_sleep = llm_client.time.sleep

        def fake_run_codex(prompt, *, image_paths=None, cwd, model=None):
            calls["run"] += 1
            return '{"ok": true}'

        try:
            llm_client.tempfile.TemporaryDirectory = RacingTemporaryDirectory
            llm_client._run_codex = fake_run_codex
            llm_client.time.sleep = lambda seconds: calls.__setitem__("sleep", calls["sleep"] + 1)

            result = llm_client._retry_local_call(
                lambda: llm_client._call_local_agent('{"answer": true}', "", provider="codex"),
                max_retries=3,
                label="codex",
            )
        finally:
            llm_client.tempfile.TemporaryDirectory = original_tempdir
            llm_client._run_codex = original_run_codex
            llm_client.time.sleep = original_sleep

        self.assertEqual(result, '{"ok": true}')
        self.assertEqual(calls, {"run": 1, "sleep": 0})

    def test_run_command_decodes_subprocess_output_with_utf8_replacement(self):
        captured = {}

        class Completed:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        original_run = llm_client.subprocess.run

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return Completed()

        try:
            llm_client.subprocess.run = fake_run

            result = llm_client._run_command(["fake"], "프롬프트", cwd=Path("."), timeout=5)
        finally:
            llm_client.subprocess.run = original_run

        self.assertEqual(result, '{"ok": true}')
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["errors"], "replace")

    def test_gemini_vision_batch_uses_batch_call(self):
        calls = []
        original = llm_client._call_gemini_vision_batch

        def fake_batch(images_b64, prompt, system="", max_retries=1, model=None):
            calls.append((images_b64, prompt, system, max_retries, model))
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
