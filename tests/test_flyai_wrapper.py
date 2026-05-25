import importlib.util
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = PROJECT_ROOT / "skills" / "flyai" / "scripts" / "flyai_wrapper.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("flyai_wrapper", WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(["flyai"], returncode, stdout=stdout, stderr=stderr)


def test_libuv_failure_with_success_json_returns_warning_and_zero_exit():
    wrapper = _load_wrapper()
    stdout = json.dumps({"status": 0, "message": "success", "data": {"itemList": []}})
    stderr = "Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file src\\win\\handle.c"

    response, exit_code = wrapper.run_wrapper(
        ["keyword-search", "--query", "what to do in Sanya"],
        runner=lambda *args, **kwargs: _completed(1, stdout=stdout, stderr=stderr),
        flyai_bin="flyai",
    )

    assert exit_code == 0
    assert response["ok"] is True
    assert response["result"]["status"] == 0
    assert response["_flyai_wrapper"]["flyai_exit_code"] == 1
    assert "known Windows libuv assertion" in response["_flyai_wrapper"]["warnings"][0]


def test_success_json_is_redacted_before_returning():
    wrapper = _load_wrapper()
    stdout = json.dumps(
        {
            "status": 0,
            "data": {
                "api_key": "unit-test-secret",
                "note": "Authorization: Bearer should-not-leak",
            },
        }
    )

    response, exit_code = wrapper.run_wrapper(
        ["keyword-search", "--query", "x"],
        runner=lambda *args, **kwargs: _completed(0, stdout=stdout),
        flyai_bin="flyai",
    )

    serialized = json.dumps(response)
    assert exit_code == 0
    assert "unit-test-secret" not in serialized
    assert "should-not-leak" not in serialized
    assert response["result"]["data"]["api_key"] == "<redacted>"
    assert response["result"]["data"]["note"] == "Authorization=<redacted>"


def test_invalid_json_failure_redacts_stderr():
    wrapper = _load_wrapper()
    stderr = "failed with FLYAI_API_KEY=unit-test-secret"

    response, exit_code = wrapper.run_wrapper(
        ["keyword-search", "--query", "x"],
        runner=lambda *args, **kwargs: _completed(2, stdout="not json", stderr=stderr),
        flyai_bin="flyai",
    )

    serialized = json.dumps(response)
    assert exit_code == 2
    assert response["ok"] is False
    assert response["error"] == "flyai CLI did not produce valid JSON on stdout."
    assert "unit-test-secret" not in serialized
    assert "FLYAI_API_KEY=<redacted>" in response["_flyai_wrapper"]["stderr"]


def test_json_can_be_extracted_from_stdout_with_extra_text():
    wrapper = _load_wrapper()
    stdout = 'debug line\n{"status":0,"message":"success"}\n'

    response, exit_code = wrapper.run_wrapper(
        ["keyword-search", "--query", "x"],
        runner=lambda *args, **kwargs: _completed(0, stdout=stdout),
        flyai_bin="flyai",
    )

    assert exit_code == 0
    assert response["ok"] is True
    assert response["result"] == {"status": 0, "message": "success"}
