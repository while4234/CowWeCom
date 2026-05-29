# encoding:utf-8

import base64
import logging

import pytest

from common import grok_image_prompt_rewriter
from integrations.hermes_xai import image_gen
from integrations.hermes_xai.auth import AuthError


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"


class FixedRandom:
    def __init__(self, roll):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, values):
        return values[0]


def write_grok_repository_skill(root):
    skill_dir = root / "image-prompt-optimization"
    repositories = skill_dir / "repositories"
    (repositories / "grok").mkdir(parents=True)
    (repositories / "general").mkdir()
    (skill_dir / "SKILL.md").write_text("# image-prompt-optimization\n", encoding="utf-8")
    (repositories / "grok" / "visual.txt").write_text("grok cinematic detail\n", encoding="utf-8")
    (repositories / "general" / "visual.txt").write_text("general cinematic detail\n", encoding="utf-8")
    return skill_dir


def png_with_dimensions(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


class FakePostResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_provider_posts_images_generation_with_oauth_token_and_payload(monkeypatch, tmp_path, caplog):
    calls = []
    rewrite_calls = []
    skill_dir = write_grok_repository_skill(tmp_path)

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
    monkeypatch.setenv("IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: FixedRandom(0.1))
    monkeypatch.setattr(image_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    def fake_rewrite(system_prompt, user_prompt):
        rewrite_calls.append((system_prompt, user_prompt))
        return "A rewritten Grok prompt for a calm cinematic lake."

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_rewrite)

    with caplog.at_level(logging.INFO):
        provider = image_gen.XAIImageGenProvider()
        path = provider.generate(
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
    assert post_call[3]["prompt"] == "A rewritten Grok prompt for a calm cinematic lake."
    assert "[CowWeCom hidden image prompt enhancement]" not in post_call[3]["prompt"]
    assert "matched_prompt_repository_keyword: grok" in rewrite_calls[0][1]
    assert "repository_selection_rule: 90% from grok, 10% from other repositories" in rewrite_calls[0][1]
    assert "[grok/visual.txt:1] grok cinematic detail" in rewrite_calls[0][1]
    assert provider.last_prompt_metadata["library"]["keyword"] == "grok"
    assert provider.last_prompt_metadata["supplements"][0]["repository"] == "grok"
    assert post_call[3]["aspect_ratio"] == "16:9"
    assert post_call[3]["resolution"] == "2k"
    assert "image" not in post_call[3]
    assert post_call[4] == 120.0
    assert path.endswith(".png")
    assert tmp_path in tmp_path.__class__(path).parents
    assert tmp_path.__class__(path).read_bytes() == PNG_BYTES
    assert "oauth-access-token" not in caplog.text
    assert "Authorization: Bearer" not in caplog.text


def test_provider_can_skip_prompt_enhancement(monkeypatch, tmp_path):
    payloads = []

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

    def fake_post(url, headers, json, timeout):
        payloads.append(json)
        return FakePostResponse(
            payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        )

    monkeypatch.setattr(image_gen.requests, "post", fake_post)
    monkeypatch.setattr(
        image_gen,
        "enhance_image_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("enhancer should not run")),
    )

    image_gen.XAIImageGenProvider().generate("raw prompt", prompt_enhancement=False)

    assert payloads[0]["prompt"] == "raw prompt"


def test_provider_adds_reference_identity_lock_for_direct_image_edit(monkeypatch, tmp_path):
    reference = tmp_path / "portrait.png"
    reference_bytes = png_with_dimensions(900, 1600)
    reference.write_bytes(reference_bytes)
    payloads = []

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

    def fake_post(url, headers, json, timeout):
        payloads.append(json)
        return FakePostResponse(
            payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        )

    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    image_gen.XAIImageGenProvider().generate(
        "change the outfit",
        image_url=str(reference),
        prompt_enhancement=False,
    )

    prompt = payloads[0]["prompt"]
    assert "change the outfit" in prompt
    assert "Reference image identity lock:" in prompt
    assert "preserve the reference subject's exact face" in prompt
    assert "do not invent a new person" in prompt


def test_provider_sends_single_reference_image_payload_as_data_uri(monkeypatch, tmp_path):
    reference = tmp_path / "portrait.png"
    reference_bytes = png_with_dimensions(900, 1600)
    reference.write_bytes(reference_bytes)
    post_calls = []

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

    def fake_post(url, headers, json, timeout):
        post_calls.append((url, json))
        return FakePostResponse(
            payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}
        )

    monkeypatch.setattr(image_gen.requests, "post", fake_post)

    image_gen.XAIImageGenProvider().generate(
        "turn this into a poster",
        image_url=str(reference),
        prompt_enhancement=False,
    )

    assert post_calls[0][0] == "https://api.x.ai/v1/images/edits"
    payload = post_calls[0][1]
    assert payload["image"]["url"].startswith("data:image/png;base64,")
    assert payload["image"]["type"] == "image_url"
    assert base64.b64decode(payload["image"]["url"].split(",", 1)[1]) == reference_bytes
    assert payload["aspect_ratio"] == "9:16"
    assert payload["resolution"] == "2k"


def test_provider_accepts_http_reference_without_logging_sensitive_query(monkeypatch, tmp_path, caplog):
    payloads = []
    reference_url = "https://cdn.example.test/input.png?token=secret-token"

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
    monkeypatch.setattr(
        image_gen.requests,
        "post",
        lambda url, headers, json, timeout: payloads.append(json)
        or FakePostResponse(payload={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode("ascii")}]}),
    )

    with caplog.at_level(logging.INFO):
        image_gen.XAIImageGenProvider().generate(
            "use the reference",
            image_url=reference_url,
            prompt_enhancement=False,
        )

    assert payloads[0]["image"]["url"] == reference_url
    assert "secret-token" not in caplog.text
    assert "token=" not in caplog.text


def test_provider_rejects_unreadable_reference_without_full_path_leak(monkeypatch, tmp_path, caplog):
    missing = tmp_path / "secret-token-folder" / "missing.png"

    with caplog.at_level(logging.INFO), pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate(
            "edit this",
            image_url=str(missing),
            prompt_enhancement=False,
        )

    message = str(exc_info.value)
    assert "Reference image is not readable" in message
    assert "missing.png" in message
    assert "secret-token-folder" not in message
    assert "secret-token-folder" not in caplog.text


def test_provider_rejects_oversized_data_uri(monkeypatch):
    monkeypatch.setattr(image_gen, "_MAX_IMAGE_BYTES", 4)
    data_uri = "data:image/png;base64," + base64.b64encode(b"12345").decode("ascii")

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate(
            "edit this",
            image_url=data_uri,
            prompt_enhancement=False,
        )

    assert "exceeds" in str(exc_info.value)


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
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "retry image",
    )

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
            payload={
                "error": {
                    "message": (
                        "Authorization: Bearer secret-token "
                        "Cookie: session=secret-token "
                        "access_token=secret-token"
                    )
                }
            },
        )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(image_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "fail image",
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("fail image")

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "Authorization: Bearer" not in message
    assert "session=" not in message
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
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "slow image",
    )

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
    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "login please",
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("login please")

    assert "not logged in" in str(exc_info.value)
