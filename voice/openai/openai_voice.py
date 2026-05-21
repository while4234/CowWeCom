import datetime
import os
import random

import requests

from bridge.reply import Reply, ReplyType
from common import const
from common.log import logger
from config import conf
from voice.voice import Voice


class OpenaiVoice(Voice):
    def __init__(self):
        # This implementation calls OpenAI-compatible HTTP endpoints directly.
        pass

    @staticmethod
    def _api_base():
        return (conf().get("open_ai_api_base") or "https://api.openai.com/v1").rstrip("/")

    @staticmethod
    def _timeout():
        return conf().get("request_timeout", 180)

    @staticmethod
    def _headers(content_type=None):
        headers = {"Authorization": "Bearer " + conf().get("open_ai_api_key")}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def voiceToText(self, voice_file):
        logger.debug("[Openai] voice file name={}".format(voice_file))
        try:
            url = f"{self._api_base()}/audio/transcriptions"
            data = {"model": conf().get("voice_to_text_model") or "whisper-1"}
            with open(voice_file, "rb") as file:
                response = requests.post(
                    url,
                    headers=self._headers(),
                    files={"file": file},
                    data=data,
                    timeout=self._timeout(),
                )

            try:
                response_data = response.json()
            except ValueError:
                response_data = {"error": response.text[:500]}

            if response.status_code != 200 or "text" not in response_data:
                logger.error(
                    "[Openai] voiceToText failed: status={}, resp={}".format(
                        response.status_code,
                        response_data,
                    )
                )
                return Reply(
                    ReplyType.ERROR,
                    "I could not transcribe this voice message. Please try again later.",
                )

            text = response_data["text"]
            logger.info("[Openai] voiceToText text={} voice file name={}".format(text, voice_file))
            return Reply(ReplyType.TEXT, text)
        except Exception as e:
            logger.error(f"[Openai] voiceToText exception: {e}", exc_info=True)
            return Reply(
                ReplyType.ERROR,
                "I could not transcribe this voice message. Please try again later.",
            )

    def textToVoice(self, text):
        try:
            url = f"{self._api_base()}/audio/speech"
            data = {
                "model": conf().get("text_to_voice_model") or const.TTS_1,
                "input": text,
                "voice": conf().get("tts_voice_id") or "alloy",
            }
            response = requests.post(
                url,
                headers=self._headers("application/json"),
                json=data,
                timeout=self._timeout(),
            )
            if response.status_code >= 400:
                logger.error(
                    "[OPENAI] text_to_Voice failed: status={}, resp={}".format(
                        response.status_code,
                        response.text[:500],
                    )
                )
                return Reply(
                    ReplyType.ERROR,
                    "I could not generate voice output. Please try again later.",
                )

            os.makedirs("tmp", exist_ok=True)
            file_name = (
                "tmp/"
                + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                + str(random.randint(0, 1000))
                + ".mp3"
            )
            logger.debug(f"[OPENAI] text_to_Voice file_name={file_name}, input={text}")
            with open(file_name, "wb") as f:
                f.write(response.content)
            logger.info("[OPENAI] text_to_Voice success")
            return Reply(ReplyType.VOICE, file_name)
        except Exception as e:
            logger.error(e)
            return Reply(
                ReplyType.ERROR,
                "I could not generate voice output. Please try again later.",
            )
