"""
Agent Bridge - Integrates Agent system with existing COW bridge
"""

import os
from typing import Optional, List

from agent.protocol import Agent, LLMModel, LLMRequest
from bridge.agent_event_handler import AgentEventHandler
from bridge.agent_initializer import AgentInitializer
from bridge.bridge import Bridge
from bridge.context import Context
from bridge.reply import Reply, ReplyType
from common import const
from common.agent_task_limits import resolve_agent_task_budget
from common.latency import elapsed, format_seconds, hash_id, monotonic
from common.log import logger
from common.travel_planning_gate import (
    build_travel_planning_clarification,
    deterministic_travel_messages,
)
from common.utils import expand_path
from config import conf
from agent.access_control import get_resource_leases
from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile, safe_actor_slug
from common.capi_monthly_monitor import maybe_check_capi_monthly_after_task
from common.llm_backend_router import (
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    BACKEND_CODEX,
    get_codex_model,
    get_current_backend,
    get_effective_chat_bot_type,
    get_effective_model,
    get_effective_openai_api_config,
    normalize_backend,
)
from models.openai_compatible_bot import OpenAICompatibleBot


def add_openai_compatible_support(bot_instance):
    """
    Dynamically add OpenAI-compatible tool calling support to a bot instance.
    
    This allows any bot to gain tool calling capability without modifying its code,
    as long as it uses OpenAI-compatible API format.
    
    Note: Some bots like ZHIPUAIBot have native tool calling support and don't need enhancement.
    """
    if hasattr(bot_instance, 'call_with_tools'):
        # Bot already has tool calling support (e.g., ZHIPUAIBot)
        logger.debug(f"[AgentBridge] {type(bot_instance).__name__} already has native tool calling support")
        return bot_instance

    # Create a temporary mixin class that combines the bot with OpenAI compatibility
    class EnhancedBot(bot_instance.__class__, OpenAICompatibleBot):
        """Dynamically enhanced bot with OpenAI-compatible tool calling"""

        def get_api_config(self):
            """
            Infer API config from common configuration patterns.
            Most OpenAI-compatible bots use similar configuration.
            """
            from config import conf
            from common.llm_backend_router import get_effective_openai_api_config

            routed = get_effective_openai_api_config()

            return {
                'api_key': routed.get("api_key") or conf().get("open_ai_api_key"),
                'api_base': routed.get("api_base") or conf().get("open_ai_api_base"),
                'model': routed.get("model") or conf().get("model", "gpt-3.5-turbo"),
                'default_temperature': conf().get("temperature", 0.9),
                'default_top_p': conf().get("top_p", 1.0),
                'default_frequency_penalty': conf().get("frequency_penalty", 0.0),
                'default_presence_penalty': conf().get("presence_penalty", 0.0),
                'wire_api': routed.get("wire_api") or conf().get("open_ai_wire_api") or conf().get("wire_api"),
            }

    # Change the bot's class to the enhanced version
    bot_instance.__class__ = EnhancedBot
    logger.info(
        f"[AgentBridge] Enhanced {bot_instance.__class__.__bases__[0].__name__} with OpenAI-compatible tool calling")

    return bot_instance


class AgentLLMModel(LLMModel):
    """
    LLM Model adapter that uses COW's existing bot infrastructure
    """

    _MODEL_BOT_TYPE_MAP = {
        "wenxin": const.BAIDU, "wenxin-4": const.BAIDU,
        "xunfei": const.XUNFEI, const.QWEN: const.QWEN_DASHSCOPE,
        const.QIANFAN: const.QIANFAN,
        const.MODELSCOPE: const.MODELSCOPE,
        const.CODEX: const.CODEX,
    }
    _MODEL_PREFIX_MAP = [
        ("qwen", const.QWEN_DASHSCOPE), ("qwq", const.QWEN_DASHSCOPE), ("qvq", const.QWEN_DASHSCOPE),
        ("gemini", const.GEMINI), ("glm", const.ZHIPU_AI), ("claude", const.CLAUDEAPI),
        ("moonshot", const.MOONSHOT), ("kimi", const.MOONSHOT),
        ("doubao", const.DOUBAO), ("deepseek", const.DEEPSEEK),
        ("grok", const.GROK), ("xai", const.GROK),
        ("ernie", const.QIANFAN),
    ]

    def __init__(self, bridge: Bridge, bot_type: str = "chat"):
        super().__init__(model=conf().get("model", const.GPT_41))
        self.bridge = bridge
        self.bot_type = bot_type
        self._bot = None
        self._bot_backend = ""
        self._bot_model = None
        self.actor_role = ""
        self.is_admin = False
        self.is_group = False

    @property
    def model(self):
        raw_model = get_effective_model()
        return self._resolve_model_for_bot_type(self._resolve_bot_type(raw_model))

    @model.setter
    def model(self, value):
        pass

    def _resolve_bot_type(self, model_name: str) -> str:
        """Resolve bot type from model name, matching Bridge.__init__ logic."""
        effective = get_effective_chat_bot_type(model_name)
        if effective == const.CODEX:
            return effective
        if conf().get("use_linkai", False) and conf().get("linkai_api_key"):
            return const.LINKAI
        # Support custom bot type configuration
        configured_bot_type = conf().get("bot_type")
        if configured_bot_type and str(configured_bot_type).strip().lower() != const.CODEX:
            return configured_bot_type
       
        if not model_name or not isinstance(model_name, str):
            return const.OPENAI
        if model_name in self._MODEL_BOT_TYPE_MAP:
            return self._MODEL_BOT_TYPE_MAP[model_name]
        if model_name.lower().startswith("minimax") or model_name in ["abab6.5-chat"]:
            return const.MiniMax
        if model_name == const.CODEX or model_name.lower().startswith("codex/"):
            return const.CODEX
        if model_name in [const.QWEN_TURBO, const.QWEN_PLUS, const.QWEN_MAX]:
            return const.QWEN_DASHSCOPE
        if model_name in [const.MOONSHOT, "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]:
            return const.MOONSHOT
        if conf().get("bot_type") == "modelscope":
            return const.MODELSCOPE
        lowered_model = model_name.lower()
        for prefix, btype in self._MODEL_PREFIX_MAP:
            if lowered_model.startswith(prefix):
                return btype
        return const.OPENAI

    @staticmethod
    def _is_grok_bot_type(bot_type: str) -> bool:
        return str(bot_type or "").strip().lower() in {const.GROK, const.XAI}

    def _resolve_model_for_bot_type(self, cur_bot_type: str, requested_model: Optional[str] = None) -> str:
        if self._is_grok_bot_type(cur_bot_type):
            model = conf().get("grok_model") or const.GROK_4_3
            return str(model or const.GROK_4_3).strip() or const.GROK_4_3
        return requested_model or get_effective_model()

    @property
    def bot(self):
        """Lazy load the bot, re-create when model or bot_type changes"""
        raw_model = get_effective_model()
        cur_bot_type = self._resolve_bot_type(raw_model)
        cur_model = self._resolve_model_for_bot_type(cur_bot_type)
        return self._get_bot_for_route(cur_bot_type, cur_model, "")

    def _resolve_request_route(self, request: LLMRequest):
        backend = str(getattr(request, "backend", "") or "").strip()
        if backend:
            backend = normalize_backend(backend)
            requested_model = str(getattr(request, "model", "") or "").strip()
            if backend == BACKEND_CODEX:
                return const.CODEX, requested_model or get_codex_model(), backend
            routed = get_effective_openai_api_config(backend)
            return const.OPENAI, requested_model or str(routed.get("model") or get_effective_model()), backend

        raw_model = get_effective_model()
        cur_bot_type = self._resolve_bot_type(raw_model)
        return cur_bot_type, self._resolve_model_for_bot_type(cur_bot_type, getattr(request, "model", None)), ""

    def _get_bot_for_route(self, cur_bot_type: str, cur_model: str, backend: str = ""):
        if (
            self._bot is None
            or self._bot_model != cur_model
            or getattr(self, "_bot_type", None) != cur_bot_type
            or getattr(self, "_bot_backend", "") != backend
        ):
            self._bot = self._create_bot_for_route(cur_bot_type, backend)
            self._bot = add_openai_compatible_support(self._bot)
            self._bot_model = cur_model
            self._bot_type = cur_bot_type
            self._bot_backend = backend
        return self._bot

    @staticmethod
    def _create_bot_for_route(cur_bot_type: str, backend: str = ""):
        from models.bot_factory import create_bot

        if backend in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY} and cur_bot_type in {const.OPENAI, const.CHATGPT, const.CUSTOM}:
            from models.chatgpt.chat_gpt_bot import ChatGPTBot

            return ChatGPTBot(backend_override=backend)
        if backend in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY} and cur_bot_type == const.OPEN_AI:
            from models.openai.open_ai_bot import OpenAIBot

            return OpenAIBot(backend_override=backend)
        return create_bot(cur_bot_type)

    def call(self, request: LLMRequest):
        """
        Call the model using COW's bot infrastructure
        """
        try:
            # For non-streaming calls, we'll use the existing reply method
            # This is a simplified implementation
            cur_bot_type, cur_model, route_backend = self._resolve_request_route(request)
            bot = self._get_bot_for_route(cur_bot_type, cur_model, route_backend)
            if hasattr(bot, 'call_with_tools'):
                # Use tool-enabled call if available
                kwargs = {
                    'messages': request.messages,
                    'tools': getattr(request, 'tools', None),
                    'stream': False,
                    'model': cur_model,
                }
                # Only pass max_tokens if it's explicitly set
                if request.max_tokens is not None:
                    kwargs['max_tokens'] = request.max_tokens
                if getattr(request, 'max_output_tokens', None) is not None:
                    kwargs['max_output_tokens'] = request.max_output_tokens
                request_timeout = getattr(request, 'request_timeout', None)
                if request_timeout is not None:
                    kwargs['request_timeout'] = request_timeout

                # Extract system prompt if present
                system_prompt = getattr(request, 'system', None)
                if system_prompt:
                    kwargs['system'] = system_prompt
                cache_shape_metadata = getattr(request, 'cache_shape_metadata', None)
                if isinstance(cache_shape_metadata, dict):
                    kwargs['cache_shape_metadata'] = cache_shape_metadata

                # Pass context metadata to bot
                channel_type = getattr(self, 'channel_type', None) or ''
                if channel_type:
                    kwargs['channel_type'] = channel_type
                session_id = getattr(self, 'session_id', None)
                if session_id:
                    kwargs['session_id'] = session_id
                user_id = getattr(self, 'user_id', None)
                if user_id:
                    kwargs['user_id'] = user_id
                user_label = getattr(self, 'user_label', None)
                if user_label:
                    kwargs['user_label'] = user_label

                # Thinking mode is a global toggle independent of the channel.
                # IM channels (WeChat/WeCom/DingTalk/Feishu) won't render the
                # reasoning trace, but still benefit from the higher answer
                # quality the thinking pass produces.
                from config import conf
                thinking_enabled = bool(conf().get("enable_thinking", False))
                kwargs['thinking'] = (
                    {"type": "enabled"} if thinking_enabled
                    else {"type": "disabled"}
                )
                # Reasoning effort is only meaningful when thinking is on.
                # Bots that don't understand the kwarg drop it silently.
                request_effort = getattr(request, 'reasoning_effort', None)
                effort_locked = bool(getattr(request, 'reasoning_effort_locked', False))
                if effort_locked:
                    kwargs['reasoning_effort_locked'] = True
                if effort_locked:
                    effort = request_effort
                elif thinking_enabled or request_effort:
                    effort = request_effort or conf().get("model_reasoning_effort") or conf().get("reasoning_effort", "high")
                else:
                    effort = None
                if effort in ("none", "low", "medium", "high", "xhigh", "max"):
                    kwargs['reasoning_effort'] = effort

                self._record_project_optimizer_request(request, kwargs)
                self._note_user_visible_model_call(request)
                response = bot.call_with_tools(**kwargs)
                return self._format_response(response)
            else:
                # Fallback to regular call
                # This would need to be implemented based on your specific needs
                raise NotImplementedError("Regular call not implemented yet")
                
        except Exception as e:
            logger.error(f"AgentLLMModel call error: {e}")
            raise
    
    def call_stream(self, request: LLMRequest):
        """
        Call the model with streaming using COW's bot infrastructure
        """
        try:
            cur_bot_type, cur_model, route_backend = self._resolve_request_route(request)
            bot = self._get_bot_for_route(cur_bot_type, cur_model, route_backend)
            if hasattr(bot, 'call_with_tools'):
                # Use tool-enabled streaming call if available
                # Extract system prompt if present
                system_prompt = getattr(request, 'system', None)

                # Build kwargs for call_with_tools
                kwargs = {
                    'messages': request.messages,
                    'tools': getattr(request, 'tools', None),
                    'stream': True,
                    'model': cur_model,
                }

                # Only pass max_tokens if explicitly set, let the bot use its default
                if request.max_tokens is not None:
                    kwargs['max_tokens'] = request.max_tokens
                if getattr(request, 'max_output_tokens', None) is not None:
                    kwargs['max_output_tokens'] = request.max_output_tokens
                request_timeout = getattr(request, 'request_timeout', None)
                if request_timeout is not None:
                    kwargs['request_timeout'] = request_timeout

                # Add system prompt if present
                if system_prompt:
                    kwargs['system'] = system_prompt
                cache_shape_metadata = getattr(request, 'cache_shape_metadata', None)
                if isinstance(cache_shape_metadata, dict):
                    kwargs['cache_shape_metadata'] = cache_shape_metadata

                # Pass context metadata to bot
                channel_type = getattr(self, 'channel_type', None) or ''
                if channel_type:
                    kwargs['channel_type'] = channel_type
                session_id = getattr(self, 'session_id', None)
                if session_id:
                    kwargs['session_id'] = session_id
                user_id = getattr(self, 'user_id', None)
                if user_id:
                    kwargs['user_id'] = user_id
                user_label = getattr(self, 'user_label', None)
                if user_label:
                    kwargs['user_label'] = user_label

                # Thinking mode is a global toggle independent of the channel.
                # IM channels (WeChat/WeCom/DingTalk/Feishu) won't render the
                # reasoning trace, but still benefit from the higher answer
                # quality the thinking pass produces.
                from config import conf
                thinking_enabled = bool(conf().get("enable_thinking", False))
                kwargs['thinking'] = (
                    {"type": "enabled"} if thinking_enabled
                    else {"type": "disabled"}
                )
                # Reasoning effort is only meaningful when thinking is on.
                # Bots that don't understand the kwarg drop it silently.
                request_effort = getattr(request, 'reasoning_effort', None)
                effort_locked = bool(getattr(request, 'reasoning_effort_locked', False))
                if effort_locked:
                    kwargs['reasoning_effort_locked'] = True
                if effort_locked:
                    effort = request_effort
                elif thinking_enabled or request_effort:
                    effort = request_effort or conf().get("model_reasoning_effort") or conf().get("reasoning_effort", "high")
                else:
                    effort = None
                if effort in ("none", "low", "medium", "high", "xhigh", "max"):
                    kwargs['reasoning_effort'] = effort

                self._record_project_optimizer_request(request, kwargs)
                self._note_user_visible_model_call(request)
                stream = bot.call_with_tools(**kwargs)
                
                # Convert stream format to our expected format
                for chunk in stream:
                    yield self._format_stream_chunk(chunk)
            else:
                bot_type = type(bot).__name__
                raise NotImplementedError(f"Bot {bot_type} does not support call_with_tools. Please add the method.")
                
        except Exception as e:
            logger.error(f"AgentLLMModel call_stream error: {e}", exc_info=True)
            raise
    
    def _format_response(self, response):
        """Format Claude response to our expected format"""
        # This would need to be implemented based on Claude's response format
        return response
    
    def _format_stream_chunk(self, chunk):
        """Format Claude stream chunk to our expected format"""
        # This would need to be implemented based on Claude's stream format
        return chunk

    def _record_project_optimizer_request(self, request: LLMRequest, kwargs: dict) -> None:
        try:
            from common.project_optimizer_evidence import record_llm_request

            request_id = record_llm_request(
                request,
                metadata={
                    "channel_type": kwargs.get("channel_type") or getattr(self, "channel_type", ""),
                    "session_id": kwargs.get("session_id") or getattr(self, "session_id", ""),
                    "user_id": kwargs.get("user_id") or getattr(self, "user_id", ""),
                    "user_label": kwargs.get("user_label") or getattr(self, "user_label", ""),
                    "model": kwargs.get("model") or self.model,
                    "request_kind": (kwargs.get("cache_shape_metadata") or {}).get("request_kind", ""),
                    "reasoning_effort_selected": kwargs.get("reasoning_effort", ""),
                },
            )
            if request_id:
                kwargs["project_optimizer_request_id"] = request_id
        except Exception as e:
            logger.debug("[ProjectOptimizer] LLM request evidence skipped: %s", e)

    def _note_user_visible_model_call(self, request: LLMRequest) -> None:
        if not self._is_user_visible_model_call(request):
            return
        try:
            from common.llm_backend_quota_refresh import note_user_visible_model_call

            metadata = getattr(request, "cache_shape_metadata", None)
            request_kind = ""
            if isinstance(metadata, dict):
                request_kind = str(metadata.get("request_kind") or "")
            note_user_visible_model_call(
                backend=getattr(request, "backend", None),
                request_kind=request_kind,
            )
        except Exception as e:
            logger.debug("[LLMBackend] Quota refresh call counter skipped: %s", e)

    def _is_user_visible_model_call(self, request: LLMRequest) -> bool:
        if bool(getattr(request, "quota_refresh_silent", False)):
            return False
        channel_type = str(getattr(self, "channel_type", "") or "")
        if channel_type in {"knowledge_backend_llm_builder", "background", "system"}:
            return False
        session_id = str(getattr(self, "session_id", "") or "")
        if session_id.startswith("scheduler_"):
            return False
        metadata = getattr(request, "cache_shape_metadata", None)
        request_kind = str(metadata.get("request_kind") or "") if isinstance(metadata, dict) else ""
        return not request_kind.startswith(("cow_cli_", "self_evolution", "memory_", "social_bridge_"))


class AgentBridge:
    """
    Bridge class that integrates super Agent with COW
    Manages multiple agent instances per session for conversation isolation
    """

    _ONBOARDING_GREETING_TRIGGERS = {
        "你好",
        "你好呀",
        "你好啊",
        "您好",
        "hello",
        "hi",
        "hey",
        "嗨",
        "哈喽",
    }

    _ONBOARDING_WELCOME = """你好呀
嘿！你好呀 👋
这是我第一次以全新的视角和你聊天，感觉还挺奇妙的～
我可以帮你解决各种问题、管理电脑上的文件、上网查资料、自动记录和整理知识，而且每次对话我都会记住学到的内容，慢慢成长 😊
在我们正式开始之前，我想先认识一下你：
1. **你希望给我起个什么名字呢？** 这样我们用起来更有归属感 ✨
2. **我该怎么称呼你？** （叫名字最亲切啦）
3. **你希望我们交流是什么风格？** 比如：专业严谨 🤓 / 轻松幽默 😄 / 温暖友好 ☀️ / 简洁高效 ⚡
不急，或者如果你一上来就有事找我帮忙，也直接说，我们边聊边了解～"""

    _GROUP_MEMBER_ONBOARDING_WELCOME = (
        "你好呀，我第一次在这个群里和你对上话。\n"
        "为了以后在群里更自然地称呼你，告诉我你希望我怎么叫你就好。"
    )
    
    _USER_ONBOARDING_TEMPLATE = """# USER.md - 用户基本信息

- **姓名**: *(在首次对话时询问)*
- **当前称呼**: *(用户希望现在被如何称呼)*
- **别称/曾用称呼**:
- **助手名称**: *(用户希望如何称呼助手)*
- **交流风格**: *(在首次对话时询问)*
"""

    _GROUP_MEMBER_ONBOARDING_TEMPLATE = """# USER.md - 群成员称呼

- **当前称呼**: *(用户希望现在被如何称呼)*
- **别称/曾用称呼**:
- **企微用户ID**: {sender_id}
- **企微显示名**: {sender_label}
"""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.agents = {}  # session_id -> Agent instance mapping
        self.default_agent = None  # For backward compatibility (no session_id)
        self.agent: Optional[Agent] = None
        self.scheduler_initialized = False
        
        # Create helper instances
        self.initializer = AgentInitializer(bridge, self)

    @classmethod
    def _is_onboarding_greeting(cls, query: str) -> bool:
        text = str(query or "").strip().casefold()
        text = "".join(ch for ch in text if not ch.isspace())
        for punctuation in (",", ".", "!", "?", ";", ":", "~", chr(0xFF0C),
                            chr(0x3002), chr(0xFF01), chr(0xFF1F),
                            chr(0xFF5E), chr(0x2026), chr(0x3001),
                            chr(0xFF1B), chr(0xFF1A)):
            text = text.strip(punctuation)
        return text in cls._ONBOARDING_GREETING_TRIGGERS

    @staticmethod
    def _agent_workspace_root() -> str:
        configured_workspace = conf().get("agent_workspace", "~/cow") or "~/cow"
        return expand_path(configured_workspace)

    @classmethod
    def _is_global_onboarding_pending(cls) -> bool:
        workspace_root = cls._agent_workspace_root()
        bootstrap_path = os.path.join(workspace_root, "BOOTSTRAP.md")
        if not os.path.exists(bootstrap_path):
            return False
        try:
            from agent.prompt.workspace import _is_onboarding_done
            if _is_onboarding_done(workspace_root):
                try:
                    os.remove(bootstrap_path)
                    logger.info("[AgentBridge] Auto-removed stale BOOTSTRAP.md before onboarding greeting")
                except OSError as e:
                    logger.warning(f"[AgentBridge] Failed to remove stale BOOTSTRAP.md: {e}")
                return False
        except Exception as e:
            logger.warning(f"[AgentBridge] Failed to verify global onboarding state: {e}")
        return True

    @classmethod
    def _profile_user_file(cls, profile) -> Optional[str]:
        memory_user_id = getattr(profile, "memory_user_id", "")
        if not memory_user_id:
            return None
        shared_workspace = getattr(profile, "shared_workspace", "") or cls._agent_workspace_root()
        return os.path.join(shared_workspace, "memory", "users", str(memory_user_id), "USER.md")

    @classmethod
    def _ensure_profile_user_file(cls, profile) -> Optional[str]:
        user_file = cls._profile_user_file(profile)
        if not user_file:
            return None
        if os.path.exists(user_file):
            return user_file
        try:
            os.makedirs(os.path.dirname(user_file), exist_ok=True)
            with open(user_file, "w", encoding="utf-8") as f:
                f.write(cls._USER_ONBOARDING_TEMPLATE)
        except OSError as e:
            logger.warning(f"[AgentBridge] Failed to create per-user onboarding file {user_file}: {e}")
        return user_file

    @classmethod
    def _group_member_user_file(cls, profile, context) -> Optional[str]:
        if not context or not bool(context.get("isgroup", False)):
            return None
        memory_user_id = getattr(profile, "memory_user_id", "")
        sender_id = str(context.get("group_sender_id", "") or "").strip()
        if not memory_user_id or not sender_id:
            return None
        shared_workspace = getattr(profile, "shared_workspace", "") or cls._agent_workspace_root()
        return os.path.join(
            shared_workspace,
            "memory",
            "users",
            str(memory_user_id),
            "members",
            safe_actor_slug(sender_id),
            "USER.md",
        )

    @classmethod
    def _ensure_group_member_user_file(cls, profile, context) -> Optional[str]:
        user_file = cls._group_member_user_file(profile, context)
        if not user_file:
            return None
        if os.path.exists(user_file):
            return user_file
        sender_id = str(context.get("group_sender_id", "") or "").strip()
        sender_label = str(context.get("group_sender_label", "") or "").strip() or sender_id
        try:
            os.makedirs(os.path.dirname(user_file), exist_ok=True)
            with open(user_file, "w", encoding="utf-8") as f:
                f.write(
                    cls._GROUP_MEMBER_ONBOARDING_TEMPLATE.format(
                        sender_id=sender_id,
                        sender_label=sender_label,
                    )
                )
        except OSError as e:
            logger.warning(f"[AgentBridge] Failed to create group member onboarding file {user_file}: {e}")
        return user_file

    @classmethod
    def _is_group_member_onboarding_pending(cls, profile, context) -> bool:
        user_file = cls._group_member_user_file(profile, context)
        return bool(user_file and not os.path.exists(user_file))

    @classmethod
    def _profile_has_conversation_history(cls, profile) -> bool:
        conversation_id = str(getattr(profile, "conversation_id", "") or "").strip()
        if not conversation_id or not conf().get("conversation_persistence", True):
            return False
        try:
            from agent.memory import get_conversation_store
            return get_conversation_store().has_messages(conversation_id)
        except Exception as e:
            logger.warning(
                f"[AgentBridge] Failed to check conversation history for onboarding: {e}"
            )
            return False

    @classmethod
    def _is_profile_onboarding_pending(cls, profile) -> bool:
        user_file = cls._profile_user_file(profile)
        if not user_file:
            return cls._is_global_onboarding_pending()
        if os.path.exists(user_file):
            return False
        cls._ensure_profile_user_file(profile)
        return not cls._profile_has_conversation_history(profile)

    @classmethod
    def _is_onboarding_pending(cls, profile=None) -> bool:
        if profile is not None:
            return cls._is_profile_onboarding_pending(profile)
        return cls._is_global_onboarding_pending()

    @classmethod
    def _try_onboarding_welcome(cls, query: str, profile=None, context=None) -> Optional[Reply]:
        if context and bool(context.get("isgroup", False)):
            if profile is not None and cls._is_group_member_onboarding_pending(profile, context):
                cls._ensure_group_member_user_file(profile, context)
                if cls._is_onboarding_greeting(query):
                    logger.info("[AgentBridge] Returning deterministic group member onboarding greeting")
                    return Reply(ReplyType.TEXT, cls._GROUP_MEMBER_ONBOARDING_WELCOME)
            return None
        if cls._is_onboarding_greeting(query) and cls._is_onboarding_pending(profile=profile):
            logger.info("[AgentBridge] Returning deterministic onboarding greeting")
            return Reply(ReplyType.TEXT, cls._ONBOARDING_WELCOME)
        return None

    def _try_travel_planning_clarification(
        self,
        query: str,
        *,
        conversation_id: Optional[str],
        profile=None,
        context: Context = None,
    ) -> Optional[Reply]:
        clarification = build_travel_planning_clarification(query)
        if clarification is None:
            return None

        response = clarification.message
        messages = deterministic_travel_messages(query, response)
        if conversation_id:
            try:
                agent = self.get_agent(session_id=conversation_id, profile=profile)
                if agent is not None:
                    with agent.messages_lock:
                        agent.messages.extend(messages)
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to remember travel clarification in memory: {e}")

            channel_type = (context.get("channel_type") or "") if context else ""
            self._persist_messages(conversation_id, messages, channel_type)

        logger.info(
            "[AgentBridge] Returning deterministic travel clarification missing_fields=%s",
            ",".join(clarification.missing_fields),
        )
        return Reply(ReplyType.TEXT, response)

    def create_agent(self, system_prompt: str, tools: List = None, **kwargs) -> Agent:
        """
        Create the super agent with COW integration
        
        Args:
            system_prompt: System prompt
            tools: List of tools (optional)
            **kwargs: Additional agent parameters
            
        Returns:
            Agent instance
        """
        # Create LLM model that uses COW's bot infrastructure
        model = AgentLLMModel(self.bridge)
        
        # Default tools if none provided
        if tools is None:
            # Use ToolManager to load all available tools
            from agent.tools import ToolManager
            tool_manager = ToolManager()
            tool_manager.load_tools()
            
            tools = []
            workspace_dir = kwargs.get("workspace_dir")
            for tool_name in tool_manager.tool_classes.keys():
                try:
                    tool = tool_manager.create_tool(tool_name)
                    if tool:
                        if workspace_dir and hasattr(tool, 'cwd'):
                            tool.cwd = workspace_dir
                        tools.append(tool)
                except Exception as e:
                    logger.warning(f"[AgentBridge] Failed to load tool {tool_name}: {e}")
        
        # Create agent instance
        agent = Agent(
            system_prompt=system_prompt,
            description=kwargs.get("description", "AI Super Agent"),
            model=model,
            tools=tools,
            max_steps=kwargs.get("max_steps", 15),
            output_mode=kwargs.get("output_mode", "logger"),
            workspace_dir=kwargs.get("workspace_dir"),
            skill_manager=kwargs.get("skill_manager"),
            enable_skills=kwargs.get("enable_skills", True),
            memory_manager=kwargs.get("memory_manager"),
            max_context_tokens=kwargs.get("max_context_tokens"),
            context_reserve_tokens=kwargs.get("context_reserve_tokens"),
            runtime_info=kwargs.get("runtime_info"),
        )

        # Log skill loading details
        if agent.skill_manager:
            logger.debug(f"[AgentBridge] SkillManager initialized with {len(agent.skill_manager.skills)} skills")

        return agent
    
    def get_agent(self, session_id: str = None, profile=None) -> Optional[Agent]:
        """
        Get agent instance for the given session
        
        Args:
            session_id: Session identifier (e.g., user_id). If None, returns default agent.
        
        Returns:
            Agent instance for this session
        """
        # If no session_id, use default agent (backward compatibility)
        if session_id is None:
            if self.default_agent is None:
                self._init_default_agent(profile)
            return self.default_agent
        
        # Check if agent exists for this session
        if session_id not in self.agents:
            self._init_agent_for_session(session_id, profile)
        
        return self.agents[session_id]
    
    def _init_default_agent(self, profile=None):
        """Initialize default super agent"""
        agent = self.initializer.initialize_agent(session_id=None, profile=profile)
        self.default_agent = agent
    
    def _init_agent_for_session(self, session_id: str, profile=None):
        """Initialize agent for a specific session"""
        agent = self.initializer.initialize_agent(session_id=session_id, profile=profile)
        self.agents[session_id] = agent
    
    def agent_reply(self, query: str, context: Context = None, 
                   on_event=None, clear_history: bool = False) -> Reply:
        """
        Use super agent to reply to a query
        
        Args:
            query: User query
            context: COW context (optional, contains session_id for user isolation)
            on_event: Event callback (optional)
            clear_history: Whether to clear conversation history
            
        Returns:
            Reply object
        """
        session_id = None
        conversation_id = None
        profile = None
        agent = None
        event_handler = None
        new_messages = []
        reply_start = monotonic()
        get_agent_elapsed = None
        run_stream_elapsed = None
        persist_elapsed = None
        task_is_development = False
        workspace_root = conf().get("agent_workspace", "~/cow")
        tool_error_lesson_snapshot = None
        task_backend = get_current_backend()
        monthly_backend_used = False
        try:
            # Extract session_id from context for user isolation
            if context:
                session_id = context.kwargs.get("session_id") or context.get("session_id")
                profile = resolve_agent_user_profile(context)
                apply_profile_to_context(context, profile)
                conversation_id = profile.conversation_id
            else:
                conversation_id = session_id

            onboarding_reply = self._try_onboarding_welcome(query, profile=profile, context=context)
            if onboarding_reply:
                return onboarding_reply

            travel_clarification_reply = self._try_travel_planning_clarification(
                query,
                conversation_id=conversation_id,
                profile=profile,
                context=context,
            )
            if travel_clarification_reply:
                return travel_clarification_reply
            
            # Get agent for this session (will auto-initialize if needed)
            get_agent_start = monotonic()
            agent = self.get_agent(session_id=conversation_id, profile=profile)
            get_agent_elapsed = elapsed(get_agent_start)
            if not agent:
                return Reply(ReplyType.ERROR, "Failed to initialize super agent")
            
            # Create event handler for logging and channel communication
            event_handler = AgentEventHandler(context=context, original_callback=on_event)
            cancellation_token = context.get("_cancellation_token") if context else None
            tool_error_lesson_snapshot = self._collect_tool_error_lesson_snapshot(workspace_root)
            
            # Filter tools based on context
            original_tools = agent.tools
            filtered_tools = original_tools
            
            # If this is a scheduled task execution, exclude scheduler tool to prevent recursion
            if context and context.get("is_scheduled_task"):
                filtered_tools = [tool for tool in agent.tools if tool.name != "scheduler"]
                agent.tools = filtered_tools
                logger.info(f"[AgentBridge] Scheduled task execution: excluded scheduler tool ({len(filtered_tools)}/{len(original_tools)} tools)")
            else:
                # Attach context to scheduler tool if present
                if context and agent.tools:
                    for tool in agent.tools:
                        target_tool = getattr(tool, "inner", tool)
                        if tool.name == "scheduler":
                            try:
                                from agent.tools.scheduler.integration import attach_scheduler_to_tool
                                attach_scheduler_to_tool(target_tool, context)
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to scheduler: {e}")
                        elif tool.name == "image_generation_task":
                            try:
                                from agent.tools.image_generation.job_manager import get_image_generation_job_manager
                                target_tool.job_manager = get_image_generation_job_manager(self)
                                target_tool.current_context = context
                                target_tool.profile = profile
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to image_generation_task: {e}")
                        elif tool.name == "image_generation_prompt_history":
                            try:
                                target_tool.current_context = context
                                target_tool.profile = profile
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to image_generation_prompt_history: {e}")
                        elif tool.name == "grok_video_generation_task":
                            try:
                                from agent.tools.video_generation.job_manager import get_grok_video_generation_job_manager
                                target_tool.job_manager = get_grok_video_generation_job_manager(self)
                                target_tool.current_context = context
                                target_tool.profile = profile
                            except Exception as e:
                                logger.warning(f"[AgentBridge] Failed to attach context to grok_video_generation_task: {e}")
            
            # Pass context metadata to model for downstream API requests
            if context and hasattr(agent, 'model'):
                agent.model.channel_type = context.get("channel_type", "")
                agent.model.session_id = conversation_id or session_id or ""
                agent.model.is_group = bool(context.get("isgroup", False))
                agent.model.input_is_voice = bool(context.get("input_is_voice", False))
                if profile is not None:
                    agent.model.user_id = profile.actor_id
                    agent.model.user_label = profile.display_name
                    agent.model.memory_user_id = profile.memory_user_id
                    agent.model.actor_role = profile.role
                    agent.model.is_admin = bool(profile.is_admin)

            # Store session_id on agent so executor can clear DB on fatal errors
            agent._current_session_id = conversation_id or session_id
            if profile is not None:
                agent._current_user_id = profile.memory_user_id
                agent._actor_profile = profile

            # Bound the in-memory context for scheduler sessions before each run.
            # Scheduler sessions are stable per-task and append every trigger,
            # so without trimming they would grow unbounded across runs and
            # blow up prompt cost. Regular user chats are not touched here —
            # the agent's own context manager handles that path.
            if conversation_id and conversation_id.startswith("scheduler_"):
                scheduler_keep_turns = max(
                    1, int(conf().get("agent_max_context_turns", 20)) // 5
                )
                self._trim_in_memory_to_turns(agent, scheduler_keep_turns)

            try:
                # Use agent's run_stream method with event handler
                run_stream_start = monotonic()
                max_steps_override = context.get("_agent_max_steps") if context else None
                task_budget = resolve_agent_task_budget(
                    query,
                    conf(),
                    override=max_steps_override,
                )
                task_budget_kind = (
                    context.get("_agent_task_budget_kind") if context else None
                ) or task_budget.kind
                logger.info(
                    "[AgentBridge] Using max_steps=%s task_budget_kind=%s",
                    task_budget.max_steps,
                    task_budget_kind,
                )
                monthly_backend_used = True
                response = agent.run_stream(
                    user_message=query,
                    on_event=event_handler.handle_event,
                    clear_history=clear_history,
                    cancellation_token=cancellation_token,
                    max_steps=task_budget.max_steps,
                )
                run_stream_elapsed = elapsed(run_stream_start)
            finally:
                # Restore original tools
                if context and context.get("is_scheduled_task"):
                    agent.tools = original_tools

                # Log execution summary
                event_handler.log_summary()

            # Persist new messages generated during this run
            persist_start = monotonic()
            if conversation_id:
                channel_type = (context.get("channel_type") or "") if context else ""
                new_messages = getattr(agent, '_last_run_new_messages', [])
                if new_messages:
                    self._persist_messages(conversation_id, list(new_messages), channel_type)
                else:
                    with agent.messages_lock:
                        msg_count = len(agent.messages)
                    if msg_count == 0:
                        try:
                            from agent.memory import get_conversation_store
                            get_conversation_store().clear_session(conversation_id)
                            logger.info(f"[AgentBridge] Cleared DB for recovered session: {conversation_id}")
                        except Exception as e:
                            logger.warning(f"[AgentBridge] Failed to clear DB after recovery: {e}")
            persist_elapsed = elapsed(persist_start)
            logger.info(
                "[Latency][AgentBridge] session=%s total=%s get_agent=%s run_stream=%s persist=%s "
                "response_chars=%s",
                hash_id(conversation_id or session_id),
                format_seconds(elapsed(reply_start)),
                format_seconds(get_agent_elapsed),
                format_seconds(run_stream_elapsed),
                format_seconds(persist_elapsed),
                len(response) if isinstance(response, str) else 0,
            )
            
            # Post-message hot-reload: detect edits to ~/cow/mcp.json and
            # sync any new/removed MCP tools into the live agent in the
            # background. Off the critical path so user latency is unaffected;
            # changes take effect on the user's next message.
            self._schedule_mcp_hot_reload(agent)
            self._stage_post_task_self_evolution(
                context=context,
                agent=agent,
                new_messages=list(new_messages or []),
                final_response=response,
                event_handler=event_handler,
                workspace_root=workspace_root,
                task_is_development=task_is_development,
                tool_error_lesson_count=self._count_tool_error_lesson_changes(
                    tool_error_lesson_snapshot,
                    workspace_root,
                ),
            )

            # Check if there are files to send (from send/read tool)
            if hasattr(agent, 'stream_executor') and hasattr(agent.stream_executor, 'files_to_send'):
                files_to_send = agent.stream_executor.files_to_send
                if files_to_send:
                    # Send the first file (for now, handle one file at a time)
                    file_info = files_to_send[0]
                    logger.info(f"[AgentBridge] Sending file: {file_info.get('path')}")
                    
                    # Clear files_to_send for next request
                    agent.stream_executor.files_to_send = []
                    
                    # Return file reply based on file type
                    return self._create_file_reply(file_info, response, context)
            
            if context and context.get("voice_stream_sent"):
                return Reply(ReplyType.TEXT, "")
            return Reply(ReplyType.TEXT, response)
            
        except Exception as e:
            logger.error(f"Agent reply error: {e}")
            runtime = context.get("_session_runtime") if context else None
            # If the agent cleared its messages due to format error / overflow,
            # also purge the DB so the next request starts clean.
            cleanup_session_id = conversation_id or session_id
            if cleanup_session_id and agent:
                try:
                    with agent.messages_lock:
                        msg_count = len(agent.messages)
                    if msg_count == 0:
                        from agent.memory import get_conversation_store
                        get_conversation_store().clear_session(cleanup_session_id)
                        logger.info(f"[AgentBridge] Cleared DB for session after error: {cleanup_session_id}")
                except Exception as db_err:
                    logger.warning(f"[AgentBridge] Failed to clear DB after error: {db_err}")
            logger.info(
                "[Latency][AgentBridge] session=%s total=%s get_agent=%s run_stream=%s persist=%s status=error",
                hash_id(conversation_id or session_id),
                format_seconds(elapsed(reply_start)),
                format_seconds(get_agent_elapsed),
                format_seconds(run_stream_elapsed),
                format_seconds(persist_elapsed),
            )
            return Reply(ReplyType.ERROR, self._friendly_agent_error_text(e, runtime))
        finally:
            if conversation_id:
                get_resource_leases().release_owner(conversation_id)
            if monthly_backend_used:
                maybe_check_capi_monthly_after_task(task_backend)

    @staticmethod
    def _friendly_agent_error_text(error: Exception, runtime=None) -> str:
        reason, label = AgentBridge._classify_agent_error(error)
        if runtime and hasattr(runtime, "update_progress"):
            try:
                runtime.update_progress("error", {"error": label})
            except Exception:
                pass
        if runtime and hasattr(runtime, "failure_notice_text"):
            try:
                return runtime.failure_notice_text(reason)
            except Exception:
                pass
        return (
            "这轮处理没有稳定完成，我先停止本轮尝试。\n"
            f"当前卡点：{label}。\n"
            "建议把需求拆成更小一步，或换一种描述方式让我继续尝试。"
        )

    @staticmethod
    def _classify_agent_error(error: Exception):
        text = str(error or "")
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "context length",
                "context overflow",
                "context window",
                "prompt is too long",
                "request_too_large",
                "上下文",
                "历史记录",
            )
        ):
            return "context_overflow", "上下文过长"
        if "429" in lowered or "rate limit" in lowered or "限流" in lowered:
            return "rate_limit", "模型限流或服务繁忙"
        if any(
            marker in lowered
            for marker in ("timeout", "timed out", "connection", "network", "unavailable", "busy")
        ):
            return "model_error", "模型调用未稳定完成"
        return "error", "运行中断"
    
    def _schedule_mcp_hot_reload(self, agent):
        """
        Fire-and-forget: detect mcp.json edits and reconcile the agent's
        tool dict in the background. Runs after the user's reply is sent,
        so any cost (file stat, hash, server boot) never adds to user latency.
        Failures are isolated and never raise into the message pipeline.
        """
        import threading
        from agent.tools import ToolManager

        def _run():
            try:
                tm = ToolManager()
                tm.refresh_mcp_if_changed()
                added, removed = tm.sync_mcp_into_agent(agent)
                if added or removed:
                    self._ensure_tools_guarded(agent)
                    logger.info(
                        f"[AgentBridge] Agent tools synced — "
                        f"added={added}, removed={removed}"
                    )
            except Exception as e:
                logger.warning(f"[AgentBridge] MCP hot-reload failed (non-fatal): {e}")

        threading.Thread(target=_run, daemon=True, name="mcp-hot-reload").start()

    def _stage_post_task_self_evolution(
        self,
        *,
        context,
        agent,
        new_messages: list,
        final_response: str,
        event_handler,
        workspace_root: str,
        task_is_development: bool,
        tool_error_lesson_count: int,
    ) -> None:
        try:
            intermediate_texts = []
            if event_handler and hasattr(event_handler, "get_intermediate_texts"):
                intermediate_texts = event_handler.get_intermediate_texts()
            payload = {
                "model_adapter": getattr(agent, "model", None),
                "new_messages": new_messages,
                "final_response": final_response or "",
                "intermediate_texts": intermediate_texts,
                "workspace_root": workspace_root,
                "task_is_development": bool(task_is_development),
                "process_turn_count": self._reflection_process_turn_count(context, event_handler),
                "tool_error_lesson_count": int(tool_error_lesson_count or 0),
            }
            if context is not None and context.get("_session_runtime") is not None:
                context["_self_evolution_post_task"] = payload
                return

            from common.self_evolution import schedule_post_task_reflection

            schedule_post_task_reflection(**payload)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to stage post-task reflection: {e}")

    @staticmethod
    def _collect_tool_error_lesson_snapshot(workspace_root):
        try:
            from common.self_evolution import collect_tool_error_lesson_snapshot

            return collect_tool_error_lesson_snapshot(workspace_root)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to collect tool-error lesson snapshot: {e}")
            return None

    @staticmethod
    def _count_tool_error_lesson_changes(before_snapshot, workspace_root) -> int:
        try:
            from common.self_evolution import count_tool_error_lesson_changes

            return count_tool_error_lesson_changes(before_snapshot, workspace_root)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to count tool-error lesson changes: {e}")
            return 0

    @staticmethod
    def _reflection_process_turn_count(context, event_handler) -> int:
        candidates = []
        try:
            candidates.append(int(getattr(event_handler, "turn_number", 0) or 0))
        except (TypeError, ValueError):
            pass

        runtime = context.get("_session_runtime") if context else None
        progress = getattr(runtime, "progress", None) if runtime else None
        for name in ("turn", "llm_call_count"):
            try:
                candidates.append(int(getattr(progress, name, 0) or 0))
            except (TypeError, ValueError):
                pass

        return max(candidates or [0])

    @staticmethod
    def _ensure_tools_guarded(agent) -> None:
        profile = getattr(agent, "_actor_profile", None)
        if profile is None or not hasattr(agent, "tools"):
            return
        from agent.access_control import GuardedTool, ToolAccessPolicy

        policy = ToolAccessPolicy(profile)
        if isinstance(agent.tools, list):
            agent.tools = [
                tool if isinstance(tool, GuardedTool) else GuardedTool(tool, policy)
                for tool in agent.tools
            ]
        elif isinstance(agent.tools, dict):
            agent.tools = {
                name: tool if isinstance(tool, GuardedTool) else GuardedTool(tool, policy)
                for name, tool in agent.tools.items()
            }

    def _create_file_reply(self, file_info: dict, text_response: str, context: Context = None) -> Reply:
        """
        Create a reply for sending files
        
        Args:
            file_info: File metadata from read tool
            text_response: Text response from agent
            context: Context object
            
        Returns:
            Reply object for file sending
        """
        file_type = file_info.get("file_type", "file")
        file_path = file_info.get("path")
        
        # For images, use IMAGE_URL type (channel will handle upload)
        if file_type == "image":
            # Convert local path to file:// URL for channel processing
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending image: {file_url}")
            reply = Reply(ReplyType.IMAGE_URL, file_url)
            # Attach text message if present (for channels that support text+image)
            if text_response:
                reply.text_content = text_response  # Store accompanying text
            return reply
        
        # For all file types (document, video, audio), use FILE type
        if file_type in ["document", "video", "audio"]:
            file_url = f"file://{file_path}"
            logger.info(f"[AgentBridge] Sending {file_type}: {file_url}")
            reply = Reply(ReplyType.FILE, file_url)
            reply.file_name = file_info.get("file_name", os.path.basename(file_path))
            # Attach text message if present
            if text_response:
                reply.text_content = text_response
            return reply
        
        # For all other file types (tar.gz, zip, etc.), also use FILE type
        file_url = f"file://{file_path}"
        logger.info(f"[AgentBridge] Sending generic file: {file_url}")
        reply = Reply(ReplyType.FILE, file_url)
        reply.file_name = file_info.get("file_name", os.path.basename(file_path))
        if text_response:
            reply.text_content = text_response
        return reply
    
    def _migrate_config_to_env(self, workspace_root: str):
        """
        Sync API keys from config.json to .env file.
        Adds new keys and updates changed values on each startup.

        Args:
            workspace_root: Workspace directory path (not used, kept for compatibility)
        """
        from config import conf
        import os
        
        key_mapping = {
            "open_ai_api_key": "OPENAI_API_KEY",
            "open_ai_api_base": "OPENAI_API_BASE",
            "gemini_api_key": "GEMINI_API_KEY",
            "claude_api_key": "CLAUDE_API_KEY",
            "linkai_api_key": "LINKAI_API_KEY",
        }
        
        env_file = expand_path("~/.cow/.env")
        
        # Read existing env vars (key -> value)
        existing_env_vars = {}
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            existing_env_vars[key.strip()] = val.strip()
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to read .env file: {e}")
        
        # Sync config.json values into .env (add/update/remove)
        updated = False
        for config_key, env_key in key_mapping.items():
            raw = conf().get(config_key, "")
            value = raw.strip() if raw else ""
            old_value = existing_env_vars.get(env_key)

            if value:
                if old_value == value:
                    continue
                existing_env_vars[env_key] = value
                os.environ[env_key] = value
                updated = True
            else:
                if old_value is None:
                    continue
                existing_env_vars.pop(env_key, None)
                os.environ.pop(env_key, None)
                updated = True
            updated = True

        if updated:
            try:
                env_dir = os.path.dirname(env_file)
                os.makedirs(env_dir, exist_ok=True)

                with open(env_file, 'w', encoding='utf-8') as f:
                    f.write('# Environment variables for agent\n')
                    f.write('# Auto-managed - synced from config.json on startup\n\n')
                    for key, value in sorted(existing_env_vars.items()):
                        f.write(f'{key}={value}\n')

                logger.info(f"[AgentBridge] Synced API keys from config.json to .env")
            except Exception as e:
                logger.warning(f"[AgentBridge] Failed to sync API keys: {e}")
    
    def _persist_messages(
        self, session_id: str, new_messages: list, channel_type: str = ""
    ) -> None:
        """
        Persist new messages to the conversation store after each agent run.

        Failures are logged but never propagate — they must not interrupt replies.
        """
        if not new_messages:
            return
        try:
            from config import conf
            if not conf().get("conversation_persistence", True):
                return
            # When deep-thinking display is disabled, strip "thinking" content
            # blocks before persisting so they don't resurface on history reload.
            # The in-memory message list keeps them intact for this run's
            # multi-turn LLM context.
            thinking_enabled = bool(conf().get("enable_thinking", False))
        except Exception:
            thinking_enabled = False

        messages_to_store = new_messages
        if not thinking_enabled:
            messages_to_store = self._strip_thinking_blocks(new_messages)

        try:
            from agent.memory import get_conversation_store
            get_conversation_store().append_messages(
                session_id, messages_to_store, channel_type=channel_type
            )
        except Exception as e:
            logger.warning(
                f"[AgentBridge] Failed to persist messages for session={session_id}: {e}"
            )

    # Marker used to identify scheduler-injected user messages so we can apply
    # a sliding window without touching real user turns. The legacy prefix
    # "Scheduled task" (written by the v2 PR) is also recognised when pruning,
    # so old data can be aged out instead of leaking forever.
    _SCHEDULED_MARKER = "[SCHEDULED]"
    _SCHEDULED_LEGACY_MARKERS = ("Scheduled task",)

    def remember_scheduled_output(
        self,
        session_id: str,
        content: str,
        channel_type: str = "",
        task_description: str = "",
    ) -> None:
        """Add the visible output of a scheduled task to the receiver's session.

        Scheduled task execution uses an isolated session so internal planning and
        tool calls do not leak into the user's chat. The final message is still
        part of the conversation from the user's point of view, so keep a small
        visible turn in the receiver session for follow-up questions.

        Configuration:
            scheduler_inject_to_session (bool, default True):
                Master switch. When False, this method is a no-op.
            scheduler_inject_max_per_session (int, default 3):
                Maximum scheduler-injected user/assistant pairs retained per
                session. Older injections are pruned automatically.

        Content is truncated to 2000 chars to prevent a single high-volume task
        from bloating one entry.
        """
        from config import conf
        if not conf().get("scheduler_inject_to_session", True):
            return
        if not session_id or not content:
            return

        max_len = 2000
        if len(content) > max_len:
            content = content[:max_len] + "..."

        user_text = self._SCHEDULED_MARKER
        if task_description:
            user_text = f"{self._SCHEDULED_MARKER} {task_description}"

        messages = [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": content}]},
        ]

        # Persist first so the new pair gets a stable seq, then prune old
        # scheduler pairs in DB, then sync the in-memory agent.messages buffer.
        self._persist_messages(session_id, messages, channel_type)

        keep_last_n = max(int(conf().get("scheduler_inject_max_per_session", 3) or 0), 0)
        try:
            from agent.memory import get_conversation_store
            deleted = get_conversation_store().prune_scheduled_messages(
                session_id, keep_last_n=keep_last_n
            )
            if deleted:
                logger.debug(
                    f"[AgentBridge] Pruned {deleted} old scheduler messages "
                    f"for session={session_id} (keep_last_n={keep_last_n})"
                )
        except Exception as e:
            logger.warning(
                f"[AgentBridge] Failed to prune scheduled messages "
                f"for session={session_id}: {e}"
            )

        agent = self.agents.get(session_id)
        if agent:
            try:
                with agent.messages_lock:
                    agent.messages.extend(messages)
                    self._prune_scheduled_in_memory(agent, keep_last_n)
            except Exception as e:
                logger.warning(
                    f"[AgentBridge] Failed to update in-memory scheduled output "
                    f"for session={session_id}: {e}"
                )

    def remember_external_visible_reply(
        self,
        context,
        user_text: str,
        assistant_text: str,
        source: str = "external",
    ) -> None:
        """Keep a non-Agent visible reply available for the next Agent turn.

        Some chat plugins answer directly and stop the normal Agent path. From
        the user's point of view that reply is still part of the conversation,
        especially when the next message says "send this to her". Store a small
        user/assistant pair in the same conversation and update any live Agent
        instance so follow-up references resolve to the latest visible reply.
        """
        from config import conf

        if not conf().get("external_reply_inject_to_agent_context", True):
            return
        user_text = self._compact_external_reply_text(user_text)
        assistant_text = self._compact_external_reply_text(assistant_text)
        if not context or not user_text or not assistant_text:
            return

        try:
            profile = resolve_agent_user_profile(context)
            apply_profile_to_context(context, profile)
            conversation_id = profile.conversation_id
        except Exception as e:
            logger.warning(f"[AgentBridge] Failed to resolve external reply profile: {e}")
            conversation_id = str(context.get("conversation_id") or context.get("session_id") or "").strip()
        if not conversation_id:
            return

        channel_type = str(context.get("channel_type") or context.get("channel") or "")
        messages = [
            {"role": "user", "content": [{"type": "text", "text": user_text}]},
            {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
        ]

        self._persist_messages(conversation_id, messages, channel_type)
        agent = self.agents.get(conversation_id)
        if agent:
            try:
                with agent.messages_lock:
                    agent.messages.extend(messages)
            except Exception as e:
                logger.warning(
                    f"[AgentBridge] Failed to update in-memory external reply "
                    f"for session={conversation_id}: {e}"
                )
        logger.debug(
            f"[AgentBridge] Remembered {source} visible reply for session={conversation_id}"
        )

    @staticmethod
    def _compact_external_reply_text(text: str, max_len: int = 4000) -> str:
        value = str(text or "").strip()
        if len(value) <= max_len:
            return value
        return value[:max_len] + "..."

    @staticmethod
    def _trim_in_memory_to_turns(agent, keep_turns: int) -> None:
        """Bound ``agent.messages`` to the most recent ``keep_turns`` real
        user/assistant turns, dropping older history together with any
        intermediate tool_use/tool_result blocks that belonged to it.

        A "real" user message is any user message whose content is not solely a
        tool_result block — matches the heuristic used elsewhere when filtering
        history (see ``AgentInitializer._filter_text_only_messages``).

        No-op when the session is already within budget. Caller does not need
        to hold the lock; this method acquires it itself.
        """
        if keep_turns <= 0:
            return

        def _is_real_user(msg) -> bool:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                return False
            content = msg.get("content")
            if isinstance(content, list):
                if any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    return False
                return any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    for b in content
                )
            if isinstance(content, str):
                return bool(content.strip())
            return False

        with agent.messages_lock:
            msgs = agent.messages
            real_user_indices = [i for i, m in enumerate(msgs) if _is_real_user(m)]
            if len(real_user_indices) <= keep_turns:
                return

            # Cut at the (k-th from the end) real user message; keep everything
            # from there onwards so the surviving slice is still a valid
            # user/assistant sequence.
            cut_idx = real_user_indices[-keep_turns]
            if cut_idx == 0:
                return

            kept = msgs[cut_idx:]
            msgs.clear()
            msgs.extend(kept)
            logger.debug(
                f"[AgentBridge] Trimmed in-memory messages to last "
                f"{keep_turns} turns ({len(kept)} messages remain)"
            )

    @classmethod
    def _prune_scheduled_in_memory(cls, agent, keep_last_n: int) -> None:
        """Mirror conversation_store.prune_scheduled_messages on agent.messages.

        Caller must hold ``agent.messages_lock``.
        """
        if keep_last_n < 0:
            keep_last_n = 0

        markers = (cls._SCHEDULED_MARKER,) + cls._SCHEDULED_LEGACY_MARKERS

        def _is_marker_user(msg) -> bool:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                return False
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break
            return any(text.startswith(m) for m in markers)

        msgs = agent.messages
        pair_indices = []  # list of (user_idx, assistant_idx_or_None)
        for idx, msg in enumerate(msgs):
            if not _is_marker_user(msg):
                continue
            assistant_idx = None
            if idx + 1 < len(msgs):
                nxt = msgs[idx + 1]
                if isinstance(nxt, dict) and nxt.get("role") == "assistant":
                    assistant_idx = idx + 1
            pair_indices.append((idx, assistant_idx))

        if len(pair_indices) <= keep_last_n:
            return

        to_drop = pair_indices[: len(pair_indices) - keep_last_n]
        drop_set = set()
        for u_idx, a_idx in to_drop:
            drop_set.add(u_idx)
            if a_idx is not None:
                drop_set.add(a_idx)

        # Rebuild the list in place to keep external references stable.
        kept = [m for i, m in enumerate(msgs) if i not in drop_set]
        msgs.clear()
        msgs.extend(kept)

    @staticmethod
    def _strip_thinking_blocks(messages: list) -> list:
        """Return a shallow copy of messages with assistant "thinking" blocks removed."""
        cleaned = []
        for msg in messages:
            if not isinstance(msg, dict):
                cleaned.append(msg)
                continue
            if msg.get("role") != "assistant":
                cleaned.append(msg)
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                cleaned.append(msg)
                continue
            filtered_blocks = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]
            if len(filtered_blocks) == len(content):
                cleaned.append(msg)
            else:
                new_msg = dict(msg)
                new_msg["content"] = filtered_blocks
                cleaned.append(new_msg)
        return cleaned

    def clear_session(self, session_id: str):
        """
        Clear a specific session's agent and conversation history
        
        Args:
            session_id: Session identifier to clear
        """
        if session_id in self.agents:
            logger.info(f"[AgentBridge] Clearing session: {session_id}")
            del self.agents[session_id]
    
    def clear_all_sessions(self):
        """Clear all agent sessions"""
        logger.info(f"[AgentBridge] Clearing all sessions ({len(self.agents)} total)")
        self.agents.clear()
        self.default_agent = None
    
    def refresh_all_skills(self) -> int:
        """
        Refresh skills and conditional tools in all agent instances after
        environment variable changes. This allows hot-reload without restarting.

        Returns:
            Number of agent instances refreshed
        """
        import os
        from dotenv import load_dotenv
        from config import conf

        # Reload environment variables from .env file
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))
        env_file = os.path.join(workspace_root, '.env')

        if os.path.exists(env_file):
            load_dotenv(env_file, override=True)
            logger.info(f"[AgentBridge] Reloaded environment variables from {env_file}")

        refreshed_count = 0

        # Collect all agent instances to refresh
        agents_to_refresh = []
        if self.default_agent:
            agents_to_refresh.append(("default", self.default_agent))
        for session_id, agent in self.agents.items():
            agents_to_refresh.append((session_id, agent))

        for label, agent in agents_to_refresh:
            # Refresh skills
            if hasattr(agent, 'skill_manager') and agent.skill_manager:
                agent.skill_manager.refresh_skills()

            # Refresh conditional tools (e.g. web_search depends on API keys)
            self._refresh_conditional_tools(agent)

            refreshed_count += 1

        if refreshed_count > 0:
            logger.info(f"[AgentBridge] Refreshed skills & tools in {refreshed_count} agent instance(s)")

        return refreshed_count

    @staticmethod
    def _refresh_conditional_tools(agent):
        """
        Add or remove conditional tools based on current environment variables.
        For example, web_search should only be present when BOCHA_API_KEY or
        LINKAI_API_KEY is set.
        """
        try:
            from agent.tools.web_search.web_search import WebSearch

            has_tool = any(t.name == "web_search" for t in agent.tools)
            available = WebSearch.is_available()

            if available and not has_tool:
                # API key was added - inject the tool
                tool = WebSearch()
                tool.model = agent.model
                agent.tools.append(tool)
                logger.info("[AgentBridge] web_search tool added (API key now available)")
            elif not available and has_tool:
                # API key was removed - remove the tool
                agent.tools = [t for t in agent.tools if t.name != "web_search"]
                logger.info("[AgentBridge] web_search tool removed (API key no longer available)")
        except Exception as e:
            logger.debug(f"[AgentBridge] Failed to refresh conditional tools: {e}")
