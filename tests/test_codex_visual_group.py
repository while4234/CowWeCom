import json
import logging

from models.codex.codex_bot import CodexBot


class FakeCredential:
    def resolve_access_tokens(self):
        return {
            "access_token": "secret-access-token",
            "account_id": "acct_test",
            "cookie": "secret-cookie",
        }


class FakeTransport:
    def __init__(self):
        self.payload = None
        self.tokens = None

    def stream_responses(self, payload, tokens, *, config=None, request_id=""):
        self.payload = payload
        self.tokens = tokens
        yield {"type": "response.output_text.delta", "delta": '{"ok": true}'}
        yield {
            "type": "response.completed",
            "response": {
                "model": payload["model"],
                "output": [],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        }


def test_codex_call_vision_images_keeps_multi_image_payload_without_secret_leak(monkeypatch, caplog):
    records = []
    provider = {
        "model": "gpt-5.5",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "endpoint_path": "/responses",
        "reasoning_effort": "xhigh",
        "tools_enabled": True,
    }
    transport = FakeTransport()
    monkeypatch.setattr(CodexBot, "_provider_config", staticmethod(lambda: provider))
    monkeypatch.setattr(CodexBot, "_record_prompt_cache_usage", lambda self, usage, **kwargs: records.append((usage, kwargs)))

    with caplog.at_level(logging.DEBUG):
        bot = CodexBot(credential_source=FakeCredential(), transport=transport)
        result = bot.call_vision_images(
            image_urls=["data:image/png;base64,AAAA", "data:image/png;base64,BBBB"],
            image_labels=["Part 1: page=12, artifact_id=a1", "Part 2: page=13, artifact_id=a2"],
            question="Merge these visual parts.",
            model="codex/gpt-5.5",
            reasoning_effort="low",
            reasoning_effort_locked=True,
        )

    assert result["content"] == '{"ok": true}'
    assert result["usage"]["total_tokens"] == 15
    assert transport.tokens["access_token"] == "secret-access-token"
    assert transport.payload["model"] == "gpt-5.5"
    assert transport.payload["reasoning"]["effort"] == "low"
    content = transport.payload["input"][0]["content"]
    image_parts = [part for part in content if part.get("type") == "input_image"]
    text = "\n".join(part.get("text", "") for part in content if part.get("type") == "input_text")
    assert [part["image_url"] for part in image_parts] == [
        "data:image/png;base64,AAAA",
        "data:image/png;base64,BBBB",
    ]
    assert "Part 1: page=12, artifact_id=a1" in text
    assert "Part 2: page=13, artifact_id=a2" in text

    combined_result = json.dumps(result, ensure_ascii=False)
    combined_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-access-token" not in combined_result
    assert "secret-cookie" not in combined_result
    assert "secret-access-token" not in combined_logs
    assert "secret-cookie" not in combined_logs
