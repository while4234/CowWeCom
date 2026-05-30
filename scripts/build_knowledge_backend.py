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
from dataclasses import asdict, is_dataclass
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
    parser.add_argument("paths", nargs="*", help="Document files to copy into the backend and index.")
    parser.add_argument("--title", default="", help="Optional title for a single input document.")
    parser.add_argument("--kb-id", default="", help="Override the configured default knowledge-base id.")
    parser.add_argument("--document-id", default="", help="Existing document id for legacy visual repair.")
    parser.add_argument("--all", action="store_true", help="Apply legacy repair command to all source documents.")
    parser.add_argument("--visual", action="store_true", help="Run visual knowledge completion after each ingest.")
    parser.add_argument("--visual-analysis-backend", default="current", help="Visual analysis backend, default: current.")
    parser.add_argument("--visual-force-prepare", action="store_true", help="Reset and rescan visual prepare state first.")
    parser.add_argument("--visual-max-steps", type=int, default=5000, help="Maximum visual build steps per document.")
    parser.add_argument("--no-export", action="store_true", help="Skip export after visual completion.")
    parser.add_argument(
        "--repair-legacy-visual-pollution",
        action="store_true",
        help="Complete visual chunks, then dry-run/apply visual-gated legacy ordinary chunk pollution repair.",
    )
    parser.add_argument("--repair-dry-run", action="store_true", help="Preview legacy visual pollution repair. This is the default.")
    parser.add_argument("--repair-apply", action="store_true", help="Write legacy visual pollution repair changes to SQLite.")
    parser.add_argument(
        "--strip-completed-visual-regions",
        action="store_true",
        help="Allow stripping ordinary chunk pollution only on pages covered by high-confidence retrievable visual chunks.",
    )
    parser.add_argument(
        "--rebuild-text-chunks",
        action="store_true",
        help="Re-extract and rebuild ordinary chunks during legacy repair while preserving visual chunks.",
    )
    args = parser.parse_args()
    if not args.paths and not args.repair_legacy_visual_pollution:
        parser.error("paths are required unless --repair-legacy-visual-pollution is used")
    if args.repair_apply and args.repair_dry_run:
        parser.error("--repair-apply and --repair-dry-run cannot be used together")
    if args.repair_legacy_visual_pollution and not (args.document_id or args.kb_id or args.all):
        parser.error("--repair-legacy-visual-pollution requires --document-id, --kb-id, or --all")

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)
    config = KnowledgeBackendConfig.from_project_config()
    if args.kb_id:
        config_mapping = _config_to_mapping(config)
        config_mapping["default_kb_id"] = args.kb_id
        config = KnowledgeBackendConfig.from_mapping(config_mapping)

    if not config.enabled:
        print(json.dumps({"status": "disabled", "message": "knowledge_backend.enabled is false"}, ensure_ascii=False))
        return 2

    results: List[Dict[str, Any]] = []
    with KnowledgeBackendService(config) as service:
        if args.repair_legacy_visual_pollution:
            repair_result = service.complete_and_repair_legacy_visual_knowledge(
                document_id=args.document_id or None,
                kb_id=(args.kb_id or None) if not args.document_id else None,
                analysis_backend=args.visual_analysis_backend or "current",
                force_prepare=args.visual_force_prepare,
                dry_run=not args.repair_apply,
                apply=bool(args.repair_apply),
                strip_completed_visual_regions=bool(args.strip_completed_visual_regions),
                rebuild_text_chunks=bool(args.rebuild_text_chunks),
                max_steps=args.visual_max_steps,
                export=not args.no_export,
            )
            _write_manifest(config.data_dir, service)
            print(json.dumps({"status": "success", "repair_legacy_visual_pollution": _jsonable(repair_result)}, ensure_ascii=False, indent=2))
            return 0 if repair_result.get("ok") else 1

        for raw_path in args.paths:
            source = Path(raw_path).expanduser().resolve()
            if not source.is_file():
                results.append({"status": "failed", "path": str(source), "message": "file not found"})
                continue
            title = args.title if args.title and len(args.paths) == 1 else None
            result = service.ingest_upload_bytes(source.name, source.read_bytes(), title=title, kb_id=args.kb_id or None)
            result["input_path"] = str(source)
            document = result.get("document") if isinstance(result, dict) else None
            document_id = document.get("id") if isinstance(document, dict) else ""
            if args.visual and result.get("status") == "succeeded" and document_id:
                try:
                    result["visual_completion"] = service.complete_visual_knowledge(
                        document_id=document_id,
                        analysis_backend=args.visual_analysis_backend or "current",
                        force_prepare=args.visual_force_prepare,
                        max_steps=args.visual_max_steps,
                        export=not args.no_export,
                    )
                except Exception as exc:
                    result["visual_completion"] = {
                        "ok": False,
                        "status": "error",
                        "message": str(exc),
                        "document_id": document_id,
                        "analysis_backend": args.visual_analysis_backend or "current",
                    }
            results.append(result)
        _write_manifest(config.data_dir, service)

    print(json.dumps({"status": "success", "results": _jsonable(results)}, ensure_ascii=False, indent=2))
    return 0


def _config_to_mapping(config: Any) -> Dict[str, Any]:
    if is_dataclass(config):
        mapping = asdict(config)
    else:
        mapping = dict(getattr(config, "__dict__", {}) or {})
    return _stringify_paths(mapping)


def _stringify_paths(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _stringify_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_paths(item) for item in value]
    return value


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
