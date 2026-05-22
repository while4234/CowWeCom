"""Export structured backend knowledge into the Web-visible Markdown library."""

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
        description="Export indexed knowledge_backend documents into the Markdown knowledge library."
    )
    parser.add_argument("--document-id", default="", help="Export one document; default exports all indexed documents.")
    parser.add_argument(
        "--document-library-root",
        default="",
        help="Override export root. Default comes from knowledge_backend.ingest.document_library_root or ~/cow.",
    )
    args = parser.parse_args()

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)

    config = KnowledgeBackendConfig.from_project_config()
    if args.document_library_root:
        config = _with_document_library_root(config, args.document_library_root)

    with KnowledgeBackendService(config) as service:
        result = service.export_document_library(document_id=args.document_id)

    print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


def _with_document_library_root(config: Any, root: str) -> Any:
    from agent.knowledge.backend import KnowledgeBackendConfig

    return KnowledgeBackendConfig.from_mapping(
        {
            **config.__dict__,
            "ingest": {
                "allowed_extensions": config.ingest.allowed_extensions,
                "allowed_import_roots": [str(path) for path in config.ingest.allowed_import_roots],
                "max_file_size_mb": config.ingest.max_file_size_mb,
                "document_library_root": root,
            },
            "vector_store": {
                "provider": config.vector_store.provider,
                "url": config.vector_store.url,
                "collection": config.vector_store.collection,
                "required": config.vector_store.required,
            },
        }
    )


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
