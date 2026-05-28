# encoding:utf-8

"""Benchmark low-latency candidates for Grok voice conversation mode."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.llm_backend_router import (  # noqa: E402
    BACKEND_CAPI,
    BACKEND_CAPI_MONTHLY,
    BACKEND_CODEX,
    get_codex_provider_config,
    get_current_backend,
    get_effective_openai_api_config,
    normalize_backend,
)
from config import conf, load_config  # noqa: E402
from integrations.hermes_xai.tts import generate_xai_tts  # noqa: E402
from integrations.hermes_xai.xai_http import has_xai_credentials  # noqa: E402
from models.codex.codex_bot import CodexBot  # noqa: E402
from models.openai.open_ai_bot import OpenAIBot  # noqa: E402


PROMPTS = [
    ("intro", "你好，简单介绍下你自己"),
    ("driving_reply", "我现在在开车，帮我快速回复一句：我马上到"),
    ("meeting_late", "今天下午会议我迟到十分钟，怎么说比较合适"),
    ("oauth_one_sentence", "帮我用一句话解释一下什么是 OAuth"),
    ("cowwecom_grok_plan", "帮我分析 CowWeCom 接入 Grok 的方案"),
]

VOICE_SYSTEM_PROMPT = (
    "你正在语音会话中。请用简短、口语化、可直接听懂的方式回答。"
    "复杂问题先给结论和下一步建议，不要展开长篇分析。"
    "最多回答 2 到 3 句，总字数尽量控制在 120 个中文字符以内。"
)
PERCEPTUAL_LATENCY_THRESHOLD_MS = 700.0
PERCEPTUAL_LATENCY_THRESHOLD_RATIO = 0.2
DEFAULT_MAX_TOKENS = 220


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    load_config()
    backends = _normalize_backends(args.backend)
    include_tts = _parse_bool(args.include_tts)
    reasoning_effort = _normalize_reasoning_effort(args.reasoning_effort)
    candidates = [_candidate(backend, args.model) for backend in backends]
    results: List[Dict[str, Any]] = []
    tts_ready = _tts_ready(include_tts)

    if not args.dry_run:
        for candidate in candidates:
            if not candidate["ready"]:
                results.append(_skip_result(candidate, candidate["skip_reason"]))
                continue
            if include_tts and not tts_ready:
                results.append(_skip_result(candidate, "missing_xai_tts_credentials"))
                continue
            for run_index in range(max(1, int(args.runs))):
                for prompt_id, prompt in PROMPTS:
                    results.append(
                        _run_one(
                            candidate,
                            prompt_id,
                            prompt,
                            run_index,
                            include_tts,
                            args.timeout_seconds,
                            reasoning_effort,
                        )
                    )

    output = {
        "schema_version": 1,
        "dry_run": bool(args.dry_run),
        "include_tts": include_tts,
        "reasoning_effort": reasoning_effort,
        "perceptual_latency_threshold_ms": PERCEPTUAL_LATENCY_THRESHOLD_MS,
        "perceptual_latency_threshold_ratio": PERCEPTUAL_LATENCY_THRESHOLD_RATIO,
        "candidates": [_public_candidate(candidate) for candidate in candidates],
        "results": results,
        "recommendation": recommend(candidates, results),
        "config_suggestions": _config_suggestions(candidates, results),
        "notes": [
            "CAPI 月卡与额度卡本质为同一后端，性能差异不应作为模型选择依据；优先按费用策略、额度策略和用户账户状态选择 billing_profile。",
            "语音会话模式支持 request-level backend override；grok_voice_low_latency_model 应与推荐后端匹配，避免把仅在其他后端可用的模型写入当前后端配置。",
            "当 first_voice_ready_ms 差距低于用户感知阈值时，推荐逻辑优先考虑回答质量、口语化和稳定性，而不是单纯追求最低延迟。",
        ],
    }
    _write_output(args.output, output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", action="append", default=[], help="capi_quota/capi, capi_monthly, or codex. Can repeat.")
    parser.add_argument("--model", default="", help="Optional model id to test on the selected backend(s).")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--include-tts", default="false")
    parser.add_argument("--reasoning-effort", default="low", help="Reasoning effort to test: low, medium, or xhigh.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--output", default="data/voice_mode_benchmark.json")
    return parser.parse_args(argv)


def _normalize_backends(values: Iterable[str]) -> List[str]:
    raw_values: List[str] = []
    for value in values or []:
        raw_values.extend(part.strip() for part in str(value).split(",") if part.strip())
    if not raw_values:
        raw_values = [BACKEND_CAPI_MONTHLY, BACKEND_CAPI, BACKEND_CODEX]
    normalized = []
    for value in raw_values:
        backend = BACKEND_CAPI if value == "capi_quota" else normalize_backend(value)
        if backend not in normalized:
            normalized.append(backend)
    return normalized


def _candidate(backend: str, model_override: str = "") -> Dict[str, Any]:
    backend = normalize_backend(backend)
    billing_profile = {
        BACKEND_CAPI: "quota",
        BACKEND_CAPI_MONTHLY: "monthly",
        BACKEND_CODEX: "codex",
    }.get(backend, "unknown")
    candidate = {
        "backend_profile": backend,
        "billing_profile": billing_profile,
        "model": "",
        "wire_api": "",
        "ready": False,
        "skip_reason": "",
    }
    if backend in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY}:
        routed = get_effective_openai_api_config(backend)
        candidate["model"] = model_override or str(routed.get("model") or "")
        candidate["wire_api"] = str(routed.get("wire_api") or "chat_completions")
        if not candidate["model"]:
            candidate["skip_reason"] = "missing_model"
            return candidate
        if not str(routed.get("api_key") or "").strip():
            candidate["skip_reason"] = "missing_capi_credentials"
            return candidate
        candidate["ready"] = True
        return candidate

    provider = get_codex_provider_config()
    candidate["model"] = model_override or str(provider.get("model") or "gpt-5.5")
    candidate["wire_api"] = "responses"
    if not _codex_auth_available(provider):
        candidate["skip_reason"] = "missing_or_unreadable_codex_auth"
        return candidate
    candidate["ready"] = True
    return candidate


def _public_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "backend_profile": candidate["backend_profile"],
        "billing_profile": candidate["billing_profile"],
        "model": candidate["model"],
        "wire_api": candidate["wire_api"],
        "ready": bool(candidate["ready"]),
        "skip_reason": candidate["skip_reason"],
    }


def _run_one(
    candidate: Dict[str, Any],
    prompt_id: str,
    prompt: str,
    run_index: int,
    include_tts: bool,
    timeout_seconds: float,
    reasoning_effort: str,
) -> Dict[str, Any]:
    started = time.perf_counter()
    result = {
        "backend_profile": candidate["backend_profile"],
        "billing_profile": candidate["billing_profile"],
        "model": candidate["model"],
        "prompt_id": prompt_id,
        "run_index": run_index,
        "ttft_ms": None,
        "first_sentence_ms": None,
        "tts_ms": None,
        "first_voice_ready_ms": None,
        "total_ms": None,
        "chars_out": 0,
        "tokens_out": None,
        "reasoning_effort": reasoning_effort,
        "quality_score": 0.0,
        "success": False,
        "error_type": "",
    }
    try:
        chunks = _stream_candidate(candidate, prompt, timeout_seconds, reasoning_effort)
        text_parts: List[str] = []
        first_text_at = None
        first_sentence_at = None
        for delta in chunks:
            now = time.perf_counter()
            if first_text_at is None:
                first_text_at = now
                result["ttft_ms"] = _ms(started, now)
            text_parts.append(delta)
            if first_sentence_at is None and _first_sentence("".join(text_parts)):
                first_sentence_at = now
                result["first_sentence_ms"] = _ms(started, now)
        total_at = time.perf_counter()
        text = "".join(text_parts).strip()
        if first_sentence_at is None and text:
            first_sentence_at = total_at
            result["first_sentence_ms"] = _ms(started, total_at)
        result["total_ms"] = _ms(started, total_at)
        result["chars_out"] = len(text)
        result["quality_score"] = _quality_score(text)
        first_voice_ready = result["first_sentence_ms"] or result["ttft_ms"] or result["total_ms"]
        if include_tts and text:
            tts_text = _first_sentence(text) or text[:160]
            tts_start = time.perf_counter()
            path = generate_xai_tts(tts_text)
            result["tts_ms"] = _ms(tts_start, time.perf_counter())
            result["first_voice_ready_ms"] = (first_voice_ready or result["total_ms"] or 0) + result["tts_ms"]
            _remove_quietly(path)
        else:
            result["first_voice_ready_ms"] = first_voice_ready
        result["success"] = bool(text)
        if not text:
            result["error_type"] = "empty_output"
        return result
    except Exception as exc:
        result["total_ms"] = _ms(started, time.perf_counter())
        result["error_type"] = _sanitize_error_type(exc)
        return result


def _stream_candidate(candidate: Dict[str, Any], prompt: str, timeout_seconds: float, reasoning_effort: str) -> Iterable[str]:
    messages = [{"role": "user", "content": prompt}]
    kwargs = {
        "model": candidate["model"],
        "stream": True,
        "system": VOICE_SYSTEM_PROMPT,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "max_output_tokens": DEFAULT_MAX_TOKENS,
        "reasoning_effort": reasoning_effort,
        "reasoning_effort_locked": True,
        "channel_type": "voice_mode_benchmark",
        "session_id": "voice-mode-benchmark",
        "request_timeout": timeout_seconds,
    }
    if candidate["backend_profile"] in {BACKEND_CAPI, BACKEND_CAPI_MONTHLY}:
        bot = OpenAIBot(backend_override=candidate["backend_profile"])
    else:
        bot = CodexBot()
    stream = bot.call_with_tools(messages, tools=None, **kwargs)
    for chunk in stream:
        if isinstance(chunk, dict) and chunk.get("error"):
            raise RuntimeError(_sanitize_error_type(chunk.get("message") or chunk.get("error") or "stream_error"))
        if not isinstance(chunk, dict) or not chunk.get("choices"):
            continue
        delta = chunk["choices"][0].get("delta") or {}
        text = str(delta.get("content") or "")
        if text:
            yield text


def recommend(candidates: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    grouped = []
    for candidate in candidates:
        candidate_results = [
            item
            for item in results
            if item.get("backend_profile") == candidate["backend_profile"] and item.get("model") == candidate["model"]
        ]
        successful = [item for item in candidate_results if item.get("success") and item.get("first_voice_ready_ms") is not None]
        if not results:
            grouped.append({
                "candidate": candidate,
                "p50": math.inf if not candidate["ready"] else 0,
                "p95": math.inf if not candidate["ready"] else 0,
                "failure_rate": 0 if candidate["ready"] else 1,
                "quality": 0.0,
            })
            continue
        total = len(candidate_results) or 1
        values = [float(item["first_voice_ready_ms"]) for item in successful]
        grouped.append({
            "candidate": candidate,
            "p50": _percentile(values, 50),
            "p95": _percentile(values, 95),
            "failure_rate": 1.0 - (len(successful) / total),
            "quality": statistics.mean([float(item.get("quality_score") or 0) for item in successful]) if successful else 0.0,
        })

    ready_groups = [item for item in grouped if item["candidate"].get("ready") and item["failure_rate"] < 1]
    if not ready_groups:
        ready_candidates = [item for item in grouped if item["candidate"].get("ready")]
        best = ready_candidates[0] if ready_candidates else (grouped[0] if grouped else None)
        return _recommendation_from_group(best, "config_only_or_all_runs_skipped")

    best_latency = min(item["p50"] for item in ready_groups)
    threshold = max(PERCEPTUAL_LATENCY_THRESHOLD_MS, best_latency * PERCEPTUAL_LATENCY_THRESHOLD_RATIO)
    perceptually_close = [item for item in ready_groups if item["p50"] <= best_latency + threshold]
    perceptually_close.sort(
        key=lambda item: (
            item["failure_rate"],
            -item["quality"],
            item["p95"],
            item["p50"],
            _cost_rank(item["candidate"]),
        )
    )
    return _recommendation_from_group(perceptually_close[0], "quality_first_within_perceptual_latency_threshold")


def _recommendation_from_group(group: Optional[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    if not group:
        return {
            "recommended_backend": "",
            "recommended_billing_profile": "",
            "recommended_model": "",
            "reason": "no_candidates",
            "fallback_backend": "",
            "fallback_model": "",
        }
    candidate = group["candidate"]
    return {
        "recommended_backend": candidate.get("backend_profile", ""),
        "recommended_billing_profile": candidate.get("billing_profile", ""),
        "recommended_model": candidate.get("model", ""),
        "reason": reason,
        "fallback_backend": "",
        "fallback_model": "",
        "p50_first_voice_ready_ms": None if math.isinf(group["p50"]) else group["p50"],
        "p95_first_voice_ready_ms": None if math.isinf(group["p95"]) else group["p95"],
        "failure_rate": group["failure_rate"],
        "quality_score": group["quality"],
    }


def _config_suggestions(candidates: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    recommendation = recommend(candidates, results)
    current_backend = get_current_backend()
    raw_recommended_backend = str(recommendation.get("recommended_backend") or "").strip()
    recommended_backend = normalize_backend(raw_recommended_backend) if raw_recommended_backend else ""
    recommended_model = recommendation.get("recommended_model") or ""
    return {
        "grok_voice_low_latency_backend": "" if not recommended_backend or recommended_backend == current_backend else recommended_backend,
        "grok_voice_low_latency_model": recommended_model,
        "grok_voice_force_reasoning_effort": "low",
        "grok_voice_max_output_tokens": DEFAULT_MAX_TOKENS,
        "grok_voice_max_segment_chars": 180,
        "grok_voice_flush_idle_ms": 1500,
        "note": "保持 grok_voice_low_latency_backend 为空以使用当前后端；如需采用其他推荐后端，请同时配置 backend override 与该后端可用的低延迟模型。",
    }


def _skip_result(candidate: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "backend_profile": candidate["backend_profile"],
        "billing_profile": candidate["billing_profile"],
        "model": candidate["model"],
        "prompt_id": "",
        "ttft_ms": None,
        "first_sentence_ms": None,
        "tts_ms": None,
        "first_voice_ready_ms": None,
        "total_ms": None,
        "chars_out": 0,
        "tokens_out": None,
        "reasoning_effort": "",
        "quality_score": 0.0,
        "success": False,
        "error_type": reason,
    }


def _codex_auth_available(provider: Dict[str, Any]) -> bool:
    path = str(provider.get("auth_file") or conf().get("codex_auth_file") or os.getenv("CODEX_AUTH_FILE") or "").strip()
    if path:
        return Path(path).expanduser().is_file()
    return (Path.home() / ".codex" / "auth.json").is_file()


def _tts_ready(include_tts: bool) -> bool:
    return (not include_tts) or has_xai_credentials()


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_reasoning_effort(value: Any) -> str:
    effort = str(value or "low").strip().lower()
    if effort == "minimal":
        effort = "low"
    return effort if effort in {"low", "medium", "xhigh"} else "low"


def _quality_score(text: str) -> float:
    clean = " ".join(str(text or "").split())
    if not clean:
        return 0.0
    score = 1.0
    length = len(clean)
    if 20 <= length <= 220:
        score += 1.0
    elif length <= 320:
        score += 0.5
    elif length > 800:
        score -= 1.2
    if any(mark in clean for mark in ("。", "，", "；", ".", "!", "！")):
        score += 0.4
    if any(marker in clean for marker in ("首先", "结论", "建议", "可以", "一句话", "直接说")):
        score += 0.3
    if "```" in clean or len(clean.splitlines()) > 4:
        score -= 0.8
    if length > 500:
        score -= 1.0
    return max(0.0, round(score, 3))


def _first_sentence(text: str) -> str:
    value = str(text or "").strip()
    for index, char in enumerate(value):
        if char in "。！？.!?":
            return value[: index + 1].strip()
    return value if 18 <= len(value) <= 160 else ""


def _percentile(values: List[float], percentile: int) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _cost_rank(candidate: Dict[str, Any]) -> int:
    return {"monthly": 0, "quota": 1, "codex": 2}.get(candidate.get("billing_profile"), 9)


def _sanitize_error_type(exc: Any) -> str:
    text = str(exc or exc.__class__.__name__)
    lowered = text.lower()
    for marker in ("authorization", "bearer", "access_token", "refresh_token", "api_key", "cookie"):
        if marker in lowered:
            return "auth_or_secret_redacted_error"
    safe = []
    for ch in lowered:
        safe.append(ch if ch.isalnum() or ch in {"_", "-"} else "_")
    return "".join(safe).strip("_")[:80] or "unknown_error"


def _ms(start: float, end: float) -> int:
    return int(round((end - start) * 1000))


def _write_output(path: str, output: Dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_quietly(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
