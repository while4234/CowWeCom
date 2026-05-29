# encoding:utf-8

"""Select random prompt fragments from the image-prompt-optimization skill."""

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
    args = parser.parse_args(argv)

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
