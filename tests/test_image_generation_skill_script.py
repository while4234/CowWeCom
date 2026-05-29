import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "image-generation" / "scripts" / "generate.py"
DEMO_ASSET = PROJECT_ROOT / "skills" / "image-generation" / "assets" / "codex-imagegen-demo.png"
PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ARK_API_KEY",
    "DASHSCOPE_API_KEY",
    "MINIMAX_API_KEY",
    "LINKAI_API_KEY",
)


def load_generate_module():
    spec = importlib.util.spec_from_file_location("image_generation_generate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_generate(payload, extra_env=None):
    env = os.environ.copy()
    for key in PROVIDER_KEYS:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), json.dumps(payload)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )


class TestImageGenerationSkillScript(unittest.TestCase):
    def test_codex_demo_asset_exists(self):
        self.assertTrue(DEMO_ASSET.exists())
        self.assertGreater(DEMO_ASSET.stat().st_size, 1024)

    def test_codex_auth_provider_generates_text_to_image(self):
        module = load_generate_module()
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMB/6X7n8sAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text(
                json.dumps({"tokens": {"access_token": "test-access-token", "account_id": "test-account"}}),
                encoding="utf-8",
            )
            provider = module.CodexAuthProvider(auth_file=str(auth_file), model="gpt-image-2")
            calls = []

            def fake_post_sse_json_events(url, payload):
                calls.append((url, payload))
                return [{"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": png_b64}}]

            provider._post_sse_json_events = fake_post_sse_json_events
            paths = provider.generate(
                "draw a tiny blue cube",
                quality="low",
                size="1024x1024",
                output_dir=tmp,
            )

            self.assertEqual(len(paths), 1)
            self.assertTrue(Path(paths[0]).exists())
            self.assertEqual(calls[0][0], "https://chatgpt.com/backend-api/codex/responses")
            self.assertEqual(calls[0][1]["model"], provider.model)
            content = calls[0][1]["input"][0]["content"]
            self.assertEqual([part["type"] for part in content], ["input_text"])
            self.assertEqual(calls[0][1]["tool_choice"], {"type": "image_generation"})
            self.assertTrue(calls[0][1]["stream"])
            self.assertFalse(calls[0][1]["store"])

    def test_codex_auth_provider_generates_image_to_image(self):
        module = load_generate_module()
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMB/6X7n8sAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            input_image = Path(tmp) / "input.png"
            input_image.write_bytes(base64.b64decode(png_b64))
            auth_file.write_text(
                json.dumps({"tokens": {"access_token": "test-access-token", "account_id": "test-account"}}),
                encoding="utf-8",
            )
            provider = module.CodexAuthProvider(auth_file=str(auth_file), model="")
            calls = []

            def fake_post_sse_json_events(url, payload):
                calls.append((url, payload))
                return [{"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": png_b64}}]

            provider._post_sse_json_events = fake_post_sse_json_events
            paths = provider.generate(
                "turn the cube red",
                image_url=str(input_image),
                output_dir=tmp,
            )

            self.assertEqual(len(paths), 1)
            content = calls[0][1]["input"][0]["content"]
            self.assertEqual([part["type"] for part in content], ["input_text", "input_image"])
            self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

    def test_codex_auth_alias_uses_direct_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_auth = Path(tmp) / "missing-auth.json"
            result = run_generate(
                {"prompt": "draw a tiny blue cube", "provider": "codex"},
                extra_env={"CODEX_AUTH_FILE": str(missing_auth)},
            )

        self.assertNotEqual(result.returncode, 0)
        body = json.loads(result.stdout)
        self.assertIn("Codex auth file not found", body["error"])
        self.assertNotIn("cannot consume Codex login auth", body["error"])

    def test_accepts_codex_input_alias_before_provider_resolution(self):
        result = run_generate({"input": "draw a tiny blue cube"})

        self.assertNotEqual(result.returncode, 0)
        body = json.loads(result.stdout)
        self.assertNotIn("Missing required parameter", body["error"])
        self.assertIn("No API key configured", body["error"])

    def test_external_broker_command_generates_image(self):
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMB/6X7n8sAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as tmp:
            broker = Path(tmp) / "fake_broker.py"
            broker.write_text(
                "\n".join([
                    "import base64, json, pathlib, sys",
                    "payload = json.loads(sys.stdin.read())",
                    "out = pathlib.Path(payload['output_dir']) / 'broker-output.png'",
                    "out.parent.mkdir(parents=True, exist_ok=True)",
                    f"out.write_bytes(base64.b64decode({png_b64!r}))",
                    "print(json.dumps({'images': [{'url': str(out)}]}))",
                ]),
                encoding="utf-8",
            )
            command = json.dumps([sys.executable, str(broker)])
            result = run_generate(
                {"prompt": "draw a tiny blue cube", "output_dir": tmp},
                extra_env={"IMAGE_GENERATION_BROKER_COMMAND_JSON": command},
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            body = json.loads(result.stdout)
            self.assertEqual(body["model"], "external-image-broker")
            image_path = Path(body["images"][0]["url"])
            self.assertTrue(image_path.exists())

    def test_codex_broker_runtime_requires_broker_even_with_api_key(self):
        result = run_generate(
            {"prompt": "draw a tiny blue cube", "runtime": "codex_broker"},
            extra_env={
                "OPENAI_API_KEY": "test-key-that-must-not-be-used",
                "OPENAI_API_BASE": "https://proxy.example/openai",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        body = json.loads(result.stdout)
        self.assertIn("Codex broker runtime is enabled", body["error"])
        self.assertNotIn("Trying OpenAI", result.stderr)
        self.assertNotIn("/images/", result.stderr + result.stdout)

    def test_codex_broker_runtime_passes_image_url_to_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Path(tmp) / "fake_broker.py"
            payload_file = Path(tmp) / "payload.json"
            input_image = Path(tmp) / "input.png"
            input_image.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "/x8AAwMB/6X7n8sAAAAASUVORK5CYII="
            ))
            broker.write_text(
                "\n".join([
                    "import base64, json, pathlib, sys",
                    "payload = json.loads(sys.stdin.read())",
                    f"pathlib.Path({str(payload_file)!r}).write_text(json.dumps(payload), encoding='utf-8')",
                    "out = pathlib.Path(payload['output_dir']) / 'broker-output.png'",
                    "out.parent.mkdir(parents=True, exist_ok=True)",
                    "out.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X7n8sAAAAASUVORK5CYII='))",
                    "print(json.dumps({'images': [{'url': str(out)}]}))",
                ]),
                encoding="utf-8",
            )
            command = json.dumps([sys.executable, str(broker)])
            result = run_generate(
                {
                    "prompt": "turn this into a red outfit",
                    "runtime": "codex_broker",
                    "image_url": str(input_image),
                    "output_dir": tmp,
                },
                extra_env={
                    "SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON": command,
                    "OPENAI_API_KEY": "test-key-that-must-not-be-used",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(payload_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["prompt"], "turn this into a red outfit")
            self.assertEqual(payload["image_url"], str(input_image))
            self.assertEqual(payload["model"], "external-image-broker")
            self.assertNotIn("Trying OpenAI", result.stderr)

    def test_skill_scoped_broker_command_env_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Path(tmp) / "fake_broker.py"
            broker.write_text(
                "\n".join([
                    "import json, pathlib, sys",
                    "payload = json.loads(sys.stdin.read())",
                    "out = pathlib.Path(payload['output_dir']) / 'skill-env-output.png'",
                    "out.write_bytes(b'image-bytes')",
                    "print(json.dumps({'images': [{'url': str(out)}]}))",
                ]),
                encoding="utf-8",
            )
            command = json.dumps([sys.executable, str(broker)])
            result = run_generate(
                {"prompt": "draw a tiny blue cube", "output_dir": tmp},
                extra_env={"SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON": command},
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            body = json.loads(result.stdout)
            self.assertTrue(Path(body["images"][0]["url"]).exists())

    def test_provider_fallback_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Path(tmp) / "failing_broker.py"
            broker.write_text("import sys; print('broker failed', file=sys.stderr); sys.exit(2)", encoding="utf-8")
            command = json.dumps([sys.executable, str(broker)])
            result = run_generate(
                {"prompt": "draw a tiny blue cube", "output_dir": tmp},
                extra_env={
                    "IMAGE_GENERATION_BROKER_COMMAND_JSON": command,
                    "OPENAI_API_KEY": "test-key-that-must-not-be-used",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Broker", result.stdout)
            self.assertNotIn("OpenAI", result.stderr)
            self.assertNotIn("Trying OpenAI", result.stderr)

    def test_external_broker_utf8_stderr_does_not_crash_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = Path(tmp) / "failing_utf8_broker.py"
            broker.write_text(
                "import os, sys; os.write(sys.stderr.fileno(), '错误：生成失败 ✅'.encode('utf-8')); sys.exit(9)",
                encoding="utf-8",
            )
            command = json.dumps([sys.executable, str(broker)])

            result = run_generate(
                {"prompt": "draw a tiny blue cube", "output_dir": tmp},
                extra_env={"IMAGE_GENERATION_BROKER_COMMAND_JSON": command},
            )

            self.assertNotEqual(result.returncode, 0)
            body = json.loads(result.stdout)
            self.assertIn("external broker exited 9", body["error"])
            self.assertIn("生成失败", body["error"])

    def test_grok_runtime_keeps_cowwecom_logs_off_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "fake_root"
            output_dir = Path(tmp) / "out"
            provider_pkg = fake_root / "integrations" / "hermes_xai"
            common_pkg = fake_root / "common"
            provider_pkg.mkdir(parents=True)
            common_pkg.mkdir(parents=True)
            (fake_root / "integrations" / "__init__.py").write_text("", encoding="utf-8")
            (provider_pkg / "__init__.py").write_text("", encoding="utf-8")
            (common_pkg / "__init__.py").write_text("", encoding="utf-8")
            (common_pkg / "log.py").write_text(
                "\n".join(
                    [
                        "import io",
                        "import logging",
                        "import sys",
                        "logger = logging.getLogger('fake-cowwecom-log')",
                        "logger.handlers.clear()",
                        "stream = sys.stdout",
                        "if hasattr(stream, 'buffer'):",
                        "    stream = io.TextIOWrapper(stream.buffer, encoding='utf-8', errors='replace', line_buffering=True)",
                        "handler = logging.StreamHandler(stream)",
                        "handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))",
                        "logger.addHandler(handler)",
                        "logger.setLevel(logging.INFO)",
                        "logger.propagate = False",
                    ]
                ),
                encoding="utf-8",
            )
            (provider_pkg / "image_gen.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "from common.log import logger",
                        "class XAIImageGenProvider:",
                        "    def generate(self, prompt, *, aspect_ratio=None, resolution=None, model=None, prompt_enhancement=True):",
                        "        logger.info('fake grok provider log on cow logger')",
                        "        path = Path(__file__).resolve().parents[2] / 'source.jpg'",
                        "        path.write_bytes(b'\\xff\\xd8\\xff\\xe0fake-jpeg')",
                        "        return str(path)",
                    ]
                ),
                encoding="utf-8",
            )

            result = run_generate(
                {
                    "prompt": "use grok to draw a test cup",
                    "runtime": "grok",
                    "output_dir": str(output_dir),
                },
                extra_env={"COWWECHAT_ROOT": str(fake_root)},
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            body = json.loads(result.stdout)
            self.assertEqual(body["model"], "grok-imagine-image")
            self.assertTrue(Path(body["images"][0]["url"]).exists())
            self.assertNotIn("fake grok provider log", result.stdout)
            self.assertIn("fake grok provider log", result.stderr)

    def test_openai_responses_wire_api_uses_responses_endpoint(self):
        module = load_generate_module()
        image_b64 = base64.b64encode(b"responses-image").decode()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "OPENAI_WIRE_API": "responses",
                "SKILL_IMAGE_GENERATION_RESPONSES_MODEL": "gpt-5.5",
            },
            clear=False,
        ):
            provider = module.OpenAIProvider("test-key", "https://proxy.example/openai", "gpt-image-2")
            calls = []

            def fake_post_sse_json_events(url, payload):
                calls.append((url, payload))
                return [{"type": "image_generation_call", "result": image_b64}]

            provider._post_sse_json_events = fake_post_sse_json_events
            paths = provider._create("draw a tiny blue cube", quality="auto", size="1024x1024", output_dir=tmp)

            self.assertEqual(len(paths), 1)
            self.assertTrue(Path(paths[0]).exists())
            self.assertEqual(calls[0][0], "https://proxy.example/openai/responses")
            self.assertEqual(calls[0][1]["model"], "gpt-5.5")
            self.assertEqual(calls[0][1]["input"][0]["content"][0]["type"], "input_text")
            self.assertTrue(calls[0][1]["stream"])
            self.assertEqual(calls[0][1]["tool_choice"], {"type": "image_generation"})


if __name__ == "__main__":
    unittest.main()
