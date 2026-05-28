# encoding:utf-8

from pathlib import Path

import pytest

from integrations.hermes_xai import image_gen


JPG_BYTES = b"\xff\xd8\xff\xe0image"
PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"


class FakePostResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeGetResponse:
    def __init__(self, content, content_type=""):
        self.headers = {"Content-Type": content_type} if content_type else {}
        self._content = content

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        yield self._content


def _patch_credentials(monkeypatch):
    monkeypatch.setattr(
        image_gen,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "oauth-token",
            "base_url": "https://api.x.ai/v1",
        },
    )


def test_url_response_is_downloaded_to_local_file(monkeypatch, tmp_path):
    get_calls = []

    def fake_post(*args, **kwargs):
        return FakePostResponse({"data": [{"url": "https://imgen.x.ai/xai-tmp-image"}]})

    def fake_get(url, timeout, stream):
        get_calls.append((url, timeout, stream))
        return FakeGetResponse(JPG_BYTES, content_type="image/jpeg")

    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)
    monkeypatch.setattr(image_gen.requests, "get", fake_get)

    path = image_gen.XAIImageGenProvider().generate("download this")

    assert get_calls == [("https://imgen.x.ai/xai-tmp-image", 60.0, True)]
    assert path != "https://imgen.x.ai/xai-tmp-image"
    assert path.endswith(".jpg")
    assert Path(path).read_bytes() == JPG_BYTES


def test_url_download_uses_magic_bytes_when_content_type_is_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    monkeypatch.setattr(
        image_gen.requests,
        "post",
        lambda *args, **kwargs: FakePostResponse({"data": [{"url": "https://imgen.x.ai/no-content-type"}]}),
    )
    monkeypatch.setattr(
        image_gen.requests,
        "get",
        lambda *args, **kwargs: FakeGetResponse(PNG_BYTES, content_type="application/octet-stream"),
    )

    path = image_gen.XAIImageGenProvider().generate("magic")

    assert path.endswith(".png")
    assert Path(path).read_bytes() == PNG_BYTES


def test_download_failure_does_not_return_remote_url(monkeypatch, tmp_path):
    remote_url = "https://imgen.x.ai/xai-tmp-fail"

    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    monkeypatch.setattr(
        image_gen.requests,
        "post",
        lambda *args, **kwargs: FakePostResponse({"data": [{"url": remote_url}]}),
    )
    monkeypatch.setattr(
        image_gen.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("must stay local")

    assert remote_url not in str(exc_info.value)


def test_oauth_credentials_are_not_sent_to_non_xai_base_url(monkeypatch):
    monkeypatch.setattr(
        image_gen,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: {
            "provider": "xai-oauth",
            "auth_mode": "oauth_pkce",
            "api_key": "oauth-token",
            "base_url": "https://example.com/v1",
        },
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("wrong host")

    assert "non-xAI endpoint" in str(exc_info.value)
