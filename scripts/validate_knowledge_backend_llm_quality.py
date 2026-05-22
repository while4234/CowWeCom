"""Validate whether LLM study docs improve local protocol knowledge retrieval."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_QUERIES = [
    "AXI4-Stream TVALID TREADY handshake 什么时候发生传输",
    "TLAST packet boundary 的作用是什么",
    "TKEEP 和 TSTRB byte qualifier 有什么区别",
    "AXI4-Stream TID TDEST routing interconnect",
    "TUSER sideband signal 可以用来做什么",
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

    parser = argparse.ArgumentParser(description="Quality-check backend LLM study document generation.")
    parser.add_argument("--document-id", default="", help="Original source document ID to evaluate.")
    parser.add_argument("--limit", type=int, default=8, help="Search result limit per query.")
    args = parser.parse_args()

    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        load_config()
    finally:
        logger.setLevel(previous_level)

    config = KnowledgeBackendConfig.from_project_config()
    with KnowledgeBackendService(config) as service:
        storage = service._backend._get_storage()
        documents = storage.list_documents()
        source_doc = _select_source_document(documents, args.document_id)
        llm_docs = _llm_docs_for_source(documents, source_doc.id if source_doc else "")
        report = _build_report(service, storage, source_doc, llm_docs, args.limit)

    print(json.dumps(_jsonable(report), ensure_ascii=False, indent=2))
    return 0 if report.get("quality_passed") else 1


def _select_source_document(documents, document_id: str):
    candidates = [document for document in documents if document.doc_type != "llm_study"]
    if document_id:
        return next((document for document in candidates if document.id == document_id), None)
    return candidates[0] if candidates else None


def _llm_docs_for_source(documents, source_document_id: str):
    result = []
    for document in documents:
        if document.doc_type != "llm_study":
            continue
        metadata = document.metadata or {}
        if not source_document_id or metadata.get("derived_from_document_id") == source_document_id:
            result.append(document)
    return result


def _build_report(service, storage, source_doc, llm_docs, limit: int) -> Dict[str, Any]:
    if source_doc is None:
        return {"quality_passed": False, "message": "source document not found"}

    chunks = storage.list_chunks(source_doc.id)
    source_span_ids = sorted({span_id for chunk in chunks for span_id in chunk.source_span_ids})
    best_llm_doc = llm_docs[0] if llm_docs else None
    llm_text = _read_llm_text(best_llm_doc)
    source_span_refs = sorted(set(re.findall(r"source_span:([A-Za-z0-9_-]+)", llm_text)))
    invalid_refs = [ref for ref in source_span_refs if ref not in source_span_ids]
    term_coverage = _term_coverage(llm_text)
    retrieval = [_query_report(service, storage, query, limit) for query in DEFAULT_QUERIES]
    llm_top3_count = sum(1 for item in retrieval if item["llm_hit_in_top3"])
    source_top3_count = sum(1 for item in retrieval if item["source_hit_in_top3"])
    source_span_ready = len(source_span_ids) == len(chunks) and len(chunks) > 0
    llm_valid = bool(best_llm_doc and len(source_span_refs) >= 8 and not invalid_refs and term_coverage >= 0.8)
    retrieval_helpful = llm_top3_count >= 3 and source_top3_count >= 3
    return {
        "quality_passed": bool(source_span_ready and llm_valid and retrieval_helpful),
        "source_document": {
            "id": source_doc.id,
            "title": source_doc.title,
            "chunks": len(chunks),
            "source_spans": len(source_span_ids),
            "all_chunks_have_source_spans": source_span_ready,
        },
        "llm_document": {
            "id": best_llm_doc.id if best_llm_doc else "",
            "title": best_llm_doc.title if best_llm_doc else "",
            "present": bool(best_llm_doc),
            "source_span_ref_count": len(source_span_refs),
            "invalid_source_span_refs": invalid_refs,
            "term_coverage": term_coverage,
            "char_count": len(llm_text),
        },
        "retrieval": retrieval,
        "judgement": {
            "llm_valid": llm_valid,
            "retrieval_helpful": retrieval_helpful,
            "llm_hit_in_top3_queries": llm_top3_count,
            "source_hit_in_top3_queries": source_top3_count,
            "conclusion": (
                "LLM study document improves retrieval breadth while original chunks remain cited"
                if source_span_ready and llm_valid and retrieval_helpful
                else "LLM study document is not yet strong enough to count as a retrieval-quality improvement"
            ),
        },
    }


def _read_llm_text(document) -> str:
    if document is None:
        return ""
    path = Path(document.source_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _term_coverage(text: str) -> float:
    terms = ["AXI4-STREAM", "TVALID", "TREADY", "TDATA", "TKEEP", "TSTRB", "TLAST", "TID", "TDEST", "TUSER"]
    upper = text.upper()
    covered = [term for term in terms if term in upper]
    return round(len(covered) / len(terms), 3)


def _query_report(service, storage, query: str, limit: int) -> Dict[str, Any]:
    hits = service.search(query, limit=limit)
    doc_types = {}
    for document in storage.list_documents():
        doc_types[document.id] = document.doc_type
    compact_hits: List[Dict[str, Any]] = []
    for hit in hits[:5]:
        doc_type = doc_types.get(hit.get("document_id", ""), "")
        compact_hits.append(
            {
                "title": hit.get("title", ""),
                "doc_type": doc_type,
                "page_start": hit.get("page_start", 0),
                "score": hit.get("score", 0),
                "has_source_spans": bool(hit.get("source_span_ids")),
            }
        )
    top3 = compact_hits[:3]
    return {
        "query": query,
        "hit_count": len(hits),
        "llm_hit_in_top3": any(hit["doc_type"] == "llm_study" for hit in top3),
        "source_hit_in_top3": any(hit["doc_type"] != "llm_study" for hit in top3),
        "top_hits": compact_hits,
    }


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
