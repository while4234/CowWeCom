# encoding:utf-8

"""Select random Grok image prompt fragments or build a random Grok prompt."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def _ensure_project_root_on_path() -> None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "common" / "prompt_optimization_repository.py").is_file():
            sys.path.insert(0, str(parent))
            return


def main(argv: list[str] | None = None) -> int:
    _ensure_project_root_on_path()
    from common.prompt_optimization_repository import select_grok_prompt_fragments

    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--repositories-root", default="")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--generate-prompt",
        action="store_true",
        help="Use Grok to produce the final English prompt and Chinese translation.",
    )
    args = parser.parse_args(argv)

    if args.generate_prompt:
        from common.grok_image_prompt_rewriter import build_grok_random_image_prompt

        result = build_grok_random_image_prompt(args.prompt, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    rng = random.Random(args.seed) if args.seed is not None else None
    result = select_grok_prompt_fragments(
        args.prompt,
        repositories_root=args.repositories_root or None,
        limit=args.limit,
        rng=rng,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
