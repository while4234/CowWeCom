# encoding:utf-8

from bridge.reply import Reply, ReplyType
from common.log import logger
from integrations.hermes_xai.tts import XaiTtsError, generate_xai_tts
from voice.voice import Voice


class XaiVoice(Voice):
    """CowWeCom Voice adapter for Grok/xAI TTS."""

    def voiceToText(self, voice_file):
        return Reply(ReplyType.ERROR, "Grok voice-to-text is not supported yet.")

    def textToVoice(self, text):
        try:
            return Reply(ReplyType.VOICE, generate_xai_tts(text))
        except XaiTtsError as exc:
            logger.warning("[GrokTTS] textToVoice failed: %s", exc)
            return Reply(ReplyType.ERROR, "Grok 语音合成失败，请稍后重试。")
