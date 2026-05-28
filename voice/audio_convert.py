import shutil
import wave
import os
import uuid
import math

from common.log import logger
from config import conf

try:
    import pysilk
except ImportError:
    logger.debug("import pysilk failed, silk voice format will not be supported.")

try:
    from pydub import AudioSegment
    _pydub_available = True
except ImportError:
    logger.debug("import pydub failed, voice conversion features will not be supported.")
    AudioSegment = None
    _pydub_available = False

WECOM_AMR_DEFAULT_BITRATE = "12.2k"
WECOM_AMR_BITRATES = {
    "4.75k",
    "5.15k",
    "5.90k",
    "6.70k",
    "7.40k",
    "7.95k",
    "10.2k",
    "12.2k",
}

sil_supports = [8000, 12000, 16000, 24000, 32000, 44100, 48000]  # slk转wav时，支持的采样率


def find_closest_sil_supports(sample_rate):
    """
    找到最接近的支持的采样率
    """
    if sample_rate in sil_supports:
        return sample_rate
    closest = 0
    mindiff = 9999999
    for rate in sil_supports:
        diff = abs(rate - sample_rate)
        if diff < mindiff:
            closest = rate
            mindiff = diff
    return closest


def get_pcm_from_wav(wav_path):
    """
    从 wav 文件中读取 pcm

    :param wav_path: wav 文件路径
    :returns: pcm 数据
    """
    wav = wave.open(wav_path, "rb")
    return wav.readframes(wav.getnframes())


def any_to_mp3(any_path, mp3_path):
    """
    把任意格式转成mp3文件
    """
    if not _pydub_available:
        raise ImportError("pydub is required for audio conversion. Please install it with: pip install pydub")
    if any_path.endswith(".mp3"):
        shutil.copy2(any_path, mp3_path)
        return
    if any_path.endswith(".sil") or any_path.endswith(".silk") or any_path.endswith(".slk"):
        sil_to_wav(any_path, any_path)
        any_path = mp3_path
    audio = AudioSegment.from_file(any_path)
    audio.export(mp3_path, format="mp3")


def any_to_wav(any_path, wav_path):
    """
    把任意格式转成wav文件
    """
    if not _pydub_available:
        raise ImportError("pydub is required for audio conversion. Please install it with: pip install pydub")
    if any_path.endswith(".wav"):
        shutil.copy2(any_path, wav_path)
        return
    if any_path.endswith(".sil") or any_path.endswith(".silk") or any_path.endswith(".slk"):
        return sil_to_wav(any_path, wav_path)
    # pydub 0.23.0+ 会将 parameters 追加到 ffmpeg 命令的输出文件 `-` 之后，
    # 因此 -nostdin 可能被当作"尾部选项"处理，是否生效取决于 ffmpeg 版本。
    # 目的是防止后台服务中 ffmpeg 子进程继承父进程的 stdin，避免死锁。
    audio = AudioSegment.from_file(any_path, parameters=["-nostdin"])
    # AudioSegment 是不可变对象：set_frame_rate/set_channels 返回新对象，不修改原对象。
    # 必须将返回值重新赋给 audio，否则修改不会生效。
    audio = audio.set_frame_rate(16000)
    audio = audio.set_channels(1)
    audio.export(wav_path, format="wav", codec='pcm_s16le')


def any_to_sil(any_path, sil_path):
    """
    把任意格式转成sil文件
    """
    if not _pydub_available:
        raise ImportError("pydub is required for audio conversion. Please install it with: pip install pydub")
    if any_path.endswith(".sil") or any_path.endswith(".silk") or any_path.endswith(".slk"):
        shutil.copy2(any_path, sil_path)
        return 10000
    audio = AudioSegment.from_file(any_path)
    rate = find_closest_sil_supports(audio.frame_rate)
    # Convert to PCM_s16
    pcm_s16 = audio.set_sample_width(2)
    pcm_s16 = pcm_s16.set_frame_rate(rate)
    wav_data = pcm_s16.raw_data
    silk_data = pysilk.encode(wav_data, data_rate=rate, sample_rate=rate)
    with open(sil_path, "wb") as f:
        f.write(silk_data)
    return audio.duration_seconds * 1000


def any_to_amr(any_path, amr_path):
    """
    把任意格式转成amr文件
    """
    if not _pydub_available:
        raise ImportError("pydub is required for audio conversion. Please install it with: pip install pydub")
    if any_path.endswith(".amr"):
        shutil.copy2(any_path, amr_path)
        return
    if any_path.endswith(".sil") or any_path.endswith(".silk") or any_path.endswith(".slk"):
        raise NotImplementedError("Not support file type: {}".format(any_path))
    audio = AudioSegment.from_file(any_path)
    audio = _prepare_wecom_voice_audio(audio)
    audio.export(amr_path, format="amr", bitrate=_wecom_amr_bitrate())
    return audio.duration_seconds * 1000


def split_audio_by_wecom_voice_limits(
    file_path,
    output_dir=None,
    max_seconds=None,
    max_bytes=None,
):
    """Convert/split audio into AMR chunks accepted by WeCom voice messages."""
    if not _pydub_available:
        raise RuntimeError("pydub is required to prepare WeCom voice audio.")
    max_seconds = _positive_number(max_seconds, conf().get("wecom_voice_max_seconds", 55), 55)
    max_bytes = int(_positive_number(max_bytes, conf().get("wecom_voice_max_bytes", 1900000), 1900000))
    max_segment_ms = int(max_seconds * 1000)
    if max_segment_ms <= 0 or max_bytes <= 5:
        raise RuntimeError("Invalid WeCom voice limits: max seconds and bytes must be positive.")

    source_path = str(file_path or "").strip()
    if not source_path or not os.path.exists(source_path):
        raise RuntimeError("WeCom voice source file does not exist.")

    output_dir = output_dir or os.path.dirname(os.path.abspath(source_path)) or "."
    os.makedirs(output_dir, exist_ok=True)
    file_prefix = os.path.splitext(os.path.basename(source_path))[0] or "wecom_voice"
    unique_prefix = f"{file_prefix}-{uuid.uuid4().hex[:8]}"

    try:
        audio = AudioSegment.from_file(source_path, parameters=["-nostdin"])
        audio = _prepare_wecom_voice_audio(audio)
    except Exception as exc:
        raise RuntimeError("Failed to decode voice audio for WeCom AMR conversion.") from exc

    total_ms = len(audio)
    if total_ms <= 0:
        raise RuntimeError("WeCom voice audio is empty.")

    paths = []
    start_ms = 0
    segment_index = 1
    amr_bitrate = _wecom_amr_bitrate()
    try:
        while start_ms < total_ms:
            duration_ms = min(max_segment_ms, total_ms - start_ms)
            path, actual_duration = _export_wecom_amr_segment(
                audio,
                output_dir,
                unique_prefix,
                segment_index,
                start_ms,
                duration_ms,
                max_bytes,
                amr_bitrate,
            )
            paths.append(path)
            start_ms += actual_duration
            segment_index += 1
        if not paths:
            raise RuntimeError("WeCom voice audio did not produce any AMR segment.")
        return total_ms, paths
    except Exception:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        raise


def _export_wecom_amr_segment(audio, output_dir, prefix, index, start_ms, duration_ms, max_bytes, amr_bitrate):
    min_duration_ms = 500
    current_ms = max(min_duration_ms, int(duration_ms))
    while current_ms >= min_duration_ms:
        end_ms = min(len(audio), start_ms + current_ms)
        if end_ms <= start_ms:
            break
        segment = audio[start_ms:end_ms]
        path = os.path.join(output_dir, f"{prefix}-{index}.amr")
        try:
            segment.export(path, format="amr", bitrate=amr_bitrate)
        except Exception as exc:
            _remove_quietly(path)
            raise RuntimeError("Failed to export WeCom AMR voice segment.") from exc
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if 5 < size <= max_bytes:
            return path, end_ms - start_ms
        _remove_quietly(path)
        if size <= 5:
            raise RuntimeError("WeCom AMR voice segment is empty after conversion.")
        current_ms = current_ms // 2

    raise RuntimeError("WeCom AMR voice segment exceeds size limit even after shortening.")


def _prepare_wecom_voice_audio(audio):
    audio = audio.set_channels(1)
    audio = _normalize_wecom_voice_audio(audio)
    return audio.set_frame_rate(8000)


def _normalize_wecom_voice_audio(audio):
    if not _config_bool(conf().get("wecom_voice_normalize_enabled", True), True):
        return audio
    target_dbfs = _bounded_number(conf().get("wecom_voice_normalize_target_dbfs", -18.0), -18.0, -35.0, -6.0)
    headroom_db = _bounded_number(conf().get("wecom_voice_normalize_headroom_db", 1.0), 1.0, 0.1, 12.0)
    loudness_dbfs = _audio_level_dbfs(audio, "dBFS")
    peak_dbfs = _audio_level_dbfs(audio, "max_dBFS")
    if loudness_dbfs is None and peak_dbfs is None:
        return audio
    gain_db = target_dbfs - loudness_dbfs if loudness_dbfs is not None else -headroom_db - peak_dbfs
    try:
        if abs(gain_db) >= 0.1:
            audio = audio.apply_gain(gain_db)
        peak_dbfs = _audio_level_dbfs(audio, "max_dBFS")
        peak_gain_db = -headroom_db - peak_dbfs if peak_dbfs is not None else 0
        if peak_gain_db < -0.1:
            audio = audio.apply_gain(peak_gain_db)
        return audio
    except Exception as exc:
        logger.warning("[VoiceConvert] WeCom voice normalization skipped: %s", exc)
        return audio


def _audio_level_dbfs(audio, attr_name):
    try:
        value = float(getattr(audio, attr_name))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _wecom_amr_bitrate():
    bitrate = str(conf().get("wecom_voice_amr_bitrate", WECOM_AMR_DEFAULT_BITRATE) or "").strip().lower()
    if bitrate in WECOM_AMR_BITRATES:
        return bitrate
    logger.warning("[VoiceConvert] Invalid wecom_voice_amr_bitrate=%r, using %s", bitrate, WECOM_AMR_DEFAULT_BITRATE)
    return WECOM_AMR_DEFAULT_BITRATE


def _config_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_number(value, default, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _positive_number(*values):
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def sil_to_wav(silk_path, wav_path, rate: int = 24000):
    """
    silk 文件转 wav
    """
    wav_data = pysilk.decode_file(silk_path, to_wav=True, sample_rate=rate)
    with open(wav_path, "wb") as f:
        f.write(wav_data)


def split_audio(file_path, max_segment_length_ms=60000):
    """
    分割音频文件
    """
    if not _pydub_available:
        raise ImportError("pydub is required for audio conversion. Please install it with: pip install pydub")
    audio = AudioSegment.from_file(file_path)
    audio_length_ms = len(audio)
    if audio_length_ms <= max_segment_length_ms:
        return audio_length_ms, [file_path]
    segments = []
    for start_ms in range(0, audio_length_ms, max_segment_length_ms):
        end_ms = min(audio_length_ms, start_ms + max_segment_length_ms)
        segment = audio[start_ms:end_ms]
        segments.append(segment)
    file_prefix = file_path[: file_path.rindex(".")]
    format = file_path[file_path.rindex(".") + 1 :]
    files = []
    for i, segment in enumerate(segments):
        path = f"{file_prefix}_{i+1}" + f".{format}"
        segment.export(path, format=format)
        files.append(path)
    return audio_length_ms, files
