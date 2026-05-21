# encoding:utf-8

"""Adapters between Chat Completions-shaped code and the Responses API.

The rest of this project consumes OpenAI-style chat completion responses and
stream chunks. Keeping that internal contract lets us support the newer
Responses API without rewriting the agent loop, tool executor, or channels.
"""

import json
from typing import Any, Dict, Generator, Iterable, List, Optional
from urllib.parse import urlparse


CHAT_COMPLETIONS_WIRE_API = "chat_completions"
RESPONSES_WIRE_API = "responses"

_CHAT_WIRE_ALIASES = {"", "chat", "chat_completions", "chat-completions"}
_RESPONSES_WIRE_ALIASES = {"response", "responses"}
_RESPONSES_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
_SAMPLING_AND_PENALTY_KEYS = {
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
}


def normalize_wire_api(value: Optional[str]) -> str:
    """Normalize config wire API values to an internal enum string."""
    raw = (value or "").strip().lower()
    if raw in _RESPONSES_WIRE_ALIASES:
        return RESPONSES_WIRE_API
    if raw in _CHAT_WIRE_ALIASES:
        return CHAT_COMPLETIONS_WIRE_API
    return CHAT_COMPLETIONS_WIRE_API


def is_responses_wire_api(value: Optional[str]) -> bool:
    return normalize_wire_api(value) == RESPONSES_WIRE_API


def normalize_reasoning_effort(value: Optional[str]) -> Optional[str]:
    """Normalize project/Codex reasoning effort names for Responses API."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    # The project historically used "max"; Responses currently uses "xhigh".
    if raw == "max":
        raw = "xhigh"
    if raw in _RESPONSES_REASONING_EFFORTS:
        return raw
    return None


def ensure_versioned_api_base(api_base: Optional[str]) -> Optional[str]:
    """Accept either Codex-style provider roots or REST API versioned bases.

    Codex config often uses a provider root like
    ``https://example.com/openai`` while this project appends endpoint paths
    directly. For OpenAI-compatible REST calls we need a versioned base such as
    ``.../v1``. Existing versioned bases are returned unchanged.
    """
    if not api_base:
        return api_base
    base = api_base.rstrip("/")
    try:
        path = urlparse(base).path.rstrip("/")
    except Exception:
        path = ""
    last_segment = path.rsplit("/", 1)[-1] if path else ""
    if last_segment.startswith("v") and len(last_segment) > 1:
        return base
    return base + "/v1"


def build_responses_payload(
    chat_payload: Dict[str, Any],
    *,
    store: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a Chat Completions payload into a Responses create payload."""
    payload = dict(chat_payload)
    messages = payload.pop("messages", [])
    tools = payload.pop("tools", None)
    payload.pop("stream", None)
    payload.pop("request_timeout", None)
    payload.pop("timeout", None)

    responses_payload: Dict[str, Any] = {
        "model": payload.pop("model", None),
        "input": convert_messages_to_responses_input(messages),
    }

    instructions = _extract_leading_instructions(responses_payload["input"])
    if instructions:
        responses_payload["instructions"] = instructions

    max_tokens = payload.pop("max_tokens", None)
    max_output_tokens = payload.pop("max_output_tokens", None)
    if max_output_tokens is not None:
        responses_payload["max_output_tokens"] = max_output_tokens
    elif max_tokens is not None:
        responses_payload["max_output_tokens"] = max_tokens

    if tools:
        responses_payload["tools"] = convert_tools_to_responses_format(tools)

    tool_choice = payload.pop("tool_choice", None)
    if tool_choice is not None:
        responses_payload["tool_choice"] = _convert_tool_choice(tool_choice)

    effort = normalize_reasoning_effort(
        reasoning_effort or payload.pop("reasoning_effort", None)
    )
    if effort:
        responses_payload["reasoning"] = {"effort": effort}

    if store is not None:
        responses_payload["store"] = store
    elif "store" in payload:
        responses_payload["store"] = payload.pop("store")

    # Pass through supported top-level generation params and a few Responses
    # native options. Drop chat-only or provider-specific values such as
    # reasoning_content/thinking.
    for key in list(payload.keys()):
        if key in _SAMPLING_AND_PENALTY_KEYS:
            responses_payload[key] = payload.pop(key)
        elif key in {
            "metadata",
            "parallel_tool_calls",
            "previous_response_id",
            "prompt_cache_key",
            "prompt_cache_retention",
            "safety_identifier",
            "text",
            "truncation",
            "user",
        }:
            responses_payload[key] = payload.pop(key)

    return {k: v for k, v in responses_payload.items() if v is not None}


def convert_messages_to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI chat-format messages into Responses input items."""
    items: List[Dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "tool":
            call_id = msg.get("tool_call_id") or msg.get("id")
            if call_id:
                items.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _stringify_tool_output(content),
                })
            continue

        if role == "assistant":
            content_items = _convert_content_to_responses(content, role=role)
            if content_items:
                assistant_item: Dict[str, Any] = {
                    "type": "message",
                    "role": "assistant",
                    "content": content_items,
                }
                if msg.get("phase"):
                    assistant_item["phase"] = msg["phase"]
                items.append(assistant_item)

            for tool_call in msg.get("tool_calls") or []:
                function = tool_call.get("function", {})
                call_id = tool_call.get("id") or tool_call.get("call_id")
                if not call_id:
                    continue
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments", "") or "{}",
                })
            continue

        content_items = _convert_content_to_responses(content, role=role)
        if not content_items:
            continue
        response_item = {
            "type": "message",
            "role": role if role in {"user", "system", "developer"} else "user",
            "content": content_items,
        }
        items.append(response_item)
    return items


def convert_tools_to_responses_format(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Chat Completions tool definitions to Responses tools."""
    response_tools = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            function = tool["function"]
            response_tools.append({
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {}),
                "strict": bool(function.get("strict", False)),
            })
        elif tool.get("type") == "function" and tool.get("name"):
            response_tools.append(tool)
        else:
            response_tools.append(tool)
    return response_tools


def responses_response_to_chat_completion(data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a non-streaming Responses result to Chat Completions shape."""
    content = extract_output_text(data)
    tool_calls = extract_function_tool_calls(data)
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": data.get("id"),
        "object": "chat.completion",
        "created": data.get("created_at"),
        "model": data.get("model"),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": _convert_usage(data.get("usage", {})),
    }


def chat_chunks_to_chat_completion(
    chunks: Iterable[Dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate Chat Completions-style stream chunks into one response."""
    content_parts: List[str] = []
    tool_state: Dict[int, Dict[str, Any]] = {}
    finish_reason = None
    usage: Dict[str, Any] = {}

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            return chunk
        if chunk.get("usage"):
            usage = _convert_usage(chunk.get("usage", {}))
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        for tc_delta in delta.get("tool_calls") or []:
            index = tc_delta.get("index", 0)
            current = tool_state.setdefault(index, {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc_delta.get("id"):
                current["id"] = tc_delta["id"]
            function = tc_delta.get("function") or {}
            if function.get("name"):
                current["function"]["name"] = function["name"]
            if function.get("arguments"):
                current["function"]["arguments"] += function["arguments"]

    content = "".join(content_parts)
    tool_calls = [tool_state[idx] for idx in sorted(tool_state)]
    if not finish_reason:
        finish_reason = "tool_calls" if tool_calls else "stop"
    if not usage or (usage.get("total_tokens", 0) == 0 and (content or tool_calls)):
        # Some Codex-style gateways stream without token usage. Keep existing
        # project success checks working by reporting a minimal completion count.
        completion_tokens = 1 if content or tool_calls else 0
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": completion_tokens,
            "total_tokens": completion_tokens,
        }

    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def responses_stream_events_to_chat_chunks(
    events: Iterable[Dict[str, Any]]
) -> Generator[Dict[str, Any], None, None]:
    """Yield Chat Completions-style chunks from Responses stream events."""
    tool_state: Dict[int, Dict[str, Any]] = {}
    emitted_arg_delta = set()
    emitted_text = False
    saw_tool_call = False

    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("choices"):
            # Some gateways claim Responses mode but still stream chat chunks.
            yield event
            continue
        if event.get("error"):
            yield event
            continue

        event_type = event.get("type")

        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                emitted_text = True
                yield _make_chat_delta({"content": delta})
            continue

        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
            "response.output_text.annotation.added",
        }:
            delta = event.get("delta") or event.get("text") or ""
            if delta and event_type != "response.output_text.annotation.added":
                yield _make_chat_delta({"reasoning_content": delta})
            continue

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                index = event.get("output_index", 0)
                saw_tool_call = True
                _remember_tool_item(tool_state, index, item)
                yield _make_tool_call_delta(index, tool_state[index], arguments="")
            continue

        if event_type == "response.function_call_arguments.delta":
            index = event.get("output_index", 0)
            saw_tool_call = True
            item = tool_state.setdefault(index, {})
            if event.get("call_id"):
                item["id"] = event["call_id"]
            if event.get("item_id"):
                item.setdefault("item_id", event["item_id"])
            delta = event.get("delta", "")
            if delta:
                emitted_arg_delta.add(index)
                item["arguments"] = item.get("arguments", "") + delta
                yield _make_tool_call_delta(index, item, arguments=delta)
            continue

        if event_type == "response.function_call_arguments.done":
            index = event.get("output_index", 0)
            saw_tool_call = True
            item = tool_state.setdefault(index, {})
            if event.get("call_id"):
                item["id"] = event["call_id"]
            if event.get("name"):
                item["name"] = event["name"]
            full_arguments = event.get("arguments", "")
            if full_arguments and index not in emitted_arg_delta:
                item["arguments"] = full_arguments
                yield _make_tool_call_delta(index, item, arguments=full_arguments)
            continue

        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                index = event.get("output_index", 0)
                saw_tool_call = True
                _remember_tool_item(tool_state, index, item)
                full_arguments = item.get("arguments", "")
                if full_arguments and index not in emitted_arg_delta:
                    yield _make_tool_call_delta(index, tool_state[index], arguments=full_arguments)
            continue

        if event_type == "response.completed":
            response = event.get("response") or {}
            if not emitted_text:
                text = extract_output_text(response)
                if text:
                    emitted_text = True
                    yield _make_chat_delta({"content": text})
            for index, tool_call in enumerate(extract_function_tool_calls(response)):
                if index in tool_state:
                    continue
                saw_tool_call = True
                function = tool_call.get("function", {})
                yield _make_chat_delta({
                    "tool_calls": [{
                        "index": index,
                        "id": tool_call.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", ""),
                        },
                    }]
                })
            yield _make_chat_delta(
                {},
                finish_reason="tool_calls" if saw_tool_call else "stop",
                usage=_convert_usage(response.get("usage", {})) if response.get("usage") else None,
            )
            return

    # If the upstream ended without a completed event, close the chat stream so
    # the agent loop still observes a stop reason.
    yield _make_chat_delta({}, finish_reason="tool_calls" if saw_tool_call else "stop")


def extract_output_text(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    text_parts: List[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "refusal":
                text_parts.append(part.get("refusal", ""))
    return "".join(text_parts)


def extract_function_tool_calls(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    tool_calls = []
    if not isinstance(data, dict):
        return tool_calls
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id") or item.get("id") or ""
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": item.get("name", ""),
                "arguments": item.get("arguments", "") or "{}",
            },
        })
    return tool_calls


def _extract_leading_instructions(items: List[Dict[str, Any]]) -> str:
    instructions = []
    keep_items = []
    for item in items:
        if (
            item.get("type") == "message"
            and item.get("role") in {"system", "developer"}
            and not keep_items
        ):
            instructions.append(_message_item_text(item))
            continue
        keep_items.append(item)
    if instructions:
        items[:] = keep_items
    return "\n\n".join(part for part in instructions if part)


def _message_item_text(item: Dict[str, Any]) -> str:
    parts = []
    for content in item.get("content") or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") in {"input_text", "output_text"}:
            parts.append(content.get("text", ""))
    return "\n".join(part for part in parts if part)


def _convert_content_to_responses(content: Any, *, role: str) -> List[Dict[str, Any]]:
    if content is None:
        return []
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        if not content:
            return []
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        return [{"type": text_type, "text": str(content)}]

    result = []
    for part in content:
        if isinstance(part, str):
            result.append({"type": text_type, "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in {"text", "input_text", "output_text"}:
            result.append({"type": text_type, "text": part.get("text", "")})
        elif ptype == "image_url":
            image = part.get("image_url") or {}
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                result.append({"type": "input_image", "image_url": url})
        elif ptype == "input_image":
            image_item = {"type": "input_image"}
            if part.get("image_url"):
                image_item["image_url"] = part["image_url"]
            if part.get("file_id"):
                image_item["file_id"] = part["file_id"]
            if part.get("detail"):
                image_item["detail"] = part["detail"]
            result.append(image_item)
        elif ptype == "input_file":
            result.append(part)
    return [part for part in result if _content_part_has_value(part)]


def _content_part_has_value(part: Dict[str, Any]) -> bool:
    ptype = part.get("type")
    if ptype in {"input_text", "output_text"}:
        return bool(part.get("text"))
    if ptype == "input_image":
        return bool(part.get("image_url") or part.get("file_id"))
    if ptype == "input_file":
        return bool(part.get("file_data") or part.get("file_id") or part.get("file_url"))
    return True


def _stringify_tool_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _convert_tool_choice(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") == "function" and isinstance(tool_choice.get("function"), dict):
        return {"type": "function", "name": tool_choice["function"].get("name")}
    return tool_choice


def _convert_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    total = usage.get("total_tokens", 0) or (prompt + completion)
    input_details = (
        usage.get("input_tokens_details")
        or usage.get("prompt_tokens_details")
        or {}
    )
    completion_details = (
        usage.get("output_tokens_details")
        or usage.get("completion_tokens_details")
        or {}
    )
    cached_tokens = 0
    if isinstance(input_details, dict):
        cached_tokens = input_details.get("cached_tokens", 0) or 0
    cached_tokens = usage.get("cached_tokens", cached_tokens) or 0
    result: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached_tokens,
        "cache_hit_rate": (cached_tokens / prompt) if prompt else 0,
    }
    if isinstance(input_details, dict) and input_details:
        result["input_tokens_details"] = dict(input_details)
        result["prompt_tokens_details"] = dict(input_details)
    if isinstance(completion_details, dict) and completion_details:
        result["output_tokens_details"] = dict(completion_details)
        result["completion_tokens_details"] = dict(completion_details)
    return result


def _remember_tool_item(tool_state: Dict[int, Dict[str, Any]], index: int, item: Dict[str, Any]) -> None:
    state = tool_state.setdefault(index, {})
    state["id"] = item.get("call_id") or item.get("id") or state.get("id", "")
    state["name"] = item.get("name") or state.get("name", "")
    if item.get("arguments"):
        state["arguments"] = item["arguments"]


def _make_tool_call_delta(index: int, item: Dict[str, Any], *, arguments: str) -> Dict[str, Any]:
    return _make_chat_delta({
        "tool_calls": [{
            "index": index,
            "id": item.get("id", ""),
            "type": "function",
            "function": {
                "name": item.get("name", ""),
                "arguments": arguments,
            },
        }]
    })


def _make_chat_delta(
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    choice: Dict[str, Any] = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    chunk = {"choices": [choice]}
    if usage:
        chunk["usage"] = usage
    return chunk
