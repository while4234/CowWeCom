# encoding:utf-8

import base64
import logging

import pytest

from integrations.hermes_xai import image_gen
from integrations.hermes_xai.auth import AuthError


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"


class FakePostResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_provider_posts_images_generation_with_oauth_token_and_payload(monkeypatch, tmp_path, caplog):
    calls = []

    def fake_resolver(force_refresh=False):
        calls.append(("resolver", force_refresh))
        return {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "oauth-access-token",
            "base_url": "https://api.x.ai/v1",
        }

    def fake_post(url, headers, json, timeout):
        calls.append(("post", url, headers, json, timeout))
        return FakePostResponse(
            payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(image_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    with caplog.at_level(logging.INFO):
        path = image_gen.XAIImageGenProvider().generate(
            "paint a calm lake",
            aspect_ratio="landscape",
            resolution="2k",
            model="grok-imagine-image-quality",
        )

    assert calls[0] == ("resolver", False)
    post_call = [item for item in calls if item[0] == "post"][0]
    assert post_call[1] == "https://api.x.ai/v1/images/generations"
    assert post_call[2]["Authorization"] == "Bearer oauth-access-token"
    assert post_call[3]["model"] == "grok-imagine-image-quality"
    assert "paint a calm lake" in post_call[3]["prompt"]
    assert "[CowWeCom hidden image prompt enhancement]" in post_call[3]["prompt"]
    assert post_call[3]["aspect_ratio"] == "16:9"
    assert post_call[3]["resolution"] == "2k"
    assert post_call[4] == 120.0
    assert path.endswith(".png")
    assert tmp_path in tmp_path.__class__(path).parents
    assert tmp_path.__class__(path).read_bytes() == PNG_BYTES
    assert "oauth-access-token" not in caplog.text
    assert "Authorization: Bearer" not in caplog.text


def test_provider_refreshes_once_on_401(monkeypatch, tmp_path):
    resolver_force_flags = []
    post_tokens = []
    responses = [
        FakePostResponse(status_code=401, payload={"error": {"message": "expired"}}),
        FakePostResponse(payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}),
    ]

    def fake_resolver(force_refresh=False):
        resolver_force_flags.append(force_refresh)
        return {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "fresh-token" if force_refresh else "old-token",
            "base_url": "https://api.x.ai/v1",
        }

    def fake_post(url, headers, json, timeout):
        post_tokens.append(headers["Authorization"])
        return responses.pop(0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(image_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    path = image_gen.XAIImageGenProvider().generate("retry image")

    assert resolver_force_flags == [False, True]
    assert post_tokens == ["Bearer old-token", "Bearer fresh-token"]
    assert tmp_path.__class__(path).exists()


def test_provider_sanitizes_http_errors(monkeypatch, tmp_path):
    def fake_resolver(force_refresh=False):
        return {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "secret-token",
            "base_url": "https://api.x.ai/v1",
        }

    def fake_post(*args, **kwargs):
        return FakePostResponse(
            status_code=500,
            payload={"error": {"message": "Authorization: Bearer secret-token access_token=secret-token"}},
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(image_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("fail image")

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "Authorization: Bearer" not in message
    assert "<redacted>" in message


def test_provider_timeout_returns_clear_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        image_gen,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "token",
            "base_url": "https://api.x.ai/v1",
        },
    )
    monkeypatch.setattr(image_gen.requests, "post", lambda *args, **kwargs: (_ for _ in ()).throw(image_gen.requests.Timeout()))

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("slow image", resolution="bad")

    assert "timed out" in str(exc_info.value)


def test_provider_reports_missing_grok_login(monkeypatch):
    monkeypatch.setattr(
        image_gen,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: (_ for _ in ()).throw(
            AuthError("Grok account is not logged in.", code="xai_auth_missing", relogin_required=True)
        ),
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("login please")

    assert "not logged in" in str(exc_info.value)
