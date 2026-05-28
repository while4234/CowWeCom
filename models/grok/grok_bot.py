# encoding:utf-8

import time

from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common import const
from common.log import logger
from config import conf, load_config
from integrations.hermes_xai.auth import AuthError
from integrations.hermes_xai.xai_http import resolve_xai_http_credentials
from models.bot import Bot
from models.chatgpt.chat_gpt_session import ChatGPTSession
from models.openai_compatible_bot import OpenAICompatibleBot
from models.openai.openai_http_client import OpenAIHTTPError
from models.openai.openai_compat import wrap_http_error
from models.session_manager import SessionManager


DEFAULT_GROK_MODEL = "grok-4.3"


class GrokBot(Bot, OpenAICompatibleBot):
    """Native xAI/Grok text bot using OAuth or xAI API-key fallback."""

    def __init__(self):
        super().__init__()
        model = self._resolve_model()
        self.sessions = SessionManager(ChatGPTSession, model=model)
        self.args = {
            "model": model,
            "temperature": conf().get("temperature", 0.9),
            "top_p": conf().get("top_p", 1.0),
            "frequency_penalty": conf().get("frequency_penalty", 0.0),
            "presence_penalty": conf().get("presence_penalty", 0.0),
            "request_timeout": conf().get("request_timeout", None),
            "timeout": conf().get("request_timeout", None),
        }

    def _resolve_model(self):
        model = conf().get("grok_model") or DEFAULT_GROK_MODEL
        if str(model).strip().lower() in {const.GROK, const.XAI}:
            return DEFAULT_GROK_MODEL
        return model

    def get_api_config(self):
        creds = resolve_xai_http_credentials()
        return {
            "api_key": creds.get("api_key"),
            "api_base": creds.get("base_url"),
            "model": self._resolve_model(),
            "default_temperature": conf().get("temperature", 0.9),
            "default_top_p": conf().get("top_p", 1.0),
            "default_frequency_penalty": conf().get("frequency_penalty", 0.0),
            "default_presence_penalty": conf().get("presence_penalty", 0.0),
            "wire_api": conf().get("grok_wire_api") or "responses",
            "provider": creds.get("provider") or const.GROK,
            "auth_mode": creds.get("auth_mode") or "",
        }

    def call_with_tools(self, messages, tools=None, stream=False, **kwargs):
        kwargs = dict(kwargs)
        # Agent callers may carry the global model setting. Native Grok requests
        # must always use grok_model so xAI never receives a non-Grok model.
        kwargs["model"] = self._resolve_model()
        return OpenAICompatibleBot.call_with_tools(
            self,
            messages=messages,
            tools=tools,
            stream=stream,
            **kwargs,
        )

    def _responses_api_base(self, api_base):
        return (api_base or "https://api.x.ai/v1").rstrip("/")

    def reply(self, query, context=None):
        if context.type != ContextType.TEXT:
            return Reply(ReplyType.ERROR, "Grok 仅支持文字对话，图片、语音和视频会在后续版本接入。")

        logger.info("[GROK] query received")
        session_id = context["session_id"]
        clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
        if query in clear_memory_commands:
            self.sessions.clear_session(session_id)
            return Reply(ReplyType.INFO, "记忆已清除")
        if query == "#清除所有":
            self.sessions.clear_all_session()
            return Reply(ReplyType.INFO, "所有人记忆已清除")
        if query == "#更新配置":
            load_config()
            return Reply(ReplyType.INFO, "配置已更新")

        session = self.sessions.session_query(query, session_id)
        reply_content = self.reply_text(session, args=dict(self.args))
        if reply_content["completion_tokens"] > 0:
            self.sessions.session_reply(reply_content["content"], session_id, reply_content["total_tokens"])
            return Reply(ReplyType.TEXT, reply_content["content"])
        return Reply(ReplyType.ERROR, reply_content["content"])

    def reply_text(self, session, args=None, retry_count=0):
        try:
            api_config = self.get_api_config()
            api_key = api_config.get("api_key")
            if not api_key:
                return self._not_logged_in_result()
            call_args = dict(args or self.args)
            call_args.setdefault("model", api_config.get("model"))
            response = self._handle_sync_response(
                {
                    **call_args,
                    "messages": session.messages,
                    "stream": False,
                    "_cache_metadata": {"session_id": session.session_id, "channel_type": "chat"},
                },
                api_key,
                api_config.get("api_base"),
                api_config,
            )
            if response.get("error"):
                return self._error_result(response)
            message = response["choices"][0]["message"]
            usage = response.get("usage") or {}
            return {
                "total_tokens": usage.get("total_tokens", 1),
                "completion_tokens": usage.get("completion_tokens", 1),
                "content": message.get("content", ""),
            }
        except AuthError:
            return self._not_logged_in_result()
        except OpenAIHTTPError as http_err:
            err = wrap_http_error(http_err)
            return {"completion_tokens": 0, "content": str(err)}
        except Exception as exc:
            logger.warning("[GROK] reply failed: %s", exc)
            if retry_count < 2:
                time.sleep(2)
                return self.reply_text(session, args, retry_count + 1)
            return {"completion_tokens": 0, "content": "Grok 暂时无法完成回复，请稍后重试。"}

    @staticmethod
    def _not_logged_in_result():
        return {
            "completion_tokens": 0,
            "content": "Grok 账号尚未登录，请先在 Web 管理页面完成 Grok 登录。",
        }

    @staticmethod
    def _error_result(response):
        message = str(response.get("message") or "Grok 请求失败，请稍后重试。")
        return {"completion_tokens": 0, "content": message}
