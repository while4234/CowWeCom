# -*- coding=utf-8 -*-
import os
import sys
import time

import web
from wechatpy.enterprise import create_reply, parse_message
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException, WeChatClientException

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.wechatcom.wechatcomapp_client import WechatComAppClient
from channel.wechatcom.wechatcomapp_message import WechatComAppMessage
from common.image_send_limits import image_send_dimensions_from_config, prepare_image_for_send
from common.log import logger
from common.singleton import singleton
from common.utils import compress_imgfile, fsize, split_string_by_utf8_length, remove_markdown_symbol
from config import conf, subscribe_msg
from integrations.hermes_xai.media_download import (
    cleanup_generated_reply_media,
    remove_file_quietly,
    safe_download_to_file,
)
from voice.audio_convert import split_audio_by_wecom_voice_limits

MAX_UTF8_LEN = 2048
REMOTE_IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
REMOTE_VIDEO_CONTENT_TYPES = {"video/mp4", "application/octet-stream"}
MAX_REMOTE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_REMOTE_VIDEO_BYTES = 512 * 1024 * 1024
WECHATCOM_IMAGE_MAX_BYTES = 10 * 1024 * 1024 - 1


@singleton
class WechatComAppChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.corp_id = conf().get("wechatcom_corp_id")
        self.secret = conf().get("wechatcomapp_secret")
        self.agent_id = conf().get("wechatcomapp_agent_id")
        self.token = conf().get("wechatcomapp_token")
        self.aes_key = conf().get("wechatcomapp_aes_key")
        self._http_server = None
        logger.info(
            "[wechatcom] Initializing WeCom app channel, corp_id: {}, agent_id: {}".format(self.corp_id, self.agent_id)
        )
        self.crypto = WeChatCrypto(self.token, self.aes_key, self.corp_id)
        self.client = WechatComAppClient(self.corp_id, self.secret)

    def startup(self):
        # start message listener
        urls = ("/wxcomapp/?", "channel.wechatcom.wechatcomapp_channel.Query")
        app = web.application(urls, globals(), autoreload=False)
        port = conf().get("wechatcomapp_port", 9898)
        logger.info("[wechatcom] ✅ WeCom app channel started successfully")
        logger.info("[wechatcom] 📡 Listening on http://0.0.0.0:{}/wxcomapp/".format(port))
        logger.info("[wechatcom] 🤖 Ready to receive messages")
        
        # Build WSGI app with middleware (same as runsimple but without print)
        func = web.httpserver.StaticMiddleware(app.wsgifunc())
        func = web.httpserver.LogMiddleware(func)
        server = web.httpserver.WSGIServer(("0.0.0.0", port), func)
        self._http_server = server
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit):
            server.stop()

    def stop(self):
        if self._http_server:
            try:
                self._http_server.stop()
                logger.info("[wechatcom] HTTP server stopped")
            except Exception as e:
                logger.warning(f"[wechatcom] Error stopping HTTP server: {e}")
            self._http_server = None

    def send(self, reply: Reply, context: Context):
        receiver = context["receiver"]
        if reply.type in [ReplyType.TEXT, ReplyType.ERROR, ReplyType.INFO]:
            reply_text = remove_markdown_symbol(reply.content)
            texts = split_string_by_utf8_length(reply_text, MAX_UTF8_LEN)
            if len(texts) > 1:
                logger.info("[wechatcom] text too long, split into {} parts".format(len(texts)))
            for i, text in enumerate(texts):
                self.client.message.send_text(self.agent_id, receiver, text)
                if i != len(texts) - 1:
                    time.sleep(0.5)  # 休眠0.5秒，防止发送过快乱序
            logger.info("[wechatcom] Do send text to {}: {}".format(receiver, reply_text))
        elif reply.type == ReplyType.VOICE:
            files = []
            file_path = str(reply.content or "")
            try:
                media_ids = []
                duration, files = split_audio_by_wecom_voice_limits(file_path)
                if len(files) > 1:
                    logger.info(
                        "[wechatcom] voice too long %.1fs, split into %s parts",
                        duration / 1000.0,
                        len(files),
                    )
                for path in files:
                    with open(path, "rb") as voice_file:
                        response = self.client.media.upload("voice", voice_file)
                    logger.debug("[wechatcom] upload voice response: {}".format(response))
                    media_ids.append(response["media_id"])
            except (ImportError, RuntimeError) as e:
                logger.error("[wechatcom] voice conversion failed: {}".format(e))
                return False
            except WeChatClientException as e:
                logger.error("[wechatcom] upload voice failed: {}".format(e))
                return False
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                for path in files:
                    if path and os.path.exists(path):
                        os.remove(path)
            except Exception:
                pass
            try:
                for media_id in media_ids:
                    self.client.message.send_voice(self.agent_id, receiver, media_id)
                    time.sleep(1)
            except WeChatClientException as e:
                logger.error("[wechatcom] send voice failed: {}".format(e))
                return False
            logger.info("[wechatcom] sendVoice={}, receiver={}".format(reply.content, receiver))
            return True
        elif reply.type == ReplyType.IMAGE_URL:  # 从网络下载图片
            img_url = reply.content
            download_path = ""
            prepared_path = ""
            download_handle = None
            image_storage = None
            try:
                download_path = safe_download_to_file(
                    img_url,
                    prefix="wechatcom_img",
                    suffix=None,
                    allowed_content_types=REMOTE_IMAGE_CONTENT_TYPES,
                    max_bytes=MAX_REMOTE_IMAGE_BYTES,
                    timeout=30.0,
                )
                prepared_path = self._prepare_image_path(download_path)
                if not prepared_path:
                    logger.error("[wechatcom] image too large after normalization: {}".format(download_path))
                    return False
                download_handle = open(prepared_path, "rb")
                image_storage = download_handle
                image_storage.seek(0)
                try:
                    response = self.client.media.upload("image", image_storage)
                    logger.debug("[wechatcom] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatcom] upload image failed: {}".format(e))
                    return False

                self.client.message.send_image(self.agent_id, receiver, response["media_id"])
                logger.info("[wechatcom] sendImage remote, receiver={}".format(receiver))
                return True
            except Exception as e:
                logger.error("[wechatcom] failed to download image: {}".format(e))
                return False
            finally:
                try:
                    if image_storage and hasattr(image_storage, "close"):
                        image_storage.close()
                except Exception:
                    pass
                try:
                    if download_handle is not None:
                        download_handle.close()
                except Exception:
                    pass
                remove_file_quietly(download_path)
                if prepared_path and prepared_path != download_path:
                    remove_file_quietly(prepared_path)
        elif reply.type == ReplyType.IMAGE:  # 从文件读取图片
            image_storage = reply.content
            close_after_upload = False
            prepared_path = ""
            if isinstance(image_storage, str):
                if not os.path.exists(image_storage):
                    logger.error("[wechatcom] image file not found: {}".format(image_storage))
                    return False
                prepared_path = self._prepare_image_path(image_storage)
                if not prepared_path:
                    logger.error("[wechatcom] image too large after normalization: {}".format(image_storage))
                    return False
                image_storage = open(prepared_path, "rb")
                close_after_upload = True
            try:
                sz = fsize(image_storage)
                if sz >= 10 * 1024 * 1024:
                    logger.info("[wechatcom] image too large, ready to compress, sz={}".format(sz))
                    compressed_storage = compress_imgfile(image_storage, WECHATCOM_IMAGE_MAX_BYTES)
                    if close_after_upload:
                        try:
                            image_storage.close()
                        except Exception:
                            pass
                    image_storage = compressed_storage
                    close_after_upload = True
                    logger.info("[wechatcom] image compressed, sz={}".format(fsize(image_storage)))
                image_storage.seek(0)
                try:
                    response = self.client.media.upload("image", image_storage)
                    logger.debug("[wechatcom] upload image response: {}".format(response))
                except WeChatClientException as e:
                    logger.error("[wechatcom] upload image failed: {}".format(e))
                    return False
                self.client.message.send_image(self.agent_id, receiver, response["media_id"])
                logger.info("[wechatcom] sendImage, receiver={}".format(receiver))
                return True
            except Exception as e:
                logger.error("[wechatcom] send image failed: {}".format(e))
                return False
            finally:
                if close_after_upload:
                    try:
                        image_storage.close()
                    except Exception:
                        pass
                if prepared_path and prepared_path != reply.content:
                    remove_file_quietly(prepared_path)
                cleanup_generated_reply_media(reply)
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            try:
                return self._send_video(reply.content, receiver)
            finally:
                cleanup_generated_reply_media(reply)

        logger.warning("[wechatcom] unsupported reply type: {}".format(reply.type))
        try:
            self.client.message.send_text(self.agent_id, receiver, "[Unsupported reply type: {}]".format(reply.type))
        except Exception:
            pass
        return False

    @staticmethod
    def _prepare_image_path(file_path: str) -> str:
        max_width, max_height = image_send_dimensions_from_config(conf())
        max_bytes = int(conf().get("wechatcom_image_send_max_bytes", WECHATCOM_IMAGE_MAX_BYTES) or WECHATCOM_IMAGE_MAX_BYTES)
        return prepare_image_for_send(
            file_path,
            max_bytes=max_bytes,
            max_width=max_width,
            max_height=max_height,
            prefix="wechatcom_img",
        )

    def _send_video(self, video_path_or_url, receiver):
        local_path, cleanup_path = self._resolve_video_file(video_path_or_url)
        if not local_path or not os.path.exists(local_path):
            logger.error("[wechatcom] video file not found: {}".format(video_path_or_url))
            self.client.message.send_text(self.agent_id, receiver, "[Video send failed: file not found]")
            return False

        try:
            with open(local_path, "rb") as video_file:
                response = self.client.media.upload("video", video_file)
            logger.debug("[wechatcom] upload video response: {}".format(response))
            media_id = response["media_id"]
            if hasattr(self.client.message, "send_video"):
                self.client.message.send_video(self.agent_id, receiver, media_id)
                logger.info("[wechatcom] sendVideo, receiver={}".format(receiver))
                return True
            if hasattr(self.client.message, "send_file"):
                with open(local_path, "rb") as file_handle:
                    file_response = self.client.media.upload("file", file_handle)
                self.client.message.send_file(self.agent_id, receiver, file_response["media_id"])
                logger.info("[wechatcom] sendVideo fallback sendFile, receiver={}".format(receiver))
                return True
            self.client.message.send_text(
                self.agent_id,
                receiver,
                "[Video send failed: current WeCom app SDK does not support video/file sending]",
            )
            return False
        except WeChatClientException as e:
            logger.error("[wechatcom] send video failed: {}".format(e))
            self.client.message.send_text(self.agent_id, receiver, "[Video send failed]")
            return False
        finally:
            if cleanup_path:
                try:
                    os.remove(cleanup_path)
                except Exception:
                    pass

    @staticmethod
    def _resolve_video_file(video_path_or_url):
        source = str(video_path_or_url or "").strip()
        if source.startswith("file://"):
            source = source[7:]
        if source.startswith(("http://", "https://")):
            try:
                tmp_path = safe_download_to_file(
                    source,
                    prefix="wechatcom_video",
                    suffix=".mp4",
                    allowed_content_types=REMOTE_VIDEO_CONTENT_TYPES,
                    max_bytes=MAX_REMOTE_VIDEO_BYTES,
                    timeout=120.0,
                )
                return tmp_path, tmp_path
            except Exception as e:
                logger.error("[wechatcom] failed to download video: {}".format(e))
                return "", ""
        return source, ""


class Query:
    def GET(self):
        channel = WechatComAppChannel()
        params = web.input()
        logger.info("[wechatcom] receive params: {}".format(params))
        try:
            signature = params.msg_signature
            timestamp = params.timestamp
            nonce = params.nonce
            echostr = params.echostr
            echostr = channel.crypto.check_signature(signature, timestamp, nonce, echostr)
        except InvalidSignatureException:
            raise web.Forbidden()
        return echostr

    def POST(self):
        channel = WechatComAppChannel()
        params = web.input()
        logger.info("[wechatcom] receive params: {}".format(params))
        try:
            signature = params.msg_signature
            timestamp = params.timestamp
            nonce = params.nonce
            message = channel.crypto.decrypt_message(web.data(), signature, timestamp, nonce)
        except (InvalidSignatureException, InvalidCorpIdException):
            raise web.Forbidden()
        msg = parse_message(message)
        logger.debug("[wechatcom] receive message: {}, msg= {}".format(message, msg))
        if msg.type == "event":
            if msg.event == "subscribe":
                pass
                # reply_content = subscribe_msg()
                # if reply_content:
                #     reply = create_reply(reply_content, msg).render()
                #     res = channel.crypto.encrypt_message(reply, nonce, timestamp)
                #     return res
        else:
            try:
                wechatcom_msg = WechatComAppMessage(msg, client=channel.client)
            except NotImplementedError as e:
                logger.debug("[wechatcom] " + str(e))
                return "success"
            context = channel._compose_context(
                wechatcom_msg.ctype,
                wechatcom_msg.content,
                isgroup=False,
                msg=wechatcom_msg,
            )
            if context:
                channel.produce(context)
        return "success"
