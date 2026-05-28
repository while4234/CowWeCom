import base64
import os
import time
import uuid

from common.log import logger
from common.image_prompt_enhancer import enhance_image_prompt, redact_hidden_image_prompt_text
from common.llm_backend_router import get_effective_openai_api_config
from common.token_bucket import TokenBucket
from config import conf
from models.openai.openai_compat import RateLimitError, wrap_http_error
from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError
from models.openai.responses_api_adapter import is_responses_wire_api


_RESPONSES_IMAGE_SIZES = {"auto", "1024x1024", "1024x1536", "1536x1024"}
_RESPONSES_IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
_IMAGE_FORMAT_EXTENSIONS = {
    "png": ".png",
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "webp": ".webp",
}
_OPENAI_IMAGE_MODEL_ALIASES = {
    "gpt-imagine": "gpt-image-2",
    "gpt-imagine2": "gpt-image-2",
    "gpt-imagine-2": "gpt-image-2",
    "gpt_image_2": "gpt-image-2",
    "gpt_image2": "gpt-image-2",
}


def _normalize_openai_image_model(model):
    value = str(model or "").strip()
    return _OPENAI_IMAGE_MODEL_ALIASES.get(value.lower(), value)


def _configured_wire_api():
    routed = get_effective_openai_api_config()
    return (
        routed.get("wire_api")
        or conf().get("open_ai_wire_api")
        or conf().get("openai_wire_api")
        or conf().get("wire_api")
        or "chat_completions"
    )


def _save_b64_image_to_tmp(image_b64, output_format="png"):
    image_bytes = base64.b64decode(image_b64)
    fmt = (output_format or "png").lower()
    ext = _IMAGE_FORMAT_EXTENSIONS.get(fmt, ".png")
    os.makedirs("tmp", exist_ok=True)
    path = os.path.abspath(os.path.join("tmp", f"openai_image_{uuid.uuid4().hex}{ext}"))
    with open(path, "wb") as f:
        f.write(image_bytes)
    return "file://" + path


def _extract_image_reference(response, output_format="png"):
    data = response.get("data") if isinstance(response, dict) else None
    if not data:
        return ""
    first = data[0] if isinstance(data, list) and data else {}
    if first.get("url"):
        return first["url"]
    if first.get("b64_json"):
        return _save_b64_image_to_tmp(first["b64_json"], output_format=output_format)
    return ""


def _extract_responses_image_reference(response_or_event, output_format="png"):
    if not isinstance(response_or_event, dict):
        return ""

    top_level_b64 = (
        response_or_event.get("result")
        or response_or_event.get("b64_json")
        or response_or_event.get("partial_image_b64")
    )
    if top_level_b64:
        return _save_b64_image_to_tmp(top_level_b64, output_format=output_format)

    candidates = []
    if response_or_event.get("type") == "image_generation_call":
        candidates.append(response_or_event)

    item = response_or_event.get("item")
    if isinstance(item, dict) and item.get("type") == "image_generation_call":
        candidates.append(item)

    response = response_or_event.get("response")
    if isinstance(response, dict):
        candidates.extend(response.get("output") or [])

    candidates.extend(response_or_event.get("output") or [])

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        image_b64 = (
            candidate.get("result")
            or candidate.get("b64_json")
            or candidate.get("partial_image_b64")
        )
        if image_b64:
            return _save_b64_image_to_tmp(image_b64, output_format=output_format)
    return ""


class OpenAIImage(object):
    """OpenAI image generation wrapper.

    In Responses wire mode, image creation first tries the Responses
    image_generation tool. If that is unavailable, it falls back to the classic
    Images API. Both URL and base64 image outputs are normalized to a sendable
    image reference.
    """

    def __init__(self):
        routed = get_effective_openai_api_config()
        self._image_api_key = routed.get("api_key") or conf().get("open_ai_api_key")
        self._image_api_base = routed.get("api_base") or conf().get("open_ai_api_base") or None
        self._image_proxy = conf().get("proxy") or None
        self._image_client = OpenAIHTTPClient(
            api_key=self._image_api_key,
            api_base=self._image_api_base,
            proxy=self._image_proxy,
        )
        if conf().get("rate_limit_dalle"):
            self.tb4dalle = TokenBucket(conf().get("rate_limit_dalle", 50))

    def create_img(self, query, retry_count=0, api_key=None, api_base=None):
        try:
            if conf().get("rate_limit_dalle") and not self.tb4dalle.get_token():
                return False, "Image requests are too frequent, please try again later."

            logger.info("[OPEN_AI] image_query={}".format(query))
            image_model = _normalize_openai_image_model(conf().get("text_to_image") or "dall-e-2")
            metadata = enhance_image_prompt(
                query,
                target="gpt",
                model=image_model,
                runtime="openai_legacy",
                size=conf().get("image_create_size", "256x256"),
                quality=conf().get("image_create_quality") or conf().get("dalle3_image_quality"),
            )
            query = metadata.get("enhanced_prompt") or query

            if is_responses_wire_api(_configured_wire_api()):
                ok, image_ref = self._create_img_with_responses(
                    query=query,
                    api_key=api_key,
                    api_base=api_base,
                )
                if ok:
                    logger.info("[OPEN_AI] responses_image={}".format(image_ref))
                    return True, image_ref
                logger.warning(
                    "[OPEN_AI] Responses image generation failed, "
                    "falling back to Images API: {}".format(image_ref)
                )

            response = self._image_client.images_generate(
                api_key=api_key or None,
                api_base=api_base or None,
                prompt=query,
                n=1,
                model=image_model,
                size=conf().get("image_create_size", "256x256"),
            )
            image_ref = _extract_image_reference(
                response,
                output_format=conf().get("image_create_format", "png"),
            )
            if not image_ref:
                logger.error("[OPEN_AI] image response has no url or b64_json")
                return False, "Image generation failed."
            logger.info("[OPEN_AI] image_ref={}".format(image_ref))
            return True, image_ref

        except OpenAIHTTPError as http_err:
            mapped = wrap_http_error(http_err)
            if isinstance(mapped, RateLimitError):
                logger.warn(mapped)
                if retry_count < 1:
                    time.sleep(5)
                    logger.warn(
                        "[OPEN_AI] ImgCreate RateLimit exceed, retry {}".format(
                            retry_count + 1
                        )
                    )
                    return self.create_img(query, retry_count + 1)
                return False, "Image generation is rate limited. Please try again later."
            logger.exception(mapped)
            return False, "Image generation failed."

        except RateLimitError as e:
            logger.warn(e)
            if retry_count < 1:
                time.sleep(5)
                logger.warn(
                    "[OPEN_AI] ImgCreate RateLimit exceed, retry {}".format(
                        retry_count + 1
                    )
                )
                return self.create_img(query, retry_count + 1)
            return False, "Image generation is rate limited. Please try again later."

        except Exception as e:
            logger.exception(e)
            return False, "Image generation failed."

    def _create_img_with_responses(self, query, api_key=None, api_base=None):
        output_format = (conf().get("image_create_format", "png") or "png").lower()
        tool = {"type": "image_generation"}

        size = conf().get("image_create_size")
        if size in _RESPONSES_IMAGE_SIZES:
            tool["size"] = size

        quality = conf().get("image_create_quality") or conf().get("dalle3_image_quality")
        if quality in _RESPONSES_IMAGE_QUALITIES:
            tool["quality"] = quality

        if output_format in _IMAGE_FORMAT_EXTENSIONS:
            tool["format"] = "jpeg" if output_format == "jpg" else output_format

        try:
            events = self._image_client.responses(
                api_key=api_key or None,
                api_base=(api_base or self._image_api_base or "").rstrip("/") or None,
                model=conf().get("model") or "gpt-5",
                input=query,
                tools=[tool],
                tool_choice={"type": "image_generation"},
                store=not bool(conf().get("disable_response_storage", False)),
                stream=True,
            )
            latest_image_ref = ""
            for event in events:
                image_ref = _extract_responses_image_reference(
                    event,
                    output_format=output_format,
                )
                if image_ref:
                    latest_image_ref = image_ref
            if latest_image_ref:
                return True, latest_image_ref
            return False, "Responses image generation returned no image."
        except Exception as e:
            safe_error = redact_hidden_image_prompt_text(e)
            logger.error(
                f"[OPEN_AI] responses image generation error: {safe_error}",
                exc_info=(safe_error == str(e)),
            )
            return False, safe_error
