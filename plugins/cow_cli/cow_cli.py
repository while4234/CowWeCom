"""
CowCli plugin - Intercept cow/slash commands in chat messages.

Matches messages like:
  cow skill list
  cow install-browser
  /skill list
  /context clear
  /status
  /install-browser

Does NOT match:
  cow是什么
  cow真好用
  /开头但不是已知命令
"""

import base64
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import plugins
from plugins import Plugin, Event, EventContext, EventAction
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from cli import __version__
from plugins.cow_cli.backend_nl import parse_backend_natural_command


# Known top-level subcommands that cow supports
KNOWN_COMMANDS = {
    "help", "version", "status", "logs",
    "start", "stop", "restart",
    "skill", "context", "config",
    "knowledge", "memory", "backend", "voice", "updates", "ledger", "tokens",
    "install-browser",
}

# Commands that can only run from the CLI (terminal), not in chat
CLI_ONLY_COMMANDS = {"start", "stop", "restart"}

# Commands that can only run from chat (need access to in-process memory)
CHAT_ONLY_COMMANDS = set()  # context is allowed in both, but behaves differently

ACCESS_PUBLIC = "public"
ACCESS_ADMIN = "admin"

# Every chat CLI command must have an access policy. Missing policies default
# to admin-only so newly added commands are not accidentally exposed.
COMMAND_ACCESS = {
    "help": ACCESS_PUBLIC,
    "version": ACCESS_PUBLIC,
    "status": ACCESS_PUBLIC,
    "logs": ACCESS_ADMIN,
    "start": ACCESS_ADMIN,
    "stop": ACCESS_ADMIN,
    "restart": ACCESS_ADMIN,
    "skill": ACCESS_PUBLIC,
    "context": ACCESS_PUBLIC,
    "config": ACCESS_ADMIN,
    "knowledge": ACCESS_PUBLIC,
    "memory": ACCESS_PUBLIC,
    "backend": ACCESS_ADMIN,
    "voice": ACCESS_ADMIN,
    "updates": ACCESS_ADMIN,
    "ledger": ACCESS_PUBLIC,
    "tokens": ACCESS_PUBLIC,
    "install-browser": ACCESS_ADMIN,
}

QUOTA_ALIASES = {
    "查询codex额度",
    "查询gpt额度",
    "查询openai额度",
    "查询codex登录",
}

_SKILL_CONTEXT_MARKERS = ("技能", "skill", "skills", "函数", "function", "functions", "功能", "能力")
_SKILL_LIST_MARKERS = (
    "有哪些", "有什么", "哪些", "支持哪些", "支持什么", "能做什么", "可以做什么",
    "会什么", "会做什么", "能干什么", "列出", "列表", "清单", "已安装",
    "有没有", "有无", "是否有", "有没有什么", "有没有相关", "有没有类似", "有没有这类", "有吗",
    "本地", "local", "list", "available", "show",
)
_SKILL_USAGE_MARKERS = ("怎么用", "如何用", "用法", "使用方法", "怎么使用", "如何使用", "usage", "help")
_SKILL_STANDALONE_LIST_MARKERS = (
    "你能做什么", "你会什么", "你可以做什么", "能帮我做什么",
    "当前支持哪些", "现在支持哪些", "当前支持什么", "现在支持什么",
)
_PROJECT_UPDATE_TIME_MARKERS = ("今天", "今日", "当天", "本日", "today")
_PROJECT_UPDATE_ACTION_MARKERS = ("更新", "新增", "改了", "变更", "提交", "commit", "github")
_PROJECT_UPDATE_SUMMARY_MARKERS = ("总结", "汇总", "整理", "有哪些", "哪些", "适合", "推送", "推荐", "功能")
_LEDGER_QUERY_MARKERS = ("账单", "记账", "消费", "支出", "花了", "花费", "收入", "退款", "转账")
_LEDGER_QUERY_INTENT_MARKERS = ("查", "查询", "统计", "汇总", "多少", "明细", "记录", "列表", "看一下", "看下")
_LEDGER_PERIOD_LABELS = {
    "today": "今天",
    "week": "本周",
    "month": "本月",
    "last_month": "上月",
    "all": "全部",
}
_TOKEN_USAGE_LOCAL_MARKERS = ("本机", "本地", "本项目", "local", "cowagent", "cowwechat", "cowwecom")
_TOKEN_USAGE_QUERY_MARKERS = ("查", "查询", "统计", "汇总", "看一下", "看下", "多少", "用了", "用量", "消耗")
_TOKEN_USAGE_DETAIL_MARKERS = (
    "每个用户", "每位用户", "每个人", "每人", "各用户", "分用户",
    "分别", "明细", "详细", "排名", "per-user", "by user",
)
_TOKEN_USAGE_EXCLUDED_CONTEXT = (
    "后端",
    "额度",
    "余额",
    "剩余",
    "codex",
    "gpt",
    "chatgpt",
    "openai",
    "capi",
    "api key",
    "apikey",
    "secret",
    "密钥",
    "秘钥",
)
_TOKEN_USAGE_PERIOD_LABELS = {
    "today": "今日",
    "month": "本月",
    "all": "累计",
}


@plugins.register(
    name="cow_cli",
    desc="Handle cow/slash commands in chat messages",
    version="0.1.0",
    author="CowAgent",
    desire_priority=1000,
)
class CowCliPlugin(Plugin):

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        logger.debug("[CowCli] initialized")

    def on_handle_context(self, e_context: EventContext):
        if e_context["context"].type != ContextType.TEXT:
            return

        content = e_context["context"].content.strip()
        parsed = self._parse_command(content)
        if parsed is None:
            return

        cmd, args = parsed

        if cmd not in KNOWN_COMMANDS:
            # Slash-prefixed near-miss: looks like a typo of a real command.
            # Intercept with a hint so we don't burn an LLM round on "/momory".
            suggestion = self._suggest_command(cmd)
            if suggestion is None:
                return
            if suggestion and not self._is_admin_context(e_context) and self._command_access_level(suggestion) == ACCESS_ADMIN:
                e_context["reply"] = Reply(ReplyType.TEXT, self._permission_denied_text(suggestion))
                e_context.action = EventAction.BREAK_PASS
                return
            hint = f"未知命令: /{cmd}"
            if suggestion:
                hint += f"\n你是不是想输入 /{suggestion} ?"
            hint += "\n发送 /help 查看全部命令。"
            e_context["reply"] = Reply(ReplyType.TEXT, hint)
            e_context.action = EventAction.BREAK_PASS
            return

        if not self._can_use_command(cmd, args, e_context):
            logger.info(f"[CowCli] denied non-admin command: {cmd} {args}")
            e_context["reply"] = Reply(ReplyType.TEXT, self._permission_denied_text(cmd))
            e_context.action = EventAction.BREAK_PASS
            return

        logger.info(f"[CowCli] intercepted command: {cmd} {args}")

        result = self._dispatch(cmd, args, e_context)
        self._mark_agent_followup_context(
            cmd,
            args,
            content,
            result,
            e_context,
        )

        reply = Reply(ReplyType.TEXT, result)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS

    def _mark_agent_followup_context(
        self,
        cmd: str,
        args: str,
        user_text: str,
        assistant_text: str,
        e_context: EventContext,
    ) -> None:
        if e_context is None or not self._should_remember_for_agent_followup(cmd, args):
            return
        if not str(user_text or "").strip() or not str(assistant_text or "").strip():
            return
        try:
            context = e_context["context"]
        except Exception:
            return
        context["_cow_cli_followup_context"] = {
            "source": "cow_cli",
            "command": cmd,
            "user_text": str(user_text).strip(),
            "assistant_text": str(assistant_text).strip(),
        }

    @staticmethod
    def _should_remember_for_agent_followup(cmd: str, args: str) -> bool:
        parts = str(args or "").strip().split(None, 1)
        first = parts[0].lower() if parts else ""
        return (
            (cmd == "updates" and first == "summary")
            or (cmd == "skill" and first == "answer")
            or cmd == "tokens"
        )

    @classmethod
    def _direct_social_target(cls, text: str) -> str:
        compact = cls._compact_for_social_intent(text)
        if not compact:
            return ""
        if cls._looks_like_recommendation_question(compact):
            return ""
        if cls._looks_like_clear_self_question(compact):
            return ""
        if cls._has_directed_send_marker(compact):
            return "recipient"
        if cls._has_social_verb(compact) and "给" in compact:
            return "recipient"
        if "通知" in compact and not cls._looks_like_status_or_howto_question(compact):
            return "recipient"
        if "告诉" in compact:
            return "recipient"
        if cls._has_between_markers(compact, "跟", "说"):
            return "recipient"
        if cls._has_between_markers(compact, "让", "知道"):
            return "recipient"
        return ""

    @classmethod
    def _looks_like_clear_self_question(cls, compact: str) -> bool:
        for prefix in ("告诉我", "跟我说", "给我说", "给我讲", "讲给我"):
            if compact.startswith(prefix):
                return cls._tail_looks_like_self_request(compact[len(prefix) :])
        first_question = cls._first_marker_index(compact, ("怎么", "如何", "怎样"))
        first_action = cls._first_marker_index(compact, cls._direct_social_action_markers())
        return first_question >= 0 and (first_action < 0 or first_question < first_action)

    @staticmethod
    def _tail_looks_like_self_request(tail: str) -> bool:
        return not tail or tail.startswith(
            (
                "一下",
                "下",
                "听",
                "这个",
                "这项",
                "这件",
                "该",
                "自助",
                "功能",
                "今天",
                "现在",
                "怎么",
                "如何",
                "怎样",
                "哪些",
                "什么",
                "多少",
                "是否",
                "有没有",
                "能不能",
            )
        )

    @staticmethod
    def _looks_like_status_or_howto_question(compact: str) -> bool:
        return any(
            marker in compact
            for marker in ("怎么", "如何", "怎样", "哪些", "什么", "多少", "吗", "是否", "有没有", "能不能")
        )

    @classmethod
    def _has_directed_send_marker(cls, compact: str) -> bool:
        return any(marker in compact for marker in cls._directed_send_markers())

    @staticmethod
    def _directed_send_markers():
        return (
            "转述给",
            "转发给",
            "发送给",
            "推送给",
            "同步给",
            "分享给",
            "发给",
            "转给",
            "讲给",
            "说给",
        )

    @classmethod
    def _direct_social_action_markers(cls):
        return cls._directed_send_markers() + (
            "告诉",
            "转述",
            "转发",
            "发送",
            "推送",
            "同步",
            "通知",
            "分享",
            "跟",
            "让",
        )

    @staticmethod
    def _has_social_verb(compact: str) -> bool:
        return any(marker in compact for marker in ("告诉", "转述", "转发", "发送", "推送", "同步", "通知", "分享"))

    @staticmethod
    def _has_between_markers(compact: str, prefix: str, suffix: str) -> bool:
        start = compact.find(prefix)
        if start < 0:
            return False
        end = compact.find(suffix, start + len(prefix))
        return end > start + len(prefix)

    @staticmethod
    def _first_marker_index(compact: str, markers) -> int:
        indexes = [compact.find(marker) for marker in markers if compact.find(marker) >= 0]
        return min(indexes) if indexes else -1

    @staticmethod
    def _compact_for_social_intent(text: str) -> str:
        return re.sub(r"[\s,，。?!？！；;：:\"'`“”‘’（）()\[\]【】<>《》]+", "", str(text or "").lower())

    @staticmethod
    def _looks_like_recommendation_question(compact: str) -> bool:
        if "适合" not in compact:
            return False
        return any(marker in compact for marker in ("哪些", "有什么", "推荐", "更新了哪些", "功能"))

    def _parse_command(self, content: str):
        """
        Parse cow command from message text.

        Supported formats:
          cow <command> [args...]   e.g. "cow skill list"
          /<command> [args...]      e.g. "/skill list"

        Returns:
          - (command, args_string): when the message looks like a command.
            'command' may NOT be in KNOWN_COMMANDS; caller should validate.
          - None: when the message is not command-like at all.

        We deliberately return parsed-but-unknown for the slash form so the
        caller can offer a typo hint instead of silently passing the message
        through to the agent.
        """
        normalized = "".join(content.lower().split())
        if normalized in QUOTA_ALIASES:
            return "backend", "quota"

        token_usage_command = self._parse_token_usage_natural_command(content)
        if token_usage_command is not None:
            return token_usage_command

        backend_command = parse_backend_natural_command(content)
        if backend_command is not None:
            return backend_command

        if self._direct_social_target(content):
            return None

        update_command = self._parse_project_update_natural_command(content)
        if update_command is not None:
            return update_command

        voice_command = self._parse_voice_mode_natural_command(content)
        if voice_command is not None:
            return voice_command

        ledger_command = self._parse_ledger_natural_command(content)
        if ledger_command is not None:
            return ledger_command

        skill_command = self._parse_skill_natural_command(content)
        if skill_command is not None:
            return skill_command

        if content.startswith("/"):
            rest = content[1:].strip()
            if not rest:
                return None
            parts = rest.split(None, 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            return cmd, args

        if content.startswith("cow "):
            rest = content[4:].strip()
            if not rest:
                return None
            parts = rest.split(None, 1)
            cmd = parts[0].lower()
            if cmd not in KNOWN_COMMANDS:
                # 'cow xxx' that isn't a command — don't intercept (could be
                # natural language like "cow xxx 怎么样").
                return None
            args = parts[1] if len(parts) > 1 else ""
            return cmd, args

        return None

    def _parse_project_update_natural_command(self, content: str):
        text = str(content or "").strip()
        if not text or text.startswith("/") or text.lower().startswith("cow "):
            return None
        normalized = text.lower()
        compact = re.sub(r"[\s,，。.!！?？:：;；\"'`“”‘’（）()\[\]【】<>《》]+", "", normalized)
        has_time = any(marker in compact or marker in normalized for marker in _PROJECT_UPDATE_TIME_MARKERS)
        has_update = any(marker in compact or marker in normalized for marker in _PROJECT_UPDATE_ACTION_MARKERS)
        has_summary = any(marker in compact or marker in normalized for marker in _PROJECT_UPDATE_SUMMARY_MARKERS)
        if has_time and has_update and has_summary:
            return "updates", self._encode_project_update_args(text, "today")
        return None

    def _parse_voice_mode_natural_command(self, content: str):
        text = str(content or "").strip()
        if not text or text.startswith("/") or text.lower().startswith("cow "):
            return None
        normalized = text.lower()
        compact = re.sub(r"[\s,，。?!？！:：\"'`“”‘’()（）\[\]【】<>《》]+", "", normalized)
        has_voice_context = any(
            marker in compact or marker in normalized
            for marker in ("语音模式", "语音回复", "语音对话", "grok语音", "voice mode", "voicemode", "grokvoice")
        )
        if not has_voice_context:
            return None
        off_markers = ("关闭", "关掉", "停用", "禁用", "不要", "别开", "off", "disable", "turnoff")
        on_markers = ("开启", "打开", "启用", "恢复", "切到", "切换到", "on", "enable", "turnon")
        if any(marker in compact or marker in normalized for marker in off_markers):
            return "voice", "off"
        if any(marker in compact or marker in normalized for marker in on_markers):
            return "voice", "on"
        if any(marker in compact or marker in normalized for marker in ("状态", "当前", "现在", "是否", "是不是", "status", "show")):
            return "voice", "status"
        return None

    def _parse_ledger_natural_command(self, content: str):
        text = str(content or "").strip()
        if not text or text.startswith("/") or text.lower().startswith("cow "):
            return None
        normalized = text.lower()
        compact = re.sub(r"[\s,，。.!！?？:：;；\"'`“”‘’（）()\[\]【】<>《》]+", "", normalized)
        if not any(marker in compact or marker in normalized for marker in _LEDGER_QUERY_MARKERS):
            return None
        period = self._ledger_period_from_text(normalized, compact)
        if not period:
            return None
        has_query_intent = any(marker in compact or marker in normalized for marker in _LEDGER_QUERY_INTENT_MARKERS)
        if not has_query_intent and self._looks_like_bill_clarification(normalized, compact):
            return None
        if not has_query_intent and not self._looks_like_ledger_period_summary(normalized, compact):
            return None
        mode = "query" if any(marker in compact for marker in ("明细", "记录", "列表")) else "summary"
        return "ledger", self._encode_ledger_args(period, mode)

    @staticmethod
    def _looks_like_bill_clarification(normalized: str, compact: str) -> bool:
        subject_markers = ("这个账单", "这张账单", "这笔账单", "这个订单", "这张订单", "这笔订单", "这个消费", "这笔消费")
        answer_markers = ("是", "买的是", "属于", "分类", "归到", "记到")
        has_subject = any(marker in compact or marker in normalized for marker in subject_markers)
        has_answer = any(marker in compact or marker in normalized for marker in answer_markers)
        if has_subject and has_answer:
            return True
        item_markers = ("中转api", "api额度", "额度卡", "apitoken", "api token", "api key", "apikey")
        return has_subject and any(marker in compact or marker in normalized for marker in item_markers)

    @staticmethod
    def _looks_like_ledger_period_summary(normalized: str, compact: str) -> bool:
        if re.search(r"\d", compact):
            return False
        summary_terms = ("账单", "消费", "支出", "收入", "退款", "转账")
        if not any(term in compact or term in normalized for term in summary_terms):
            return False
        record_action_terms = ("记账", "记一笔", "记录一下", "花了", "花费")
        return not any(term in compact or term in normalized for term in record_action_terms)

    @staticmethod
    def _ledger_period_from_text(normalized: str, compact: str) -> str:
        if any(marker in compact for marker in ("上个月", "上月", "上个自然月")):
            return "last_month"
        if any(marker in compact for marker in ("这个月", "本月", "当月", "月账单", "月消费")):
            return "month"
        if any(marker in compact for marker in ("这周", "本周", "本星期", "这个星期", "周账单", "周消费")):
            return "week"
        if any(marker in compact for marker in ("今天", "今日", "当天", "本日", "日账单", "日消费")):
            return "today"
        if "today" in normalized:
            return "today"
        if "thisweek" in compact:
            return "week"
        if "thismonth" in compact:
            return "month"
        if "lastmonth" in compact:
            return "last_month"
        return ""

    @staticmethod
    def _encode_ledger_args(period: str, mode: str = "summary") -> str:
        payload = {
            "period": str(period or "today").strip() or "today",
            "mode": "query" if mode == "query" else "summary",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"local {encoded}"

    @staticmethod
    def _decode_ledger_args(args: str) -> dict:
        raw = str(args or "").strip()
        if not raw.startswith("local "):
            return {"period": raw or "today", "mode": "summary"}
        encoded = raw.split(None, 1)[1].strip()
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception:
            return {"period": "today", "mode": "summary"}
        if not isinstance(data, dict):
            return {"period": "today", "mode": "summary"}
        period = str(data.get("period") or "today").strip() or "today"
        if period not in {"today", "week", "month", "last_month", "all"}:
            period = "today"
        mode = "query" if str(data.get("mode") or "").strip() == "query" else "summary"
        return {"period": period, "mode": mode}

    def _parse_token_usage_natural_command(self, content: str):
        text = str(content or "").strip()
        if not text or text.startswith("/") or text.lower().startswith("cow "):
            return None
        normalized = text.lower()
        compact = re.sub(r"[\s,，。?!？！；;:\"'`“”‘’（）()\[\]【】<>《》]+", "", normalized)
        if "token" not in normalized and "tokens" not in normalized:
            return None
        if any(marker in compact or marker in normalized for marker in _TOKEN_USAGE_EXCLUDED_CONTEXT):
            return None
        if not any(marker in compact or marker in normalized for marker in _TOKEN_USAGE_LOCAL_MARKERS):
            return None
        if not any(marker in compact or marker in normalized for marker in _TOKEN_USAGE_QUERY_MARKERS):
            return None
        period = self._token_usage_period_from_text(normalized, compact)
        detail = any(marker in compact or marker in normalized for marker in _TOKEN_USAGE_DETAIL_MARKERS)
        return "tokens", f"{period} details" if detail else period

    @staticmethod
    def _token_usage_period_from_text(normalized: str, compact: str) -> str:
        if any(marker in compact for marker in ("本月", "这个月", "当月", "月度")):
            return "month"
        if any(marker in compact for marker in ("累计", "全部", "总计", "历史", "all")):
            return "all"
        if any(marker in compact for marker in ("今天", "今日", "当天", "本日", "today")):
            return "today"
        if "thismonth" in normalized.replace(" ", ""):
            return "month"
        return "today"

    @staticmethod
    def _encode_project_update_args(question: str, period: str = "today") -> str:
        payload = {
            "question": str(question or "").strip(),
            "period": str(period or "today").strip() or "today",
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"summary {encoded}"

    @staticmethod
    def _decode_project_update_args(args: str) -> dict:
        raw = str(args or "").strip()
        if not raw.startswith("summary "):
            return {"question": "", "period": raw or "today"}
        encoded = raw.split(None, 1)[1].strip()
        try:
            decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception:
            return {"question": "", "period": "today"}
        if not isinstance(data, dict):
            return {"question": "", "period": "today"}
        return {
            "question": str(data.get("question") or "").strip(),
            "period": str(data.get("period") or "today").strip() or "today",
        }

    def _parse_skill_natural_command(self, content: str):
        text = str(content or "").strip()
        if not text:
            return None
        if text.startswith("/") or text.lower().startswith("cow "):
            return None

        normalized = text.lower()
        compact = re.sub(r"[\s,，。.!！?？:：;；\"'`“”‘’（）()\[\]【】<>《》]+", "", normalized)
        has_skill_context = any(marker in compact or marker in normalized for marker in _SKILL_CONTEXT_MARKERS)
        has_list_request = any(marker in compact or marker in normalized for marker in _SKILL_LIST_MARKERS)
        has_standalone_list_request = any(
            marker in compact or marker in normalized for marker in _SKILL_STANDALONE_LIST_MARKERS
        )
        if (has_skill_context and has_list_request) or has_standalone_list_request:
            if self._wants_explicit_skill_inventory(text, "list"):
                return "skill", self._encode_skill_answer_args(text, "list")
            categories = self._find_skill_categories_in_text(text)
            if categories:
                return "skill", self._encode_skill_answer_args(text, "category", categories=categories)
            return "skill", self._encode_skill_answer_args(text, "list")

        has_usage_request = any(marker in compact or marker in normalized for marker in _SKILL_USAGE_MARKERS)
        if not has_usage_request:
            return None

        entry = self._skill_catalog().find_entry_in_text(text)
        if entry is not None:
            return "skill", self._encode_skill_answer_args(text, "usage", entry.name)
        if has_skill_context:
            return "skill", self._encode_skill_answer_args(text, "usage")
        return None

    @staticmethod
    def _encode_skill_answer_args(
        question: str,
        mode: str,
        skill_name: str = "",
        category: str = "",
        categories=None,
    ) -> str:
        category_values = []
        if categories:
            category_values = [str(item or "").strip() for item in categories if str(item or "").strip()]
        elif category:
            category_values = [
                item.strip()
                for item in re.split(r"[,，|、\s]+", str(category or ""))
                if item.strip()
            ]
        payload = {
            "question": str(question or "").strip(),
            "mode": str(mode or "list").strip() or "list",
            "skill": str(skill_name or "").strip(),
            "category": ",".join(category_values) if category_values else str(category or "").strip(),
            "categories": category_values,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return f"answer {encoded}"

    @staticmethod
    def _decode_skill_answer_args(encoded: str) -> dict:
        raw = str(encoded or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _find_skill_categories_in_text(self, text: str):
        catalog = self._skill_catalog()
        finder = getattr(catalog, "find_categories_in_text", None)
        if callable(finder):
            return finder(text)
        category = catalog.find_category_in_text(text)
        return [category] if category else []

    @staticmethod
    def _suggest_command(cmd: str) -> str:
        """
        Return the closest known command if cmd is a likely typo, else "".
        Returns None to indicate "do not intercept" (when input is too far off).

        Heuristic: edit distance <= 1 (single insert/delete/substitute) when
        |cmd| >= 3, and the candidate shares the same first letter.
        """
        if not cmd:
            return ""
        if len(cmd) < 3:
            return None

        def edit_distance_le1(a: str, b: str) -> bool:
            if a == b:
                return True
            la, lb = len(a), len(b)
            if abs(la - lb) > 1:
                return False
            if la == lb:
                diffs = sum(1 for x, y in zip(a, b) if x != y)
                return diffs <= 1
            short, long_ = (a, b) if la < lb else (b, a)
            i = j = 0
            skipped = False
            while i < len(short) and j < len(long_):
                if short[i] != long_[j]:
                    if skipped:
                        return False
                    skipped = True
                    j += 1
                else:
                    i += 1
                    j += 1
            return True

        for known in KNOWN_COMMANDS:
            if known[0] == cmd[0] and edit_distance_le1(cmd, known):
                return known
        return None

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def execute(self, query: str, session_id: str = "") -> str:
        """Execute a cow/slash command string without a channel context.

        Used by cloud on_chat to intercept commands before the agent runs.
        Returns None when *query* is not command-like at all (e.g. natural
        language). For slash-prefixed typos returns a hint string so the
        caller still short-circuits the agent round.
        """
        parsed = self._parse_command(query.strip())
        if parsed is None:
            return None
        cmd, args = parsed
        if cmd not in KNOWN_COMMANDS:
            suggestion = self._suggest_command(cmd)
            if suggestion is None:
                return None
            hint = f"未知命令: /{cmd}"
            if suggestion:
                hint += f"\n你是不是想输入 /{suggestion} ?"
            hint += "\n发送 /help 查看全部命令。"
            return hint
        return self._dispatch(cmd, args, e_context=None, session_id=session_id)

    def _dispatch(self, cmd: str, args: str, e_context: EventContext, session_id: str = "") -> str:
        if cmd in CLI_ONLY_COMMANDS:
            return f"⚠️ `cow {cmd}` 只能在命令行终端中执行。\n请在终端运行: cow {cmd}"

        handler_attr = "_cmd_" + cmd.replace("-", "_")
        handler = getattr(self, handler_attr, None)
        if handler:
            try:
                return handler(args, e_context, session_id=session_id)
            except Exception as e:
                logger.error(f"[CowCli] command '{cmd}' failed: {e}")
                return f"命令执行失败: {e}"

        return f"未知命令: {cmd}"

    # ------------------------------------------------------------------
    # command access
    # ------------------------------------------------------------------

    def _can_use_command(self, cmd: str, args: str, e_context: EventContext) -> bool:
        if self._is_admin_context(e_context):
            return True
        if cmd == "backend" and self._is_personal_backend_switch_args(args):
            profile = self._resolve_backend_actor_profile(e_context)
            if profile is not None:
                try:
                    from common.llm_backend_router import can_use_restricted_backend

                    return can_use_restricted_backend(profile)
                except Exception as e:
                    logger.debug(f"[CowCli] restricted backend access check failed: {e}")
                    return False
        return self._command_access_level(cmd, args) == ACCESS_PUBLIC

    def _command_access_level(self, cmd: str, args: str = "") -> str:
        if cmd == "skill":
            return self._skill_access_level(args)
        if cmd == "context":
            return ACCESS_ADMIN if self._first_arg(args) == "clear" else ACCESS_PUBLIC
        if cmd == "memory":
            sub = self._first_arg(args)
            return ACCESS_PUBLIC if sub in {"", "status", "info"} else ACCESS_ADMIN
        if cmd == "knowledge":
            sub = self._first_arg(args)
            return ACCESS_PUBLIC if sub in {"", "stats", "status", "list", "tree"} else ACCESS_ADMIN
        if cmd == "backend":
            return self._backend_access_level(args)
        if cmd == "voice":
            sub = self._first_arg(args)
            return ACCESS_PUBLIC if sub in {"", "status", "show"} else ACCESS_ADMIN
        return COMMAND_ACCESS.get(cmd, ACCESS_ADMIN)

    @staticmethod
    def _skill_access_level(args: str) -> str:
        parts = str(args or "").strip().split()
        sub = parts[0].lower() if parts else ""
        if sub in {"", "list", "search", "info", "usage", "use", "answer"}:
            return ACCESS_PUBLIC
        return ACCESS_ADMIN

    def _backend_access_level(self, args: str) -> str:
        return ACCESS_ADMIN

    @staticmethod
    def _first_arg(args: str) -> str:
        parts = str(args or "").strip().split()
        return parts[0].lower() if parts else ""

    @staticmethod
    def _is_personal_backend_switch_args(args: str) -> bool:
        parts = str(args or "").strip().split()
        sub = parts[0].lower() if parts else ""
        if sub in {"grok", "xai", "x.ai", "xai-oauth", "grok-account", "gpt", "chatgpt", "default", "global"}:
            return True
        return False

    @staticmethod
    def _resolve_backend_actor_profile(e_context: EventContext):
        if e_context is None:
            return None
        try:
            context = e_context["context"]
        except Exception:
            return None
        try:
            profile = getattr(context, "_actor_profile", None) or context.get("_actor_profile")
        except Exception:
            profile = getattr(context, "_actor_profile", None)
        if profile is not None:
            return profile
        try:
            from agent.user_profiles import apply_profile_to_context, resolve_agent_user_profile

            profile = resolve_agent_user_profile(context)
            apply_profile_to_context(context, profile)
            context["_actor_profile"] = profile
            return profile
        except Exception as e:
            logger.debug(f"[CowCli] failed to resolve backend actor profile: {e}")
            return None

    def _is_admin_context(self, e_context: EventContext) -> bool:
        if e_context is None:
            return True
        try:
            context = e_context["context"]
        except Exception:
            return False

        role = str(context.get("actor_role", "") or "").strip().lower()
        if role == "admin":
            return True

        actor_id = str(context.get("actor_id", "") or "").strip()
        raw_user_id = str(context.get("raw_user_id", "") or context.get("from_user_id", "") or "").strip()
        actual_user_id = str(context.get("actual_user_id", "") or context.get("group_sender_id", "") or "").strip()
        sender_staff_id = str(context.get("sender_staff_id", "") or "").strip()
        receiver = str(context.get("receiver", "") or "").strip()
        channel_type = str(context.get("channel_type", "") or context.get("channel", "") or "").strip()
        candidates = {actor_id, raw_user_id, actual_user_id, sender_staff_id, receiver}
        if channel_type:
            for user_id in (raw_user_id, actual_user_id, sender_staff_id, receiver):
                if user_id:
                    candidates.add(f"{channel_type}:{user_id}")
        try:
            msg = context.get("msg")
        except Exception:
            msg = None
        if msg is not None:
            for attr in ("actual_user_id", "from_user_id", "sender_staff_id"):
                value = str(getattr(msg, attr, "") or "").strip()
                if not value:
                    continue
                candidates.add(value)
                if channel_type:
                    candidates.add(f"{channel_type}:{value}")

        try:
            from config import conf, global_config

            admin_users = conf().get("agent_admin_users", []) or []
            if isinstance(admin_users, str):
                configured = {item.strip() for item in admin_users.split(",") if item.strip()}
            else:
                configured = {str(item).strip() for item in admin_users if str(item).strip()}
            configured.update(str(item).strip() for item in (global_config.get("admin_users", []) or []) if str(item).strip())
        except Exception:
            configured = set()

        return any(candidate in configured for candidate in candidates if candidate)

    @staticmethod
    def _permission_denied_text(cmd: str) -> str:
        return "\n".join([
            f"命令 /{cmd} 需要管理员权限。",
            "普通用户可以使用 /help 查看自己可用的命令，也可以直接查询自己的本地账本。",
        ])

    # ------------------------------------------------------------------
    # local token usage
    # ------------------------------------------------------------------

    def _cmd_tokens(self, args: str, e_context: EventContext, session_id: str = "", **_) -> str:
        period = self._token_usage_arg_period(args)
        detail_requested = self._token_usage_args_request_detail(args)
        is_admin = self._is_admin_context(e_context)
        if self._token_usage_args_request_all_users(args) and not is_admin:
            return self._permission_denied_text("tokens --all-users")

        scope_all = is_admin
        user_id = "" if scope_all else self._get_memory_user_id(e_context)
        if not scope_all and not user_id:
            return "没有识别到当前用户身份，不能安全查询本地 token 用量。请在微信或企业微信会话里重试。"

        script = self._find_token_usage_script()
        if not script:
            return "未找到 token-usage-tracker 脚本，无法查询本地 token 用量。"

        periods = [period] if period == "all" else [period, "all"]
        snapshots = []
        for item_period in periods:
            try:
                snapshots.append(self._run_token_usage_summary(script, item_period, scope_all, user_id))
            except Exception as exc:
                logger.warning(f"[CowCli] local token usage query failed: {exc}")
                return f"本地 token 用量查询失败: {str(exc)[:1000]}"

        return self._format_token_usage_reply(snapshots, scope_all, show_user_details=scope_all or detail_requested)

    @staticmethod
    def _token_usage_arg_period(args: str) -> str:
        raw = str(args or "").strip().lower()
        parts = {part.strip() for part in re.split(r"[\s,，]+", raw) if part.strip()}
        if parts & {"month", "this-month", "本月", "月度"}:
            return "month"
        if parts & {"all", "total", "history", "累计", "全部", "历史"}:
            return "all"
        return "today"

    @staticmethod
    def _token_usage_args_request_all_users(args: str) -> bool:
        raw = str(args or "").strip().lower()
        parts = {part.strip() for part in re.split(r"[\s,，]+", raw) if part.strip()}
        return bool(parts & {"--all-users", "all-users", "users", "所有用户", "全员"})

    @staticmethod
    def _token_usage_args_request_detail(args: str) -> bool:
        raw = str(args or "").strip().lower()
        compact = re.sub(r"[\s,，。?!？！；;:\"'`“”‘’（）()\[\]【】<>《》]+", "", raw)
        parts = {part.strip() for part in re.split(r"[\s,，]+", raw) if part.strip()}
        return bool(
            parts & {"details", "detail", "users", "per-user", "by-user", "明细", "详细", "分别"}
            or any(marker in compact for marker in _TOKEN_USAGE_DETAIL_MARKERS)
        )

    @staticmethod
    def _project_root() -> str:
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _find_token_usage_script(self) -> str:
        project_root = self._project_root()
        workspace = self._agent_workspace_root()
        candidates = [
            os.path.join(project_root, "skills", "token-usage-tracker", "scripts", "token_usage.py"),
            os.path.join(workspace, "skills", "token-usage-tracker", "scripts", "token_usage.py"),
            os.path.join(os.path.expanduser("~/cow"), "skills", "token-usage-tracker", "scripts", "token_usage.py"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return ""

    def _agent_workspace_root(self) -> str:
        try:
            from config import conf
            from common.utils import expand_path

            workspace = conf().get("agent_workspace", "")
            if workspace:
                return expand_path(workspace)
        except Exception:
            pass
        for key in ("COW_WORKSPACE", "COW_HOME"):
            workspace = os.getenv(key)
            if workspace:
                return os.path.abspath(os.path.expanduser(workspace))
        cow_home = os.path.abspath(os.path.expanduser("~/cow"))
        if os.path.isdir(cow_home):
            return cow_home
        return self._project_root()

    def _run_token_usage_summary(self, script: str, period: str, scope_all: bool, user_id: str) -> dict:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["COW_WORKSPACE"] = self._agent_workspace_root()
        command = [
            sys.executable,
            script,
            "summary",
            "--period",
            period,
            "--source",
            "llm-cache",
        ]
        for alias, canonical in self._token_usage_user_aliases():
            command.extend(["--user-alias", f"{alias}={canonical}"])
        if scope_all:
            command.append("--all")
        else:
            command.extend(["--user-id", user_id])

        proc = subprocess.run(
            command,
            cwd=self._project_root(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(detail or f"token_usage.py exited with {proc.returncode}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"token_usage.py returned non-JSON output: {exc}")
        if not isinstance(payload, dict):
            raise RuntimeError("token_usage.py returned an invalid payload")
        return payload

    @staticmethod
    def _token_usage_user_aliases() -> list:
        try:
            from config import conf

            aliases = conf().get("llm_usage_user_aliases", {}) or {}
        except Exception:
            aliases = {}
        pairs = []
        if isinstance(aliases, dict):
            for alias, canonical in aliases.items():
                alias_text = str(alias or "").strip()
                canonical_text = str(canonical or "").strip()
                if alias_text and canonical_text:
                    pairs.append((alias_text, canonical_text))
        elif isinstance(aliases, list):
            for item in aliases:
                if not isinstance(item, dict):
                    continue
                alias_text = str(item.get("alias") or "").strip()
                canonical_text = str(item.get("canonical") or "").strip()
                if alias_text and canonical_text:
                    pairs.append((alias_text, canonical_text))
        return pairs

    def _format_token_usage_reply(self, snapshots: list, scope_all: bool, show_user_details: bool = False) -> str:
        lines = [
            "本地 CowAgent/CowWechat token 用量",
            "统计口径：本机运行日志，按北京时间自然日/自然月；不是 CAPI/Codex 后台额度。",
            "",
        ]
        if scope_all:
            lines.append("范围：管理员视图，汇总本机已记录用户。")
        else:
            lines.append("范围：当前用户。")
        for payload in snapshots:
            label = _TOKEN_USAGE_PERIOD_LABELS.get(str(payload.get("period") or "all"), str(payload.get("period") or "all"))
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
            lines.extend(["", f"{label}", *self._format_token_usage_summary_lines(summary)])
            if scope_all and isinstance(payload.get("users"), dict):
                identified_user_count = self._token_usage_identified_user_count(payload.get("users") or {})
                unattributed_count = max(len(payload.get("users") or {}) - identified_user_count, 0)
                count_label = "历史有用量用户数" if str(payload.get("period") or "") == "all" else f"{label}有用量用户数"
                lines.append(f"- {count_label}: {identified_user_count}")
                if unattributed_count:
                    lines.append(f"- 未归属/系统身份数: {unattributed_count}")
                if show_user_details:
                    lines.extend(self._format_token_usage_user_lines(payload.get("users") or {}))
            source = payload.get("source")
            if source:
                lines.append(f"- 来源: {source}")
        return "\n".join(lines)

    @classmethod
    def _format_token_usage_summary_lines(cls, summary: dict) -> list:
        input_tokens = cls._format_int(summary.get("input_tokens"))
        output_tokens = cls._format_int(summary.get("output_tokens"))
        total_tokens = cls._format_int(summary.get("total_tokens"))
        cached_tokens = int(summary.get("cached_tokens") or 0)
        prompt_tokens = int(summary.get("input_tokens") or 0)
        cache_rate = (cached_tokens / prompt_tokens * 100) if prompt_tokens else 0.0
        return [
            f"- 请求/事件: {cls._format_int(summary.get('events'))}",
            f"- 输入: {input_tokens}，输出: {output_tokens}，总计: {total_tokens}",
            f"- 缓存命中: {cls._format_int(cached_tokens)} ({cache_rate:.1f}%)",
            f"- 推理 tokens: {cls._format_int(summary.get('reasoning_tokens'))}",
        ]

    @classmethod
    def _format_token_usage_user_lines(cls, users: dict, limit: int = 20) -> list:
        if not users:
            return []
        rows = []
        unattributed = {
            "events": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
        for uhash, payload in users.items():
            if not isinstance(payload, dict):
                continue
            label = str(payload.get("display_name") or "").strip()
            if not label or cls._token_usage_is_system_identity(label, payload):
                for key in unattributed:
                    unattributed[key] += int(payload.get(key) or 0)
                continue
            rows.append((int(payload.get("total_tokens") or 0), label, payload))
        rows.sort(key=lambda item: item[0], reverse=True)
        lines = ["- 用户明细:"]
        for _, label, payload in rows[:limit]:
            lines.append(
                "  - "
                f"{label}: 请求 {cls._format_int(payload.get('events'))}，"
                f"输入 {cls._format_int(payload.get('input_tokens'))}，"
                f"输出 {cls._format_int(payload.get('output_tokens'))}，"
                f"总计 {cls._format_int(payload.get('total_tokens'))}，"
                f"缓存 {cls._format_int(payload.get('cached_tokens'))}，"
                f"推理 {cls._format_int(payload.get('reasoning_tokens'))}"
            )
        if len(rows) > limit:
            lines.append(f"  - 其余 {len(rows) - limit} 个低用量身份已省略。")
        if unattributed["events"]:
            lines.append(
                "  - "
                f"未归属/系统: 请求 {cls._format_int(unattributed.get('events'))}，"
                f"输入 {cls._format_int(unattributed.get('input_tokens'))}，"
                f"输出 {cls._format_int(unattributed.get('output_tokens'))}，"
                f"总计 {cls._format_int(unattributed.get('total_tokens'))}，"
                f"缓存 {cls._format_int(unattributed.get('cached_tokens'))}，"
                f"推理 {cls._format_int(unattributed.get('reasoning_tokens'))}"
            )
        return lines

    @classmethod
    def _token_usage_identified_user_count(cls, users: dict) -> int:
        return sum(
            1
            for payload in users.values()
            if (
                isinstance(payload, dict)
                and str(payload.get("display_name") or "").strip()
                and not cls._token_usage_is_system_identity(
                    str(payload.get("display_name") or "").strip(),
                    payload,
                )
            )
        )

    @staticmethod
    def _token_usage_is_system_identity(label: str, payload: dict) -> bool:
        lowered = str(label or "").strip().lower()
        if any(marker in lowered for marker in ("benchmark", "wecom-user-image", "system", "test-user")):
            return True
        channels = payload.get("by_channel") if isinstance(payload, dict) else {}
        if isinstance(channels, dict) and channels:
            channel_names = {str(name).lower() for name in channels}
            if channel_names <= {"voice_mode_benchmark", "unknown", "web"} and not lowered:
                return True
        return False

    @staticmethod
    def _format_int(value) -> str:
        try:
            return f"{int(value or 0):,}"
        except (TypeError, ValueError):
            return "0"

    # ------------------------------------------------------------------
    # local expense ledger
    # ------------------------------------------------------------------

    def _cmd_ledger(self, args: str, e_context: EventContext, session_id: str = "", **_) -> str:
        request = self._decode_ledger_args(args)
        user_id = self._get_memory_user_id(e_context)
        if not user_id:
            return "没有识别到当前用户的记账身份，不能安全查询账本。请在微信或企业微信会话里重试。"

        ledger = self._load_china_expense_ledger()
        db_path = ledger.db_path_from_env()
        conn = ledger.open_db(db_path)
        try:
            ledger.init_db(conn)
            period = request["period"]
            if request["mode"] == "query":
                rows = ledger.query_transactions(conn, user_id, period, 20)
                return self._format_ledger_query(rows, period)
            summary = ledger.summarize_transactions(conn, user_id, period)
            return self._format_ledger_summary(summary, period)
        finally:
            conn.close()

    @staticmethod
    def _load_china_expense_ledger():
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates = [
            os.path.join(project_root, "skills", "china-expense-ledger", "scripts", "ledger.py"),
            os.path.join(os.path.expanduser("~/cow"), "skills", "china-expense-ledger", "scripts", "ledger.py"),
        ]
        for candidate in candidates:
            if not os.path.isfile(candidate):
                continue
            spec = importlib.util.spec_from_file_location("china_expense_ledger_cow_cli", candidate)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
        raise FileNotFoundError("china-expense-ledger script not found")

    @staticmethod
    def _format_ledger_money(cents: int) -> str:
        try:
            return f"¥{int(cents) / 100:.2f}"
        except (TypeError, ValueError):
            return "¥0.00"

    def _format_ledger_summary(self, summary: dict, period: str) -> str:
        label = _LEDGER_PERIOD_LABELS.get(period, period)
        totals = summary.get("totals") or {}
        lines = [
            f"📒 {label}本地账单",
            "",
            f"支出：{self._format_ledger_money(totals.get('expense_cents', 0))}",
            f"收入：{self._format_ledger_money(totals.get('income_cents', 0))}",
            f"退款：{self._format_ledger_money(totals.get('refund_cents', 0))}",
            f"转账：{self._format_ledger_money(totals.get('transfer_cents', 0))}",
            f"记录：{int(summary.get('count') or 0)} 笔",
        ]
        by_category = summary.get("by_category") or {}
        if by_category:
            lines.extend(["", "分类"])
            for category, cents in sorted(by_category.items(), key=lambda item: int(item[1] or 0), reverse=True)[:8]:
                lines.append(f"{category}：{self._format_ledger_money(cents)}")
        return "\n".join(lines)

    def _format_ledger_query(self, rows: list, period: str) -> str:
        label = _LEDGER_PERIOD_LABELS.get(period, period)
        if not rows:
            return f"{label}本地账本暂无记录。"
        lines = [f"{label}本地账单明细（最近 {len(rows)} 笔）：", ""]
        for row in rows:
            item = row.get("item_name") or row.get("merchant") or row.get("order_platform") or "未命名"
            category = row.get("category") or "未分类"
            amount = self._format_ledger_money(row.get("amount_cents") or 0)
            occurred_at = str(row.get("occurred_at") or "")[:16].replace("T", " ")
            lines.append(f"- {occurred_at} {category} {amount} {item}".strip())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # help / version
    # ------------------------------------------------------------------

    def _cmd_help(self, args: str, e_context, **_) -> str:
        is_admin = self._is_admin_context(e_context)
        lines = ["📋 CowAgent 命令列表", ""]
        lines.extend(self._help_sections(is_admin))
        lines.append("")
        lines.append("提示：也可以用 cow <command> 代替 /<command>。")
        if not is_admin:
            lines.append("管理员命令已隐藏。")
        return "\n".join(lines)

    def _help_sections(self, is_admin: bool) -> list:
        sections = [
            ("常用", [
                ("help", "", "/help：查看可用命令"),
                ("status", "", "/status：查看运行状态"),
                ("version", "", "/version：查看版本"),
                ("tokens", "", "/tokens：查询本地 CowAgent/CowWechat token 用量"),
                ("logs", "20", "/logs 20：查看最近日志"),
            ]),
            ("账本", [
                ("ledger", self._encode_ledger_args("today"), "查询本日账单：今天本地记账汇总"),
                ("ledger", self._encode_ledger_args("week"), "查询本周消费：本周本地记账汇总"),
                ("ledger", self._encode_ledger_args("month"), "查询本月账单：本月本地记账汇总"),
                ("ledger", self._encode_ledger_args("last_month"), "查询上月账单：上月本地记账汇总"),
            ]),
            ("后端", [
                ("backend", "", "/backend：查看当前模型后端"),
                ("backend", "quota-current", "/backend quota-current：查询当前后端额度"),
                ("backend", "quota", "/backend quota：查询 Codex 额度"),
                ("backend", "quota capi", "/backend quota capi：查询 CAPI 额度卡"),
                ("backend", "codex", "/backend codex：切到 Codex"),
                ("backend", "capi", "/backend capi：切到 CAPI 额度卡"),
                ("backend", "capi-monthly", "/backend capi-monthly：切到 CAPI 月卡"),
                ("backend", "auto reset", "/backend auto reset：重置后端自动切换"),
            ]),
            ("语音", [
                ("voice", "", "/voice：查看语音模式状态"),
                ("voice", "on", "/voice on：热开启语音模式"),
                ("voice", "off", "/voice off：热关闭语音模式"),
            ]),
            ("上下文", [
                ("context", "", "/context：查看当前对话上下文"),
                ("context", "clear", "/context clear：清除当前对话上下文"),
            ]),
            ("技能", [
                ("skill", "list", "/skill list：查看已安装技能"),
                ("skill", "info example", "/skill info <名称>：查看技能详情"),
                ("skill", "search example", "/skill search <关键词>：搜索技能"),
                ("skill", "install example", "/skill install <名称>：安装技能"),
                ("skill", "uninstall example", "/skill uninstall <名称>：卸载技能"),
                ("skill", "enable example", "/skill enable <名称>：启用技能"),
                ("skill", "disable example", "/skill disable <名称>：禁用技能"),
            ]),
            ("记忆与知识库", [
                ("memory", "status", "/memory status：查看记忆索引状态"),
                ("memory", "dream 3", "/memory dream 3：手动整理近 3 天记忆"),
                ("memory", "rebuild-index", "/memory rebuild-index：重建记忆索引"),
                ("knowledge", "", "/knowledge：查看知识库统计"),
                ("knowledge", "list", "/knowledge list：查看知识库文件树"),
                ("knowledge", "on", "/knowledge on|off：开启或关闭知识库"),
            ]),
            ("配置", [
                ("config", "", "/config：查看当前配置"),
                ("config", "model", "/config <key>：查看某项配置"),
                ("config", "model gpt-5", "/config <key> <val>：修改配置"),
            ]),
        ]

        lines = []
        for title, entries in sections:
            visible = [
                label
                for command, command_args, label in entries
                if is_admin or self._command_access_level(command, command_args) == ACCESS_PUBLIC
            ]
            if not visible:
                continue
            if lines:
                lines.append("")
            lines.append(title)
            lines.extend(visible)
        return lines

    def _cmd_version(self, args: str, e_context, **_) -> str:
        return f"CowAgent v{__version__}"

    # ------------------------------------------------------------------
    # project updates
    # ------------------------------------------------------------------

    def _cmd_updates(self, args: str, e_context, session_id: str = "", **_) -> str:
        payload = self._decode_project_update_args(args)
        question = payload.get("question", "")
        period = str(payload.get("period") or "today").strip().lower() or "today"
        if period not in {"today", "今日", "今天"}:
            period = "today"
        commits = self._git_commits_for_today()
        update_entries = self._read_readme_update_entries_for_today()
        if not commits and not update_entries:
            return "我查了本项目今天的 Git 更新记录，没看到今天的新提交。"

        recommended = self._summarize_updates_for_private_use(commits, update_entries)
        update_count = max(len(commits), len(update_entries))
        context = self._format_project_update_context(commits, update_entries, recommended, update_count)
        target_label = self._project_update_target_label(question)
        if question:
            try:
                answer = self._call_project_update_summary_model(
                    question=question,
                    update_context=context,
                    e_context=e_context,
                    session_id=session_id,
                )
                if answer:
                    return answer
            except Exception as exc:
                logger.warning(f"[CowCli] project update model summary failed: {exc}")

        lines = [
            "我查的是本项目今天的 Git/README 更新记录，不是泛化功能清单。",
            f"今天共看到 {update_count} 条更新记录。适合推荐给{target_label}的主要是：",
            "",
        ]
        for index, item in enumerate(recommended[:6], 1):
            lines.append(f"{index}. {item}")
        internal_count = update_count - len(recommended)
        if internal_count > 0:
            lines.extend([
                "",
                f"另外有 {internal_count} 个偏内部的后端、测试、文档或发布安全更新，不建议作为功能点单独推送。",
            ])
        return "\n".join(lines)

    @staticmethod
    def _project_update_target_label(question: str) -> str:
        text = str(question or "")
        if any(marker in text for marker in ("老婆", "妻子", "太太", "媳妇", "夫人")):
            return "你老婆"
        if any(marker in text for marker in ("老公", "丈夫", "先生")):
            return "你老公"
        if "家人" in text:
            return "你家人"
        if "朋友" in text:
            return "朋友"
        return "你指定的人"

    @staticmethod
    def _format_project_update_context(commits, update_entries, recommended, update_count: int) -> str:
        lines = [
            f"今日更新记录数：{update_count}",
            "",
            "README 更新日志：",
        ]
        if update_entries:
            for entry in update_entries[:20]:
                lines.append(f"- {entry}")
        else:
            lines.append("- （README 今日更新日志为空，使用 Git 提交主题兜底。）")
        lines.extend(["", "Git 提交主题："])
        if commits:
            for commit in commits[:40]:
                lines.append(f"- {commit.get('hash', '')} {commit.get('subject', '')}")
        else:
            lines.append("- （今日没有读取到 Git 提交。）")
        lines.extend(["", "本地候选功能分类（仅供模型筛选，不要照搬）："])
        for item in recommended[:8]:
            lines.append(f"- {item}")
        return "\n".join(lines)[:12000]

    def _call_project_update_summary_model(
        self,
        *,
        question: str,
        update_context: str,
        e_context,
        session_id: str = "",
    ) -> str:
        from agent.protocol import LLMRequest
        from bridge.agent_bridge import AgentLLMModel
        from bridge.bridge import Bridge

        llm = AgentLLMModel(Bridge())
        try:
            context = e_context["context"] if e_context is not None else None
        except Exception:
            context = None
        if context is not None:
            llm.channel_type = context.get("channel_type", "") or context.get("channel", "")
            llm.session_id = session_id or context.get("session_id", "") or context.get("from_user_id", "")
            llm.user_id = context.get("from_user_id", "") or context.get("receiver", "")
            llm.user_label = context.get("actual_user_nickname", "") or context.get("from_user_nickname", "")
        elif session_id:
            llm.session_id = session_id

        system = (
            "你是 CowWeCom 项目更新总结助手。"
            "只能依据提供的 Git/README 更新记录回答，不要泛化成本机功能清单。"
            "必须保留用户原话中的推荐对象和筛选目标；如果用户说推荐给老婆，就直接围绕“适合推给你老婆”筛选。"
            "优先推荐普通使用者能直接感知的功能和体验改进，排除后端、测试、发布、安全流程等内部维护，除非它们能转化成明显体验收益。"
            "回答要像可直接转发前的建议，简洁、具体、自然中文；不要泄露本地路径、密钥、内部实现细节。"
        )
        user = (
            f"用户原话：{question}\n\n"
            f"项目今日 Git/README 更新记录：\n{update_context}\n\n"
            "请按用户原话筛选并总结，不要把候选分类原样照搬。"
        )
        response = llm.call(
            LLMRequest(
                messages=[{"role": "user", "content": user}],
                system=system,
                max_tokens=900,
                temperature=0.2,
                tools=[],
                request_timeout=45,
                reasoning_effort="medium",
                reasoning_effort_locked=True,
                cache_shape_metadata={"request_kind": "cow_cli_project_update_summary"},
            )
        )
        return self._extract_model_text(response)

    @staticmethod
    def _git_commits_for_today():
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        now = datetime.now()
        start = now.strftime("%Y-%m-%d 00:00")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d 00:00")
        try:
            proc = subprocess.run(
                [
                    "git",
                    "log",
                    f"--since={start}",
                    f"--until={end}",
                    "--date=short",
                    "--pretty=format:%h%x09%ad%x09%s",
                ],
                cwd=project_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            logger.warning(f"[CowCli] git update summary failed: {exc}")
            return []
        if proc.returncode != 0:
            logger.warning(f"[CowCli] git update summary failed: {(proc.stderr or '').strip()[:300]}")
            return []
        commits = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
        return commits

    @staticmethod
    def _read_readme_update_entries_for_today():
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        readme = os.path.join(project_root, "README.md")
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(readme, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            logger.warning(f"[CowCli] README update summary failed: {exc}")
            return []

        entries = []
        in_changelog = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("## 更新日志"):
                in_changelog = True
                continue
            if in_changelog and line.startswith("## "):
                break
            if not in_changelog or not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] == today:
                entries.append(cells[1])
        return entries

    @staticmethod
    def _summarize_updates_for_private_use(commits, update_entries=None) -> list:
        subjects = [str(commit.get("subject") or "") for commit in commits]
        entries = [str(entry or "") for entry in (update_entries or [])]
        joined = "\n".join(subjects + entries).lower()
        items = []
        if any(marker in joined for marker in ("wecom bot", "websocket", "subscribe", "企业微信", "智能机器人", "长连接", "无回复")):
            items.append("企业微信回复更稳：机器人订阅卡住时会自动断开重连，减少消息长时间无回复。")
        if any(marker in joined for marker in ("travel", "amap", "railway", "flyai", "hotel", "旅行", "行程", "酒店", "住宿", "票价", "高德", "12306")):
            items.append("旅行规划更适合直接用：会先做规划前确认，再结合交通、天气、住宿和票价信息整理行程。")
        if any(marker in joined for marker in ("long task", "progress", "finish long tasks", "长任务", "进度提醒", "完成回执")):
            items.append("长任务体验更清楚：等待时有进度提醒，完成后会补完成回执，减少以为卡住的情况。")
        if any(marker in joined for marker in ("image", "vision", "followup", "图片", "图像", "追问")):
            items.append("图片识别追问更稳：旧图片上下文不会轻易污染新的问题。")
        if any(marker in joined for marker in ("skill catalog", "skill inventory", "quick answer", "category", "skill", "功能快答", "功能查询", "分类")):
            items.append("功能查询更清楚：问支持哪些功能、某类功能有哪些时，会按中文分类给说明。")
        if any(marker in joined for marker in ("capi", "codex", "backend", "quota", "后端", "额度", "月卡")):
            items.append("模型后端更抗失败：额度或网络异常时会更稳地切换/重试，减少直接报错。")
        if not items:
            items.append("今天有项目更新，但从提交主题看主要偏内部维护；建议先查看 README 更新日志再决定是否推送。")
        return items

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def _cmd_status(self, args: str, e_context: EventContext, session_id: str = "", **_) -> str:
        from config import conf

        cfg = conf()
        lines = ["📊 CowAgent 运行状态", ""]

        lines.append(f"  版本: v{__version__}")
        lines.append(f"  进程: PID {os.getpid()}")

        channel = cfg.get("channel_type", "unknown")
        if isinstance(channel, list):
            channel = ", ".join(channel)
        lines.append(f"  通道: {channel}")

        model_name = cfg.get("model", "unknown")
        lines.append(f"  模型: {model_name}")

        mode = "Chat" if cfg.get("agent") is False else "Agent"
        lines.append(f"  模式: {mode}")

        session_id = self._get_session_id(e_context, fallback=session_id)
        agent = self._get_agent(session_id)
        if agent:
            lines.append("")
            with agent.messages_lock:
                msg_count = len(agent.messages)
            lines.append(f"  会话消息数: {msg_count}")

            if agent.skill_manager:
                total = len(agent.skill_manager.skills)
                enabled = sum(
                    1 for v in agent.skill_manager.skills_config.values()
                    if v.get("enabled", True)
                )
                lines.append(f"  已加载技能: {enabled}/{total}")
        else:
            lines.append("")
            lines.append(f"  Agent: 未初始化 (首次对话后自动创建)")

        return "\n".join(lines)

    def _cmd_backend(self, args: str, e_context, **_) -> str:
        from common.llm_backend_router import (
            USER_BACKEND_DEFAULT,
            clear_manual_override,
            describe_status,
            normalize_backend,
            set_current_backend,
            set_user_backend_override,
        )

        parts = args.strip().split()
        if not parts or parts[0].lower() in {"status", "show"}:
            return describe_status()

        sub = parts[0].lower()
        if sub in {"credential-safety", "key-safety", "secret-safety"}:
            return self._backend_credential_safety()

        if sub in {"quota-current", "current-quota", "active-quota"}:
            return self._backend_current_quota()

        quota_backend = self._backend_quota_target(parts)
        if quota_backend:
            return self._backend_capi_quota(quota_backend)

        if self._is_personal_backend_switch_args(args):
            profile = self._resolve_backend_actor_profile(e_context)
            if profile is None:
                return "Personal backend switch requires a chat user context."
            backend = normalize_backend(sub)
            set_user_backend_override(profile, backend, manual=True, reason="cow_cli")
            if backend == USER_BACKEND_DEFAULT:
                return "Personal LLM backend switched to shared GPT default"
            return "Personal LLM backend switched to {}".format(backend)

        if sub in {"codex", "capi", "capi_monthly", "capi-monthly", "monthly", "capi-month"}:
            backend = normalize_backend(sub)
            set_current_backend(backend, manual=True, reason="cow_cli")
            return "LLM backend switched to {}".format(backend)

        if sub == "auto" and len(parts) > 1 and parts[1].lower() == "reset":
            clear_manual_override()
            return "LLM backend auto-switch has been reset."

        if sub in {"quota", "gpt-quota", "codex-quota"}:
            return self._backend_quota()

        return "\n".join([
            "Usage:",
            "  /backend",
            "  /backend codex",
            "  /backend capi            切换到 CAPI 额度卡",
            "  /backend capi-monthly    切换到 CAPI 月卡",
            "  /backend grok            switch your personal backend to Grok (admin/whitelist only)",
            "  /backend gpt             switch your personal backend back to the shared GPT default",
            "  /backend auto reset",
            "  /backend quota",
            "  /backend quota-current",
            "  /backend quota capi",
            "  /backend quota capi-monthly",
        ])

    def _cmd_voice(self, args: str, e_context, **_) -> str:
        from config import conf

        parts = args.strip().split()
        sub = parts[0].lower() if parts else "status"
        if sub in {"", "status", "show"}:
            return self._voice_mode_status()
        if sub not in {"on", "off", "enable", "disable"}:
            return "\n".join([
                "Usage:",
                "  /voice",
                "  /voice on",
                "  /voice off",
            ])

        enabled = sub in {"on", "enable"}
        updates = {
            "grok_voice_mode_enabled": enabled,
            "grok_voice_reply_enabled": enabled,
            "grok_voice_conversation_mode_enabled": enabled,
            "grok_voice_streaming_enabled": enabled,
            "grok_voice_force_voice_for_voice_input_in_conversation_mode": enabled,
        }
        if enabled and not conf().get("grok_voice_reply_channels"):
            updates["grok_voice_reply_channels"] = ["wechatcom_app", "wecom_bot"]

        error = self._persist_runtime_config_updates(updates)
        if error:
            return f"语音模式内存已切换，但写入 config.json 失败: {error}"

        status = "开启" if enabled else "关闭"
        return f"语音模式已{status}，已热切换，无需重启。"

    def _voice_mode_status(self) -> str:
        from config import conf

        cfg = conf()
        enabled = bool(cfg.get("grok_voice_conversation_mode_enabled") or cfg.get("grok_voice_mode_enabled"))
        streaming = bool(cfg.get("grok_voice_streaming_enabled", True))
        channels = cfg.get("grok_voice_reply_channels", [])
        if isinstance(channels, (list, tuple, set)):
            channels_text = ", ".join(str(item) for item in channels)
        else:
            channels_text = str(channels or "")
        return "\n".join([
            f"语音模式: {'开启' if enabled else '关闭'}",
            f"流式语音: {'开启' if streaming else '关闭'}",
            f"允许通道: {channels_text or '未配置'}",
        ])

    def _persist_runtime_config_updates(self, updates: dict) -> str:
        from config import conf

        config_path = self._project_config_path()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
            file_config.update(updates)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(file_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            conf().update(updates)
            return str(e)
        conf().update(updates)
        logger.info(f"[CowCli] runtime config hot update: {updates}")
        return ""

    @staticmethod
    def _project_config_path() -> str:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(project_root, "config.json")

    def _backend_quota(self) -> str:
        from common.codex_quota_query import format_codex_quota_snapshot_text, query_codex_quota_json
        from common.llm_backend_router import record_codex_quota_check

        try:
            snapshot = query_codex_quota_json(timeout_seconds=120)
        except Exception as e:
            return "Codex quota query failed:\n{}".format(str(e)[:1200])
        record_codex_quota_check(snapshot, action="manual_quota_query")
        return format_codex_quota_snapshot_text(snapshot) or "Codex quota query returned no content."

    def _backend_current_quota(self) -> str:
        from common.llm_backend_router import BACKEND_CAPI_MONTHLY, BACKEND_CODEX, get_current_backend

        backend = get_current_backend()
        if backend == BACKEND_CODEX:
            return self._backend_quota()
        if backend == BACKEND_CAPI_MONTHLY:
            return self._backend_capi_quota(BACKEND_CAPI_MONTHLY)
        return self._backend_capi_quota("capi")

    @staticmethod
    def _backend_quota_target(parts) -> str:
        if not parts:
            return ""
        sub = parts[0].lower()
        raw = " ".join(parts).lower().replace("_", "-")
        if sub in {"quota-capi-monthly", "capi-monthly-quota", "monthly-quota"}:
            return "capi_monthly"
        if sub in {"quota-capi", "capi-quota", "quota-card", "quota-card-quota"}:
            return "capi"
        if sub == "quota" and len(parts) > 1:
            if any(marker in raw for marker in ("capi-monthly", "monthly", "month", "月卡")):
                return "capi_monthly"
            if any(marker in raw for marker in ("capi", "quota-card", "card", "额度卡")):
                return "capi"
        if sub in {"capi", "capi-monthly", "capi_monthly", "monthly", "capi-month"} and len(parts) > 1:
            tail = " ".join(parts[1:]).lower()
            if any(marker in tail for marker in ("quota", "usage", "balance", "额度", "余额", "用量")):
                return "capi_monthly" if sub in {"capi-monthly", "capi_monthly", "monthly", "capi-month"} else "capi"
        return ""

    def _backend_capi_quota(self, backend: str) -> str:
        from common.capi_quota_query import format_capi_quota_snapshot_text, query_capi_quota_snapshot
        from common.llm_backend_router import (
            BACKEND_CAPI,
            BACKEND_CAPI_MONTHLY,
            get_capi_provider_config,
            record_capi_quota_check,
            resolve_provider_value,
        )

        normalized = BACKEND_CAPI_MONTHLY if backend == BACKEND_CAPI_MONTHLY else BACKEND_CAPI
        provider = get_capi_provider_config(normalized)
        api_key = resolve_provider_value(provider, "api_key", "api_key_env")
        if not api_key:
            label = "CAPI monthly" if normalized == BACKEND_CAPI_MONTHLY else "CAPI quota-card"
            return f"{label} API key is not configured."

        label = "CAPI monthly quota" if normalized == BACKEND_CAPI_MONTHLY else "CAPI quota-card quota"
        try:
            snapshot = query_capi_quota_snapshot(normalized, include_usage=True, timeout_seconds=120)
        except Exception as e:
            text = self._redact_secret(str(e), api_key)
            return f"{label} query failed:\n{text[:1200]}"
        record_capi_quota_check(normalized, snapshot, action="manual_quota_query")
        return format_capi_quota_snapshot_text(snapshot) or f"{label} query returned no content."

    @staticmethod
    def _redact_secret(text: str, secret: str) -> str:
        if not text or not secret:
            return text
        replacement = f"{secret[:3]}***{secret[-3:]}" if len(secret) >= 8 else "***"
        return text.replace(secret, replacement)

    @staticmethod
    def _backend_credential_safety() -> str:
        return "\n".join([
            "我不能显示原始 key、token 或 secret。",
            "可以查看安全状态：/backend status",
            "需要配置时，请使用 env_config 设置对应环境变量；回复和日志只应保留掩码或后缀。",
        ])

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def _cmd_logs(self, args: str, e_context, **_) -> str:
        num_lines = 20
        if args.strip().isdigit():
            num_lines = min(int(args.strip()), 50)

        log_file = self._find_log_file()
        if not log_file:
            return "未找到日志文件"

        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-num_lines:]
            content = "".join(tail).strip()
            if not content:
                return "日志为空"
            return f"📄 最近 {len(tail)} 条日志:\n\n{content}"
        except Exception as e:
            return f"读取日志失败: {e}"

    def _find_log_file(self) -> str:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates = [
            os.path.join(project_root, "nohup.out"),
            os.path.join(project_root, "run.log"),
        ]
        import glob as glob_mod
        candidates.extend(sorted(glob_mod.glob(os.path.join(project_root, "logs", "*.log")), reverse=True))
        for f in candidates:
            if os.path.isfile(f) and os.path.getsize(f) > 0:
                return f
        return ""

    # ------------------------------------------------------------------
    # context
    # ------------------------------------------------------------------

    def _cmd_context(self, args: str, e_context: EventContext, session_id: str = "", **_) -> str:
        session_id = self._get_session_id(e_context, fallback=session_id)
        agent = self._get_agent(session_id)

        sub = args.strip().lower()
        if sub == "clear":
            return self._context_clear(agent, session_id)
        else:
            return self._context_info(agent, session_id)

    def _context_info(self, agent, session_id: str) -> str:
        if not agent:
            return "⚠️ Agent 未初始化，暂无上下文信息"

        with agent.messages_lock:
            messages = agent.messages.copy()

        if not messages:
            return "当前对话上下文为空"

        user_msgs = sum(1 for m in messages if m.get("role") == "user")
        assistant_msgs = sum(1 for m in messages if m.get("role") == "assistant")
        tool_msgs = sum(1 for m in messages if m.get("role") == "tool")

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)

        lines = [
            "💬 当前对话上下文",
            "",
            f"  会话: {session_id or 'default'}",
            f"  总消息数: {len(messages)}",
            f"  用户消息: {user_msgs}",
            f"  助手回复: {assistant_msgs}",
            f"  工具调用: {tool_msgs}",
            f"  内容总长度: ~{total_chars} 字符",
            "",
            "  发送 /context clear 可清除对话上下文",
        ]
        return "\n".join(lines)

    def _context_clear(self, agent, session_id: str) -> str:
        if not agent:
            return "⚠️ Agent 未初始化"

        with agent.messages_lock:
            count = len(agent.messages)
            agent.messages.clear()

        return f"✅ 已清除当前对话上下文 ({count} 条消息)"

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------

    _CONFIG_WRITABLE = {
        "model",
        "agent_max_context_tokens",
        "agent_max_context_turns",
        "agent_max_steps",
        "knowledge",
        "enable_thinking",
        "grok_voice_mode_enabled",
        "grok_voice_reply_enabled",
        "grok_voice_conversation_mode_enabled",
        "grok_voice_streaming_enabled",
        "grok_voice_force_voice_for_voice_input_in_conversation_mode",
    }

    _CONFIG_READABLE = _CONFIG_WRITABLE | {"channel_type"}

    def _cmd_config(self, args: str, e_context, **_) -> str:
        from config import conf, load_config
        import json as _json

        parts = args.strip().split(None, 1)
        if not parts:
            return self._config_show_all()

        key = parts[0].lower()
        if len(parts) == 1:
            return self._config_get(key)

        value_str = parts[1].strip()
        return self._config_set(key, value_str)

    def _config_show_all(self) -> str:
        from config import conf
        cfg = conf()
        lines = ["⚙️ 当前配置", ""]
        for key in sorted(self._CONFIG_READABLE):
            val = cfg.get(key, "")
            lines.append(f"  {key}: {val}")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 /config <key>        查看配置")
        lines.append("💡 /config <key> <val>  修改配置")
        return "\n".join(lines)

    def _config_get(self, key: str) -> str:
        from config import conf
        if key not in self._CONFIG_READABLE:
            available = ", ".join(sorted(self._CONFIG_READABLE))
            return f"不支持查看 '{key}'\n\n可查看的配置项: {available}"
        val = conf().get(key, "")
        return f"⚙️ {key}: {val}"

    def _config_set(self, key: str, value_str: str) -> str:
        from config import conf, load_config, available_setting
        import json as _json

        if key not in self._CONFIG_WRITABLE:
            if key in self._CONFIG_READABLE:
                return f"⚠️ '{key}' 为只读配置，不支持修改"
            available = ", ".join(sorted(self._CONFIG_WRITABLE))
            return f"不支持修改 '{key}'\n\n可修改的配置项: {available}"

        old_val = conf().get(key, "")

        try:
            new_val = _json.loads(value_str)
        except (_json.JSONDecodeError, ValueError):
            if value_str.lower() == "true":
                new_val = True
            elif value_str.lower() == "false":
                new_val = False
            else:
                new_val = value_str

        updates = {key: new_val}
        old_bot_type = conf().get("bot_type", "")

        if key == "model" and old_bot_type:
            from common import const
            if old_bot_type not in (const.CUSTOM,):
                resolved = self._resolve_bot_type_for_model(str(new_val))
                if resolved:
                    updates["bot_type"] = resolved

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(project_root, "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = _json.load(f)
            file_config.update(updates)
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(file_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            return f"写入 config.json 失败: {e}"

        # Sync updated values to environment variables so that load_config()
        # won't overwrite the new value with a stale env var (common in Docker).
        # Match env var keys case-insensitively (Docker compose typically uses
        # upper-case like MODEL, but lower-case is also possible).
        synced_envs = {}
        for k, v in updates.items():
            if k not in available_setting:
                continue
            str_val = str(v)
            k_lower = k.lower()
            for env_key in list(os.environ):
                if env_key.lower() == k_lower:
                    os.environ[env_key] = str_val
                    synced_envs[env_key] = str_val
        logger.info(f"[CowCli] config update: {updates}, synced envs: {synced_envs}")

        try:
            load_config()
        except Exception as e:
            logger.warning(f"[CowCli] config reload warning: {e}")

        result = f"✅ 配置已更新\n\n  {key}: {old_val} → {new_val}"
        if "bot_type" in updates and updates["bot_type"] != old_bot_type:
            result += f"\n  bot_type: {old_bot_type} → {updates['bot_type']}"
        return result

    @staticmethod
    def _resolve_bot_type_for_model(model_name: str) -> str:
        """Resolve bot_type from model name, matching AgentBridge mapping."""
        from common import const
        _EXACT = {
            "wenxin": const.BAIDU, "wenxin-4": const.BAIDU,
            "xunfei": const.XUNFEI, const.QWEN: const.QWEN_DASHSCOPE,
            const.QIANFAN: const.QIANFAN,
            const.MODELSCOPE: const.MODELSCOPE,
            const.CODEX: const.CODEX,
            const.MOONSHOT: const.MOONSHOT,
            "moonshot-v1-8k": const.MOONSHOT, "moonshot-v1-32k": const.MOONSHOT,
            "moonshot-v1-128k": const.MOONSHOT,
        }
        _PREFIX = [
            ("qwen", const.QWEN_DASHSCOPE), ("qwq", const.QWEN_DASHSCOPE),
            ("qvq", const.QWEN_DASHSCOPE),
            ("gemini", const.GEMINI), ("glm", const.ZHIPU_AI),
            ("claude", const.CLAUDEAPI),
            ("moonshot", const.MOONSHOT), ("kimi", const.MOONSHOT),
            ("doubao", const.DOUBAO), ("deepseek", const.DEEPSEEK),
            ("grok", const.GROK), ("xai", const.GROK),
            ("ernie", const.QIANFAN),
        ]
        if not model_name:
            return const.OPENAI
        if model_name in _EXACT:
            return _EXACT[model_name]
        if model_name.lower().startswith("minimax") or model_name in ["abab6.5-chat"]:
            return const.MiniMax
        if model_name == const.CODEX or model_name.lower().startswith("codex/"):
            return const.CODEX
        if model_name in [const.QWEN_TURBO, const.QWEN_PLUS, const.QWEN_MAX]:
            return const.QWEN_DASHSCOPE
        lowered_model = model_name.lower()
        for prefix, btype in _PREFIX:
            if lowered_model.startswith(prefix):
                return btype
        return const.OPENAI

    # ------------------------------------------------------------------
    # install-browser (shared logic with cow install-browser CLI)
    # ------------------------------------------------------------------

    @staticmethod
    def _send_install_progress(e_context, text: str) -> None:
        """Push a short status line to the chat channel (SSE: phase event, not done)."""
        if e_context is None:
            logger.info(f"[CowCli] install-browser: {text}")
            return
        try:
            channel = e_context["channel"]
            context = e_context["context"]
            if channel and context:
                r = Reply(ReplyType.TEXT, text)
                r.sse_phase = True
                channel.send(r, context)
        except Exception as e:
            logger.warning(f"[CowCli] install-browser progress send failed: {e}")

    def _cmd_install_browser(self, args: str, e_context, **_) -> str:
        from cli.commands.install import run_install_browser

        if args.strip():
            return (
                "用法: /install-browser\n\n"
                "无需参数，等同于终端执行 `cow install-browser`。\n"
                "安装过程可能持续数分钟；进度会以多条消息推送，pip 详细输出见服务日志。"
            )

        # Suppress detailed stream in chat; phases go through channel.send
        def _noop_stream(msg: str, fg=None):
            pass

        code = run_install_browser(
            stream=_noop_stream,
            on_phase=lambda m: self._send_install_progress(e_context, m),
        )
        if code != 0:
            return (
                "❌ 安装未成功结束，请查看上方分段提示或服务器日志；"
                "也可在终端执行 `cow install-browser`。"
            )
        return "✅ 安装流程已结束。请重启 CowAgent 后使用 browser 工具（进度见上方消息）。"

    # ------------------------------------------------------------------
    # skill
    # ------------------------------------------------------------------

    def _cmd_skill(self, args: str, e_context, **kwargs) -> str:
        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if sub == "list":
            return self._skill_list(sub_args)
        elif sub == "search":
            return self._skill_search(sub_args)
        elif sub == "install":
            return self._skill_install(sub_args, e_context)
        elif sub == "uninstall":
            return self._skill_uninstall(sub_args)
        elif sub == "info":
            return self._skill_info(sub_args)
        elif sub in {"usage", "use"}:
            return self._skill_usage(sub_args)
        elif sub == "answer":
            return self._skill_answer(sub_args, e_context, session_id=kwargs.get("session_id", ""))
        elif sub == "enable":
            return self._skill_set_enabled(sub_args, True)
        elif sub == "disable":
            return self._skill_set_enabled(sub_args, False)
        else:
            return (
                "用法: /skill <子命令>\n\n"
                "子命令:\n"
                "  list [--remote]  查看技能列表\n"
                "  search <关键词>  搜索技能\n"
                "  install <名称>   安装技能\n"
                "  uninstall <名称> 卸载技能\n"
                "  info <名称>      查看技能详情\n"
                "  usage <名称>     查看技能用法\n"
                "  enable <名称>    启用技能\n"
                "  disable <名称>   禁用技能"
            )

    def _refresh_skill_manager(self):
        """Re-scan skill directories so skills_config.json reflects disk state."""
        try:
            from bridge.bridge import Bridge
            bridge = Bridge()
            agent_bridge = bridge.get_agent_bridge()
            for agent in [agent_bridge.default_agent] + list(agent_bridge.agents.values()):
                if agent and hasattr(agent, 'skill_manager') and agent.skill_manager:
                    agent.skill_manager.refresh_skills()
                    break
        except Exception as e:
            logger.debug(f"[CowCli] skill refresh skipped: {e}")

    @staticmethod
    def _skill_catalog():
        from agent.skills.cache import get_skill_catalog_cache
        from cli.utils import get_builtin_skills_dir, get_skills_dir

        return get_skill_catalog_cache(
            builtin_dir=get_builtin_skills_dir(),
            custom_dir=get_skills_dir(),
        )

    @staticmethod
    def _invalidate_skill_catalog():
        from agent.skills.cache import invalidate_skill_catalog_cache

        invalidate_skill_catalog_cache()

    def _skill_list_local(self) -> str:
        return self._skill_catalog().format_local_list()

    def _skill_list(self, args: str) -> str:
        parts = args.strip().split()
        if "--remote" in parts or "-r" in parts:
            page = 1
            for i, p in enumerate(parts):
                if p == "--page" and i + 1 < len(parts) and parts[i + 1].isdigit():
                    page = max(1, int(parts[i + 1]))
            return self._skill_list_remote(page=page)
        return self._skill_list_local()

    def _skill_answer(self, encoded_args: str, e_context, session_id: str = "") -> str:
        payload = self._decode_skill_answer_args(encoded_args)
        question = str(payload.get("question") or "").strip()
        mode = str(payload.get("mode") or "list").strip() or "list"
        skill_name = str(payload.get("skill") or "").strip()
        category = str(payload.get("category") or "").strip()
        categories = payload.get("categories") or []
        if not isinstance(categories, list):
            categories = []
        if not question:
            return self._skill_list_local()
        if self._wants_explicit_skill_inventory(question, mode, skill_name, category, categories):
            catalog = self._skill_catalog()
            formatter = getattr(catalog, "inventory_summary_zh", catalog.overview_summary)
            return self._call_catalog_text(formatter, max_chars=20000)
        categories = self._resolve_skill_answer_categories(
            question=question,
            mode=mode,
            category=category,
            categories=categories,
            e_context=e_context,
            session_id=session_id,
        )
        if categories and mode == "list":
            mode = "category"

        catalog_context = self._skill_answer_context(
            mode=mode,
            skill_name=skill_name,
            category=category,
            categories=categories,
        )
        fallback = self._skill_answer_fallback(
            mode=mode,
            skill_name=skill_name,
            category=category,
            categories=categories,
        )
        try:
            answer = self._call_skill_answer_model(
                question=question,
                catalog_context=catalog_context,
                e_context=e_context,
                session_id=session_id,
            )
            if answer:
                if self._requests_full_skill_fallback(answer) and skill_name:
                    return self._skill_answer_with_full_skill(
                        question=question,
                        skill_name=skill_name,
                        e_context=e_context,
                        session_id=session_id,
                        fallback=fallback,
                    )
                return answer
        except Exception as exc:
            logger.warning(f"[CowCli] skill catalog model answer failed: {exc}")
        return fallback

    def _skill_answer_context(
        self,
        mode: str = "list",
        skill_name: str = "",
        category: str = "",
        categories=None,
        max_chars: int = 12000,
    ) -> str:
        catalog = self._skill_catalog()
        if skill_name:
            return self._call_catalog_text(catalog.format_skill_detail_summary, skill_name, max_chars=max_chars)
        if mode == "category" or category:
            category_input = categories or category
            summary = ""
            if category_input:
                summarizer = getattr(catalog, "multi_category_summary", None)
                if not callable(summarizer):
                    if isinstance(category_input, list):
                        category_input = ",".join(str(item) for item in category_input)
                    summarizer = catalog.category_summary
                summary = self._call_catalog_text(summarizer, category_input, max_chars=max_chars)
            return summary or catalog.overview_summary(max_chars=max_chars)
        if mode == "list":
            category_summary = self._call_catalog_text(catalog.category_summary_for_text, category or "", max_chars=max_chars)
            if category_summary:
                return category_summary
        return self._call_catalog_text(catalog.overview_summary, max_chars=max_chars)

    def _skill_answer_fallback(
        self,
        mode: str = "list",
        skill_name: str = "",
        category: str = "",
        categories=None,
    ) -> str:
        catalog = self._skill_catalog()
        if skill_name:
            return self._skill_usage(skill_name)
        if mode == "category" or category:
            category_input = categories or category
            summary = ""
            if category_input:
                summarizer = getattr(catalog, "multi_category_summary", None)
                if not callable(summarizer):
                    if isinstance(category_input, list):
                        category_input = ",".join(str(item) for item in category_input)
                    summarizer = catalog.category_summary
                summary = self._call_catalog_text(summarizer, category_input, max_chars=6000)
            if summary:
                return summary
        return self._skill_list_local()

    @staticmethod
    def _call_catalog_text(method, *args, **kwargs) -> str:
        try:
            return method(*args, **kwargs)
        except TypeError:
            return method(*args)

    @staticmethod
    def _wants_explicit_skill_inventory(
        question: str,
        mode: str,
        skill_name: str = "",
        category: str = "",
        categories=None,
    ) -> bool:
        if str(mode or "") != "list" or skill_name or category or categories:
            return False
        normalized = str(question or "").lower()
        compact = re.sub(r"[\s,，。?!？！:：;；\"'`“”‘’（）()\[\]【】<>《》]+", "", normalized)
        mentions_skill = any(marker in compact or marker in normalized for marker in ("skill", "skills", "技能"))
        asks_inventory = any(
            marker in compact or marker in normalized
            for marker in (
                "有哪些",
                "有什么",
                "哪些",
                "支持哪些",
                "支持什么",
                "列表",
                "清单",
                "已安装",
                "本地",
                "list",
                "available",
                "show",
            )
        )
        return mentions_skill and asks_inventory

    def _resolve_skill_answer_categories(
        self,
        *,
        question: str,
        mode: str,
        category: str,
        categories,
        e_context,
        session_id: str,
    ):
        normalized = self._normalize_skill_category_values(categories or category)
        if normalized:
            return normalized
        if mode != "list":
            return []

        local_categories = self._find_skill_categories_in_text(question)
        if local_categories:
            return self._normalize_skill_category_values(local_categories)

        try:
            model_categories = self._infer_skill_categories_with_model(
                question=question,
                e_context=e_context,
                session_id=session_id,
            )
            return self._normalize_skill_category_values(model_categories)
        except Exception as exc:
            logger.warning(f"[CowCli] skill category model inference failed: {exc}")
            return []

    @staticmethod
    def _normalize_skill_category_values(categories) -> list:
        if isinstance(categories, str):
            raw_values = re.split(r"[,，|、\s]+", categories)
        elif isinstance(categories, list):
            raw_values = categories
        else:
            raw_values = []

        normalized = []
        for value in raw_values:
            item = str(value or "").strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    def _infer_skill_categories_with_model(self, question: str, e_context, session_id: str = "") -> list:
        from agent.protocol import LLMRequest
        from bridge.agent_bridge import AgentLLMModel
        from bridge.bridge import Bridge

        catalog = self._skill_catalog()
        options_summary = self._call_catalog_text(catalog.category_options_summary)
        llm = AgentLLMModel(Bridge())
        try:
            context = e_context["context"] if e_context is not None else None
        except Exception:
            context = None
        if context is not None:
            llm.channel_type = context.get("channel_type", "") or context.get("channel", "")
            llm.session_id = session_id or context.get("session_id", "") or context.get("from_user_id", "")
            llm.user_id = context.get("from_user_id", "") or context.get("receiver", "")
            llm.user_label = context.get("actual_user_nickname", "") or context.get("from_user_nickname", "")
        elif session_id:
            llm.session_id = session_id

        system = (
            "你是 CowWechat 本地 Skill 分类器。"
            "根据用户原话判断他们想查询哪些功能分类，可以选择 0 到多个分类。"
            "用户可能不会说准确分类名，要理解同义表达，例如买东西、下单、找优惠可归为购物餐饮。"
            "如果用户只是泛问全部能力，不要硬选分类，返回空列表。"
            "只能输出 JSON，不要解释。格式：{\"categories\":[\"category_id\"]}。"
        )
        user = f"用户原话：{question}\n\n{options_summary}"
        response = llm.call(
            LLMRequest(
                messages=[{"role": "user", "content": user}],
                system=system,
                max_tokens=200,
                temperature=0,
                tools=[],
                request_timeout=30,
                reasoning_effort="medium",
                reasoning_effort_locked=True,
                cache_shape_metadata={"request_kind": "cow_cli_skill_category_router"},
            )
        )
        return self._extract_category_router_output(response)

    def _extract_category_router_output(self, response) -> list:
        text = self._extract_model_text(response)
        if not text:
            return []
        try:
            data = json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text, flags=re.S)
            if not match:
                return []
            try:
                data = json.loads(match.group(0))
            except Exception:
                return []
        categories = data.get("categories") if isinstance(data, dict) else None
        if not isinstance(categories, list):
            return []
        return [str(category or "").strip() for category in categories if str(category or "").strip()]

    @staticmethod
    def _requests_full_skill_fallback(answer: str) -> bool:
        text = str(answer or "").strip()
        return text.startswith("[[READ_FULL_SKILL") or "READ_FULL_SKILL" in text[:120]

    def _skill_answer_with_full_skill(
        self,
        *,
        question: str,
        skill_name: str,
        e_context,
        session_id: str,
        fallback: str,
    ) -> str:
        full_context = self._skill_catalog().full_skill_context(skill_name)
        try:
            answer = self._call_skill_answer_model(
                question=question,
                catalog_context=full_context,
                e_context=e_context,
                session_id=session_id,
                full_skill=True,
            )
            if answer and not self._requests_full_skill_fallback(answer):
                return answer
        except Exception as exc:
            logger.warning(f"[CowCli] full skill fallback answer failed: {exc}")
        return fallback

    def _call_skill_answer_model(
        self,
        question: str,
        catalog_context: str,
        e_context,
        session_id: str = "",
        full_skill: bool = False,
    ) -> str:
        from agent.protocol import LLMRequest
        from bridge.agent_bridge import AgentLLMModel
        from bridge.bridge import Bridge

        llm = AgentLLMModel(Bridge())
        try:
            context = e_context["context"] if e_context is not None else None
        except Exception:
            context = None
        if context is not None:
            llm.channel_type = context.get("channel_type", "") or context.get("channel", "")
            llm.session_id = session_id or context.get("session_id", "") or context.get("from_user_id", "")
            llm.user_id = context.get("from_user_id", "") or context.get("receiver", "")
            llm.user_label = context.get("actual_user_nickname", "") or context.get("from_user_nickname", "")
        elif session_id:
            llm.session_id = session_id

        system = (
            "你是 CowWechat 的本机功能说明助手。"
            "你只能依据用户问题和已缓存的本机 skill/功能摘要回答；不要重新扫描、不要假装执行工具。"
            "不要逐字照抄缓存清单，除非用户明确要求完整清单。"
            "如果用户是在问能做什么，按用户关心的方向做简洁归类；如果是在问某个功能怎么用，给可直接发送的说法或命令。"
            "如果当前摘要不足以可靠回答某个特定 Skill 的细节问题，只输出 [[READ_FULL_SKILL]]，不要猜。"
            "如果缓存里没有对应能力，直接说明没看到匹配的本机 skill，并建议用户描述具体目标让完整 Agent 处理。"
            "不要泄露密钥、token、私有配置、完整本地路径或内部实现细节。"
            "使用自然中文，控制在 8 行以内。"
        )
        if full_skill:
            system = (
                "你正在依据完整 SKILL.md 回答 CowWechat 本机 Skill 用法问题。"
                "只回答用户问到的部分，给可执行步骤或可直接发送的命令；不要输出完整原文。"
                "不要泄露密钥、token、私有配置、完整本地路径或内部实现细节。"
                "如果完整内容仍无法回答，直接说明没有在 Skill 中看到该信息。"
                "使用自然中文，控制在 10 行以内。"
            )
        user = (
            f"用户原话：{question}\n\n"
            f"{'完整 Skill 内容' if full_skill else '已缓存的本机 skill/功能摘要'}：\n{catalog_context}\n\n"
            "请结合用户原话回答，不要把上面的摘要原样贴回去。"
        )
        response = llm.call(
            LLMRequest(
                messages=[{"role": "user", "content": user}],
                system=system,
                max_tokens=800,
                temperature=0.2,
                tools=[],
                request_timeout=45,
                reasoning_effort="medium",
                reasoning_effort_locked=True,
                cache_shape_metadata={"request_kind": "cow_cli_skill_catalog_answer"},
            )
        )
        return self._extract_model_text(response)

    @staticmethod
    def _extract_model_text(response) -> str:
        if not isinstance(response, dict) or response.get("error"):
            return ""
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if text:
                            parts.append(str(text))
                    elif item:
                        parts.append(str(item))
                return "\n".join(parts).strip()
        content = response.get("content")
        return str(content or "").strip()

    _REMOTE_PAGE_SIZE = 10

    def _skill_list_remote(self, page: int = 1) -> str:
        import requests
        from cli.utils import SKILL_HUB_API, load_skills_config
        page_size = self._REMOTE_PAGE_SIZE
        try:
            resp = requests.get(
                f"{SKILL_HUB_API}/skills",
                params={"page": page, "limit": page_size},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            skills = data.get("skills", [])
            total = data.get("total", len(skills))
        except Exception as e:
            return f"获取技能广场失败: {e}"

        if not skills and page == 1:
            return "技能广场暂无可用技能"

        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        installed = set(load_skills_config().keys())

        lines = ["🌐 技能广场", ""]
        for s in skills:
            name = s.get("name", "")
            display = s.get("display_name", "") or name
            desc = s.get("description", "")
            if len(desc) > 50:
                desc = desc[:47] + "…"
            badge = " [已安装]" if name in installed else ""
            lines.append(f"📌 {display}{badge}")
            lines.append(f"   名称: {name}")
            if desc:
                lines.append(f"   {desc}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📄 第 {page}/{total_pages} 页")
        if page < total_pages:
            lines.append(f"💡 /skill list --remote --page {page + 1}  下一页")
        if page > 1:
            lines.append(f"💡 /skill list --remote --page {page - 1}  上一页")
        lines.append("💡 /skill install <名称>  安装技能")
        lines.append("💡 /skill search <关键词>  搜索技能")
        lines.append("🌐 https://skills.cowagent.ai  在线浏览全部技能")
        return "\n".join(lines)

    def _skill_search(self, query: str) -> str:
        if not query:
            return "请指定搜索关键词: /skill search <关键词>"

        import requests
        from cli.utils import SKILL_HUB_API, load_skills_config
        try:
            resp = requests.get(f"{SKILL_HUB_API}/skills/search", params={"q": query}, timeout=10)
            resp.raise_for_status()
            skills = resp.json().get("skills", [])
        except Exception as e:
            return f"搜索失败: {e}"

        if not skills:
            return f"未找到与「{query}」相关的技能"

        installed = set(load_skills_config().keys())
        lines = [f"🔍 搜索「{query}」({len(skills)} 个结果)", ""]
        for s in skills:
            name = s.get("name", "")
            display = s.get("display_name", "") or name
            desc = s.get("description", "")
            if len(desc) > 50:
                desc = desc[:47] + "…"
            badge = " [已安装]" if name in installed else ""
            lines.append(f"📌 {display}{badge}")
            lines.append(f"   名称: {name}")
            if desc:
                lines.append(f"   {desc}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 /skill install <名称>  安装技能")
        return "\n".join(lines)

    _INSTALL_TIMEOUT = 60

    def _skill_install(self, name: str, e_context: EventContext) -> str:
        if not name:
            return "请指定要安装的技能: /skill install <名称>"

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from cli.commands.skill import install_skill

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(install_skill, name)
                result = future.result(timeout=self._INSTALL_TIMEOUT)

            if result.error:
                return f"安装失败: {result.error}"

            if not result.installed:
                return "\n".join(result.messages) if result.messages else "未找到可安装的技能"

            self._invalidate_skill_catalog()
            return self._format_install_result(result)
        except FuturesTimeout:
            return "安装超时，请稍后重试或检查网络连接"
        except Exception as e:
            return f"安装失败: {e}"

    @staticmethod
    def _format_install_result(result) -> str:
        """Format InstallResult into a chat-friendly message."""
        from cli.commands.skill import _read_skill_description
        from cli.utils import get_skills_dir, load_skills_config
        skills_dir = get_skills_dir()
        config = load_skills_config()

        lines = []
        for skill_name in result.installed:
            desc = _read_skill_description(os.path.join(skills_dir, skill_name))
            display = config.get(skill_name, {}).get("display_name", "")
            lines.append(f"✅ 技能安装成功：{skill_name}")
            if display and display != skill_name:
                lines.append(f"   名称：{display}")
            if desc:
                lines.append(f"   描述：{desc}")

        if len(result.installed) > 1:
            lines.append(f"\n共安装 {len(result.installed)} 个技能")

        return "\n".join(lines)

    def _skill_uninstall(self, name: str) -> str:
        if not name:
            return "请指定要卸载的技能: /skill uninstall <名称>"

        import shutil
        import json
        from cli.utils import get_skills_dir

        skills_dir = get_skills_dir()
        skill_dir = os.path.join(skills_dir, name)

        if not os.path.exists(skill_dir):
            skill_dir = self._resolve_skill_dir(name, skills_dir)

        if not skill_dir:
            return f"技能 '{name}' 未安装"

        shutil.rmtree(skill_dir)

        config_path = os.path.join(skills_dir, "skills_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                config.pop(name, None)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
            except Exception:
                pass

        self._invalidate_skill_catalog()
        return f"✅ 技能 '{name}' 已卸载"

    @staticmethod
    def _resolve_skill_dir(name: str, skills_dir: str):
        """Find actual directory for a skill whose folder name may differ from its config name."""
        if not os.path.isdir(skills_dir):
            return None
        for entry in os.listdir(skills_dir):
            entry_path = os.path.join(skills_dir, entry)
            if not os.path.isdir(entry_path) or entry.startswith("."):
                continue
            if entry == name or entry.startswith(name + "-") or entry.endswith("-" + name):
                skill_md = os.path.join(entry_path, "SKILL.md")
                if os.path.exists(skill_md):
                    return entry_path
        return None

    @staticmethod
    def _strip_frontmatter(content: str):
        """Strip YAML frontmatter and return (metadata_dict, body)."""
        if not content.startswith("---"):
            return {}, content
        end = content.find("\n---", 3)
        if end == -1:
            return {}, content
        fm_text = content[3:end].strip()
        body = content[end + 4:].lstrip("\n")
        meta = {}
        for line in fm_text.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip().strip('"').strip("'")
        return meta, body

    def _skill_info(self, name: str) -> str:
        if not name:
            return "请指定技能名称: /skill info <名称>"

        from cli.utils import get_skills_dir, get_builtin_skills_dir

        skills_dir = get_skills_dir()
        builtin_dir = get_builtin_skills_dir()

        skill_dir = None
        source = None
        for d, src in [(skills_dir, "custom"), (builtin_dir, "builtin")]:
            candidate = os.path.join(d, name)
            if os.path.isdir(candidate):
                skill_dir = candidate
                source = src
                break

        if not skill_dir:
            resolved = self._resolve_skill_dir(name, skills_dir)
            if resolved:
                skill_dir = resolved
                source = "custom"

        if not skill_dir:
            return f"技能 '{name}' 未找到"

        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.exists(skill_md):
            return f"技能 '{name}' 没有 SKILL.md 文件"

        with open(skill_md, "r", encoding="utf-8") as f:
            content = f.read()

        meta, body = self._strip_frontmatter(content)

        header_lines = [f"📖 技能: {name} [{source}]", ""]
        desc = meta.get("description", "")
        if desc:
            header_lines.append(f"  {desc}")
            header_lines.append("")

        lines = body.split("\n")
        preview = "\n".join(lines[:30])
        result = "\n".join(header_lines) + preview
        if len(lines) > 30:
            result += f"\n\n... ({len(lines) - 30} more lines)"
        return result

    def _skill_usage(self, name: str) -> str:
        if not name:
            return "请指定技能名称: /skill usage <名称>"
        return self._skill_catalog().format_skill_usage(name)

    def _skill_set_enabled(self, name: str, enabled: bool) -> str:
        if not name:
            action = "启用" if enabled else "禁用"
            return f"请指定技能名称: /skill {'enable' if enabled else 'disable'} <名称>"

        import json
        from cli.utils import get_skills_dir

        skills_dir = get_skills_dir()
        config_path = os.path.join(skills_dir, "skills_config.json")

        if not os.path.exists(config_path):
            return "技能配置文件不存在"

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            return f"读取配置失败: {e}"

        if name not in config:
            return f"技能 '{name}' 未在配置中找到"

        config[name]["enabled"] = enabled
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)

        self._invalidate_skill_catalog()
        action = "启用" if enabled else "禁用"
        icon = "✅" if enabled else "⬚"
        return f"{icon} 技能 '{name}' 已{action}"

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------

    def _cmd_memory(self, args: str, e_context, session_id: str = "", **_) -> str:
        parts = args.strip().split()
        sub = parts[0].lower() if parts else ""

        if sub == "dream":
            days = 3
            if len(parts) > 1 and parts[1].isdigit():
                days = max(1, min(int(parts[1]), 30))
            return self._memory_dream(days, e_context, session_id)
        elif sub in ("rebuild-index", "rebuild_index", "rebuild"):
            return self._memory_rebuild_index(e_context, session_id)
        elif sub in ("status", "info", ""):
            if sub == "":
                return self._memory_help()
            return self._memory_status()
        else:
            return self._memory_help()

    @staticmethod
    def _memory_help() -> str:
        return (
            "🧠 记忆管理\n\n"
            "用法: /memory <子命令>\n\n"
            "子命令:\n"
            "  status              查看索引状态 (provider / model / dim / chunks)\n"
            "  rebuild-index       清空并重建向量索引 (切换 embedding 模型后必须执行)\n"
            "  dream [N]           手动触发记忆蒸馏 (整理近N天, 默认3, 最多30)"
        )

    def _memory_dream(self, days: int, e_context, session_id: str) -> str:
        session_id = self._get_session_id(e_context, fallback=session_id)
        agent = self._get_agent(session_id)

        flush_mgr = None
        if agent and agent.memory_manager:
            flush_mgr = agent.memory_manager.flush_manager

        if not flush_mgr:
            try:
                flush_mgr = self._create_standalone_flush_manager()
            except Exception as e:
                return f"⚠️ 无法初始化记忆蒸馏: {e}"

        if not flush_mgr.llm_model:
            return "⚠️ 未配置 LLM 模型，无法执行记忆蒸馏"

        # SaaS (e_context is None): run synchronously, return full result
        if e_context is None:
            return self._memory_dream_sync(flush_mgr, days)

        # Local channels: run in background, notify via channel.send()
        is_web = self._is_web_channel(e_context)

        def _run():
            try:
                result = flush_mgr.deep_dream(lookback_days=days, force=True)
                if result:
                    self._notify(e_context, self._build_dream_result(flush_mgr, is_web))
                else:
                    self._notify(e_context, "💤 记忆蒸馏跳过 — 没有新的记忆内容需要整理")
            except Exception as e:
                logger.warning(f"[CowCli] /memory dream failed: {e}")
                self._notify(e_context, f"❌ 记忆蒸馏失败: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return f"🌙 记忆蒸馏已启动 (整理近 {days} 天的记忆)\n\n整理在后台执行，完成后会通知你。"

    def _memory_dream_sync(self, flush_mgr, days: int) -> str:
        """Run deep dream synchronously and return the full result."""
        try:
            result = flush_mgr.deep_dream(lookback_days=days, force=True)
            if result:
                return self._build_dream_result(flush_mgr, is_web=True)
            return "💤 记忆蒸馏跳过 — 没有新的记忆内容需要整理"
        except Exception as e:
            logger.warning(f"[CowCli] /memory dream sync failed: {e}")
            return f"❌ 记忆蒸馏失败: {e}"

    def _memory_status(self) -> str:
        """Show current memory index status."""
        from agent.memory.embedding import detect_index_dim
        from config import conf

        agent = self._get_agent("")
        memory_manager = agent.memory_manager if agent else None

        lines = ["🧠 记忆索引状态", ""]
        if not memory_manager:
            lines.append("  ⚠️ Agent 尚未初始化，先发一条普通消息再试")
            return "\n".join(lines)

        stats = memory_manager.storage.get_stats()
        db_path = memory_manager.config.get_db_path()
        embedded = stats.get('embedded', 0)
        chunks = stats.get('chunks', 0)
        lines.append(f"  索引DB  : {db_path}")
        lines.append(f"  Files   : {stats.get('files', 0)}")
        lines.append(f"  Chunks  : {chunks} (embedded: {embedded})")
        lines.append("")

        # Active provider (from running config + provider instance).
        provider_obj = memory_manager.embedding_provider
        cfg_provider = (conf().get("embedding_provider") or "").strip().lower() or "(legacy)"
        if provider_obj is not None:
            cfg_model = getattr(provider_obj, "model", "?")
            cfg_dim = getattr(provider_obj, "_dimensions", None) or "?"
            lines.append(f"  Provider : {cfg_provider}")
            lines.append(f"  Model    : {cfg_model}")
            lines.append(f"  Dim      : {cfg_dim}")
        else:
            lines.append("  Provider : (未初始化, keyword-only)")

        # Health hints — only shown when the user has explicitly opted into
        # vector search via `embedding_provider`. Legacy users (no explicit
        # provider) are running in a "best-effort vectors" mode by design;
        # nagging them about missing/mismatched vectors would be noise.
        warnings = []
        explicitly_opted_in = (conf().get("embedding_provider") or "").strip() != ""
        if explicitly_opted_in and provider_obj is not None:
            if chunks > 0 and embedded < chunks:
                missing = chunks - embedded
                warnings.append(
                    f"  ⚠️ {missing}/{chunks} 个 chunk 没有向量；"
                    f"运行 /memory rebuild-index 后所有记忆才会被向量化检索"
                )

            index_dim = detect_index_dim(memory_manager.storage)
            cfg_dim = getattr(provider_obj, "_dimensions", None)
            if index_dim is not None and cfg_dim and index_dim != cfg_dim:
                warnings.append(
                    f"  ⚠️ 索引中存量向量为 {index_dim} 维，与当前配置 {cfg_dim} 维不一致；"
                    f"运行 /memory rebuild-index 重建后向量检索才会生效"
                )

        if warnings:
            lines.append("")
            lines.extend(warnings)

        return "\n".join(lines)

    def _memory_rebuild_index(self, e_context, session_id: str) -> str:
        """Rebuild the vector index using the current agent's memory_manager."""
        session_id = self._get_session_id(e_context, fallback=session_id)
        agent = self._get_agent(session_id)
        if not agent or not agent.memory_manager:
            return (
                "⚠️ Agent 尚未初始化，无法重建索引。\n"
                "请先发送一条普通消息触发 Agent 启动后再试。"
            )

        memory_manager = agent.memory_manager
        if memory_manager.embedding_provider is None:
            return (
                "⚠️ 当前没有可用的 embedding provider。\n"
                "请检查 config.json 中的 embedding 相关配置 (provider / api key)。"
            )

        provider_obj = memory_manager.embedding_provider
        model_label = getattr(provider_obj, "model", "?")
        dim_label = getattr(provider_obj, "dimensions", "?")

        # SaaS (e_context is None): run synchronously, return final result
        if e_context is None:
            return self._memory_rebuild_sync(memory_manager, model_label, dim_label)

        # Local channels: run in background, push progress + final result
        from agent.memory.embedding import rebuild_in_process

        def _run():
            try:
                result = rebuild_in_process(memory_manager)
                if result.ok:
                    self._notify(
                        e_context,
                        (
                            f"✅ 索引重建完成\n"
                            f"  cleared : {result.removed}\n"
                            f"  chunks  : {result.chunks}\n"
                            f"  files   : {result.files}"
                        ),
                    )
                else:
                    self._notify(e_context, f"❌ 索引重建失败: {result.error}")
            except Exception as e:
                logger.exception("[CowCli] /memory rebuild-index failed")
                self._notify(e_context, f"❌ 索引重建失败: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return (
            f"🔧 索引重建已启动 (model={model_label}, dim={dim_label})\n\n"
            f"将清空现有 chunks 并重新 embed 所有记忆文件，完成后会通知你。"
        )

    @staticmethod
    def _memory_rebuild_sync(memory_manager, model_label, dim_label) -> str:
        from agent.memory.embedding import rebuild_in_process

        try:
            result = rebuild_in_process(memory_manager)
        except Exception as e:
            logger.exception("[CowCli] /memory rebuild-index sync failed")
            return f"❌ 索引重建失败: {e}"

        if not result.ok:
            return f"❌ 索引重建失败: {result.error}"
        return (
            f"✅ 索引重建完成 (model={model_label}, dim={dim_label})\n"
            f"  cleared : {result.removed}\n"
            f"  chunks  : {result.chunks}\n"
            f"  files   : {result.files}"
        )

    @staticmethod
    def _notify(e_context, text: str):
        """Push a notification message back to the chat channel."""
        if e_context is None:
            logger.info(f"[CowCli] {text}")
            return
        try:
            channel = e_context["channel"]
            context = e_context["context"]
            if channel and context:
                channel.send(Reply(ReplyType.TEXT, text), context)
        except Exception as e:
            logger.warning(f"[CowCli] notify failed: {e}")

    @staticmethod
    def _is_web_channel(e_context) -> bool:
        if e_context is None:
            return False
        try:
            return e_context["context"].kwargs.get("channel_type") == "web"
        except Exception:
            return False

    @staticmethod
    def _build_dream_result(flush_mgr, is_web: bool) -> str:
        """Build dream completion message with diary content."""
        from datetime import datetime
        lines = ["✅ 记忆蒸馏完成"]

        # Read today's dream diary
        today = datetime.now().strftime("%Y-%m-%d")
        diary_file = flush_mgr.memory_dir / "dreams" / f"{today}.md"
        if diary_file.exists():
            diary = diary_file.read_text(encoding="utf-8").strip()
            # Strip the "# Dream Diary: ..." header line
            diary_lines = diary.split("\n")
            if diary_lines and diary_lines[0].startswith("# "):
                diary = "\n".join(diary_lines[1:]).strip()
            if diary:
                lines.append(f"\n{diary}")

        if is_web:
            lines.append("\n[MEMORY.md](/memory/MEMORY.md) | [梦境日记](/memory/dreams)")
        else:
            lines.append("\nMEMORY.md 已更新")

        return "\n".join(lines)

    @staticmethod
    def _create_standalone_flush_manager():
        """Create a MemoryFlushManager without a running agent (for pre-init dream)."""
        from pathlib import Path
        from config import conf
        from common.utils import expand_path
        from agent.memory.summarizer import MemoryFlushManager
        from bridge.bridge import Bridge
        from bridge.agent_bridge import AgentLLMModel

        workspace = Path(expand_path(conf().get("agent_workspace", "~/cow")))
        flush_mgr = MemoryFlushManager(workspace_dir=workspace)
        flush_mgr.llm_model = AgentLLMModel(Bridge())
        return flush_mgr

    # ------------------------------------------------------------------
    # knowledge
    # ------------------------------------------------------------------

    def _cmd_knowledge(self, args: str, e_context, **_) -> str:
        sub = args.strip().lower().split(None, 1)[0] if args.strip() else ""

        if sub == "on":
            return self._knowledge_toggle(True)
        elif sub == "off":
            return self._knowledge_toggle(False)
        elif sub in ("list", "tree"):
            return self._knowledge_tree()
        else:
            return self._knowledge_stats()

    def _knowledge_toggle(self, enabled: bool) -> str:
        from config import conf
        import json as _json

        conf()["knowledge"] = enabled

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(project_root, "config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = _json.load(f)
            file_config["knowledge"] = enabled
            with open(config_path, "w", encoding="utf-8") as f:
                _json.dump(file_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            return f"⚠️ 内存中已切换，但写入 config.json 失败: {e}"

        status = "开启 ✅" if enabled else "关闭 ❌"
        note = "知识库将在下次对话中生效" if enabled else "知识库系统已停用，不再注入提示词和索引知识文件"
        return f"📚 知识库已{status}\n\n{note}"

    def _knowledge_stats(self) -> str:
        from config import conf
        from common.utils import expand_path
        knowledge_dir = os.path.join(
            expand_path(conf().get("agent_workspace", "~/cow")),
            "knowledge"
        )
        if not os.path.isdir(knowledge_dir):
            return "📚 知识库目录不存在\n\n💡 开启知识库: /knowledge on"

        enabled = conf().get("knowledge", True)
        total_files = 0
        total_bytes = 0
        cat_count = {}

        for root, dirs, files in os.walk(knowledge_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            rel_root = os.path.relpath(root, knowledge_dir)
            category = rel_root.split(os.sep)[0] if rel_root != "." else "root"
            for f in files:
                if f.endswith(".md") and f not in ("index.md", "log.md"):
                    total_files += 1
                    total_bytes += os.path.getsize(os.path.join(root, f))
                    cat_count[category] = cat_count.get(category, 0) + 1

        status = "✅ 已开启" if enabled else "❌ 已关闭"
        lines = [
            "📚 知识库统计",
            "",
            f"状态: {status}",
            f"页面: {total_files} 篇",
            f"大小: {total_bytes / 1024:.1f} KB",
            "",
        ]
        if cat_count:
            for cat in sorted(cat_count.keys()):
                lines.append(f"- {cat}/ ({cat_count[cat]} pages)")
            lines.append("")

        lines.append(f"路径: {knowledge_dir}")
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "💡 /knowledge list    查看文件树",
            "💡 /knowledge on|off  开关知识库",
        ])
        return "\n".join(lines)

    def _knowledge_tree(self) -> str:
        from config import conf
        from common.utils import expand_path
        knowledge_dir = os.path.join(
            expand_path(conf().get("agent_workspace", "~/cow")),
            "knowledge"
        )
        if not os.path.isdir(knowledge_dir):
            return "📚 知识库目录不存在\n\n💡 开启知识库: /knowledge on"

        tree = ["knowledge/"]

        subdirs = sorted([
            d for d in os.listdir(knowledge_dir)
            if os.path.isdir(os.path.join(knowledge_dir, d)) and not d.startswith(".")
        ])

        for i, subdir in enumerate(subdirs):
            is_last_dir = (i == len(subdirs) - 1)
            branch = "└── " if is_last_dir else "├── "
            subdir_path = os.path.join(knowledge_dir, subdir)
            md_files = sorted([
                f for f in os.listdir(subdir_path)
                if f.endswith(".md") and not f.startswith(".")
            ])
            tree.append(f"{branch}{subdir}/ ({len(md_files)})")

            child_prefix = "    " if is_last_dir else "│   "
            max_show = 12
            for j, fname in enumerate(md_files[:max_show]):
                is_last_file = (j == len(md_files[:max_show]) - 1) and len(md_files) <= max_show
                fb = "└── " if is_last_file else "├── "
                name = fname.replace(".md", "")
                tree.append(f"{child_prefix}{fb}{name}")
            if len(md_files) > max_show:
                tree.append(f"{child_prefix}└── ... +{len(md_files) - max_show} more")

        if not subdirs:
            tree.append("(空)")

        return "```\n" + "\n".join(tree) + "\n```"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_session_id(self, e_context, fallback: str = "") -> str:
        if e_context is None:
            return fallback
        context = e_context["context"]
        return context.kwargs.get("session_id") or context.get("session_id", "")

    def _get_memory_user_id(self, e_context) -> str:
        if e_context is None:
            return ""
        try:
            context = e_context["context"]
        except Exception:
            return ""
        return str(context.get("memory_user_id", "") or "").strip()

    def _get_agent(self, session_id: str):
        try:
            from bridge.bridge import Bridge
            bridge = Bridge()
            if not bridge._agent_bridge:
                return None
            return bridge._agent_bridge.get_agent(session_id=session_id or None)
        except Exception:
            return None

    def get_help_text(self, **kwargs):
        return (
            "在对话中使用 /help 或 cow help 查看可用命令。\n"
            "普通用户可查看状态、账本、Skill/知识库说明和后端额度；"
            "后端切换、配置、日志、安装和启停类命令需要管理员权限。"
        )
