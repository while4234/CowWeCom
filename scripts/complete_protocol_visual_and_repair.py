"""Complete protocol visual knowledge, repair text chunks, and validate readiness."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.knowledge.backend.service import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.storage import KnowledgeStorage


DEFAULT_PROTOCOL_KBS = ("amba_axi_v2_0", "axi4_stream", "ucie_1_1")


def main() -> int:
    os.chdir(_PROJECT_ROOT)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    load_project_config()
    config = KnowledgeBackendConfig.from_project_config()
    if not config.enabled:
        print(json.dumps({"ok": False, "error": "knowledge_backend.enabled is false"}, ensure_ascii=False, indent=2))
        return 2

    report = run(args, config)
    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Complete and validate public protocol visual knowledge.")
    parser.add_argument("--kb-id", action="append", default=[], help="Protocol knowledge-base id. Can be repeated.")
    parser.add_argument("--all", action="store_true", help="Process the known public protocol KBs.")
    parser.add_argument("--analysis-backend", default="current")
    parser.add_argument("--force-prepare", action="store_true")
    parser.add_argument("--no-retry-failed", action="store_true", help="Do not retry failed visual rows during completion.")
    parser.add_argument("--max-steps", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true", help="Do not write destructive repair changes.")
    parser.add_argument("--apply", action="store_true", help="Apply repair only after visual completion succeeds.")
    parser.add_argument("--strip-completed-visual-regions", action="store_true")
    parser.add_argument("--rebuild-text-chunks", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--validate", action="store_true", help="Run readiness validation after completion/repair.")
    return parser.parse_args()


def run(args: argparse.Namespace, config: KnowledgeBackendConfig) -> Dict[str, Any]:
    if args.apply and args.dry_run:
        return {"ok": False, "error": "--apply and --dry-run cannot be used together"}
    kb_ids = selected_kb_ids(args)
    if not kb_ids:
        return {"ok": False, "error": "one of --kb-id or --all is required"}

    started_at = int(time.time())
    report: Dict[str, Any] = {
        "ok": True,
        "started_at": started_at,
        "db_path": str(config.sqlite_path),
        "data_dir": str(config.data_dir),
        "apply_requested": bool(args.apply),
        "validate": bool(args.validate),
        "kb_ids": kb_ids,
        "results": [],
        "readiness": [],
        "errors": [],
    }
    with KnowledgeBackendService(config) as service:
        for kb_id in kb_ids:
            result = service.complete_and_repair_legacy_visual_knowledge(
                kb_id=kb_id,
                analysis_backend=args.analysis_backend,
                force_prepare=bool(args.force_prepare),
                retry_failed=not bool(args.no_retry_failed),
                dry_run=not bool(args.apply),
                apply=bool(args.apply),
                strip_completed_visual_regions=bool(args.strip_completed_visual_regions),
                rebuild_text_chunks=bool(args.rebuild_text_chunks),
                max_steps=int(args.max_steps or 0) or None,
                export=bool(args.export),
            )
            report["results"].append(result)
            if not result.get("ok"):
                report["ok"] = False
                report["errors"].append({"kb_id": kb_id, "error": result.get("block_reason") or result.get("status")})

    if args.validate:
        readiness = validate_readiness(config.sqlite_path, kb_ids)
        report["readiness"] = readiness
        failures = [item for item in readiness if item.get("status") == "fail"]
        if failures:
            report["ok"] = False
            report["errors"].extend({"kb_id": item.get("kb_id"), "error": item.get("message")} for item in failures)

    report["finished_at"] = int(time.time())
    report["report_path"] = str(write_report(config.data_dir, report, started_at))
    return report


def selected_kb_ids(args: argparse.Namespace) -> List[str]:
    values: List[str] = []
    if args.all:
        values.extend(DEFAULT_PROTOCOL_KBS)
    values.extend(args.kb_id or [])
    return [item for item in dict.fromkeys(str(value or "").strip() for value in values) if item]


def validate_readiness(db_path: Path, kb_ids: Iterable[str]) -> List[Dict[str, Any]]:
    storage = KnowledgeStorage(Path(db_path), read_only=True, immutable_read=False)
    try:
        return [validate_kb_readiness(storage, kb_id) for kb_id in kb_ids]
    finally:
        storage.close()


def validate_kb_readiness(storage: KnowledgeStorage, kb_id: str) -> Dict[str, Any]:
    documents = [document for document in storage.list_documents() if document.kb_id == kb_id and document.doc_type == "document"]
    stats = storage.visual_stats(kb_id=kb_id)
    groups = storage.visual_group_stats(kb_id=kb_id)
    prepare = storage.visual_prepare_stats(kb_id=kb_id)
    visual_chunks = count_visual_chunks(storage, kb_id)
    checks = {
        "documents": len(documents),
        "visual_artifacts": int(stats.get("total") or 0),
        "visual_chunks": visual_chunks,
        "prepare_states": len(prepare.get("states") or []),
        "prepare_status": prepare.get("status"),
        "pending": int(stats.get("pending") or 0) + int(groups.get("pending") or 0),
        "running": int(stats.get("running") or 0) + int(groups.get("running") or 0),
        "failed": int(stats.get("failed") or 0) + int(groups.get("failed") or 0),
    }
    failures = []
    if checks["documents"] <= 0:
        failures.append("no source documents")
    if checks["visual_artifacts"] <= 0:
        failures.append("visual_artifacts is 0")
    if checks["visual_chunks"] <= 0:
        failures.append("visual_analysis chunks is 0")
    if checks["prepare_states"] <= 0:
        failures.append("visual prepare state is missing")
    if checks["prepare_status"] != "done":
        failures.append(f"visual prepare status is {checks['prepare_status']}")
    if checks["pending"] or checks["running"] or checks["failed"]:
        failures.append("pending/running/failed visual rows remain")
    return {
        "kb_id": kb_id,
        "status": "fail" if failures else "pass",
        "message": "; ".join(failures),
        "checks": checks,
    }


def count_visual_chunks(storage: KnowledgeStorage, kb_id: str) -> int:
    row = storage.conn.execute(
        """
        SELECT COUNT(*)
        FROM chunks
        WHERE kb_id = ?
          AND COALESCE(
                CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.source') ELSE '' END,
                ''
              ) = 'visual_analysis'
        """,
        (kb_id,),
    ).fetchone()
    return int(row[0] or 0)


def write_report(data_dir: Path, report: Dict[str, Any], started_at: int) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
    path = Path(data_dir) / "reports" / f"complete-protocol-visual-and-repair-{timestamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_project_config() -> None:
    try:
        from common.log import logger
        from config import load_config

        previous_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            load_config()
        finally:
            logger.setLevel(previous_level)
    except Exception:
        pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
