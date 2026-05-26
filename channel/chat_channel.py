import os
import re
import threading
import time
import uuid
from asyncio import CancelledError
from concurrent.futures import Future, ThreadPoolExecutor

from bridge.context import *
from bridge.reply import *
from channel.channel import Channel
from channel.image_recognition import get_image_recognition_manager
from common.agent_task_limits import resolve_agent_task_budget
from common.agent_task_runtime import SessionRuntime, TaskPolicy
from common.latency import elapsed, format_seconds, hash_id, monotonic
from common import memory
from plugins import *

try:
    from voice.audio_convert import any_to_wav
except Exception as e:
    pass

handler_pool = ThreadPoolExecutor(max_workers=8)  # 处理消息的线程池
control_pool = ThreadPoolExecutor(max_workers=4)  # 本地控制命令线程池
quick_reply_pool = ThreadPoolExecutor(max_workers=4)  # /q 快答线程池


# 抽象类, 它包含了与消息通道无关的通用处理逻辑
class ChatChannel(Channel):
    name = None  # 登录的用户名
    user_id = None  # 登录的用户id

    def __init__(self):
        super().__init__()
        # Instance-level attributes so each channel subclass has its own
        # independent session queue and lock. Previously these were class-level,
        # which caused contexts from one channel (e.g. Feishu) to be consumed
        # by another channel's consume() thread (e.g. Web), leading to errors
        # like "No request_id found in context".
        self.futures = {}
        self.sessions = {}
        self.lock = threading.Lock()
        _thread = threading.Thread(target=self.consume)
        _thread.setDaemon(True)
        _thread.start()

    def _get_or_create_runtime(self, session_id) -> SessionRuntime:
        with self.lock:
            runtime = self.sessions.get(session_id)
            if runtime is None:
                runtime = SessionRuntime(conf().get("concurrency_in_session", 1))
                self.sessions[session_id] = runtime
            return runtime

    @staticmethod
    def _is_control_command(content: str, command: str) -> bool:
        if content == command:
            return True
        return content.startswith(command + " ")

    @staticmethod
    def _single_chat_image_recognition_enabled() -> bool:
        return bool(conf().get("single_chat_image_recognition", True))

    @staticmethod
    def _background_image_recognition_enabled() -> bool:
        return bool(conf().get("background_image_recognition_enabled", True))

    def _register_image_recognition_context(self, context: Context) -> bool:
        if not context or context.type != ContextType.IMAGE:
            return False
        if not self._background_image_recognition_enabled():
            return False
        if not context.get("isgroup", False) and not self._single_chat_image_recognition_enabled():
            return False

        image_path = str(context.content or "").strip()
        if not image_path:
            return False

        try:
            from channel.image_recognition import get_image_recognition_manager

            msg = context.get("msg")
            session_id = str(context.get("session_id") or "").strip()
            sender_label = (
                str(context.get("group_sender_label") or "").strip()
                or str(getattr(msg, "actual_user_nickname", "") or "").strip()
                or str(getattr(msg, "from_user_nickname", "") or "").strip()
            )
            record = get_image_recognition_manager().register_image(
                session_id=session_id,
                channel_type=str(context.get("channel_type") or self.channel_type or ""),
                image_path=image_path,
                is_group=bool(context.get("isgroup", False)),
                msg_id=str(getattr(msg, "msg_id", "") or ""),
                sender_label=sender_label,
            )
            if not record:
                return False
            logger.info(
                "[ImageRecognition] registered image session=%s group=%s new_job=%s",
                hash_id(session_id),
                bool(context.get("isgroup", False)),
                bool(record.started_new_job),
            )
            if not context.get("isgroup", False):
                self._schedule_private_image_recognition_reply(context, record)
            return True
        except Exception as e:
            logger.warning("[ImageRecognition] failed to register image: %s", e, exc_info=True)
            return False

    def _schedule_private_image_recognition_reply(self, context: Context, record) -> None:
        try:
            from channel.image_recognition import get_image_recognition_manager

            manager = get_image_recognition_manager()
            def _send_result(done_record, delay: bool = True) -> None:
                if manager.is_auto_reply_suppressed(getattr(done_record, "record_id", "")):
                    return
                wait_seconds = 0.0
                if delay:
                    try:
                        configured_wait = conf().get("image_recognition_followup_wait_seconds", 6)
                        wait_seconds = 6.0 if configured_wait in (None, "") else float(configured_wait)
                    except (TypeError, ValueError):
                        wait_seconds = 6.0
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                if manager.is_auto_reply_suppressed(getattr(done_record, "record_id", "")):
                    return
                text = manager.public_reply_for(done_record, context=context)
                if text:
                    self._send_plain_text(context, text)

            cached = manager.get_record(record.record_id) or record
            if cached.status in {"done", "error"} and not manager.is_auto_reply_suppressed(record.record_id):
                control_pool.submit(_send_result, cached, False)
                return
            if not getattr(record, "started_new_job", False):
                return

            if not manager.add_done_callback(record, _send_result):
                latest = manager.get_record(record.record_id)
                if latest and latest.status in {"done", "error"}:
                    _send_result(latest, False)
        except Exception as e:
            logger.debug("[ImageRecognition] failed to schedule private reply: %s", e)

    def _append_image_recognition_context(self, session_id: str, content: str) -> str:
        if not self._background_image_recognition_enabled():
            return content
        if "[Recent image context]" in str(content or ""):
            return content
        try:
            from channel.image_recognition import get_image_recognition_manager

            manager = get_image_recognition_manager()
            if not manager.should_use_followup_context(session_id, content):
                return content
            extra = manager.build_followup_context(session_id, wait_seconds=0)
        except Exception as e:
            logger.debug("[ImageRecognition] failed to build followup context: %s", e)
            extra = ""
        if not extra:
            return content
        return f"{content.rstrip()}{extra}"

    @staticmethod
    def _private_image_recognition_prompt() -> str:
        default_prompt = (
            "请先识别这张图片，再结合当前短期对话上下文和可用的长期记忆来回答用户。"
            "不要只给图片说明；如果图片与已知偏好、任务、人物或正在讨论的事情相关，"
            "请把这些上下文一起用于回复。图中文字请提取关键内容；看不清时说明不确定之处。"
        )
        prompt = conf().get("single_chat_image_recognition_prompt", default_prompt)
        if not isinstance(prompt, str) or not prompt.strip():
            return default_prompt
        return prompt.strip()

    def _build_private_image_recognition_content(self, image_path: str) -> str:
        return f"{self._private_image_recognition_prompt()}\n[图片: {image_path}]"

    def _compose_private_image_recognition_context(self, context: Context):
        if context.get("isgroup", False) or not self._single_chat_image_recognition_enabled():
            return None

        image_path = str(context.content or "").strip()
        if not image_path:
            return None

        kwargs = dict(context.kwargs)
        kwargs["origin_ctype"] = ContextType.IMAGE
        return self._compose_context(
            ContextType.TEXT,
            self._build_private_image_recognition_content(image_path),
            **kwargs,
        )

    def _classify_fast_lane(self, context: Context, runtime: SessionRuntime):
        if context.type != ContextType.TEXT:
            return TaskPolicy.NORMAL, {}

        content = (context.content or "").strip()
        if self._is_control_command(content, "/状态"):
            return TaskPolicy.CONTROL_PROGRESS, {"include_eta_note": False}
        if self._is_control_command(content, "/取消"):
            return TaskPolicy.CONTROL_CANCEL, {}
        if self._is_control_command(content, "/跳过"):
            return TaskPolicy.CONTROL_SKIP, {}

        query = self._extract_quick_query(content)
        if query is None:
            return TaskPolicy.NORMAL, {}
        if not query:
            if runtime.has_running():
                return TaskPolicy.CONTROL_PROGRESS, {"include_eta_note": False}
            return TaskPolicy.QUICK_REPLY, {"query": "", "help": True}
        if self._is_progress_query(query):
            return TaskPolicy.CONTROL_PROGRESS, {
                "include_eta_note": self._is_eta_query(query),
            }
        if self._quick_reply_requires_normal_agent(query):
            return TaskPolicy.QUICK_REPLY, {"query": query, "refuse": True}
        return TaskPolicy.QUICK_REPLY, {"query": query}

    @staticmethod
    def _extract_quick_query(content: str):
        if content == "/q":
            return ""
        if content.startswith("/q "):
            return content[3:].strip()
        if content.startswith("/q\t"):
            return content[3:].strip()
        if content.startswith("/q:") or content.startswith("/q："):
            return content[3:].strip()
        if content.startswith("/q") and len(content) > 2:
            suffix = content[2:]
            if suffix[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/-":
                return suffix.strip(" \t:：")
        return None

    @staticmethod
    def _is_progress_query(query: str) -> bool:
        normalized = query.strip().lower()
        compact = re.sub(r"[\s?？!！。,.，、]+", "", normalized)
        if compact in {"status", "progress"}:
            return True
        return any(
            keyword in compact
            for keyword in (
                "进展",
                "状态",
                "到哪",
                "做到哪",
                "现在怎么样",
                "现在怎样",
                "怎么样了",
                "还要多久",
                "多久完成",
            )
        )

    @staticmethod
    def _is_eta_query(query: str) -> bool:
        compact = re.sub(r"\s+", "", query.lower())
        return any(keyword in compact for keyword in ("还要多久", "多久完成", "eta"))

    @staticmethod
    def _quick_reply_requires_normal_agent(query: str) -> bool:
        lowered = query.lower()
        patterns = (
            "查看", "读取", "修改", "编辑", "删除", "写入", "创建文件",
            "项目", "仓库", "代码库", "目录", "文件", "运行命令", "执行命令",
            "终端", "联网", "搜索", "查天气", "天气", "价格", "股价",
            "新闻", "最新", "生成图片", "画图", "图片", "继续", "刚才",
            "上面", "当前任务", "长任务", "上下文", "总结当前",
            "read file", "write file", "edit file", "delete file", "project",
            "repo", "run command", "shell", "terminal", "web search", "search",
            "weather", "price", "stock", "news", "latest", "generate image",
            "continue", "context",
        )
        return any(pattern in lowered for pattern in patterns)

    def _send_plain_text(
        self,
        context: Context,
        text: str,
        track_visible: bool = True,
        visible_source: str = "send",
    ):
        reply = Reply(ReplyType.TEXT, text)
        reply = self._decorate_reply(context, reply)
        sent = self._send_reply(context, reply)
        runtime = context.get("_session_runtime") if context else None
        if sent is not False and track_visible and runtime and hasattr(runtime, "mark_visible_output"):
            runtime.mark_visible_output(visible_source)
        return sent

    def _handle_control_progress(self, context: Context, runtime: SessionRuntime, include_eta_note: bool = False):
        self._send_plain_text(
            context,
            runtime.status_text(include_eta_note=include_eta_note),
            track_visible=False,
        )
        if hasattr(runtime, "mark_visible_output"):
            runtime.mark_visible_output("control_progress")

    def _handle_control_cancel(self, context: Context, runtime: SessionRuntime):
        running_cancelled = runtime.cancel_running()
        pending_count = runtime.queue.qsize()
        self.cancel_session(context.get("session_id"), clear_pending=False)
        if running_cancelled:
            text = "已收到取消请求，当前任务会在下一次可取消点停止。"
        else:
            text = "当前没有运行中的任务。"
        if pending_count:
            text += f"\n队列中还有 {pending_count} 条消息，当前任务停止后会继续处理；如需清空队列请发送 /跳过。"
        self._send_plain_text(context, text)

    def _handle_control_skip(self, context: Context, runtime: SessionRuntime):
        pending_cleared = runtime.clear_pending()
        text = f"已清空排队消息 {pending_cleared} 条。" if pending_cleared else "当前没有排队消息。"
        self._send_plain_text(context, text)

    def _handle_quick_reply(self, context: Context, query: str, help: bool = False, refuse: bool = False):
        if help:
            self._send_plain_text(
                context,
                "/q 用法：\n"
                "/q 进展：查看当前长任务进展。\n"
                "/q 只回复 pong：快速单轮回复，不读取普通 Agent 历史。\n"
                "需要工具、文件、联网或当前上下文的请求，请直接发送普通消息。",
            )
            return
        if refuse:
            self._send_plain_text(
                context,
                "这个请求需要普通 Agent 或当前任务上下文处理，请去掉 /q 后重新发送。",
            )
            return

        quick_start = monotonic()
        generate_elapsed = None
        send_elapsed = None
        quick_session_id = f"quick-{hash_id(context.get('session_id'))}-{uuid.uuid4().hex[:8]}"
        quick_context = Context(ContextType.TEXT, query)
        quick_context.kwargs = dict(context.kwargs)
        quick_context["session_id"] = quick_session_id
        quick_context["conversation_id"] = quick_session_id
        quick_context["quick_reply"] = True

        try:
            from bridge.bridge import Bridge

            bridge = Bridge()
            generate_start = monotonic()
            reply = bridge.fetch_reply_content(query, quick_context)
            generate_elapsed = elapsed(generate_start)
            if not reply or not reply.content:
                reply = Reply(ReplyType.ERROR, "快答暂时没有生成回复。")

            send_start = monotonic()
            reply = self._decorate_reply(context, reply)
            self._send_reply(context, reply)
            send_elapsed = elapsed(send_start)

            try:
                bot = bridge.get_bot("chat")
                if hasattr(bot, "sessions"):
                    bot.sessions.clear_session(quick_session_id)
            except Exception as e:
                logger.debug(f"[FastLane] Failed to clear quick session: {e}")
        except Exception as e:
            logger.error(f"[FastLane] quick reply failed: {e}", exc_info=True)
            self._send_plain_text(context, "快答失败，请去掉 /q 后重新发送。")
        finally:
            logger.info(
                "[Latency][Quick] session=%s total=%s generate=%s send=%s query_chars=%s",
                hash_id(context.get("session_id")),
                format_seconds(elapsed(quick_start)),
                format_seconds(generate_elapsed),
                format_seconds(send_elapsed),
                len(query or ""),
            )

    # 根据消息构造context，消息内容相关的触发项写在这里
    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype
        # context首次传入时，receiver是None，根据类型设置receiver
        first_in = "receiver" not in context
        # 群名匹配过程，设置session_id和receiver
        if first_in:  # context首次传入时，receiver是None，根据类型设置receiver
            config = conf()
            cmsg = context["msg"]
            user_data = conf().get_user_data(cmsg.from_user_id)
            context["openai_api_key"] = user_data.get("openai_api_key")
            context["gpt_model"] = user_data.get("gpt_model")
            if context.get("isgroup", False):
                group_name = cmsg.other_user_nickname
                group_id = cmsg.other_user_id

                group_name_white_list = config.get("group_name_white_list", [])
                group_name_keyword_white_list = config.get("group_name_keyword_white_list", [])
                if any(
                    [
                        group_name in group_name_white_list,
                        "ALL_GROUP" in group_name_white_list,
                        check_contain(group_name, group_name_keyword_white_list),
                    ]
                ):
                    # Check global group_shared_session config first
                    group_shared_session = conf().get("group_shared_session", True)
                    if group_shared_session:
                        # All users in the group share the same session
                        session_id = group_id
                    else:
                        # Check group-specific whitelist (legacy behavior)
                        group_chat_in_one_session = conf().get("group_chat_in_one_session", [])
                        session_id = cmsg.actual_user_id
                        if any(
                            [
                                group_name in group_chat_in_one_session,
                                "ALL_GROUP" in group_chat_in_one_session,
                            ]
                        ):
                            session_id = group_id
                else:
                    logger.debug(f"No need reply, groupName not in whitelist, group_name={group_name}")
                    return None
                context["session_id"] = session_id
                context["receiver"] = group_id
            else:
                context["session_id"] = cmsg.other_user_id
                context["receiver"] = cmsg.other_user_id
            e_context = PluginManager().emit_event(EventContext(Event.ON_RECEIVE_MESSAGE, {"channel": self, "context": context}))
            context = e_context["context"]
            if e_context.is_pass() or context is None:
                return context
            if cmsg.from_user_id == self.user_id and not config.get("trigger_by_self", True):
                logger.debug("[chat_channel]self message skipped")
                return None

        # 消息内容匹配过程，并处理content
        if ctype == ContextType.TEXT:
            if first_in and "」\n- - - - - - -" in content:  # 初次匹配 过滤引用消息
                logger.debug(content)
                logger.debug("[chat_channel]reference query skipped")
                return None

            nick_name_black_list = conf().get("nick_name_black_list", [])
            if context.get("isgroup", False):  # 群聊
                # 校验关键字
                match_prefix = check_prefix(content, conf().get("group_chat_prefix"))
                match_contain = check_contain(content, conf().get("group_chat_keyword"))
                flag = False
                if context["msg"].to_user_id != context["msg"].actual_user_id:
                    if match_prefix is not None or match_contain is not None:
                        flag = True
                        if match_prefix:
                            content = content.replace(match_prefix, "", 1).strip()
                    if context["msg"].is_at:
                        nick_name = context["msg"].actual_user_nickname
                        if nick_name and nick_name in nick_name_black_list:
                            # 黑名单过滤
                            logger.warning(f"[chat_channel] Nickname {nick_name} in In BlackList, ignore")
                            return None

                        logger.info("[chat_channel]receive group at")
                        if not conf().get("group_at_off", False):
                            flag = True
                        self.name = self.name if self.name is not None else ""  # 部分渠道self.name可能没有赋值
                        pattern = f"@{re.escape(self.name)}(\u2005|\u0020)"
                        subtract_res = re.sub(pattern, r"", content)
                        if isinstance(context["msg"].at_list, list):
                            for at in context["msg"].at_list:
                                pattern = f"@{re.escape(at)}(\u2005|\u0020)"
                                subtract_res = re.sub(pattern, r"", subtract_res)
                        if subtract_res == content and context["msg"].self_display_name:
                            # 前缀移除后没有变化，使用群昵称再次移除
                            pattern = f"@{re.escape(context['msg'].self_display_name)}(\u2005|\u0020)"
                            subtract_res = re.sub(pattern, r"", content)
                        content = subtract_res
                if not flag:
                    if context["origin_ctype"] == ContextType.VOICE:
                        logger.info("[chat_channel]receive group voice, but checkprefix didn't match")
                    return None
            else:  # 单聊
                nick_name = context["msg"].from_user_nickname
                if nick_name and nick_name in nick_name_black_list:
                    # 黑名单过滤
                    logger.warning(f"[chat_channel] Nickname '{nick_name}' in In BlackList, ignore")
                    return None

                match_prefix = check_prefix(content, conf().get("single_chat_prefix", [""]))
                if match_prefix is not None:  # 判断如果匹配到自定义前缀，则返回过滤掉前缀+空格后的内容
                    content = content.replace(match_prefix, "", 1).strip()
                elif context["origin_ctype"] in (ContextType.VOICE, ContextType.IMAGE):  # 如果源消息是私聊的语音或图片消息，允许不匹配前缀，放宽条件
                    pass
                else:
                    logger.info("[chat_channel]receive single chat msg, but checkprefix didn't match")
                    return None
            content = content.strip()
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix",[""]))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            if context.type == ContextType.TEXT:
                context.content = self._append_image_recognition_context(
                    context.get("session_id", ""),
                    context.content,
                )
            if "desire_rtype" not in context and conf().get("always_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        elif context.type == ContextType.VOICE:
            if "desire_rtype" not in context and conf().get("voice_reply_voice") and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                context["desire_rtype"] = ReplyType.VOICE
        return context

    def _handle(self, context: Context):
        if context is None or not context.content:
            return
        handle_start = monotonic()
        context["_latency_handle_start_at"] = handle_start
        generate_elapsed = None
        decorate_elapsed = None
        send_elapsed = None
        reply = None
        runtime = context.get("_session_runtime")
        final_phase = "done"
        logger.debug("[chat_channel] handling context: {}".format(context))
        try:
            # reply的构建步骤
            generate_start = monotonic()
            reply = self._generate_reply(context)
            generate_elapsed = elapsed(generate_start)

            logger.debug("[chat_channel] decorating reply: {}".format(reply))

            # reply的包装步骤
            if reply and reply.content:
                decorate_start = monotonic()
                reply = self._decorate_reply(context, reply)
                decorate_elapsed = elapsed(decorate_start)

                # reply的发送步骤
                send_start = monotonic()
                sent = self._send_reply(context, reply)
                send_elapsed = elapsed(send_start)
                if sent is not False and runtime and hasattr(runtime, "mark_visible_output"):
                    runtime.mark_visible_output("final_reply")
                    if reply.type == ReplyType.TEXT:
                        self._send_completion_notice_if_needed(context, runtime)
                if sent is not False and reply.type == ReplyType.TEXT:
                    self._remember_cow_cli_followup_context(context, reply)
        except Exception as e:
            final_phase = "error"
            if runtime:
                runtime.update_progress("error", {"error": str(e)})
            raise
        finally:
            if runtime:
                runtime.finish_task(final_phase)
            self._schedule_post_task_self_evolution(context)
            content = getattr(reply, "content", "") if reply else ""
            logger.info(
                "[Latency][Handle] session=%s total=%s queue_wait=%s generate=%s decorate=%s send=%s "
                "ctype=%s reply_type=%s reply_chars=%s",
                hash_id(context.get("session_id")),
                format_seconds(elapsed(handle_start)),
                format_seconds(elapsed(context.get("_latency_enqueued_at"), handle_start)),
                format_seconds(generate_elapsed),
                format_seconds(decorate_elapsed),
                format_seconds(send_elapsed),
                context.type,
                getattr(reply, "type", "none") if reply else "none",
                len(content) if isinstance(content, str) else 0,
            )

    @staticmethod
    def _remember_cow_cli_followup_context(context: Context, reply: Reply) -> None:
        payload = context.get("_cow_cli_followup_context") if context else None
        if not isinstance(payload, dict):
            return
        user_text = str(payload.get("user_text") or context.content or "").strip()
        assistant_text = str(payload.get("assistant_text") or reply.content or "").strip()
        if not user_text or not assistant_text:
            return
        try:
            from bridge.bridge import Bridge

            Bridge().get_agent_bridge().remember_external_visible_reply(
                context=context,
                user_text=user_text,
                assistant_text=assistant_text,
                source=str(payload.get("source") or "cow_cli"),
            )
        except Exception as e:
            logger.warning(f"[CowCli] Failed to remember direct reply for follow-up: {e}")

    @staticmethod
    def _schedule_post_task_self_evolution(context: Context):
        if not context:
            return
        payload = context.kwargs.pop("_self_evolution_post_task", None)
        if not payload:
            return
        try:
            from common.self_evolution import schedule_post_task_reflection

            schedule_post_task_reflection(**payload)
        except Exception as e:
            logger.debug(f"[SelfEvolution] Failed to schedule post-task reflection: {e}")

    def _generate_reply(self, context: Context, reply: Reply = Reply()) -> Reply:
        e_context = PluginManager().emit_event(
            EventContext(
                Event.ON_HANDLE_CONTEXT,
                {"channel": self, "context": context, "reply": reply},
            )
        )
        reply = e_context["reply"]
        if not e_context.is_pass():
            logger.debug("[chat_channel] type={}, content={}".format(context.type, context.content))
            if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:  # 文字和图片消息
                context["channel"] = e_context["channel"]
                reply = super().build_reply_content(context.content, context)
            elif context.type == ContextType.VOICE:  # 语音消息
                cmsg = context["msg"]
                cmsg.prepare()
                file_path = context.content
                wav_path = os.path.splitext(file_path)[0] + ".wav"
                try:
                    any_to_wav(file_path, wav_path)
                except Exception as e:  # 转换失败，直接使用mp3，对于某些api，mp3也可以识别
                    logger.warning("[chat_channel]any to wav error, use raw path. " + str(e))
                    wav_path = file_path
                # 语音识别
                reply = super().build_voice_to_text(wav_path)
                # 删除临时文件
                try:
                    os.remove(file_path)
                    if wav_path != file_path:
                        os.remove(wav_path)
                except Exception as e:
                    pass
                    # logger.warning("[chat_channel]delete temp file error: " + str(e))

                if reply.type == ReplyType.TEXT:
                    new_context = self._compose_context(ContextType.TEXT, reply.content, **context.kwargs)
                    if new_context:
                        reply = self._generate_reply(new_context)
                    else:
                        return
            elif context.type == ContextType.IMAGE:  # 图片消息
                if self._register_image_recognition_context(context):
                    return
                auto_context = self._compose_private_image_recognition_context(context)
                if auto_context:
                    logger.info(
                        "[chat_channel]auto private image recognition, session=%s",
                        hash_id(auto_context.get("session_id")),
                    )
                    return self._generate_reply(auto_context)
                memory.USER_IMAGE_CACHE[context["session_id"]] = {
                    "path": context.content,
                    "msg": context.get("msg")
                }
            elif context.type == ContextType.SHARING:  # 分享信息，当前无默认逻辑
                pass
            elif context.type == ContextType.FUNCTION or context.type == ContextType.FILE:  # 文件消息及函数调用等，当前无默认逻辑
                pass
            else:
                logger.warning("[chat_channel] unknown context type: {}".format(context.type))
                return
        return reply

    def _decorate_reply(self, context: Context, reply: Reply) -> Reply:
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_DECORATE_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            desire_rtype = context.get("desire_rtype")
            if not e_context.is_pass() and reply and reply.type:
                if reply.type in self.NOT_SUPPORT_REPLYTYPE:
                    logger.error("[chat_channel]reply type not support: " + str(reply.type))
                    reply.type = ReplyType.ERROR
                    reply.content = "不支持发送的消息类型: " + str(reply.type)

                if reply.type == ReplyType.TEXT:
                    reply_text = reply.content
                    if desire_rtype == ReplyType.VOICE and ReplyType.VOICE not in self.NOT_SUPPORT_REPLYTYPE:
                        reply = super().build_text_to_voice(reply.content)
                        return self._decorate_reply(context, reply)
                    if context.get("isgroup", False):
                        if not context.get("no_need_at", False):
                            reply_text = "@" + context["msg"].actual_user_nickname + "\n" + reply_text.strip()
                        reply_text = conf().get("group_chat_reply_prefix", "") + reply_text + conf().get("group_chat_reply_suffix", "")
                    else:
                        reply_text = conf().get("single_chat_reply_prefix", "") + reply_text + conf().get("single_chat_reply_suffix", "")
                    reply.content = reply_text
                elif reply.type == ReplyType.ERROR or reply.type == ReplyType.INFO:
                    reply.content = "[" + str(reply.type) + "]\n" + reply.content
                elif reply.type == ReplyType.IMAGE_URL or reply.type == ReplyType.VOICE or reply.type == ReplyType.IMAGE or reply.type == ReplyType.FILE or reply.type == ReplyType.VIDEO or reply.type == ReplyType.VIDEO_URL:
                    pass
                else:
                    logger.error("[chat_channel] unknown reply type: {}".format(reply.type))
                    return
            if desire_rtype and desire_rtype != reply.type and reply.type not in [ReplyType.ERROR, ReplyType.INFO]:
                logger.warning("[chat_channel] desire_rtype: {}, but reply type: {}".format(context.get("desire_rtype"), reply.type))
            return reply

    def _send_reply(self, context: Context, reply: Reply):
        if reply and reply.type:
            e_context = PluginManager().emit_event(
                EventContext(
                    Event.ON_SEND_REPLY,
                    {"channel": self, "context": context, "reply": reply},
                )
            )
            reply = e_context["reply"]
            if not e_context.is_pass() and reply and reply.type:
                logger.debug("[chat_channel] sending reply: {}, context: {}".format(reply, context))
                
                # 如果是文本回复，尝试提取并发送图片
                # Web channel renders images/videos inline via renderMarkdown,
                # so skip the extract-and-send step to avoid duplicate media.
                if reply.type == ReplyType.TEXT and context.get("channel_type") != "web":
                    return self._extract_and_send_images(reply, context)
                elif reply.type == ReplyType.TEXT:
                    return self._send(reply, context)
                # 如果是图片回复但带有文本内容，先发文本再发图片
                elif reply.type == ReplyType.IMAGE_URL and hasattr(reply, 'text_content') and reply.text_content:
                    # 先发送文本
                    text_reply = Reply(ReplyType.TEXT, reply.text_content)
                    text_sent = self._send(text_reply, context)
                    # 短暂延迟后发送图片
                    time.sleep(0.3)
                    media_sent = self._send(reply, context)
                    return text_sent is not False and media_sent is not False
                else:
                    return self._send(reply, context)
        return False
    
    @staticmethod
    def _clean_media_reference(value) -> str:
        if isinstance(value, tuple):
            value = next((part for part in value if part), "")
        return str(value or "").strip().strip("'\"")

    @staticmethod
    def _is_remote_media_reference(url: str) -> bool:
        return str(url or "").lower().startswith(("http://", "https://"))

    @classmethod
    def _should_auto_send_extracted_media(cls, url: str, source: str) -> bool:
        if not cls._is_remote_media_reference(url):
            return True
        return source == "explicit"

    def _extract_and_send_images(self, reply: Reply, context: Context):
        """
        从文本回复中提取图片/视频URL并单独发送。
        远程 Markdown/裸 URL 常来自酒店、OTA、搜索结果等正文资料，保留在文本中，
        不自动拆成图片消息，避免企业微信下载失败后暴露 image failed。
        """
        content = reply.content or ""
        media_items = []  # [(url, type, source), ...]
        skipped_remote_items = 0

        patterns = [
            (r'\[图片:\s*([^\]]+)\]', 'image', 'explicit'),
            (r'\[视频:\s*([^\]]+)\]', 'video', 'explicit'),
            (r'!\[.*?\]\(([^\)]+)\)', 'image', 'embedded'),
            (r'<img[^>]+src=["\']([^"\']+)["\']', 'image', 'embedded'),
            (r'<video[^>]+src=["\']([^"\']+)["\']', 'video', 'embedded'),
            (r'https?://[^\s<>\)]+\.(?:jpg|jpeg|png|gif|webp)(?:\?[^\s<>\)]*)?', 'image', 'direct'),
            (r'https?://[^\s<>\)]+\.(?:mp4|avi|mov|wmv|flv)(?:\?[^\s<>\)]*)?', 'video', 'direct'),
        ]

        for pattern, media_type, source in patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                url = self._clean_media_reference(match)
                if not url:
                    continue
                if not self._should_auto_send_extracted_media(url, source):
                    skipped_remote_items += 1
                    continue
                media_items.append((url, media_type, source))

        seen = set()
        unique_items = []
        for url, mtype, source in media_items:
            if url not in seen:
                seen.add(url)
                unique_items.append((url, mtype, source))
        media_items = unique_items[:5]

        if skipped_remote_items:
            logger.info(
                "[chat_channel] Skipped %d embedded/direct remote media URL(s); leaving them in text",
                skipped_remote_items,
            )

        if not media_items:
            return self._send(reply, context)

        logger.info(f"[chat_channel] Extracted {len(media_items)} media item(s) from reply")
        logger.info(f"[chat_channel] Sending text content before media: {reply.content[:100]}...")
        text_sent = self._send(reply, context)
        logger.info(f"[chat_channel] Text sent, now sending {len(media_items)} media item(s)")

        for i, (url, media_type, _source) in enumerate(media_items):
            try:
                if url.startswith(('http://', 'https://')):
                    if media_type == 'video':
                        media_reply = Reply(ReplyType.FILE, url)
                        media_reply.file_name = os.path.basename(url)
                    else:
                        media_reply = Reply(ReplyType.IMAGE_URL, url)
                elif os.path.exists(url):
                    if media_type == 'video':
                        media_reply = Reply(ReplyType.FILE, f"file://{url}")
                        media_reply.file_name = os.path.basename(url)
                    else:
                        media_reply = Reply(ReplyType.IMAGE_URL, f"file://{url}")
                else:
                    logger.warning(f"[chat_channel] Media file not found or invalid URL: {url}")
                    continue

                if i > 0:
                    time.sleep(0.5)
                media_sent = self._send(media_reply, context)
                if media_sent is not False:
                    logger.info(f"[chat_channel] Sent {media_type} {i+1}/{len(media_items)}: {url[:50]}...")
                else:
                    logger.warning(
                        f"[chat_channel] Failed to send extracted {media_type} {i+1}/{len(media_items)}: {url[:50]}..."
                    )

            except Exception as e:
                logger.error(f"[chat_channel] Failed to send {media_type} {url}: {e}")
        return text_sent

    def _send(self, reply: Reply, context: Context, retry_cnt=0):
        try:
            sent = self.send(reply, context)
            if sent is False:
                logger.warning(
                    "[chat_channel] send returned false for session=%s reply_type=%s",
                    context.get("session_id", ""),
                    reply.type,
                )
            return sent is not False
        except Exception as e:
            logger.error("[chat_channel] sendMsg error: {}".format(str(e)))
            if isinstance(e, NotImplementedError):
                return False
            logger.exception(e)
            if retry_cnt < 2:
                time.sleep(3 + 3 * retry_cnt)
                return self._send(reply, context, retry_cnt + 1)
            return False

    def _success_callback(self, session_id, **kwargs):  # 线程正常结束时的回调函数
        logger.debug("Worker return success, session_id = {}".format(session_id))

    def _fail_callback(self, session_id, exception, **kwargs):  # 线程异常结束时的回调函数
        logger.exception("Worker return exception: {}".format(exception))

    def _thread_pool_callback(self, session_id, **kwargs):
        def func(worker: Future):
            try:
                worker_exception = worker.exception()
                if worker_exception:
                    self._fail_callback(session_id, exception=worker_exception, **kwargs)
                else:
                    self._success_callback(session_id, **kwargs)
            except CancelledError as e:
                logger.info("Worker cancelled, session_id = {}".format(session_id))
            except Exception as e:
                logger.exception("Worker raise exception: {}".format(e))
            with self.lock:
                runtime = self.sessions.get(session_id)
                if runtime:
                    runtime.semaphore.release()

        return func

    @staticmethod
    def _long_task_expectation_enabled() -> bool:
        return bool(conf().get("long_task_expectation_enabled", True))

    @staticmethod
    def _long_task_notice_seconds():
        first = _safe_float(conf().get("long_task_silence_first_notice_seconds", 10), 10.0)
        second = _safe_float(conf().get("long_task_silence_second_notice_seconds", 45), 45.0)
        repeat = _safe_float(conf().get("long_task_silence_repeat_notice_seconds", 90), 90.0)
        return max(0.0, first), max(0.0, second), max(0.0, repeat)

    @staticmethod
    def _long_task_completion_notice_enabled() -> bool:
        return bool(conf().get("long_task_completion_notice_enabled", True))

    @staticmethod
    def _long_task_completion_notice_min_turns() -> int:
        try:
            value = int(conf().get("long_task_completion_notice_min_turns", 10) or 10)
        except (TypeError, ValueError):
            value = 10
        return max(1, value)

    @staticmethod
    def _long_task_completion_notice_min_silence_notices() -> int:
        try:
            value = int(conf().get("long_task_completion_notice_min_silence_notices", 2) or 2)
        except (TypeError, ValueError):
            value = 2
        return max(1, value)

    def _send_silence_notice(self, context: Context, notice: str):
        return self._send_plain_text(
            context,
            notice,
            True,
            "silence_notice",
        )

    def _start_silence_watchdog(self, context: Context, runtime: SessionRuntime, token):
        if not self._long_task_expectation_enabled():
            return

        first_notice_seconds, second_notice_seconds, repeat_notice_seconds = self._long_task_notice_seconds()

        def _watch():
            while runtime.has_running() and not token.is_cancelled():
                notice = runtime.claim_silence_notice(
                    first_notice_seconds=first_notice_seconds,
                    second_notice_seconds=second_notice_seconds,
                    repeat_notice_seconds=repeat_notice_seconds,
                )
                if notice:
                    control_pool.submit(self._send_silence_notice, context, notice)
                time.sleep(1.0)

        thread = threading.Thread(
            target=_watch,
            daemon=True,
            name=f"long-task-watchdog-{hash_id(context.get('session_id'))}",
        )
        thread.start()

    def _send_completion_notice_if_needed(self, context: Context, runtime: SessionRuntime):
        if not self._long_task_completion_notice_enabled():
            return
        min_turns = self._long_task_completion_notice_min_turns()
        min_silence_notices = self._long_task_completion_notice_min_silence_notices()
        if not runtime.should_send_completion_notice(min_turns, min_silence_notices):
            return
        self._send_plain_text(
            context,
            runtime.completion_notice_text(),
            True,
            "completion_notice",
        )

    def produce(self, context: Context):
        if context.type == ContextType.IMAGE and self._register_image_recognition_context(context):
            logger.info(
                "[Latency][Queue] bypass image recognition session=%s",
                hash_id(context.get("session_id", "")),
            )
            return

        session_id = context["session_id"]
        enqueued_at = monotonic()
        context["_latency_enqueued_at"] = enqueued_at
        if "_latency_received_at" not in context:
            context["_latency_received_at"] = enqueued_at
        runtime = self._get_or_create_runtime(session_id)
        if context.type == ContextType.TEXT:
            try:
                content = context.get("_visible_task_summary") or context.content
                if get_image_recognition_manager().handle_text(self, context, content):
                    logger.info("[ImageRecognition] handled image follow-up session=%s", hash_id(session_id))
                    return
            except Exception as e:
                logger.debug("[ImageRecognition] follow-up routing skipped: %s", e)
        policy, payload = self._classify_fast_lane(context, runtime)

        if policy == TaskPolicy.CONTROL_PROGRESS:
            control_pool.submit(
                self._handle_control_progress,
                context,
                runtime,
                payload.get("include_eta_note", False),
            )
            logger.info("[Latency][FastLane] control=progress session=%s", hash_id(session_id))
            return
        if policy == TaskPolicy.CONTROL_CANCEL:
            control_pool.submit(self._handle_control_cancel, context, runtime)
            logger.info("[Latency][FastLane] control=cancel session=%s", hash_id(session_id))
            return
        if policy == TaskPolicy.CONTROL_SKIP:
            control_pool.submit(self._handle_control_skip, context, runtime)
            logger.info("[Latency][FastLane] control=skip session=%s", hash_id(session_id))
            return
        if policy == TaskPolicy.QUICK_REPLY:
            quick_reply_pool.submit(self._handle_quick_reply, context, **payload)
            logger.info("[Latency][FastLane] control=quick session=%s", hash_id(session_id))
            return

        with self.lock:
            if self.sessions.get(session_id) is not runtime:
                self.sessions[session_id] = runtime
            pending_before = runtime.queue.qsize()
            context["_latency_queue_pending_before"] = pending_before
            priority = context.type == ContextType.TEXT and context.content.startswith("#")
            if priority:
                runtime.queue.putleft(context)  # 优先处理管理命令
            else:
                runtime.queue.put(context)
            logger.info(
                "[Latency][Queue] enqueue session=%s ctype=%s priority=%s pending_before=%s",
                hash_id(session_id),
                context.type,
                priority,
                pending_before,
            )

        if runtime.should_send_queue_notice():
            control_pool.submit(
                self._send_plain_text,
                context,
                "上一条还在处理，已排队。发送 /状态 查看进展，/取消 取消当前任务，/q 可快速问一句。",
            )

    # 消费者函数，单独线程，用于从消息队列中取出消息并处理
    def consume(self):
        while True:
            with self.lock:
                session_ids = list(self.sessions.keys())
            for session_id in session_ids:
                with self.lock:
                    runtime = self.sessions.get(session_id)
                if not runtime:
                    continue
                if runtime.semaphore.acquire(blocking=False):  # 等线程处理完毕才能删除
                    if not runtime.queue.empty():
                        context = runtime.queue.get()
                        dequeued_at = monotonic()
                        context["_latency_dequeued_at"] = dequeued_at
                        task_budget = resolve_agent_task_budget(context.content, conf())
                        context["_agent_max_steps"] = task_budget.max_steps
                        context["_agent_task_budget_kind"] = task_budget.kind
                        task_summary = context.get("_visible_task_summary") or context.content
                        token = runtime.start_task(
                            task_summary,
                            max_turns=task_budget.max_steps,
                        )
                        context["_session_runtime"] = runtime
                        context["_cancellation_token"] = token
                        self._start_silence_watchdog(context, runtime, token)
                        logger.info(
                            "[Latency][Queue] dequeue session=%s queue_wait=%s pending_after=%s",
                            hash_id(session_id),
                            format_seconds(elapsed(context.get("_latency_enqueued_at"), dequeued_at)),
                            runtime.queue.qsize(),
                        )
                        logger.debug("[chat_channel] consume context: {}".format(context))
                        future: Future = handler_pool.submit(self._handle, context)
                        future.add_done_callback(self._thread_pool_callback(session_id, context=context))
                        with self.lock:
                            if session_id not in self.futures:
                                self.futures[session_id] = []
                            self.futures[session_id].append(future)
                    else:
                        runtime.semaphore.release()
                        with self.lock:
                            active_futures = [t for t in self.futures.get(session_id, []) if not t.done()]
                            self.futures[session_id] = active_futures
                            if not runtime.has_running() and runtime.queue.empty() and not active_futures:
                                del self.sessions[session_id]
                                del self.futures[session_id]
            time.sleep(0.2)

    # 取消session_id对应的所有任务，只能取消排队的消息和已提交线程池但未执行的任务
    def cancel_session(self, session_id, clear_pending: bool = True):
        with self.lock:
            if session_id in self.sessions:
                for future in self.futures.get(session_id, []):
                    future.cancel()
                runtime = self.sessions[session_id]
                runtime.cancel_running()
                if clear_pending:
                    cnt = runtime.clear_pending()
                    if cnt > 0:
                        logger.info("Cancel %s queued messages in session %s", cnt, hash_id(session_id))

    def cancel_all_session(self):
        with self.lock:
            for session_id in self.sessions:
                for future in self.futures.get(session_id, []):
                    future.cancel()
                runtime = self.sessions[session_id]
                runtime.cancel_running()
                cnt = runtime.clear_pending()
                if cnt > 0:
                    logger.info("Cancel %s queued messages in session %s", cnt, hash_id(session_id))


def check_prefix(content, prefix_list):
    if not prefix_list:
        return None
    for prefix in prefix_list:
        if content.startswith(prefix):
            return prefix
    return None


def check_contain(content, keyword_list):
    if not keyword_list:
        return None
    for ky in keyword_list:
        if content.find(ky) != -1:
            return True
    return None


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
