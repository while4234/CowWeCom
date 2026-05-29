from models.bot_factory import create_bot
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from common import const
from common.image_generation_routing import explicit_gpt_image_requested
from common.log import logger
from common.singleton import singleton
from config import conf
from common.llm_backend_router import BACKEND_CODEX, BACKEND_GROK, get_current_backend_for_profile, get_effective_chat_bot_type
from translate.factory import create_translator
from voice.factory import create_voice


@singleton
class Bridge(object):
    def __init__(self):
        self.btype = {
            "chat": get_effective_chat_bot_type(),
            "voice_to_text": conf().get("voice_to_text", "openai"),
            "text_to_voice": conf().get("text_to_voice", "google"),
            "translate": conf().get("translate", "baidu"),
        }
        # 这边取配置的模型
        bot_type = conf().get("bot_type")
        if self.btype["chat"] == const.CODEX:
            pass
        elif bot_type and str(bot_type).strip().lower() != const.CODEX:
            self.btype["chat"] = bot_type
        else:
            model_type = conf().get("model") or const.GPT_41_MINI
            
            # Ensure model_type is string to prevent AttributeError when using startswith()
            # This handles cases where numeric model names (e.g., "1") are parsed as integers from YAML
            if not isinstance(model_type, str):
                logger.warning(f"[Bridge] model_type is not a string: {model_type} (type: {type(model_type).__name__}), converting to string")
                model_type = str(model_type)
            
            if model_type in ["text-davinci-003"]:
                self.btype["chat"] = const.OPEN_AI
            if conf().get("use_azure_chatgpt", False):
                self.btype["chat"] = const.CHATGPTONAZURE
            if model_type in ["wenxin", "wenxin-4"]:
                self.btype["chat"] = const.BAIDU
            if model_type in ["xunfei"]:
                self.btype["chat"] = const.XUNFEI
            if model_type in [const.QWEN, const.QWEN_TURBO, const.QWEN_PLUS, const.QWEN_MAX]:
                self.btype["chat"] = const.QWEN_DASHSCOPE
            if model_type and (model_type.startswith("qwen") or model_type.startswith("qwq") or model_type.startswith("qvq")):
                self.btype["chat"] = const.QWEN_DASHSCOPE
            if model_type and model_type.startswith("gemini"):
                self.btype["chat"] = const.GEMINI
            if model_type and model_type.startswith("glm"):
                self.btype["chat"] = const.ZHIPU_AI
            if model_type and model_type.startswith("claude"):
                self.btype["chat"] = const.CLAUDEAPI

            if model_type in [const.MOONSHOT, "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]:
                self.btype["chat"] = const.MOONSHOT
            if model_type and model_type.startswith("kimi"):
                self.btype["chat"] = const.MOONSHOT

            if model_type and model_type.startswith("doubao"):
                self.btype["chat"] = const.DOUBAO

            if model_type and model_type.startswith("deepseek"):
                self.btype["chat"] = const.DEEPSEEK

            if model_type and isinstance(model_type, str):
                lowered_model_type = model_type.lower()
                if lowered_model_type.startswith("grok") or lowered_model_type == const.XAI:
                    self.btype["chat"] = const.GROK

            if model_type and isinstance(model_type, str):
                lowered_model_type = model_type.lower()
                if lowered_model_type == const.QIANFAN or lowered_model_type.startswith("ernie"):
                    self.btype["chat"] = const.QIANFAN

            if model_type in [const.MODELSCOPE]:
                self.btype["chat"] = const.MODELSCOPE
            
            # MiniMax models
            if model_type and (model_type in ["abab6.5-chat", "abab6.5"] or model_type.lower().startswith("minimax")):
                self.btype["chat"] = const.MiniMax

            if model_type and (model_type == const.CODEX or model_type.lower().startswith("codex/")):
                self.btype["chat"] = const.CODEX

            if conf().get("use_linkai") and conf().get("linkai_api_key"):
                self.btype["chat"] = const.LINKAI
                if not conf().get("voice_to_text") or conf().get("voice_to_text") in ["openai"]:
                    self.btype["voice_to_text"] = const.LINKAI
                if not conf().get("text_to_voice") or conf().get("text_to_voice") in ["openai", const.TTS_1, const.TTS_1_HD]:
                    self.btype["text_to_voice"] = const.LINKAI

        self.bots = {}
        self.chat_bots = {}
        self._agent_bridge = None

    # 模型对应的接口
    def get_bot(self, typename):
        if self.bots.get(typename) is None:
            logger.info("create bot {} for {}".format(self.btype[typename], typename))
            if typename == "text_to_voice":
                self.bots[typename] = create_voice(self.btype[typename])
            elif typename == "voice_to_text":
                self.bots[typename] = create_voice(self.btype[typename])
            elif typename == "chat":
                self.bots[typename] = create_bot(self.btype[typename])
            elif typename == "translate":
                self.bots[typename] = create_translator(self.btype[typename])
        return self.bots[typename]

    def get_bot_type(self, typename):
        return self.btype[typename]

    def fetch_reply_content(self, query, context: Context) -> Reply:
        from common.capi_monthly_monitor import maybe_check_capi_monthly_after_task
        from common.llm_backend_router import get_current_backend
        from common.llm_backend_quota_refresh import note_user_visible_model_call

        profile = getattr(context, "_actor_profile", None) if context else None
        if profile is None and context:
            try:
                from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile

                profile = resolve_agent_user_profile(context)
                apply_profile_to_context(context, profile)
                context["_actor_profile"] = profile
            except Exception as e:
                logger.debug(f"[Bridge] Actor backend profile resolution skipped: {e}")
        task_backend = get_current_backend_for_profile(profile) if profile is not None else get_current_backend()
        try:
            if context and context.type == ContextType.VIDEO_CREATE:
                from models.grok.grok_video import generate_reply, is_grok_video_provider

                if is_grok_video_provider():
                    return generate_reply(query, context)
            if context and context.type == ContextType.IMAGE_CREATE:
                explicit_gpt_image = explicit_gpt_image_requested(query)
                from models.grok.grok_image import generate_reply, is_grok_image_provider

                if not explicit_gpt_image and (task_backend == BACKEND_GROK or is_grok_image_provider()):
                    return generate_reply(query, context)
                if explicit_gpt_image and task_backend == BACKEND_GROK:
                    task_backend = get_current_backend()
            if not (context and context.get("is_scheduled_task")):
                note_user_visible_model_call(task_backend, request_kind="chat_reply")
            if task_backend == BACKEND_GROK:
                reply = self.find_chat_bot(BACKEND_GROK).reply(query, context)
            elif task_backend != get_current_backend():
                reply = self.find_backend_chat_bot(task_backend).reply(query, context)
            else:
                reply = self.get_bot("chat").reply(query, context)
            return reply
        finally:
            if task_backend == get_current_backend():
                maybe_check_capi_monthly_after_task(task_backend)

    def fetch_voice_to_text(self, voiceFile) -> Reply:
        return self.get_bot("voice_to_text").voiceToText(voiceFile)

    def fetch_text_to_voice(self, text) -> Reply:
        return self.get_bot("text_to_voice").textToVoice(text)

    def fetch_translate(self, text, from_lang="", to_lang="en") -> Reply:
        return self.get_bot("translate").translate(text, from_lang, to_lang)

    def find_chat_bot(self, bot_type: str):
        if self.chat_bots.get(bot_type) is None:
            self.chat_bots[bot_type] = create_bot(bot_type)
        return self.chat_bots.get(bot_type)

    def find_backend_chat_bot(self, backend: str):
        key = "backend:{}".format(backend)
        if self.chat_bots.get(key) is None:
            if backend == BACKEND_CODEX:
                self.chat_bots[key] = create_bot(const.CODEX)
            else:
                from models.chatgpt.chat_gpt_bot import ChatGPTBot
                self.chat_bots[key] = ChatGPTBot(backend_override=backend)
        return self.chat_bots.get(key)

    def reset_bot(self):
        """
        重置bot路由
        """
        self.__init__()

    def get_agent_bridge(self):
        """
        Get agent bridge for agent-based conversations
        """
        if self._agent_bridge is None:
            from bridge.agent_bridge import AgentBridge
            self._agent_bridge = AgentBridge(self)
        return self._agent_bridge

    def fetch_agent_reply(self, query: str, context: Context = None,
                          on_event=None, clear_history: bool = False) -> Reply:
        """
        Use super agent to handle the query

        Args:
            query: User query
            context: Context object
            on_event: Event callback for streaming
            clear_history: Whether to clear conversation history

        Returns:
            Reply object
        """
        try:
            agent_bridge = self.get_agent_bridge()
            return agent_bridge.agent_reply(query, context, on_event, clear_history)
        except Exception as e:
            logger.error(f"[Bridge] Agent reply failed before AgentBridge handled it: {e}")
            try:
                from bridge.agent_bridge import AgentBridge

                runtime = context.get("_session_runtime") if context else None
                return Reply(ReplyType.ERROR, AgentBridge._friendly_agent_error_text(e, runtime))
            except Exception:
                return Reply(
                    ReplyType.ERROR,
                    "这轮处理没有稳定完成，我先停止本轮尝试。\n"
                    "建议把需求拆成更小一步，或换一种描述方式让我继续尝试。",
                )
