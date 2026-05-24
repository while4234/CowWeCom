# encoding:utf-8

"""Benchmark same-backend reasoning-effort classifier candidates.

This script does not change config files. It prints the single recommended
runtime classifier config after testing candidates on the active backend.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.protocol.models import LLMRequest
from bridge.agent_bridge import AgentLLMModel
from common.llm_backend_router import BACKEND_CAPI, BACKEND_CODEX, get_current_backend, get_effective_model
from common.reasoning_effort_policy import benchmark_samples, rank_mini_models
from config import conf, load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", help="Optional JSONL file with {label,prompt} rows")
    parser.add_argument("--max-samples", type=int, default=0, help="Limit samples for a smoke run")
    parser.add_argument("--timeout-ms", type=int, default=700)
    args = parser.parse_args()

    load_config()
    backend = get_current_backend()
    main_model = get_effective_model()
    samples = _load_samples(args.samples)
    if args.max_samples:
        samples = _limit_samples_balanced(samples, max(1, args.max_samples))

    model_adapter = AgentLLMModel(None)
    candidates = _build_candidates(model_adapter, backend, main_model)
    if not candidates:
        print(json.dumps({
            "backend": backend,
            "main_model": main_model,
            "selected": None,
            "reason": "no_candidates",
        }, ensure_ascii=False, indent=2))
        return 1

    results = []
    for candidate in candidates:
        results.append(_run_candidate(model_adapter, candidate, samples, args.timeout_ms))

    selected = _select_result(results, args.timeout_ms)
    print(json.dumps({
        "backend": backend,
        "main_model": main_model,
        "samples": len(samples),
        "results": results,
        "selected": selected,
        "recommended_config": _recommended_config(backend, selected),
    }, ensure_ascii=False, indent=2))
    return 0 if selected else 2


def _load_samples(path: str = "") -> List[Dict[str, str]]:
    if not path:
        return benchmark_samples()
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            rows.append({"label": str(item["label"]), "prompt": str(item["prompt"])})
    return rows


def _limit_samples_balanced(samples: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    if len(samples) <= limit:
        return samples
    labels = sorted({sample["label"] for sample in samples})
    selected: List[Dict[str, str]] = []
    buckets = {label: [sample for sample in samples if sample["label"] == label] for label in labels}
    while len(selected) < limit and any(buckets.values()):
        for label in labels:
            if buckets[label] and len(selected) < limit:
                selected.append(buckets[label].pop(0))
    return selected


def _build_candidates(model_adapter: AgentLLMModel, backend: str, main_model: str) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    mini_model = _resolve_latest_mini(model_adapter, backend)
    if mini_model:
        candidates.append({
            "name": "latest_mini",
            "backend": backend,
            "model": mini_model,
            "reasoning_effort": "",
        })
    for effort in ("none", "low"):
        candidates.append({
            "name": f"same_model_{effort}",
            "backend": backend,
            "model": main_model,
            "reasoning_effort": effort,
        })
    return candidates


def _resolve_latest_mini(model_adapter: AgentLLMModel, backend: str) -> str:
    if backend == BACKEND_CAPI:
        discovered = _discover_capi_models(model_adapter)
        ranked = rank_mini_models(discovered)
        if ranked:
            return ranked[0]
        configured = conf().get("reasoning_effort_policy_capi_mini_model_candidates") or [
            "gpt-5-mini",
            "gpt-5.4-mini",
            "gpt-4.1-mini",
        ]
        ranked = rank_mini_models(configured)
        return ranked[0] if ranked else ""
    if backend == BACKEND_CODEX:
        configured = conf().get("reasoning_effort_policy_codex_mini_model_candidates") or [
            "gpt-5.1-codex-mini",
            "gpt-5-mini",
        ]
        ranked = rank_mini_models(configured)
        return ranked[0] if ranked else ""
    return ""


def _discover_capi_models(model_adapter: AgentLLMModel) -> List[str]:
    try:
        config = model_adapter.bot.get_api_config()
        api_base = str(config.get("api_base") or "").rstrip("/")
        api_key = str(config.get("api_key") or "")
        if not api_base:
            return []
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        response = requests.get(f"{api_base}/models", headers=headers, timeout=10)
        if response.status_code >= 400:
            return []
        data = response.json()
        return [str(item.get("id") or "") for item in data.get("data") or [] if isinstance(item, dict)]
    except Exception:
        return []


def _run_candidate(
    model_adapter: AgentLLMModel,
    candidate: Dict[str, str],
    samples: List[Dict[str, str]],
    timeout_ms: int,
) -> Dict[str, Any]:
    latencies = []
    errors = 0
    timeout_count = 0
    false_medium = 0
    correct = 0
    total_tokens = 0
    predictions = []
    for sample in samples:
        started = time.perf_counter()
        predicted = ""
        try:
            response = model_adapter.call(_request(candidate, sample["prompt"], timeout_ms))
            predicted = _parse_response(response)
            usage = response.get("usage") if isinstance(response, dict) else {}
            total_tokens += int((usage or {}).get("total_tokens") or 0)
        except Exception as exc:
            predicted = f"error:{type(exc).__name__}"
        latency_ms = int((time.perf_counter() - started) * 1000)
        latencies.append(latency_ms)
        if latency_ms > timeout_ms:
            timeout_count += 1
        if predicted == "invalid" or predicted.startswith("error"):
            errors += 1
        label = sample["label"]
        if label == predicted:
            correct += 1
        if label == "xhigh" and predicted == "medium":
            false_medium += 1
        predictions.append({"label": label, "predicted": predicted, "latency_ms": latency_ms})
    return {
        **candidate,
        "samples": len(samples),
        "accuracy": correct / len(samples) if samples else 0,
        "false_medium": false_medium,
        "errors": errors,
        "timeout_count": timeout_count,
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "avg_total_tokens": total_tokens / len(samples) if samples else 0,
        "predictions": predictions,
    }


def _request(candidate: Dict[str, str], prompt: str, timeout_ms: int) -> LLMRequest:
    system = (
        "Classify the user's task for reasoning depth. Return only JSON: "
        '{"effort":"medium"|"xhigh"}. Use xhigh for coding, debugging, repository/file/system '
        "operations, tools, multi-step planning, security, permissions, deletion, high-risk advice, "
        "or quality-first requests. Use medium only for clearly simple IM/chat, short rewrite, "
        "short translation, or simple explanation."
    )
    request = LLMRequest(
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        model=candidate["model"],
        system=system,
        temperature=0,
        max_tokens=80,
        stream=False,
        request_timeout=max(timeout_ms / 1000.0, 0.2),
    )
    if candidate.get("reasoning_effort"):
        request.reasoning_effort = candidate["reasoning_effort"]
    else:
        request.reasoning_effort_locked = True
    return request


def _parse_response(response: Any) -> str:
    if not isinstance(response, dict) or response.get("error"):
        return "error"
    content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    raw = str(content or "").strip()
    try:
        parsed = json.loads(raw)
        effort = str(parsed.get("effort") or "").lower()
        return effort if effort in {"medium", "xhigh"} else "invalid"
    except Exception:
        lowered = raw.lower()
        if "xhigh" in lowered:
            return "xhigh"
        if "medium" in lowered:
            return "medium"
        return "invalid"


def _select_result(results: List[Dict[str, Any]], timeout_ms: int) -> Dict[str, Any]:
    eligible = [
        result for result in results
        if result["false_medium"] == 0
        and result["errors"] == 0
        and result["timeout_count"] == 0
        and result["p95_latency_ms"] <= timeout_ms
    ]
    if not eligible:
        return {}
    eligible.sort(key=lambda item: (item["p95_latency_ms"], item["avg_total_tokens"]))
    return {
        "name": eligible[0]["name"],
        "backend": eligible[0]["backend"],
        "model": eligible[0]["model"],
        "reasoning_effort": eligible[0]["reasoning_effort"],
        "p95_latency_ms": eligible[0]["p95_latency_ms"],
        "accuracy": eligible[0]["accuracy"],
    }


def _recommended_config(backend: str, selected: Dict[str, Any]) -> Dict[str, Any]:
    if not selected:
        return {
            "reasoning_effort_policy_classifier_selected_backend": "",
            "reasoning_effort_policy_classifier_model": "",
            "reasoning_effort_policy_classifier_reasoning_effort": "",
        }
    return {
        "reasoning_effort_policy_classifier_selected_backend": backend,
        "reasoning_effort_policy_classifier_model": selected["model"],
        "reasoning_effort_policy_classifier_reasoning_effort": selected.get("reasoning_effort", ""),
    }


def _percentile(values: Iterable[int], pct: int) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    if len(ordered) == 1:
        return ordered[0]
    return int(statistics.quantiles(ordered, n=100, method="inclusive")[pct - 1])


if __name__ == "__main__":
    raise SystemExit(main())
