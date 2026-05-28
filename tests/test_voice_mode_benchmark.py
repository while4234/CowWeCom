# encoding:utf-8

import json

from scripts import benchmark_voice_mode_models as bench


def test_benchmark_dry_run_skips_missing_credentials_without_secrets(monkeypatch, tmp_path):
    output = tmp_path / "benchmark.json"
    monkeypatch.setattr(bench, "load_config", lambda: None)
    monkeypatch.setattr(
        bench,
        "get_effective_openai_api_config",
        lambda backend: {
            "model": "gpt-test",
            "api_key": "",
            "wire_api": "responses",
        },
    )

    assert bench.main([
        "--backend",
        "capi_monthly",
        "--dry-run",
        "--output",
        str(output),
    ]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["reasoning_effort"] == "low"
    assert payload["candidates"][0]["backend_profile"] == "capi_monthly"
    assert payload["candidates"][0]["ready"] is False
    assert payload["candidates"][0]["skip_reason"] == "missing_capi_credentials"
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "api_key" not in rendered
    assert "authorization" not in rendered
    assert "bearer" not in rendered
    assert "recommendation" in payload


def test_benchmark_accepts_reasoning_effort_override(monkeypatch, tmp_path):
    output = tmp_path / "benchmark.json"
    monkeypatch.setattr(bench, "load_config", lambda: None)
    monkeypatch.setattr(
        bench,
        "get_effective_openai_api_config",
        lambda backend: {
            "model": "gpt-test",
            "api_key": "",
            "wire_api": "responses",
        },
    )

    assert bench.main([
        "--backend",
        "capi_monthly",
        "--reasoning-effort",
        "xhigh",
        "--dry-run",
        "--output",
        str(output),
    ]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["reasoning_effort"] == "xhigh"


def test_recommendation_prefers_quality_when_latency_is_perceptually_close():
    candidates = [
        {"backend_profile": "capi", "billing_profile": "quota", "model": "fast", "ready": True},
        {"backend_profile": "codex", "billing_profile": "codex", "model": "better", "ready": True},
    ]
    results = [
        {
            "backend_profile": "capi",
            "model": "fast",
            "success": True,
            "first_voice_ready_ms": 1000,
            "quality_score": 1.0,
        },
        {
            "backend_profile": "codex",
            "model": "better",
            "success": True,
            "first_voice_ready_ms": 1500,
            "quality_score": 3.0,
        },
    ]

    recommendation = bench.recommend(candidates, results)

    assert recommendation["recommended_backend"] == "codex"
    assert recommendation["recommended_model"] == "better"
    assert recommendation["reason"] == "quality_first_within_perceptual_latency_threshold"
