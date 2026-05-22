"""Generate optional LLM study documents from indexed knowledge backend chunks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)
    sys.path.insert(0, str(project_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from common.log import logger
    from config import load_config
    from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService

    parser = argparse.ArgumentParser(
        description="Generate a source-span-validated LLM study page for an indexed protocol document."
    )
    parser.add_argument("--document-id", default="", help="Source document ID. Default uses the newest non-derived doc.")
    parser.add_argument("--max-chunks", type=int, default=0, help="Maximum source chunks to send to the LLM.")
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Write the Markdown study page but do not index it back into the backend search store.",
    )
    args = parser.parse_args()

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)

    config = KnowledgeBackendConfig.from_project_config()
    with KnowledgeBackendService(config) as service:
        result = service.generate_llm_study_document(
            document_id=args.document_id,
            index_generated_document=not args.no_index,
            max_chunks=args.max_chunks or None,
        )

    print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
