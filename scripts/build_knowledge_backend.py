"""Build the optional local knowledge backend from one or more documents.

This script copies source documents into the configured project-local
knowledge_backend data directory before indexing them, so the resulting
SQLite index and original documents can move with the project folder.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)
    sys.path.insert(0, str(project_root))

    from common.log import logger
    from config import load_config
    from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService

    parser = argparse.ArgumentParser(description="Build CowAgent local knowledge backend.")
    parser.add_argument("paths", nargs="+", help="Document files to copy into the backend and index.")
    parser.add_argument("--title", default="", help="Optional title for a single input document.")
    parser.add_argument("--kb-id", default="", help="Override the configured default knowledge-base id.")
    args = parser.parse_args()

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)
    config = KnowledgeBackendConfig.from_project_config()
    if args.kb_id:
        config = KnowledgeBackendConfig.from_mapping(
            {
                **config.__dict__,
                "default_kb_id": args.kb_id,
                "ingest": {
                    "allowed_extensions": config.ingest.allowed_extensions,
                    "allowed_import_roots": [str(path) for path in config.ingest.allowed_import_roots],
                    "max_file_size_mb": config.ingest.max_file_size_mb,
                },
                "vector_store": {
                    "provider": config.vector_store.provider,
                    "url": config.vector_store.url,
                    "collection": config.vector_store.collection,
                    "required": config.vector_store.required,
                },
            }
        )

    if not config.enabled:
        print(json.dumps({"status": "disabled", "message": "knowledge_backend.enabled is false"}, ensure_ascii=False))
        return 2

    results: List[Dict[str, Any]] = []
    with KnowledgeBackendService(config) as service:
        for raw_path in args.paths:
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                results.append({"status": "failed", "path": str(source), "message": "file not found"})
                continue
            title = args.title if args.title and len(args.paths) == 1 else None
            result = service.ingest_upload_bytes(source.name, source.read_bytes(), title=title)
            result["input_path"] = str(source)
            results.append(result)
        _write_manifest(config.data_dir, service)

    print(json.dumps({"status": "success", "results": _jsonable(results)}, ensure_ascii=False, indent=2))
    return 0


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _write_manifest(data_dir: Path, service: Any) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    documents = service.list_documents()
    manifest = {
        "format": "cowagent-knowledge-backend-v1",
        "sqlite_path": "indexes/kb.sqlite",
        "documents": [
            {
                "id": document["id"],
                "title": document["title"],
                "kb_id": document.get("kb_id", ""),
                "version_id": document.get("version_id", ""),
                "source_path": _relative_to_data_dir(data_dir, document["source_path"]),
                "content_hash": document.get("content_hash", ""),
                "status": document.get("status", ""),
            }
            for document in documents
        ],
    }
    (data_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _relative_to_data_dir(data_dir: Path, source_path: str) -> str:
    try:
        return Path(source_path).resolve().relative_to(data_dir.resolve()).as_posix()
    except Exception:
        return Path(source_path).name


if __name__ == "__main__":
    raise SystemExit(main())
