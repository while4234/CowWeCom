# encoding:utf-8

from pathlib import Path

import pytest

from integrations.hermes_xai import image_gen, media_download


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
    def __init__(self, content=b"", content_type="", status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type} if content_type else {}
        if headers:
            self.headers.update(headers)
        self._content = content
        self._chunks = chunks

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            for chunk in self._chunks:
                yield chunk
            return
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


def _patch_public_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = str(host)
        if address.replace(".", "").isdigit() or ":" in address:
            return [(None, None, None, "", (address, port))]
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr(
        media_download.socket,
        "getaddrinfo",
        fake_getaddrinfo,
    )


def test_url_response_is_downloaded_to_local_file(monkeypatch, tmp_path):
    get_calls = []

    def fake_post(*args, **kwargs):
        return FakePostResponse({"data": [{"url": "https://imgen.x.ai/xai-tmp-image"}]})

    def fake_get(url, timeout, stream, allow_redirects):
        get_calls.append((url, timeout, stream, allow_redirects))
        return FakeGetResponse(JPG_BYTES, content_type="image/jpeg")

    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(image_gen.requests, "post", fake_post)
    monkeypatch.setattr(media_download.requests, "get", fake_get)

    path = image_gen.XAIImageGenProvider().generate("download this")

    assert get_calls == [("https://imgen.x.ai/xai-tmp-image", (60.0, 60.0), True, False)]
    assert path != "https://imgen.x.ai/xai-tmp-image"
    assert path.endswith(".jpg")
    assert Path(path).parent.name == "grok_media"
    assert Path(path).read_bytes() == JPG_BYTES


def test_url_download_uses_allowed_content_type_extension(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(
        image_gen.requests,
        "post",
        lambda *args, **kwargs: FakePostResponse({"data": [{"url": "https://imgen.x.ai/no-content-type"}]}),
    )
    monkeypatch.setattr(
        media_download.requests,
        "get",
        lambda *args, **kwargs: FakeGetResponse(PNG_BYTES, content_type="image/png"),
    )

    path = image_gen.XAIImageGenProvider().generate("magic")

    assert path.endswith(".png")
    assert Path(path).read_bytes() == PNG_BYTES


def test_download_failure_does_not_return_remote_url(monkeypatch, tmp_path):
    remote_url = "https://imgen.x.ai/xai-tmp-fail"

    monkeypatch.chdir(tmp_path)
    _patch_credentials(monkeypatch)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(
        image_gen.requests,
        "post",
        lambda *args, **kwargs: FakePostResponse({"data": [{"url": remote_url}]}),
    )
    monkeypatch.setattr(
        media_download.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    with pytest.raises(image_gen.XaiImageGenError) as exc_info:
        image_gen.XAIImageGenProvider().generate("must stay local")

    assert remote_url not in str(exc_info.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.png",
        "https://localhost/x.png",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/x.png",
    ],
)
def test_safe_download_rejects_non_public_or_non_https_urls(url):
    with pytest.raises(media_download.MediaDownloadError):
        media_download.validate_public_https_url(url)


def test_safe_download_rejects_redirect_to_private_address(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_public_dns(monkeypatch)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeGetResponse(status_code=302, headers={"Location": "https://10.0.0.1/private.png"})

    monkeypatch.setattr(media_download.requests, "get", fake_get)

    with pytest.raises(media_download.MediaDownloadError):
        media_download.safe_download_to_file(
            "https://cdn.example.test/image.png",
            prefix="redirect_test",
            allowed_content_types={"image/png"},
            max_bytes=1024,
            timeout=5,
        )

    assert calls == ["https://cdn.example.test/image.png"]


def test_safe_download_allows_mock_public_https_cdn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(
        media_download.requests,
        "get",
        lambda *args, **kwargs: FakeGetResponse(PNG_BYTES, content_type="image/png"),
    )

    path = media_download.safe_download_to_file(
        "https://cdn.example.test/image.png",
        prefix="cdn_test",
        allowed_content_types={"image/png"},
        max_bytes=1024,
        timeout=5,
    )

    assert Path(path).read_bytes() == PNG_BYTES
    assert Path(path).parent.name == "grok_media"


def test_safe_download_deletes_partial_file_when_size_limit_exceeded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(
        media_download.requests,
        "get",
        lambda *args, **kwargs: FakeGetResponse(
            content_type="image/png",
            chunks=[b"1234", b"5678"],
        ),
    )

    with pytest.raises(media_download.MediaDownloadError):
        media_download.safe_download_to_file(
            "https://cdn.example.test/huge.png",
            prefix="huge_test",
            allowed_content_types={"image/png"},
            max_bytes=5,
            timeout=5,
        )

    media_dir = tmp_path / "tmp" / "grok_media"
    assert list(media_dir.glob("*")) == []


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
