"""Validate the local knowledge backend deployment quality."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_QUERIES = [
    "AXI4-Stream TVALID TREADY handshake",
    "TLAST packet boundary",
    "TKEEP byte qualifier",
]


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)
    sys.path.insert(0, str(project_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from common.log import logger
    from config import load_config
    from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
    from agent.knowledge.backend.extractors import dependency_status

    parser = argparse.ArgumentParser(description="Validate CowAgent knowledge backend quality.")
    parser.add_argument("--min-chunks", type=int, default=50)
    parser.add_argument("--min-entities", type=int, default=20)
    parser.add_argument("--min-relations", type=int, default=5)
    args = parser.parse_args()

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)

    config = KnowledgeBackendConfig.from_project_config()
    report: Dict[str, Any] = {
        "status": "pass",
        "checks": [],
        "summary": {},
    }

    def check(name: str, ok: bool, detail: Any = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            report["status"] = "fail"

    check("backend_enabled", config.enabled, {"enabled": config.enabled})
    check("provider_is_optional", not config.provider_api_enabled, {"provider_api_enabled": config.provider_api_enabled})
    check("project_local_data_dir", _is_under(project_root, config.data_dir), str(config.data_dir))
    check("project_local_sqlite", _is_under(project_root, config.sqlite_path), str(config.sqlite_path))

    deps = {name: status.to_dict() for name, status in dependency_status().items()}
    check("sqlite_dependency", deps.get("sqlite3", {}).get("available") is True, deps.get("sqlite3"))
    check("pdf_dependency", deps.get("pypdf", {}).get("available") is True, deps.get("pypdf"))

    check("sqlite_file_exists", config.sqlite_path.is_file(), str(config.sqlite_path))
    manifest_path = config.data_dir / "manifest.json"
    check("manifest_exists", manifest_path.is_file(), str(manifest_path))

    if config.sqlite_path.is_file():
        with _connect_readonly_sqlite(config.sqlite_path) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        check("sqlite_integrity", integrity == "ok", integrity)

    with KnowledgeBackendService(config) as service:
        storage = service._backend._get_storage()
        stats = storage.stats()
        report["summary"]["stats"] = stats
        check("document_count", stats["documents"] >= 1, stats["documents"])
        check("chunk_count", stats["chunks"] >= args.min_chunks, stats["chunks"])
        check("source_span_coverage", stats["source_spans"] >= stats["chunks"], stats)
        check("entity_count", stats["entities"] >= args.min_entities, stats["entities"])
        check("relation_count", stats["relations"] >= args.min_relations, stats["relations"])

        documents = service.list_documents()
        report["summary"]["documents"] = [
            {
                "title": document["title"],
                "kb_id": document.get("kb_id"),
                "version_id": document.get("version_id"),
                "source_path": _relative_or_name(project_root, document["source_path"]),
            }
            for document in documents
        ]
        for document in documents:
            source_path = Path(document["source_path"])
            exists = source_path.is_file()
            check(f"source_copy_exists:{document['id']}", exists, _relative_or_name(project_root, source_path))
            if exists:
                check(
                    f"source_hash_matches:{document['id']}",
                    _sha256(source_path) == document.get("content_hash"),
                    document.get("title"),
                )

        query_results = []
        for query in DEFAULT_QUERIES:
            hits = service.search(query, limit=3)
            query_results.append({"query": query, "hits": len(hits), "top_page": hits[0]["page_start"] if hits else None})
            check(
                f"query_hit:{query}",
                bool(hits) and bool(hits[0].get("source_span_ids")),
                query_results[-1],
            )
        report["summary"]["queries"] = query_results

        graph = service.graph_neighbors(term="AXI4-Stream", max_hops=1)
        node_names = {node.get("name") or node.get("canonical_name") for node in graph.get("nodes", [])}
        check("graph_has_neighbors", len(graph.get("nodes", [])) >= 3 and len(graph.get("links", [])) >= args.min_relations, {
            "nodes": len(graph.get("nodes", [])),
            "links": len(graph.get("links", [])),
        })
        check("graph_has_axi_signals", {"TVALID", "TREADY"}.issubset(node_names), sorted(node_names))

        verification = service.verify_source("AXI4-Stream uses TVALID and TREADY handshake")
        verification_detail = {
            "status": verification.get("status"),
            "confidence": verification.get("confidence"),
            "evidence_count": len(verification.get("evidence", [])),
            "pages": [
                span.get("page_start")
                for span in verification.get("evidence", [])
                if isinstance(span, dict) and span.get("page_start")
            ],
        }
        check("source_verification_supported", verification.get("status") == "supported", verification_detail)

    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_sources = [item.get("source_path") for item in manifest.get("documents", [])]
        check("manifest_sources_relative", all(source and not Path(source).is_absolute() for source in manifest_sources), manifest_sources)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


def _is_under(root: Path, path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _connect_readonly_sqlite(path: Path):
    uri_path = Path(path).resolve().as_posix()
    return sqlite3.connect(f"file:{uri_path}?mode=ro&immutable=1", uri=True)


def _relative_or_name(root: Path, path: Any) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
