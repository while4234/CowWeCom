# encoding:utf-8

import base64
from pathlib import Path

import pytest

from integrations.hermes_xai import media_download, video_gen
from integrations.hermes_xai.auth import AuthError


MP4_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video"
PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", body=b"", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.body = body
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise video_gen.requests.HTTPError(self.text or str(self.status_code))

    def iter_content(self, chunk_size=1024):
        yield self.body


def _oauth_creds(token="token"):
    return {
        "provider": "xai-oauth",
        "auth_mode": "oauth_pkce",
        "api_key": token,
        "base_url": "https://api.x.ai/v1",
    }


def _patch_public_dns(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = str(host)
        if address.replace(".", "").isdigit() or ":" in address:
            return [(None, None, None, "", (address, port))]
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr(media_download.socket, "getaddrinfo", fake_getaddrinfo)


def _patch_video_download(monkeypatch, body=MP4_BYTES, content_type="video/mp4", calls=None):
    def fake_safe_download(url, *, prefix, suffix, allowed_content_types, max_bytes, timeout):
        if calls is not None:
            calls.append((url, (timeout, timeout), True, False))
        assert content_type in allowed_content_types
        path = media_download.new_generated_media_path(prefix, suffix or ".mp4")
        Path(path).write_bytes(body)
        return path

    monkeypatch.setattr(video_gen, "safe_download_to_file", fake_safe_download)


def test_provider_submits_polls_and_downloads_local_mp4(monkeypatch, tmp_path):
    resolver_calls = []
    post_calls = []
    get_calls = []
    download_calls = []

    def fake_resolver(force_refresh=False):
        resolver_calls.append(force_refresh)
        return _oauth_creds()

    def fake_post(url, headers, json, timeout):
        post_calls.append((url, headers, json, timeout))
        return FakeResponse(payload={"request_id": "req-1"})

    def fake_get(url, headers=None, timeout=None, stream=False):
        get_calls.append((url, headers, timeout, stream))
        if url.endswith("/videos/req-1"):
            if sum(1 for item in get_calls if item[0].endswith("/videos/req-1")) == 1:
                return FakeResponse(payload={"status": "queued"})
            return FakeResponse(payload={"status": "done", "video": {"url": "https://cdn.x.ai/out.mp4"}})
        return FakeResponse(body=MP4_BYTES, headers={"Content-Type": "video/mp4"})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(video_gen.requests, "post", fake_post)
    monkeypatch.setattr(video_gen.requests, "get", fake_get)
    _patch_video_download(monkeypatch, calls=download_calls)
    monkeypatch.setattr(video_gen.time, "sleep", lambda seconds: None)

    path = video_gen.XAIVideoGenProvider().generate("make a calm ocean video", poll_interval_seconds=0.01)

    assert resolver_calls == [False, False, False]
    post = post_calls[0]
    assert post[0] == "https://api.x.ai/v1/videos/generations"
    assert post[1]["Authorization"] == "Bearer token"
    assert post[1]["x-idempotency-key"]
    assert post[2] == {
        "model": "grok-imagine-video",
        "prompt": "make a calm ocean video",
        "duration": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
    }
    assert Path(path).read_bytes() == MP4_BYTES
    assert path.endswith(".mp4")
    assert not path.startswith(("http://", "https://"))
    assert download_calls == [("https://cdn.x.ai/out.mp4", (120.0, 120.0), True, False)]


def test_provider_sends_single_and_multi_image_payloads_as_data_uri(monkeypatch, tmp_path):
    image_a = tmp_path / "a.png"
    image_b = tmp_path / "b.png"
    image_a.write_bytes(PNG_BYTES)
    image_b.write_bytes(PNG_BYTES)
    payloads = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_gen, "resolve_xai_http_credentials", lambda force_refresh=False: _oauth_creds())
    monkeypatch.setattr(
        video_gen.requests,
        "post",
        lambda url, headers, json, timeout: payloads.append(json) or FakeResponse(payload={"request_id": "req"}),
    )
    monkeypatch.setattr(
        video_gen.requests,
        "get",
        lambda url, **kwargs: FakeResponse(payload={"status": "done", "video": {"url": "https://cdn.x.ai/out.mp4"}}),
    )
    _patch_video_download(monkeypatch)

    provider = video_gen.XAIVideoGenProvider()
    provider.generate("animate one", image_url=str(image_a))
    provider.generate("animate two", reference_image_urls=[str(image_a), str(image_b)])

    single = payloads[0]["image"]["url"]
    assert single.startswith("data:image/png;base64,")
    assert base64.b64decode(single.split(",", 1)[1]) == PNG_BYTES
    refs = payloads[1]["reference_images"]
    assert len(refs) == 2
    assert all(item["url"].startswith("data:image/png;base64,") for item in refs)


def test_provider_retries_submit_and_poll_once_after_401(monkeypatch, tmp_path):
    resolver_force_flags = []
    post_tokens = []
    poll_tokens = []
    post_responses = [
        FakeResponse(status_code=401, payload={"error": {"message": "expired"}}),
        FakeResponse(payload={"request_id": "req"}),
    ]
    poll_responses = [
        FakeResponse(status_code=401, payload={"error": {"message": "expired"}}),
        FakeResponse(payload={"status": "done", "video": {"url": "https://cdn.x.ai/out.mp4"}}),
    ]

    def fake_resolver(force_refresh=False):
        resolver_force_flags.append(force_refresh)
        return _oauth_creds("fresh" if force_refresh else "old")

    def fake_post(url, headers, json, timeout):
        post_tokens.append(headers["Authorization"])
        return post_responses.pop(0)

    def fake_get(url, headers=None, timeout=None, stream=False):
        if "/videos/req" in url:
            poll_tokens.append(headers["Authorization"])
            return poll_responses.pop(0)
        return FakeResponse(body=MP4_BYTES, headers={"Content-Type": "video/mp4"})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_gen, "resolve_xai_http_credentials", fake_resolver)
    monkeypatch.setattr(video_gen.requests, "post", fake_post)
    monkeypatch.setattr(video_gen.requests, "get", fake_get)
    _patch_video_download(monkeypatch)

    path = video_gen.XAIVideoGenProvider().generate("retry video")

    assert Path(path).exists()
    assert resolver_force_flags == [False, True, False, True]
    assert post_tokens == ["Bearer old", "Bearer fresh"]
    assert poll_tokens == ["Bearer old", "Bearer fresh"]


def test_safe_download_accepts_octet_stream_only_when_mp4_magic_matches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_public_dns(monkeypatch)
    monkeypatch.setattr(
        media_download.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(body=MP4_BYTES, headers={"Content-Type": "application/octet-stream"}),
    )

    path = media_download.safe_download_to_file(
        "https://cdn.x.ai/out.bin",
        prefix="octet_test",
        suffix=".mp4",
        allowed_content_types={"video/mp4", "application/octet-stream"},
        max_bytes=1024,
        timeout=5,
    )

    assert Path(path).read_bytes() == MP4_BYTES
    assert path.endswith(".mp4")


@pytest.mark.parametrize("status", ["failed", "error", "expired", "cancelled"])
def test_provider_reports_terminal_poll_status(monkeypatch, tmp_path, status):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_gen, "resolve_xai_http_credentials", lambda force_refresh=False: _oauth_creds())
    monkeypatch.setattr(video_gen.requests, "post", lambda *args, **kwargs: FakeResponse(payload={"request_id": "req"}))
    monkeypatch.setattr(
        video_gen.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(payload={"status": status, "error": {"message": "bad status"}}),
    )

    with pytest.raises(video_gen.XaiVideoGenError) as exc_info:
        video_gen.XAIVideoGenProvider().generate("fail video", timeout_seconds=1, poll_interval_seconds=0.01)

    assert "bad status" in str(exc_info.value)


def test_provider_times_out_without_infinite_poll(monkeypatch, tmp_path):
    sleep_calls = []
    now = [0.0]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(video_gen, "resolve_xai_http_credentials", lambda force_refresh=False: _oauth_creds())
    monkeypatch.setattr(video_gen.requests, "post", lambda *args, **kwargs: FakeResponse(payload={"request_id": "req"}))
    monkeypatch.setattr(video_gen.requests, "get", lambda *args, **kwargs: FakeResponse(payload={"status": "running"}))
    monkeypatch.setattr(video_gen.time, "monotonic", lambda: now[0])

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(video_gen.time, "sleep", fake_sleep)

    with pytest.raises(video_gen.XaiVideoGenError) as exc_info:
        video_gen.XAIVideoGenProvider().generate("slow video", timeout_seconds=1, poll_interval_seconds=0.5)

    assert "Timed out" in str(exc_info.value)
    assert len(sleep_calls) < 10


def test_provider_reports_missing_grok_login(monkeypatch):
    monkeypatch.setattr(
        video_gen,
        "resolve_xai_http_credentials",
        lambda force_refresh=False: (_ for _ in ()).throw(
            AuthError("Grok account is not logged in.", code="xai_auth_missing", relogin_required=True)
        ),
    )

    with pytest.raises(video_gen.XaiVideoGenError) as exc_info:
        video_gen.XAIVideoGenProvider().generate("login please")

    assert "not logged in" in str(exc_info.value)


def test_provider_rejects_too_many_reference_images():
    with pytest.raises(video_gen.XaiVideoGenError) as exc_info:
        video_gen.XAIVideoGenProvider().generate(
            "too many",
            reference_image_urls=[f"https://example.com/{i}.png" for i in range(8)],
        )

    assert "at most 7" in str(exc_info.value)
