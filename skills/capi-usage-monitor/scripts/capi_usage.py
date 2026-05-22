#!/usr/bin/env python3
"""CAPI/Codex usage monitor for omilg.com frontend backend.

Local storage only for snapshots. API key/card is never persisted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "https://deepl.micosoft.icu"
DEFAULT_DAILY_QUOTA = 90.0
DEFAULT_CHATLOG_SUMMARY_PAGE_SIZE = 100
MAX_CHATLOG_SUMMARY_PAGES = 200
USER_AGENT = "cow-capi-usage-monitor/1.0"


def redact_secret(text: str, secret: str | None = None) -> str:
    if not text:
        return text
    out = text
    if secret:
        out = out.replace(secret, f"{secret[:3]}***{secret[-3:]}" if len(secret) >= 8 else "***")
    for key in [os.getenv("CAPI_API_KEY"), os.getenv("CAPI_ACTIVATION_CODE"), os.getenv("CAPI_CARD"), os.getenv("OPENAI_API_KEY")]:
        if key:
            out = out.replace(key, f"{key[:3]}***{key[-3:]}" if len(key) >= 8 else "***")
    return out


def safe_account(value: object, api_key: str | None = None) -> object:
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    secrets = [api_key, os.getenv("CAPI_API_KEY"), os.getenv("CAPI_ACTIVATION_CODE"), os.getenv("CAPI_CARD"), os.getenv("OPENAI_API_KEY")]
    for secret in secrets:
        if secret and text == secret:
            return f"{secret[:6]}***{secret[-4:]}" if len(secret) >= 12 else "***"
    # UUID-like activation codes are also sensitive enough to mask.
    if len(text) >= 24 and text.count('-') >= 3:
        return f"{text[:6]}***{text[-4:]}"
    return text


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def workspace_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] if len(here.parents) >= 4 else Path.cwd().resolve()


def data_dir(args: argparse.Namespace) -> Path:
    raw = args.data_dir or os.getenv("CAPI_USAGE_DATA_DIR")
    path = Path(raw).expanduser() if raw else workspace_root() / "data" / "capi-usage-monitor"
    path.mkdir(parents=True, exist_ok=True)
    (path / "snapshots").mkdir(parents=True, exist_ok=True)
    return path.resolve()


def api_key_from_args(args: argparse.Namespace) -> str:
    key = args.api_key
    key_env = getattr(args, "api_key_env", None)
    if not key and key_env:
        key = os.getenv(key_env)
    if not key:
        key = os.getenv("CAPI_API_KEY") or os.getenv("CAPI_ACTIVATION_CODE") or os.getenv("CAPI_CARD") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit("Error: missing CAPI API key/card. Pass --api-key, --api-key-env, or set CAPI_API_KEY/CAPI_ACTIVATION_CODE/OPENAI_API_KEY.")
    return key.strip()


def to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def key_suffix(api_key: str) -> str:
    return api_key[-6:] if len(api_key) >= 6 else "*" * len(api_key)


def http_json(method: str, url: str, body: Any | None = None, token: str | None = None, timeout: int = 30) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["x-auth-token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {raw[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error {url}: {e}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]}")
    if isinstance(payload, dict) and payload.get("code"):
        raise RuntimeError(str(payload.get("msg") or payload.get("message") or payload))
    return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def api_base(args: argparse.Namespace) -> str:
    return (args.api_base or os.getenv("CAPI_USAGE_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def default_daily_quota(args: argparse.Namespace | None = None) -> float:
    arg_value = getattr(args, "default_daily_quota", None) if args is not None else None
    if arg_value is not None:
        return to_float(arg_value, DEFAULT_DAILY_QUOTA)
    env_value = os.getenv("CAPI_USAGE_DEFAULT_DAILY_QUOTA")
    if env_value:
        return to_float(env_value, DEFAULT_DAILY_QUOTA)
    return DEFAULT_DAILY_QUOTA


def login(args: argparse.Namespace, api_key: str) -> dict[str, Any]:
    base = api_base(args)
    data = http_json("POST", f"{base}/api/users/card-login", {"card": api_key, "agent": args.agent})
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError(f"Login response missing token: {data}")
    return data


def whoami(args: argparse.Namespace, token: str) -> dict[str, Any]:
    data = http_json("GET", f"{api_base(args)}/api/users/whoami", token=token)
    return data if isinstance(data, dict) else {"raw": data}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return datetime.fromisoformat(v).replace(tzinfo=datetime.now().astimezone().tzinfo)
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def datetime_from_any(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1_000_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=datetime.now().astimezone().tzinfo)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime_from_any(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def range_from_args(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    now = datetime.now().astimezone()
    start = parse_dt(args.start)
    end = parse_dt(args.end)
    if args.period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif args.period == "yesterday":
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
    elif args.period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = None
    return start, end


def filters(start: datetime | None, end: datetime | None) -> dict[str, int]:
    obj: dict[str, int] = {}
    if start:
        obj["create_at>"] = int(start.timestamp() * 1000)
    if end:
        obj["create_at<"] = int(end.timestamp() * 1000)
    return obj


def first_positive_float(*values: object, fallback: float = 0.0) -> float:
    for value in values:
        number = to_float(value)
        if number > 0:
            return number
    return fallback


def quota_summary(user: dict[str, Any], default_daily: float = DEFAULT_DAILY_QUOTA) -> dict[str, Any]:
    vip = user.get("vip") if isinstance(user.get("vip"), dict) else None
    if not vip:
        return {
            "total": 0,
            "used": 0,
            "remaining": 0,
            "progress": 0,
            "daily": 0,
            "total_mode": False,
            "expire_at": None,
            "status": status_summary(user),
        }
    total_quota = to_float(vip.get("score"))
    total_mode = total_quota > 0
    daily = first_positive_float(vip.get("day_score"), user.get("day_score"), fallback=default_daily)
    total = total_quota if total_mode else daily
    used = to_float(user.get("score_used") if total_mode else user.get("day_score_used"))
    remaining = max(total - used, 0)
    progress = round((used / total * 100), 1) if total > 0 else 0
    return {
        "total": total,
        "used": used,
        "remaining": remaining,
        "progress": progress,
        "daily": daily,
        "mode": "total" if total_mode else "daily",
        "total_mode": total_mode,
        "expire_at": vip.get("expire_at"),
        "status": status_summary(user),
    }


def status_summary(user: dict[str, Any]) -> dict[str, Any] | None:
    vip = user.get("vip") if isinstance(user.get("vip"), dict) else None
    if vip:
        expire_at = vip.get("expire_at")
        expire_dt = datetime_from_any(expire_at)
        if expire_dt and expire_dt.timestamp() < time.time():
            return {"label": "已过期", "kind": "expired"}
        return None
    score = float(user.get("score") or 0)
    day_score = float(user.get("day_score") or 0)
    day_score_date = str(user.get("day_score_date") or "")
    today = datetime.now().date().isoformat()
    if score > 0 or day_score > 0:
        return {"label": "已销毁", "kind": "destroyed"}
    if day_score_date and day_score_date < today:
        return {"label": "已过期", "kind": "expired"}
    if not day_score_date and score == 0 and day_score == 0:
        return {"label": "已失效", "kind": "invalid"}
    return None


def usages(args: argparse.Namespace, token: str, start: datetime | None, end: datetime | None) -> list[dict[str, Any]]:
    data = http_json("POST", f"{api_base(args)}/api/chatgpt/usages", filters(start, end), token=token)
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return data["list"]
    if isinstance(data, list):
        return data
    return []


def clamp_page_size(page_size: int) -> int:
    return min(100, max(1, int(page_size)))


def chatlog_page(args: argparse.Namespace, token: str, start: datetime | None, end: datetime | None, page: int, page_size: int) -> dict[str, Any]:
    body = {
        "page": page,
        "pageSize": clamp_page_size(page_size),
        "sortBy": args.sort_by,
        "desc": 1 if args.desc else 0,
        **filters(start, end),
    }
    data = http_json("POST", f"{api_base(args)}/api/chatgpt/chatlog", body, token=token)
    return data if isinstance(data, dict) else {"list": [], "total": 0, "raw": data}


def chatlog_items(
    args: argparse.Namespace,
    token: str,
    start: datetime | None,
    end: datetime | None,
    page_size: int = DEFAULT_CHATLOG_SUMMARY_PAGE_SIZE,
    max_pages: int = MAX_CHATLOG_SUMMARY_PAGES,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    total: int | None = None
    pages = 0
    complete = False
    for page in range(1, max_pages + 1):
        data = chatlog_page(args, token, start, end, page, page_size)
        page_items = data.get("list") if isinstance(data.get("list"), list) else []
        if total is None:
            total_value = data.get("total")
            total = int(total_value) if isinstance(total_value, int) or str(total_value).isdigit() else None
        pages = page
        if not page_items:
            complete = True
            break
        items.extend(page_items)
        if total is not None and len(items) >= total:
            complete = True
            break
    if total is not None and len(items) > total:
        items = items[:total]
    return {"items": items, "total": total if total is not None else len(items), "pages": pages, "complete": complete}


def summarize_usage_items(items: list[dict[str, Any]], source: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    by_model: dict[str, float] = {}
    total_cost = 0.0
    for item in items:
        model = str(item.get("model") or "unknown")
        score = to_float(item.get("score"))
        by_model[model] = by_model.get(model, 0.0) + score
        total_cost += score
    summary = {"source": source, "entries": len(items), "total_cost": total_cost, "by_model": by_model}
    if extra:
        summary.update(extra)
    return summary


def usage_summary(args: argparse.Namespace, token: str, start: datetime | None, end: datetime | None) -> tuple[dict[str, Any], int | None]:
    if not args.include_usage:
        return summarize_usage_items([], "disabled"), None

    source = args.usage_source
    if source == "auto":
        # The backend usages endpoint can return historical buckets even when
        # create_at filters are supplied. Chatlog rows honor the time window.
        source = "chatlog" if start or end else "usages"

    if source == "chatlog":
        chatlog = chatlog_items(
            args,
            token,
            start,
            end,
            page_size=args.summary_page_size,
            max_pages=args.max_summary_pages,
        )
        extra = {
            "chatlog_total": chatlog["total"],
            "pages": chatlog["pages"],
            "complete": chatlog["complete"],
        }
        return summarize_usage_items(chatlog["items"], "chatlog", extra), chatlog["total"]

    usage_items = usages(args, token, start, end)
    return summarize_usage_items(usage_items, "usages"), None


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    api_key = api_key_from_args(args)
    kh = key_hash(api_key)
    login_data = login(args, api_key)
    token = str(login_data["token"])
    user = whoami(args, token)
    start, end = range_from_args(args)
    summary, summary_chatlog_total = usage_summary(args, token, start, end)
    chatlog = chatlog_page(args, token, start, end, args.page, args.page_size) if args.include_chatlog else None
    chatlog_total = summary_chatlog_total
    if chatlog_total is None and isinstance(chatlog, dict):
        chatlog_total = chatlog.get("total")
    result = {
        "ok": True,
        "captured_at": now_iso(),
        "api_base": api_base(args),
        "site_url": "https://omilg.com/dhh8888/login",
        "key_hash": kh,
        "key_suffix": key_suffix(api_key),
        "account": safe_account(user.get("account") or login_data.get("account"), api_key),
        "user_id": user.get("id") or user.get("user_id"),
        "quota": quota_summary(user, default_daily_quota(args)),
        "period": args.period,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "usage_summary": summary,
        "raw_user": user if args.raw else None,
        "chatlog_total": chatlog_total,
    }
    if args.raw and chatlog is not None:
        result["raw_chatlog_page"] = chatlog
    result = {k: v for k, v in result.items() if v is not None}
    if args.save:
        save_snapshot(data_dir(args), kh, result)
    return result


def save_snapshot(base: Path, kh: str, snap: dict[str, Any]) -> Path:
    day = datetime.now().date().isoformat()
    path = base / "snapshots" / f"{kh}-{day}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n")
    latest = base / f"latest-{kh}.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, latest)
    return path


def load_snapshots(base: Path, kh: str | None = None) -> list[dict[str, Any]]:
    files = sorted((base / "snapshots").glob(f"{kh}-*.jsonl" if kh else "*.jsonl"))
    out: list[dict[str, Any]] = []
    for path in files:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def latest_snapshot_path(base: Path, kh: str | None = None) -> Path | None:
    if kh:
        path = base / f"latest-{kh}.json"
        return path if path.exists() else None
    candidates = sorted(base.glob("latest-*.json"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
    return candidates[0] if candidates else None


def cmd_latest(args: argparse.Namespace) -> None:
    base = data_dir(args)
    kh = None
    if args.api_key or getattr(args, "api_key_env", None) or os.getenv("CAPI_API_KEY") or os.getenv("CAPI_ACTIVATION_CODE") or os.getenv("CAPI_CARD"):
        kh = key_hash(api_key_from_args(args))
    path = latest_snapshot_path(base, kh)
    if not path:
        print(json.dumps({"ok": True, "found": False, "message": "No latest snapshot found.", "data_dir": str(base)}, ensure_ascii=False, indent=2))
        return
    print(path.read_text(encoding="utf-8"))


def cmd_doctor(args: argparse.Namespace) -> None:
    checks = []
    base = data_dir(args)
    checks.append({"name": "data_dir", "ok": base.exists(), "path": str(base)})
    has_key = bool(args.api_key or getattr(args, "api_key_env", None) and os.getenv(args.api_key_env) or os.getenv("CAPI_API_KEY") or os.getenv("CAPI_ACTIVATION_CODE") or os.getenv("CAPI_CARD") or os.getenv("OPENAI_API_KEY"))
    checks.append({"name": "api_key_configured", "ok": has_key, "hint": "Set CAPI_API_KEY/OPENAI_API_KEY or pass --api-key/--api-key-env"})
    checks.append({"name": "api_base", "ok": True, "value": api_base(args)})
    if args.online:
        try:
            api_key = api_key_from_args(args)
            ld = login(args, api_key)
            user = whoami(args, str(ld["token"]))
            checks.append({"name": "online_login_and_whoami", "ok": True, "key_hash": key_hash(api_key), "account": safe_account(user.get("account"), api_key)})
        except Exception as e:
            checks.append({"name": "online_login_and_whoami", "ok": False, "error": redact_secret(str(e))})
    print(json.dumps({"ok": all(c.get("ok") for c in checks), "checks": checks}, ensure_ascii=False, indent=2))


def cmd_snapshot(args: argparse.Namespace) -> None:
    print(json.dumps(snapshot(args), ensure_ascii=False, indent=2))


def cmd_history(args: argparse.Namespace) -> None:
    kh = None
    if args.api_key or os.getenv("CAPI_API_KEY") or os.getenv("CAPI_ACTIVATION_CODE") or os.getenv("CAPI_CARD"):
        kh = key_hash(api_key_from_args(args))
    items = load_snapshots(data_dir(args), kh)
    if args.limit:
        items = items[-args.limit:]
    print(json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2))


def cmd_export_csv(args: argparse.Namespace) -> None:
    kh = None
    if args.api_key or os.getenv("CAPI_API_KEY") or os.getenv("CAPI_ACTIVATION_CODE") or os.getenv("CAPI_CARD"):
        kh = key_hash(api_key_from_args(args))
    items = load_snapshots(data_dir(args), kh)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["captured_at", "key_hash", "account", "total", "used", "remaining", "progress", "period", "usage_total_cost"])
        writer.writeheader()
        for it in items:
            q = it.get("quota") or {}
            us = it.get("usage_summary") or {}
            writer.writerow({
                "captured_at": it.get("captured_at"),
                "key_hash": it.get("key_hash"),
                "account": it.get("account"),
                "total": q.get("total"),
                "used": q.get("used"),
                "remaining": q.get("remaining"),
                "progress": q.get("progress"),
                "period": it.get("period"),
                "usage_total_cost": us.get("total_cost"),
            })
    print(json.dumps({"ok": True, "output": str(output), "rows": len(items)}, ensure_ascii=False, indent=2))


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key", help="CAPI activation code/API key. Prefer env CAPI_API_KEY. Never persisted.")
    parser.add_argument("--api-key-env", help="Read API key/card from this environment variable name")
    parser.add_argument("--api-base", help=f"Backend API base. Default: {DEFAULT_API_BASE}")
    parser.add_argument("--data-dir", help="Local snapshot directory. Default: <workspace>/data/capi-usage-monitor")
    parser.add_argument("--default-daily-quota", type=float, default=None, help=f"Daily quota fallback when backend returns 0. Default: {DEFAULT_DAILY_QUOTA}")
    parser.add_argument("--agent", default="main")


def add_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--period", choices=["all", "today", "yesterday", "month"], default="today")
    parser.add_argument("--start", help="ISO/date start; overrides period if set unless period is not all")
    parser.add_argument("--end", help="ISO/date end")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CAPI usage/quota monitor")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot", help="Query current quota/usage and optionally save local snapshot")
    add_common(s); add_range(s)
    s.add_argument("--save", action="store_true", help="Append snapshot to local JSONL and latest JSON")
    s.add_argument("--raw", action="store_true", help="Include raw user/chatlog payloads. Avoid in routine logs.")
    s.add_argument("--include-usage", action="store_true", default=True)
    s.add_argument("--no-usage", dest="include_usage", action="store_false")
    s.add_argument("--usage-source", choices=["auto", "chatlog", "usages"], default="auto", help="Use chatlog for filtered periods by default; usages is a backend aggregate debug source.")
    s.add_argument("--summary-page-size", type=int, default=DEFAULT_CHATLOG_SUMMARY_PAGE_SIZE, help="Chatlog page size for period summary.")
    s.add_argument("--max-summary-pages", type=int, default=MAX_CHATLOG_SUMMARY_PAGES, help="Maximum chatlog pages to read for period summary.")
    s.add_argument("--include-chatlog", action="store_true")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--page-size", type=int, default=10)
    s.add_argument("--sort-by", default="create_at")
    s.add_argument("--desc", action="store_true", default=True)
    s.set_defaults(func=cmd_snapshot)

    latest = sub.add_parser("latest", help="Read latest local snapshot")
    add_common(latest)
    latest.set_defaults(func=cmd_latest)

    doctor = sub.add_parser("doctor", help="Diagnose local config and optionally test online login")
    add_common(doctor)
    doctor.add_argument("--online", action="store_true", help="Attempt login/whoami with configured key")
    doctor.set_defaults(func=cmd_doctor)

    h = sub.add_parser("history", help="Read local saved snapshots")
    add_common(h)
    h.add_argument("--limit", type=int)
    h.set_defaults(func=cmd_history)

    e = sub.add_parser("export-csv", help="Export local saved snapshots to CSV")
    add_common(e)
    e.add_argument("--output", required=True)
    e.set_defaults(func=cmd_export_csv)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": redact_secret(str(e))}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
