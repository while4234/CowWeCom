from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.codex_quota_logic import decide_codex_auto_switch, decision_to_dict  # noqa: E402


def _prepare_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    return env


def _check_script_path() -> Path:
    return Path(__file__).resolve().parent / "check_codex_quota.py"


def _run_quota_query(output_format: str, timeout_seconds: int) -> str:
    script = _check_script_path()
    if not script.exists():
        raise RuntimeError("Codex quota query script was not found.")
    timeout_ms = max(1000, int(timeout_seconds * 1000))
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--project-dir",
            str(PROJECT_ROOT),
            "--format",
            output_format,
            "--timeout-ms",
            str(timeout_ms),
        ],
        cwd=str(PROJECT_ROOT),
        env=_prepare_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(text or f"Codex quota query failed with exit code {proc.returncode}.")
    return (proc.stdout or "").strip()


def _snapshot(args) -> int:
    try:
        text = _run_quota_query(args.format, args.timeout_seconds)
        if args.save and args.format == "json":
            _save_snapshot(json.loads(text))
        print(text or "{}" if args.format == "json" else text or "Codex quota query returned no content.")
        return 0
    except subprocess.TimeoutExpired:
        print(f"Codex quota query timed out after {args.timeout_seconds} seconds.", file=sys.stderr)
        return 124
    except Exception as e:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"Codex quota query failed: {e}", file=sys.stderr)
        return 1


def _decision(args) -> int:
    try:
        raw = _run_quota_query("json", args.timeout_seconds)
        payload = json.loads(raw)
        decision = decide_codex_auto_switch(
            payload,
            fair_share_days=args.fair_share_days,
            min_remaining_percent=args.min_remaining_percent,
        )
        result = {
            "ok": True,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "decision": decision_to_dict(decision),
            "account": payload.get("account", {}),
        }
        if args.format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(_format_decision_text(result))
        return 0
    except subprocess.TimeoutExpired:
        print(f"Codex quota decision timed out after {args.timeout_seconds} seconds.", file=sys.stderr)
        return 124
    except Exception as e:
        if args.format == "json":
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"Codex quota decision failed: {e}", file=sys.stderr)
        return 1


def _format_decision_text(result: Dict[str, Any]) -> str:
    decision = result.get("decision", {})
    window = decision.get("window", {})
    return "\n".join([
        "Codex auto-switch decision",
        f"- should_switch: {decision.get('should_switch')}",
        f"- reason: {decision.get('reason')}",
        f"- used_percent: {window.get('used_percent', '')}",
        f"- allowed_used_percent: {decision.get('allowed_used_percent', '')}",
        f"- completed_days: {decision.get('completed_days', '')}",
    ])


def _save_snapshot(payload: Dict[str, Any]) -> None:
    path = PROJECT_ROOT / "data" / "codex-quota-query" / "snapshots.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Query GPT/Codex quota without opening a browser.")
    sub = parser.add_subparsers(dest="command")

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--format", choices=["text", "json"], default="text")
    snapshot.add_argument("--timeout-seconds", type=int, default=90)
    snapshot.add_argument("--save", action="store_true")
    snapshot.set_defaults(func=_snapshot)

    decision = sub.add_parser("decision")
    decision.add_argument("--format", choices=["text", "json"], default="text")
    decision.add_argument("--timeout-seconds", type=int, default=90)
    decision.add_argument("--fair-share-days", type=int, default=7)
    decision.add_argument("--min-remaining-percent", type=float, default=15.0)
    decision.set_defaults(func=_decision)

    args = parser.parse_args()
    if not args.command:
        args = parser.parse_args(["snapshot"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
