#!/usr/bin/env python3
"""Read-only wrapper around the installed OpenClaw taobao/maishou search script."""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


PLATFORMS = {
    "all": [("全平台", "0")],
    "taobao": [("淘宝/天猫", "1")],
    "tmall": [("淘宝/天猫", "1")],
    "jd": [("京东", "2")],
    "jingdong": [("京东", "2")],
    "pdd": [("拼多多", "3")],
    "pinduoduo": [("拼多多", "3")],
    "douyin": [("抖音", "7")],
}

SOURCE_NAMES = {
    "1": "淘宝/天猫",
    "2": "京东",
    "3": "拼多多",
    "4": "苏宁",
    "5": "唯品会",
    "6": "考拉",
    "7": "抖音",
    "8": "快手",
    "10": "1688",
}


@dataclass
class SearchResult:
    platform: str
    title: str
    price: str
    coupon: str
    manual_link: str
    copy_command: str
    reason: str
    risk: str


def installed_taobao_script() -> Path | None:
    home = Path.home()
    candidates = [
        home / ".openclaw" / "workspace" / "skills" / "taobao" / "scripts" / "main.py",
        home / ".openclaw" / "skills" / "taobao" / "scripts" / "main.py",
        home / ".claude" / "skills" / "taobao" / "scripts" / "main.py",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def run_search(script: Path, keyword: str, source: str, limit: int) -> list[dict[str, str]]:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is not available; install or repair the taobao/maishou skill runtime")

    command = [
        uv,
        "run",
        str(script),
        "search",
        f"--source={source}",
        f"--keyword={keyword}",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        command,
        cwd=str(script.parent.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "taobao search failed")

    text = completed.stdout.strip()
    if not text or "," not in text:
        return []

    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        if row.get("title"):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def run_detail(script: Path, row: dict[str, str]) -> tuple[str, str]:
    uv = shutil.which("uv")
    if not uv:
        return "-", "-"
    source = row.get("source")
    goods_id = row.get("goodsId")
    if not source or not goods_id:
        return "-", "-"

    command = [
        uv,
        "run",
        str(script),
        "detail",
        f"--source={source}",
        f"--id={goods_id}",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(
        command,
        cwd=str(script.parent.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        env=env,
    )
    if completed.returncode != 0:
        return "-", "-"

    link = "-"
    copy_command = "-"
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("购买链接:"):
            link = stripped.split(":", 1)[1].strip() or "-"
        elif stripped.startswith("复制口令:"):
            copy_command = stripped.split(":", 1)[1].strip() or "-"
    return link, copy_command


def normalize_result(
    row: dict[str, str],
    fallback_platform: str,
    manual_link: str = "-",
    copy_command: str = "-",
) -> SearchResult:
    source = str(row.get("source") or "").strip()
    platform = SOURCE_NAMES.get(source, fallback_platform)
    actual = row.get("actualPrice") or row.get("originalPrice") or "-"
    coupon = row.get("couponPrice") or "-"
    sales = row.get("monthSales")
    reason_parts = []
    if sales:
        reason_parts.append(f"月销量/热度字段：{sales}")
    if coupon and coupon != "-":
        reason_parts.append(f"返回券额/优惠字段：{coupon}")
    if not reason_parts:
        reason_parts.append("只读搜索结果，可作为候选比价项")
    return SearchResult(
        platform=platform,
        title=row.get("title") or "-",
        price=str(actual),
        coupon=str(coupon),
        manual_link=manual_link,
        copy_command=copy_command,
        reason="；".join(reason_parts),
        risk="以官方 App 实际价格、店铺、售后、运费和优惠券为准",
    )


def render_markdown(keyword: str, results: list[SearchResult], incomplete: str | None = None) -> str:
    lines = ["一、比价结论", ""]
    if results:
        lines.append(f"以下是 `{keyword}` 的只读搜索比价摘要。未打开购买链接，未下单，未加购。")
    else:
        lines.append(f"未能获取 `{keyword}` 的可用只读比价结果。")
    if incomplete:
        lines.extend(["", f"不完整原因：{incomplete}"])

    lines.extend(
        [
            "",
            "二、只读比价表",
            "",
            "| 平台 | 商品标题 | 展示价格 | 优惠/券信息 | 手动链接 | 复制口令 | 推荐理由 | 风险提示 |",
            "|---|---|---:|---|---|---|---|---|",
        ]
    )
    for result in results:
        safe_title = result.title.replace("|", "/")
        link_cell = f"[手动打开]({result.manual_link})" if result.manual_link.startswith(("http://", "https://")) else "-"
        copy_cell = result.copy_command.replace("|", "/") if result.copy_command != "-" else "-"
        lines.append(
            f"| {result.platform} | {safe_title} | {result.price} | {result.coupon} | {link_cell} | {copy_cell} | {result.reason} | {result.risk} |"
        )
    if not results:
        lines.append("| - | - | - | - | - | - | 本地 taobao/maishou 搜索不可用或无结果 | 不使用浏览器 fallback，避免人机验证 |")

    lines.extend(
        [
            "",
            "三、安全提醒",
            "",
            "未自动打开购买链接，未下单，未加购，未填写地址/手机号/验证码/cookie/token。若表格包含链接或口令，它们来自上游详情接口，可能包含分享、推广、返利或邀请码参数，仅供你手动核验。请回淘宝/天猫、京东、拼多多或抖音官方 App 核验价格、优惠、店铺、售后、运费和库存。",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only shopping comparison helper")
    parser.add_argument("keyword", help="Product keyword")
    parser.add_argument("--platform", default="all", choices=sorted(PLATFORMS), help="Platform scope")
    parser.add_argument("--limit", type=int, default=5, help="Max rows per search scope")
    parser.add_argument(
        "--no-links",
        action="store_true",
        help="Do not fetch upstream manual purchase/share links",
    )
    args = parser.parse_args()

    script = installed_taobao_script()
    if not script:
        print(render_markdown(args.keyword, [], "未找到已安装的 OpenClaw taobao/maishou 脚本"))
        return 2

    results: list[SearchResult] = []
    errors = []
    for platform_name, source in PLATFORMS[args.platform]:
        try:
            rows = run_search(script, args.keyword, source, args.limit)
            for row in rows:
                manual_link, copy_command = ("-", "-")
                if not args.no_links:
                    manual_link, copy_command = run_detail(script, row)
                results.append(normalize_result(row, platform_name, manual_link, copy_command))
        except Exception as exc:
            errors.append(f"{platform_name}: {exc}")

    incomplete = "；".join(errors) if errors else None
    print(render_markdown(args.keyword, results[: args.limit], incomplete))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
