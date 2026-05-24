from __future__ import annotations

import os
import time
from typing import Any, Iterable, Mapping, Optional
from uuid import uuid4

from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common import const
from common.log import logger
from config import conf, load_config
from models.bot import Bot
from models.chatgpt.chat_gpt_session import ChatGPTSession
from models.codex.codex_auth import CodexAuthCredentialSource
from models.codex.codex_direct_client import CodexResponsesTransport
from models.openai.responses_api_adapter import (
    build_responses_payload,
    chat_chunks_to_chat_completion,
    normalize_reasoning_effort,
    responses_stream_events_to_chat_chunks,
)
from models.openai_compatible_bot import OpenAICompatibleBot
from models.session_manager import SessionManager


DEFAULT_CODEX_MODEL = "gpt-5.5"
TEXT_ONLY_DIRECTIVE = (
    "Use text responses only. Do not call image generation, browser actions, "
    "or other external actions through this backend. If a tool would be useful, explain the "
    "best answer from the available conversation context."
)


class CodexBot(Bot, OpenAICompatibleBot):
    """Codex backend using the user's current Codex login."""

    def __init__(
        self,
        *,
        credential_source: Optional[Any] = None,
        transport: Optional[Any] = None,
    ) -> None:
        super().__init__()
        model = self._configured_model()
        self.sessions = SessionManager(ChatGPTSession, model=model)
        self.args = {
            "model": model,
            "request_timeout": conf().get("request_timeout", None),
        }
        provider = self._provider_config()
        self._credential_source = credential_source or CodexAuthCredentialSource(provider.get("auth_file") or None)
        self._transport = transport or CodexResponsesTransport(proxy=conf().get("proxy") or None)

    def get_api_config(self) -> dict[str, Any]:
        provider = self._provider_config()
        return {
            "api_key": "",
            "api_base": provider.get("base_url") or conf().get("codex_base_url") or conf().get("codex_direct_base_url", ""),
            "model": self._configured_model(),
            "default_temperature": 0,
            "default_top_p": 1,
            "default_frequency_penalty": 0,
            "default_presence_penalty": 0,
            "wire_api": "responses",
        }

    def reply(self, query, context=None):
        if context is None or context.type != ContextType.TEXT:
            return Reply(ReplyType.ERROR, "Codex backend only supports text chat.")

        logger.info("[CODEX] query=%s", query)
        session_id = context["session_id"]
        reply = None
        clear_memory_commands = conf().get("clear_memory_commands", [])
        if query in clear_memory_commands:
            self.sessions.clear_session(session_id)
            reply = Reply(ReplyType.INFO, "Memory cleared.")
        elif query == "#clear_all":
            self.sessions.clear_all_session()
            reply = Reply(ReplyType.INFO, "All sessions cleared.")
        elif query == "#reload":
            load_config()
            reply = Reply(ReplyType.INFO, "Config reloaded.")
        if reply:
            return reply

        session = self.sessions.session_query(query, session_id)
        reply_content = self.reply_text(session)
        if reply_content["completion_tokens"] > 0:
            self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
            return Reply(ReplyType.TEXT, reply_content["content"])
        return Reply(ReplyType.ERROR, reply_content["content"])

    def reply_text(self, session: ChatGPTSession, retry_count: int = 0) -> dict[str, Any]:
        try:
            response = self.call_with_tools(
                session.messages,
                stream=False,
                model=self.args["model"],
                request_timeout=self.args.get("request_timeout"),
            )
            if response.get("error"):
                raise RuntimeError(response.get("message") or response.get("error"))
            usage = response.get("usage") or {}
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.info("[CODEX] reply=%s, total_tokens=%s", content, usage.get("total_tokens", 0))
            return {
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or (1 if content else 0)),
                "content": content,
            }
        except Exception as exc:
            logger.warning("[CODEX] reply failed: %s", exc)
            if retry_count < 1 and self._is_retryable_error(exc):
                time.sleep(2)
                return self.reply_text(session, retry_count + 1)
            return {"total_tokens": 0, "completion_tokens": 0, "content": str(exc)}

    def call_with_tools(self, messages, tools=None, stream: bool = False, **kwargs):
        allow_tools = bool(tools) and self._codex_tools_enabled()
        if tools and not allow_tools:
            message = "codex_tools_disabled: Codex backend tools are disabled by configuration"
            if stream:
                return iter([self._error_chunk(message)])
            return {"error": True, "message": message, "status_code": 400}

        try:
            payload = self._build_payload(messages, tools=tools, allow_tools=allow_tools, **kwargs)
            request_id = uuid4().hex[:12]
            tokens = self._credential_source.resolve_access_tokens()
            metadata = self._usage_metadata(kwargs)
            client_config = self._client_config()
            request_timeout = kwargs.get("request_timeout") or kwargs.get("timeout")
            if request_timeout is not None:
                client_config["request_timeout"] = request_timeout
            events = self._transport.stream_responses(
                payload,
                tokens,
                config=client_config,
                request_id=request_id,
            )
            chunks = self._chat_chunks(events, allow_tools=allow_tools)
            if stream:
                return self._recording_stream(chunks, payload=payload, metadata=metadata)
            result = chat_chunks_to_chat_completion(chunks, model=payload.get("model"))
            self._record_prompt_cache_usage(
                result.get("usage"),
                request_payload=payload,
                metadata=metadata,
                wire_api="codex",
            )
            return result
        except Exception as exc:
            logger.error("[CODEX] call_with_tools error: %s", exc)
            if stream:
                return iter([self._error_chunk(str(exc))])
            return {
                "error": True,
                "message": str(exc),
                "status_code": 500,
            }

    @staticmethod
    def _codex_tools_enabled() -> bool:
        provider = CodexBot._provider_config()
        return bool(provider.get("tools_enabled", True))

    def _build_payload(self, messages, tools=None, allow_tools: bool = False, **kwargs) -> dict[str, Any]:
        model = normalize_codex_model_name(str(kwargs.get("model") or self._configured_model()))
        request_messages = (
            self._convert_messages_to_openai_format(messages or [])
            if allow_tools
            else self._to_text_only_messages(messages)
        )
        system_prompt = str(kwargs.get("system") or "").strip()
        if system_prompt:
            if request_messages and request_messages[0].get("role") == "system":
                request_messages[0] = {"role": "system", "content": system_prompt}
            else:
                request_messages = [{"role": "system", "content": system_prompt}] + request_messages

        request: dict[str, Any] = {
            "model": model,
            "messages": request_messages,
        }
        # ChatGPT Codex rejects the Responses max_output_tokens field that the
        # shared adapter derives from Chat-style max_tokens, so this backend
        # intentionally leaves short-output control to the prompt.
        if tools and allow_tools:
            request["tools"] = self._convert_tools_to_openai_format(tools)
            request["tool_choice"] = kwargs.get("tool_choice", "auto")
            if "parallel_tool_calls" in kwargs:
                request["parallel_tool_calls"] = kwargs.get("parallel_tool_calls")

        payload = build_responses_payload(
            request,
            store=False,
            reasoning_effort=self._resolve_codex_reasoning_effort(kwargs),
        )
        payload.update(
            self._build_prompt_cache_options(
                payload.get("model"),
                self._usage_metadata(kwargs),
            )
        )
        payload.pop("prompt_cache_retention", None)
        payload["stream"] = True
        payload["store"] = False
        if allow_tools:
            payload.pop("parallel_tool_calls", None)
        else:
            payload["instructions"] = self._with_text_only_directive(payload.get("instructions", ""))
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload.pop("parallel_tool_calls", None)
        if tools and not allow_tools:
            logger.debug("[CODEX] tool schemas ignored for text-only Codex backend")
        return payload

    def _to_text_only_messages(self, messages: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
        converted = self._convert_messages_to_openai_format(messages or [])
        text_messages: list[dict[str, str]] = []
        for message in converted:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "user").strip().lower()
            content = self._content_to_text(message.get("content"))
            if role == "tool":
                text = self._compact_tool_result(content)
                if text:
                    text_messages.append({"role": "user", "content": text})
                continue

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                names = ", ".join(
                    str((call.get("function") or {}).get("name") or call.get("name") or "tool")
                    for call in tool_calls
                    if isinstance(call, Mapping)
                )
                notice = f"[Previous assistant tool call omitted in Codex text-only mode: {names or 'tool'}]"
                content = "\n".join(part for part in (content, notice) if part).strip()

            if not content:
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                role = "user"
            text_messages.append({"role": role, "content": content})
        return text_messages

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    text = str(item.get("text") or item.get("content") or "").strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return str(content or "").strip()

    @staticmethod
    def _compact_tool_result(content: str, limit: int = 8000) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        if len(text) > limit:
            text = text[:limit] + "\n[Tool result truncated for Codex text-only mode.]"
        return f"[Previous tool result, provided as plain context because Codex tool calls are disabled]\n{text}"

    @staticmethod
    def _with_text_only_directive(instructions: str) -> str:
        parts = [str(instructions or "").strip(), TEXT_ONLY_DIRECTIVE]
        return "\n\n".join(part for part in parts if part)

    def _chat_chunks(self, events: Iterable[dict[str, Any]], *, allow_tools: bool = False):
        warned_tool_call = False
        for chunk in responses_stream_events_to_chat_chunks(events):
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if delta.get("tool_calls") and not allow_tools:
                    if not warned_tool_call:
                        warned_tool_call = True
                        yield {
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "content": (
                                        "Codex backend requested a tool call, but tool execution is "
                                        "disabled for this backend. Answering from available context only."
                                    )
                                },
                            }]
                        }
                    continue
                if choices[0].get("finish_reason") == "tool_calls" and not allow_tools:
                    choices[0]["finish_reason"] = "stop"
            yield chunk

    def _recording_stream(self, chunks, *, payload: dict[str, Any], metadata: dict[str, Any]):
        recorded_usage = False
        for chunk in chunks:
            if not recorded_usage and isinstance(chunk, dict) and chunk.get("usage"):
                self._record_prompt_cache_usage(
                    chunk.get("usage"),
                    request_payload=payload,
                    metadata=metadata,
                    wire_api="codex",
                )
                recorded_usage = True
            yield chunk

    @staticmethod
    def _resolve_codex_reasoning_effort(kwargs: Mapping[str, Any]) -> Optional[str]:
        if kwargs.get("reasoning_effort_locked"):
            return normalize_codex_reasoning_effort(kwargs.get("reasoning_effort"))
        thinking = kwargs.get("thinking")
        if isinstance(thinking, Mapping) and str(thinking.get("type") or "").lower() == "disabled":
            explicit = (
                kwargs.get("reasoning_effort")
                or CodexBot._provider_config().get("reasoning_effort")
                or conf().get("codex_reasoning_effort")
                or conf().get("codex_direct_reasoning_effort")
                or "xhigh"
            )
            return normalize_codex_reasoning_effort(explicit)
        effort = (
            kwargs.get("reasoning_effort")
            or CodexBot._provider_config().get("reasoning_effort")
            or conf().get("codex_reasoning_effort")
            or conf().get("codex_direct_reasoning_effort")
            or "xhigh"
        )
        return normalize_codex_reasoning_effort(effort)

    @staticmethod
    def _usage_metadata(kwargs: Mapping[str, Any]) -> dict[str, Any]:
        metadata = dict(kwargs.get("cache_shape_metadata") or {})
        for key in ("channel_type", "session_id", "user_id", "user_label"):
            if kwargs.get(key):
                metadata[key] = kwargs[key]
        metadata["model"] = kwargs.get("model") or CodexBot._configured_model()
        return metadata

    @staticmethod
    def _error_chunk(message: str) -> dict[str, Any]:
        return {
            "error": {"message": message, "code": "", "type": ""},
            "message": message,
            "status_code": 500,
        }

    @staticmethod
    def _is_retryable_error(exc: BaseException) -> bool:
        text = str(exc or "").lower()
        return any(
            marker in text
            for marker in (
                "provider_timeout",
                "provider_network_error",
                "timed out",
                "connection reset",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
            )
        )

    @staticmethod
    def _configured_model() -> str:
        env_model = str(os.environ.get("CODEX_MODEL") or "").strip()
        if env_model:
            return normalize_codex_model_name(env_model)
        provider_model = str(CodexBot._provider_config().get("model") or "").strip()
        if provider_model:
            return normalize_codex_model_name(provider_model)
        return normalize_codex_model_name(str(conf().get("model") or DEFAULT_CODEX_MODEL))

    @staticmethod
    def _provider_config() -> dict[str, Any]:
        try:
            from common.llm_backend_router import get_codex_provider_config

            provider = get_codex_provider_config()
            return provider if isinstance(provider, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _client_config() -> dict[str, Any]:
        provider = dict(CodexBot._provider_config())
        for key in (
            "codex_direct_base_url",
            "codex_base_url",
            "codex_direct_endpoint_path",
            "codex_endpoint_path",
            "codex_direct_timeout_seconds",
            "codex_timeout_seconds",
            "codex_direct_max_response_bytes",
            "codex_max_response_bytes",
            "codex_direct_max_error_response_bytes",
            "codex_max_error_response_bytes",
            "codex_direct_user_agent",
            "codex_user_agent",
            "codex_direct_originator",
            "codex_originator",
            "codex_direct_extra_headers",
            "codex_extra_headers",
        ):
            value = conf().get(key)
            if value not in (None, ""):
                provider[key] = value
        return provider


def normalize_codex_model_name(model: str) -> str:
    raw = str(model or "").strip()
    if not raw or raw.lower() == const.CODEX:
        return DEFAULT_CODEX_MODEL
    if "/" in raw:
        provider, model_id = raw.split("/", 1)
        if provider.strip().lower() in {"codex", "openai-codex", "openai"}:
            raw = model_id
    return raw.strip() or DEFAULT_CODEX_MODEL


def normalize_codex_reasoning_effort(value: Any) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if raw == "minimal":
        raw = "low"
    elif raw == "max":
        raw = "xhigh"
    return normalize_reasoning_effort(raw)
