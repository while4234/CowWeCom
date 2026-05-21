import unittest
import os

from models.openai.open_ai_image import (
    _extract_image_reference,
    _extract_responses_image_reference,
)
from models.openai.responses_api_adapter import (
    build_responses_payload,
    responses_response_to_chat_completion,
    responses_stream_events_to_chat_chunks,
)


class TestOpenAIResponsesAdapter(unittest.TestCase):
    def test_build_payload_converts_messages_tools_and_tool_results(self):
        payload = build_responses_payload(
            {
                "model": "gpt-5.5",
                "messages": [
                    {"role": "system", "content": "You are concise."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this?"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,abc"},
                            },
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": "{\"q\":\"x\"}",
                            },
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": "{\"answer\": 1}",
                    },
                ],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Lookup data",
                        "parameters": {"type": "object"},
                    },
                }],
                "tool_choice": "auto",
                "max_tokens": 123,
            },
            store=False,
            reasoning_effort="xhigh",
        )

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["instructions"], "You are concise.")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
        self.assertEqual(payload["max_output_tokens"], 123)
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["name"], "lookup")
        self.assertEqual(payload["input"][0]["content"][0]["type"], "input_text")
        self.assertEqual(payload["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")

    def test_response_to_chat_completion_converts_text_and_tools(self):
        data = {
            "id": "resp_1",
            "model": "gpt-5.5",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Need a tool."}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{\"q\":\"x\"}",
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        }

        result = responses_response_to_chat_completion(data)

        message = result["choices"][0]["message"]
        self.assertEqual(message["content"], "Need a tool.")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertEqual(result["usage"]["completion_tokens"], 5)

    def test_stream_events_convert_to_chat_chunks(self):
        events = [
            {"type": "response.output_text.delta", "delta": "hello"},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "call_id": "call_1",
                "delta": "{\"q\"",
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "call_id": "call_1",
                "delta": ":\"x\"}",
            },
            {"type": "response.completed", "response": {"output": []}},
        ]

        chunks = list(responses_stream_events_to_chat_chunks(events))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "hello")
        tool_chunks = [
            c["choices"][0]["delta"]["tool_calls"][0]
            for c in chunks
            if c["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertEqual(tool_chunks[0]["function"]["name"], "lookup")
        self.assertEqual(tool_chunks[1]["function"]["arguments"], "{\"q\"")
        self.assertEqual(tool_chunks[2]["function"]["arguments"], ":\"x\"}")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_image_generation_helpers_accept_url_and_b64_outputs(self):
        url_result = {"data": [{"url": "https://example.com/image.png"}]}
        self.assertEqual(
            _extract_image_reference(url_result),
            "https://example.com/image.png",
        )

        image_b64 = "aW1hZ2UtYnl0ZXM="
        image_ref = _extract_image_reference(
            {"data": [{"b64_json": image_b64}]},
            output_format="png",
        )
        try:
            self.assertTrue(image_ref.startswith("file://"))
            self.assertTrue(os.path.exists(image_ref[7:]))
        finally:
            if image_ref.startswith("file://") and os.path.exists(image_ref[7:]):
                os.remove(image_ref[7:])

    def test_responses_image_generation_output_is_extracted(self):
        image_b64 = "cmVzcG9uc2VzLWltYWdl"
        event = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": image_b64,
                    }
                ]
            },
        }

        image_ref = _extract_responses_image_reference(event)
        try:
            self.assertTrue(image_ref.startswith("file://"))
            self.assertTrue(os.path.exists(image_ref[7:]))
        finally:
            if image_ref.startswith("file://") and os.path.exists(image_ref[7:]):
                os.remove(image_ref[7:])


if __name__ == "__main__":
    unittest.main()
