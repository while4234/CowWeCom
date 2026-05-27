"""Repair legacy PDF ordinary text chunks with the current PDF sanitizer.

This script is intentionally one-shot and explicit: it never runs at service
startup, never calls visual analysis, and only writes SQLite when --apply is
used.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.knowledge.backend.builders import HeuristicKnowledgeBuilder
from agent.knowledge.backend.extractors import extract_document
from agent.knowledge.backend.models import KnowledgeChunk, KnowledgeDocument
from agent.knowledge.backend.service import KnowledgeBackendConfig, KnowledgeBackendService, LocalKnowledgeBackend
from agent.knowledge.backend.storage import KnowledgeStorage
from agent.knowledge.backend.text_sanitizer import is_visual_noise_line, sanitize_pages_for_knowledge_chunks


SCRIPT_NAME = "scripts/repair_knowledge_text_chunks.py"
DEFAULT_PUBLIC_PROTOCOL_DB = Path("public_protocol_knowledge") / "indexes" / "kb.sqlite"


@dataclass
class RepairOptions:
    db_path: Path
    workspace_root: Path
    data_dir: Path
    document_id: str = ""
    kb_id: str = ""
    all_documents: bool = False
    apply: bool = False
    export: bool = False
    backup_path: Optional[Path] = None


def main() -> int:
    project_root = project_root_from_script()
    os.chdir(project_root)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args()
    load_project_config_for_script()
    try:
        options = repair_options_from_args(args, project_root)
        report = run_repair(options)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(
        json.dumps(
            {
                "ok": bool(report.get("ok")),
                "mode": report.get("mode"),
                "summary": report.get("summary", {}),
                "backup_path": report.get("backup_path", ""),
                "report_path": report.get("report_path", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.get("ok") else 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair legacy PDF ordinary text chunks by rebuilding them with the current sanitizer."
    )
    parser.add_argument("--db", required=True, help="Path to the knowledge_backend SQLite database.")
    parser.add_argument("--document-id", default="", help="Repair one document id. Highest selector priority.")
    parser.add_argument("--kb-id", default="", help="Repair all non-LLM-study PDF documents in one KB.")
    parser.add_argument("--all", action="store_true", dest="all_documents", help="Repair all non-LLM-study PDF documents.")
    parser.add_argument("--apply", action="store_true", help="Write the repaired ordinary chunks to SQLite.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing SQLite. This is the default.")
    parser.add_argument("--export", action="store_true", help="Export repaired documents to the Markdown document library after apply.")
    parser.add_argument("--workspace-root", default="", help="Workspace root used to resolve relative document source paths.")
    parser.add_argument("--data-dir", default="", help="Knowledge backend data directory used for reports and export config.")
    parser.add_argument("--backup", default="", help="Backup directory or backup file path. --apply always creates a backup.")
    return parser.parse_args(argv)


def repair_options_from_args(args: argparse.Namespace, project_root: Path) -> RepairOptions:
    if args.apply and args.dry_run:
        raise ValueError("--dry-run and --apply cannot be used together")
    if args.export and not args.apply:
        raise ValueError("--export requires --apply")
    if not (args.document_id or args.kb_id or args.all_documents):
        raise ValueError("one of --document-id, --kb-id, or --all is required")

    config = load_project_backend_config()
    db_path = resolve_cli_path(args.db, project_root)
    workspace_root = resolve_workspace_root(args.workspace_root, config, project_root)
    data_dir = resolve_data_dir(args.data_dir, config, db_path, project_root)
    backup_path = resolve_cli_path(args.backup, project_root) if args.backup else None
    return RepairOptions(
        db_path=db_path,
        workspace_root=workspace_root,
        data_dir=data_dir,
        document_id=str(args.document_id or ""),
        kb_id=str(args.kb_id or ""),
        all_documents=bool(args.all_documents),
        apply=bool(args.apply),
        export=bool(args.export),
        backup_path=backup_path,
    )


def run_repair(options: RepairOptions) -> Dict[str, Any]:
    if options.export and not options.apply:
        raise ValueError("dry-run cannot export; use --apply --export")
    if not (options.document_id or options.kb_id or options.all_documents):
        raise ValueError("one of document_id, kb_id, or all_documents is required")

    project_root = project_root_from_script()
    options = normalize_options(options, project_root)
    started_at = int(time.time())
    report = new_report(options, started_at)

    try:
        selection_storage = KnowledgeStorage(options.db_path, read_only=True, immutable_read=False)
        try:
            selected_documents = select_documents(selection_storage.list_documents(), options)
        finally:
            selection_storage.close()
        report["summary"]["selected_documents"] = len(selected_documents)
        if not selected_documents:
            handle_empty_selection(report, options)
            report["finished_at"] = int(time.time())
            report["report_path"] = str(write_report(options, report, started_at))
            return report
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        report["finished_at"] = int(time.time())
        report["report_path"] = str(write_report(options, report, started_at))
        return report

    if options.apply:
        try:
            backup = create_sqlite_backup(options.db_path, options.backup_path)
            report["backup_path"] = str(backup)
        except Exception as exc:
            report["ok"] = False
            report["error"] = f"backup failed: {exc}"
            report["finished_at"] = int(time.time())
            report["report_path"] = str(write_report(options, report, started_at))
            return report

    storage: Optional[KnowledgeStorage] = None
    try:
        storage = KnowledgeStorage(
            options.db_path,
            read_only=not options.apply,
            immutable_read=False if not options.apply else True,
        )
        for document in selected_documents:
            try:
                document_report = repair_one_document(storage, document, options, apply=options.apply)
            except Exception as exc:
                document_report = base_document_report(document, options)
                document_report["error"] = str(exc)
            report["documents"].append(document_report)

        if options.apply and options.export:
            try:
                storage.conn.execute("PRAGMA wal_checkpoint(FULL)")
            except Exception:
                pass
            add_export_results(options, report)
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
    finally:
        if storage is not None:
            storage.close()

    summarize_report(report)
    report["ok"] = not report.get("error") and int(report["summary"]["failed"]) == 0
    report["finished_at"] = int(time.time())
    report["report_path"] = str(write_report(options, report, started_at))
    return report


def repair_one_document(
    storage: KnowledgeStorage,
    document: KnowledgeDocument,
    options: RepairOptions,
    *,
    apply: bool,
) -> Dict[str, Any]:
    report = base_document_report(document, options)
    if not is_pdf_document(document):
        report["skipped"] = True
        report["skipped_reason"] = "not_pdf"
        return report

    source_path, attempts = resolve_source_path_with_attempts(
        document,
        options.workspace_root,
        options.data_dir,
        project_root_from_script(),
    )
    report["resolved_attempts"] = [str(path) for path in attempts]
    if source_path is None:
        report["skipped"] = True
        report["skipped_reason"] = "source_file_not_found"
        return report
    report["resolved_source_path"] = str(source_path)

    try:
        old_chunks = storage.list_chunks(document.id)
        visual_chunks = [chunk for chunk in old_chunks if chunk_metadata_source(chunk) == "visual_analysis"]
        old_text_chunks = [chunk for chunk in old_chunks if chunk_metadata_source(chunk) != "visual_analysis"]
        old_total_chars = sum(len(chunk.text or "") for chunk in old_text_chunks)

        extracted = extract_document(source_path)
        sanitized_pages, sanitizer_report = sanitize_pages_for_knowledge_chunks(
            source_path,
            extracted.pages,
            enabled=True,
            strip_visual_regions=True,
            strip_visual_noise_lines=True,
        )
        kb_id = document.kb_id or "kb_default"
        backend = LocalKnowledgeBackend(
            workspace_root=str(options.workspace_root),
            db_path=str(options.db_path),
            enabled=True,
            default_kb_id=kb_id,
        )
        new_raw_chunks = backend._build_chunks(
            document.id,
            sanitized_pages,
            kb_id=kb_id,
            version_id=document.version_id,
        )
        build_document = replace(document, size=source_path.stat().st_size)
        build = HeuristicKnowledgeBuilder().build(build_document, new_raw_chunks)
        new_total_chars = sum(len(chunk.text or "") for chunk in build.chunks)
        removed_examples = collect_removed_noise_examples(old_text_chunks, build.chunks)

        report.update(
            {
                "old_text_chunks": len(old_text_chunks),
                "preserved_visual_chunks": len(visual_chunks),
                "new_text_chunks": len(build.chunks),
                "old_total_chars": old_total_chars,
                "new_total_chars": new_total_chars,
                "removed_noise_line_examples": removed_examples,
                "sanitizer_report": sanitizer_report,
                "skipped": False,
                "error": "",
            }
        )

        repair_metadata = dict(document.metadata or {})
        repair_metadata["repair_text_chunks"] = {
            "repaired_at": int(time.time()),
            "script": SCRIPT_NAME,
            "dry_run": not apply,
            "old_text_chunks": len(old_text_chunks),
            "preserved_visual_chunks": len(visual_chunks),
            "new_text_chunks": len(build.chunks),
            "old_total_chars": old_total_chars,
            "new_total_chars": new_total_chars,
            "sanitizer_report": sanitizer_report,
        }
        repaired_document = replace(build_document, metadata=repair_metadata)

        if not apply:
            return report

        storage.conn.execute("BEGIN IMMEDIATE")
        try:
            storage.save_document(
                repaired_document,
                build.chunks,
                source_spans=build.source_spans,
                entities=build.entities,
                relations=build.relations,
                commit=False,
            )
            normalized = normalize_visual_chunk_ordinals_after_text_chunks(
                storage,
                document_id=document.id,
                max_text_ordinal=max((chunk.ordinal for chunk in build.chunks), default=0),
            )
            deleted_spans = delete_unreferenced_source_spans_for_document(storage, document.id)
            if storage.fts5_available:
                storage._rebuild_fts()
            storage.conn.commit()
        except Exception:
            storage.conn.rollback()
            raise

        report["normalized_visual_ordinals"] = normalized
        report["deleted_unreferenced_source_spans"] = deleted_spans
        report["applied"] = True
        return report
    except Exception as exc:
        if apply and getattr(storage.conn, "in_transaction", False):
            storage.conn.rollback()
        report["error"] = str(exc)
        return report


def normalize_visual_chunk_ordinals_after_text_chunks(
    storage: KnowledgeStorage,
    document_id: str,
    max_text_ordinal: int,
) -> int:
    rows = storage.conn.execute(
        """
        SELECT id
        FROM chunks
        WHERE document_id = ?
          AND COALESCE(
                CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.source') ELSE '' END,
                ''
              ) = 'visual_analysis'
        ORDER BY ordinal ASC, id ASC
        """,
        (document_id,),
    ).fetchall()

    for index, row in enumerate(rows, start=1):
        storage.conn.execute(
            "UPDATE chunks SET ordinal = ? WHERE id = ?",
            (max_text_ordinal + index, row["id"]),
        )
    return len(rows)


def delete_unreferenced_source_spans_for_document(storage: KnowledgeStorage, document_id: str) -> int:
    chunks = storage.list_chunks(document_id)
    referenced = set()
    for chunk in chunks:
        referenced.update(chunk.source_span_ids or [])
    if not referenced:
        cursor = storage.conn.execute("DELETE FROM source_spans WHERE document_id = ?", (document_id,))
        return cursor.rowcount or 0

    placeholders = ",".join("?" for _ in referenced)
    cursor = storage.conn.execute(
        f"""
        DELETE FROM source_spans
        WHERE document_id = ?
          AND id NOT IN ({placeholders})
        """,
        [document_id, *sorted(referenced)],
    )
    return cursor.rowcount or 0


def create_sqlite_backup(db_path: Path, backup_path: Optional[Path]) -> Path:
    source = Path(db_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    default_name = f"{source.name}.backup-{timestamp}"
    final_backup_path = resolve_backup_target(source, backup_path, default_name)
    final_backup_path.parent.mkdir(parents=True, exist_ok=True)
    if final_backup_path.exists():
        raise FileExistsError(str(final_backup_path))

    src = sqlite3.connect(str(source))
    try:
        try:
            src.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
        dst = sqlite3.connect(str(final_backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return final_backup_path


def resolve_backup_target(db_path: Path, backup_path: Optional[Path], default_name: str) -> Path:
    if backup_path is None:
        return db_path.with_name(default_name)
    target = Path(backup_path).expanduser()
    if not target.is_absolute():
        target = project_root_from_script() / target
    if target.exists() and target.is_dir():
        return target / default_name
    if target.suffix:
        return target
    return target / default_name


def collect_removed_noise_examples(
    old_text_chunks: Iterable[KnowledgeChunk],
    new_chunks: Iterable[KnowledgeChunk],
) -> List[str]:
    old_lines: List[str] = []
    seen = set()
    for chunk in old_text_chunks:
        for raw_line in str(chunk.text or "").splitlines():
            line = normalize_line(raw_line)
            key = line.lower()
            if not line or key in seen:
                continue
            seen.add(key)
            old_lines.append(line)

    new_text = "\n".join(chunk.text for chunk in new_chunks)
    examples: List[str] = []
    for line in old_lines:
        if line in new_text:
            continue
        if is_visual_noise_line(line) or looks_like_known_visual_pollution(line):
            examples.append(line)
        if len(examples) >= 20:
            break
    return examples


def looks_like_known_visual_pollution(line: str) -> bool:
    text = normalize_line(line)
    lowered = text.lower()
    compact = re.sub(r"\s+", "", lowered)
    if "layer10123" in compact or "layer101234" in compact:
        return True
    if "l a y e r 1 0 1 2 3" in lowered:
        return True
    if any(marker in compact for marker in ("rxdatasbtxdatasb", "txcksb", "rxcksb")):
        return True

    tokens = text.split()
    if len(tokens) >= 10:
        one_char = sum(1 for token in tokens if len(token) == 1 and token.isalnum())
        if one_char / max(1, len(tokens)) >= 0.5:
            return True

    visual_labels = {"sideband", "tx", "rx", "module"}
    if len(tokens) == 1 and tokens[0].lower() in visual_labels:
        return True
    if len(tokens) <= 8 and len(tokens) >= 2:
        labels = [token.lower() for token in tokens]
        if sum(1 for token in labels if token in visual_labels) >= max(2, len(tokens) - 1):
            return True
    return False


def add_export_results(options: RepairOptions, report: Dict[str, Any]) -> None:
    repaired_reports = [
        item
        for item in report.get("documents", [])
        if not item.get("skipped") and not item.get("error") and item.get("document_id")
    ]
    if not repaired_reports:
        return

    config = export_config(options)
    with KnowledgeBackendService(config) as service:
        for item in repaired_reports:
            try:
                item["export"] = service.export_document_library(document_id=item["document_id"])
            except Exception as exc:
                item["export"] = {"status": "failed", "error": str(exc)}
                item["error"] = f"export failed: {exc}"


def export_config(options: RepairOptions) -> KnowledgeBackendConfig:
    config = load_project_backend_config() or KnowledgeBackendConfig.from_mapping({})
    document_library_root = str(config.ingest.document_library_root or "")
    if not document_library_root:
        document_library_root = str(options.workspace_root)
    return KnowledgeBackendConfig.from_mapping(
        {
            **config.__dict__,
            "enabled": True,
            "sqlite_path": str(options.db_path),
            "workspace_root": str(options.workspace_root),
            "data_dir": str(options.data_dir),
            "vector_store": {
                "provider": "sqlite",
                "required": False,
                "url": "",
                "collection": "cowagent_knowledge",
            },
            "ingest": {
                "allowed_extensions": config.ingest.allowed_extensions,
                "allowed_import_roots": [str(path) for path in config.ingest.allowed_import_roots],
                "max_file_size_mb": config.ingest.max_file_size_mb,
                "document_library_root": document_library_root,
                "document_library_category": config.ingest.document_library_category,
                "sanitize_pdf_visual_text": True,
                "sanitize_pdf_visual_regions": True,
                "sanitize_pdf_noise_lines": True,
            },
        }
    )


def select_documents(documents: Iterable[KnowledgeDocument], options: RepairOptions) -> List[KnowledgeDocument]:
    candidates = [document for document in documents if document.doc_type != "llm_study"]
    if options.document_id:
        return [document for document in candidates if document.id == options.document_id]
    if options.kb_id:
        return [document for document in candidates if document.kb_id == options.kb_id]
    if options.all_documents:
        return candidates
    return []


def resolve_source_path(
    document: KnowledgeDocument,
    workspace_root: Path,
    data_dir: Path,
    project_root: Path,
) -> Optional[Path]:
    resolved, _ = resolve_source_path_with_attempts(document, workspace_root, data_dir, project_root)
    return resolved


def resolve_source_path_with_attempts(
    document: KnowledgeDocument,
    workspace_root: Path,
    data_dir: Path,
    project_root: Path,
) -> Tuple[Optional[Path], List[Path]]:
    raw_source_path = str(document.source_path or "").strip()
    if not raw_source_path:
        return None, []
    source_path = Path(raw_source_path).expanduser()
    attempts: List[Path] = []
    if source_path.is_absolute():
        attempts.append(source_path)
    else:
        attempts.extend(
            [
                Path(workspace_root) / source_path,
                Path(project_root) / source_path,
                Path(data_dir) / source_path,
                Path(data_dir).parent / source_path,
            ]
        )
    for candidate in attempts:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            resolved = candidate.expanduser()
        if resolved.is_file():
            return resolved, attempts
    return None, attempts


def is_pdf_document(document: KnowledgeDocument) -> bool:
    return (
        Path(str(document.source_path or "")).suffix.lower() == ".pdf"
        or str(document.mime_type or "").lower() == "application/pdf"
    )


def chunk_metadata_source(chunk: KnowledgeChunk) -> str:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    return str(metadata.get("source") or "")


def base_document_report(document: KnowledgeDocument, options: RepairOptions) -> Dict[str, Any]:
    return {
        "document_id": document.id,
        "title": document.title,
        "kb_id": document.kb_id,
        "source_path": document.source_path,
        "resolved_source_path": "",
        "dry_run": not options.apply,
        "old_text_chunks": 0,
        "preserved_visual_chunks": 0,
        "new_text_chunks": 0,
        "old_total_chars": 0,
        "new_total_chars": 0,
        "removed_noise_line_examples": [],
        "sanitizer_report": {},
        "normalized_visual_ordinals": 0,
        "deleted_unreferenced_source_spans": 0,
        "skipped": False,
        "skipped_reason": "",
        "error": "",
    }


def new_report(options: RepairOptions, started_at: int) -> Dict[str, Any]:
    return {
        "ok": True,
        "mode": "apply" if options.apply else "dry-run",
        "db_path": str(options.db_path),
        "backup_path": "",
        "report_path": "",
        "workspace_root": str(options.workspace_root),
        "data_dir": str(options.data_dir),
        "started_at": started_at,
        "finished_at": 0,
        "message": "",
        "error": "",
        "summary": {
            "selected_documents": 0,
            "processed": 0,
            "repaired": 0,
            "skipped": 0,
            "failed": 0,
            "old_text_chunks": 0,
            "new_text_chunks": 0,
            "preserved_visual_chunks": 0,
            "old_total_chars": 0,
            "new_total_chars": 0,
        },
        "documents": [],
    }


def summarize_report(report: Dict[str, Any]) -> None:
    documents = report.get("documents", [])
    summary = report["summary"]
    summary["processed"] = sum(1 for item in documents if not item.get("skipped"))
    summary["repaired"] = sum(1 for item in documents if not item.get("skipped") and not item.get("error"))
    summary["skipped"] = sum(1 for item in documents if item.get("skipped"))
    summary["failed"] = sum(1 for item in documents if item.get("error"))
    for key in (
        "old_text_chunks",
        "new_text_chunks",
        "preserved_visual_chunks",
        "old_total_chars",
        "new_total_chars",
    ):
        summary[key] = sum(int(item.get(key) or 0) for item in documents if not item.get("skipped"))


def handle_empty_selection(report: Dict[str, Any], options: RepairOptions) -> None:
    if options.document_id:
        report["ok"] = False
        report["error"] = f"document not found: {options.document_id}"
        return
    if options.kb_id:
        report["ok"] = False
        report["error"] = f"no documents found for kb_id: {options.kb_id}"
        return
    report["message"] = "no documents selected"


def write_report(options: RepairOptions, report: Dict[str, Any], started_at: int) -> Path:
    report_path = default_report_path(options, started_at)
    report["report_path"] = str(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def default_report_path(options: RepairOptions, started_at: int) -> Path:
    data_dir = Path(options.data_dir or options.db_path.parent.parent).expanduser()
    if not data_dir:
        data_dir = options.db_path.parent.parent
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
    return data_dir / "reports" / f"repair-text-chunks-{timestamp}.json"


def normalize_options(options: RepairOptions, project_root: Path) -> RepairOptions:
    return RepairOptions(
        db_path=absolute_path(options.db_path, project_root),
        workspace_root=absolute_path(options.workspace_root, project_root),
        data_dir=absolute_path(options.data_dir, project_root),
        document_id=options.document_id,
        kb_id=options.kb_id,
        all_documents=options.all_documents,
        apply=options.apply,
        export=options.export,
        backup_path=absolute_path(options.backup_path, project_root) if options.backup_path else None,
    )


def resolve_workspace_root(raw: str, config: Optional[KnowledgeBackendConfig], project_root: Path) -> Path:
    if raw:
        return resolve_cli_path(raw, project_root)
    if config is not None and config.workspace_root:
        return absolute_path(Path(config.workspace_root), project_root)
    return project_root


def resolve_data_dir(raw: str, config: Optional[KnowledgeBackendConfig], db_path: Path, project_root: Path) -> Path:
    if raw:
        return resolve_cli_path(raw, project_root)
    db_path_abs = absolute_path(db_path, project_root)
    inferred = infer_data_dir_from_db_path(db_path_abs)
    if inferred is not None:
        return inferred
    if config is not None and config.data_dir:
        return absolute_path(Path(config.data_dir), project_root)
    return project_root / "public_protocol_knowledge"


def infer_data_dir_from_db_path(db_path: Path) -> Optional[Path]:
    db_path = Path(db_path).expanduser().resolve()
    if db_path.name == "kb.sqlite" and db_path.parent.name == "indexes":
        return db_path.parent.parent
    return None


def resolve_cli_path(raw: str, project_root: Path) -> Path:
    return absolute_path(Path(str(raw)).expanduser(), project_root)


def absolute_path(path: Path, project_root: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return left == right


def load_project_config_for_script() -> None:
    try:
        from common.log import logger
        from config import load_config

        previous_level = logger.level
        previous_disabled = logger.disabled
        logger.disabled = True
        logger.setLevel(logging.WARNING)
        try:
            load_config()
        finally:
            logger.disabled = previous_disabled
            logger.setLevel(previous_level)
    except Exception:
        pass


def load_project_backend_config() -> Optional[KnowledgeBackendConfig]:
    try:
        return KnowledgeBackendConfig.from_project_config()
    except Exception:
        return None


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def normalize_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", str(line or "").strip())


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
