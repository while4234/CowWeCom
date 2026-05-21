import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BROKER = PROJECT_ROOT / "skills" / "image-generation" / "scripts" / "codex_cli_broker.py"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


def load_broker_module():
    spec = importlib.util.spec_from_file_location("codex_cli_broker", BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCodexCliBroker(unittest.TestCase):
    def test_broker_invokes_codex_cli_and_copies_generated_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_codex = tmp_path / "fake_codex.py"
            payload_file = tmp_path / "codex_payload.json"
            code = "\n".join([
                "import base64, json, os, pathlib, sys",
                "stdin_text = sys.stdin.read()",
                "pathlib.Path(os.environ['FAKE_CODEX_PAYLOAD_FILE']).write_text(",
                "    json.dumps({'argv': sys.argv[1:], 'stdin': stdin_text}),",
                "    encoding='utf-8',",
                ")",
                "out = pathlib.Path(os.environ['CODEX_HOME']) / 'generated_images' / 'fake-session' / 'out.png'",
                "out.parent.mkdir(parents=True, exist_ok=True)",
                f"out.write_bytes(base64.b64decode({PNG_B64!r}))",
                "print(str(out))",
            ])
            fake_codex.write_text(code, encoding="utf-8")

            input_image = tmp_path / "input.png"
            input_image.write_bytes(base64.b64decode(PNG_B64))
            output_dir = tmp_path / "job-output"

            env = os.environ.copy()
            env.update({
                "CODEX_HOME": str(tmp_path / "codex-home"),
                "CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake_codex)]),
                "CODEX_IMAGE_BROKER_TIMEOUT": "30",
                "CODEX_IMAGE_BROKER_SCAN_TIMEOUT": "2",
                "FAKE_CODEX_PAYLOAD_FILE": str(payload_file),
            })

            result = subprocess.run(
                [sys.executable, str(BROKER)],
                input=json.dumps({
                    "prompt": "make the outfit red",
                    "image_url": str(input_image),
                    "output_dir": str(output_dir),
                    "quality": "medium",
                    "size": "1K",
                    "aspect_ratio": "1:1",
                }),
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            body = json.loads(result.stdout)
            image_path = Path(body["images"][0]["url"])
            self.assertTrue(image_path.exists())
            self.assertEqual(image_path.parent.resolve(), output_dir.resolve())

            codex_payload = json.loads(payload_file.read_text(encoding="utf-8"))
            self.assertIn("exec", codex_payload["argv"])
            self.assertIn("--ignore-user-config", codex_payload["argv"])
            self.assertIn("--image", codex_payload["argv"])
            self.assertIn(str(input_image.resolve()), codex_payload["argv"])
            self.assertIn("make the outfit red", codex_payload["stdin"])
            self.assertIn("quality: medium", codex_payload["stdin"])

    def test_broker_fails_when_codex_generates_no_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_codex = tmp_path / "fake_codex.py"
            fake_codex.write_text("import sys; sys.stdin.read(); print('done')", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "CODEX_HOME": str(tmp_path / "codex-home"),
                "CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON": json.dumps([sys.executable, str(fake_codex)]),
                "CODEX_IMAGE_BROKER_TIMEOUT": "30",
                "CODEX_IMAGE_BROKER_SCAN_TIMEOUT": "1",
            })

            result = subprocess.run(
                [sys.executable, str(BROKER)],
                input=json.dumps({"prompt": "draw a red square", "output_dir": str(tmp_path / "out")}),
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertNotEqual(result.returncode, 0)
            body = json.loads(result.stdout)
            self.assertIn("no new generated image", body["error"])

    def test_command_base_skips_unrunnable_path_candidates(self):
        module = load_broker_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad = tmp_path / "bad-codex.exe"
            good = tmp_path / "good-codex.exe"
            bad.write_text("", encoding="utf-8")
            good.write_text("", encoding="utf-8")

            with patch.dict(os.environ, {
                "CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON": "",
                "CODEX_IMAGE_BROKER_CODEX_BINARY": "",
                "CODEX_CLI_BINARY": "",
                "CODEXMOBILE_CODEX_BINARY": "",
            }, clear=False), patch.object(
                module,
                "_candidate_codex_binaries",
                return_value=[bad, good],
            ), patch.object(
                module,
                "_is_runnable_codex_binary",
                side_effect=lambda path: Path(path) == good.resolve(),
            ):
                self.assertEqual(module._codex_command_base(), [str(good.resolve())])


if __name__ == "__main__":
    unittest.main()
