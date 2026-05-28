# encoding:utf-8

import os

import pytest

from integrations.hermes_xai import tts


class FakeResponse:
    def __init__(self, status_code=200, content=b"audio", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def test_generate_xai_tts_posts_to_tts_with_oauth_token(monkeypatch, tmp_path):
    calls = []

    def fake_resolver(force_refresh=False):
        calls.append(("resolver", force_refresh))
        return {"api_key": "oauth-access-token", "base_url": "https://api.x.ai/v1"}

    def fake_post(url, headers, json, timeout):
        calls.append(("post", url, headers, json, timeout))
        return FakeResponse(content=b"mp3-bytes")

    monkeypatch.setattr(tts, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(tts.requests, "post", fake_post)

    output_path = tmp_path / "voice.mp3"
    result = tts.generate_xai_tts("你好", str(output_path))

    assert result == str(output_path)
    assert output_path.read_bytes() == b"mp3-bytes"
    post_call = [item for item in calls if item[0] == "post"][0]
    assert post_call[1] == "https://api.x.ai/v1/tts"
    assert post_call[2]["Authorization"] == "Bearer oauth-access-token"
    assert post_call[3]["voice_id"] == "eve"
    assert post_call[3]["language"] == "zh"
    assert "output_format" not in post_call[3]
    assert calls[0] == ("resolver", False)


def test_generate_xai_tts_refreshes_once_on_401(monkeypatch, tmp_path):
    resolver_force_flags = []
    responses = [FakeResponse(status_code=401, text="expired"), FakeResponse(content=b"ok")]

    def fake_resolver(force_refresh=False):
        resolver_force_flags.append(force_refresh)
        return {"api_key": "fresh-token" if force_refresh else "old-token", "base_url": "https://api.x.ai/v1"}

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(tts, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(tts.requests, "post", fake_post)

    output_path = tmp_path / "retry.mp3"
    tts.generate_xai_tts("你好", str(output_path))

    assert resolver_force_flags == [False, True]
    assert output_path.read_bytes() == b"ok"


def test_generate_xai_tts_sanitizes_errors_and_cleans_partial_file(monkeypatch, tmp_path):
    output_path = tmp_path / "failed.mp3"
    output_path.write_bytes(b"partial")

    def fake_resolver(force_refresh=False):
        return {"api_key": "secret-access-token", "base_url": "https://api.x.ai/v1"}

    def fake_post(*args, **kwargs):
        return FakeResponse(
            status_code=500,
            text='Authorization: Bearer secret-access-token access_token="secret-access-token"',
        )

    monkeypatch.setattr(tts, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(tts.requests, "post", fake_post)

    with pytest.raises(tts.XaiTtsError) as exc_info:
        tts.generate_xai_tts("你好", str(output_path))

    message = str(exc_info.value)
    assert "secret-access-token" not in message
    assert "Authorization: Bearer" not in message
    assert "<redacted>" in message
    assert not os.path.exists(output_path)


def test_generate_xai_tts_sends_output_format_for_custom_codec(monkeypatch, tmp_path):
    payloads = []

    monkeypatch.setattr(
        tts,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: {"api_key": "token", "base_url": "https://api.x.ai/v1"},
    )

    def fake_post(url, headers, json, timeout):
        payloads.append(json)
        return FakeResponse(content=b"wav")

    monkeypatch.setattr(tts.requests, "post", fake_post)

    tts.generate_xai_tts("hello", str(tmp_path / "voice.wav"), codec="wav", sample_rate=16000)

    assert payloads[0]["output_format"] == {"codec": "wav", "sample_rate": 16000}
