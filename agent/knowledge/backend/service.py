"""Service facade for the optional local knowledge backend."""

from __future__ import annotations

import uuid
import os
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from common.log import logger

from .extractors import (
    ExtractionError,
    MissingDependencyError,
    UnsupportedDocumentError,
    dependency_status,
    extract_document,
)
from .builders import HeuristicKnowledgeBuilder
from .models import (
    Citation,
    ExtractedDocument,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    QueryResult,
    VisualArtifactCandidate,
    VisualAnalysisResult,
)
from .storage import (
    KnowledgeStorage,
    compute_file_hash,
    stable_chunk_id,
    stable_document_id,
    stable_version_id,
)
from .visual_analyzer import VisualAnalyzer, validate_visual_analysis_json, visual_result_to_chunks
from .visual_extractors import PyMuPDFVisualArtifactExtractor


DEFAULT_DB_NAME = "public_protocol_knowledge/indexes/kb.sqlite"
FALSE_VALUES = {"", "0", "false", "off", "disabled", "no", "n"}
TRUE_VALUES = {"1", "true", "on", "enabled", "yes", "y"}


DEFAULT_VISUAL_ANALYSIS_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "auto_build_after_upload": True,
    "use_current_model": True,
    "model": "gpt-5.5",
    "reasoning_effort": "xhigh",
    "prompt_version": "visual-v1",
    "max_items_per_request": 1,
    "min_confidence": 0.78,
    "min_ocr_confidence": 0.70,
    "min_structure_confidence": 0.75,
    "min_semantic_confidence": 0.75,
    "index_low_confidence": False,
    "page_render_dpi": 180,
    "crop_padding_px": 12,
    "max_image_long_edge": 1800,
    "candidate_min_area_ratio": 0.015,
    "include_page_context": True,
    "context_before_chars": 1200,
    "context_after_chars": 1200,
    "parser_provider": "pymupdf",
    "mineru_api_url": "",
    "unstructured_enabled": False,
}


class MissingProviderTokenError(RuntimeError):
    """Raised when an enabled provider needs a token but none is configured."""


@dataclass(frozen=True)
class VectorStoreConfig:
    provider: str = "sqlite"
    url: str = ""
    collection: str = "cowagent_knowledge"
    required: bool = False


@dataclass(frozen=True)
class IngestConfig:
    allowed_extensions: List[str] = field(default_factory=lambda: [".pdf", ".docx", ".txt", ".md"])
    allowed_import_roots: List[Path] = field(default_factory=list)
    max_file_size_mb: int = 500
    document_library_root: Optional[Path] = None


@dataclass(frozen=True)
class KnowledgeBackendConfig:
    enabled: bool = False
    admin_api_enabled: bool = True
    provider_api_enabled: bool = False
    sqlite_path: Path = Path(DEFAULT_DB_NAME)
    workspace_root: Path = Path(".")
    data_dir: Path = Path("public_protocol_knowledge")
    default_kb_id: str = "kb_default"
    fail_open: bool = True
    vector_store: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    llm_builder: Dict[str, Any] = field(default_factory=dict)
    retrieval: Dict[str, Any] = field(default_factory=dict)
    visual_analysis: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_VISUAL_ANALYSIS_CONFIG))
    security: Dict[str, Any] = field(default_factory=dict)

    @property
    def vector_provider(self) -> str:
        return self.vector_store.provider

    @property
    def backend(self) -> str:
        return self.vector_store.provider

    @property
    def path(self) -> Path:
        return self.sqlite_path

    @classmethod
    def from_env(cls) -> "KnowledgeBackendConfig":
        mapping: Dict[str, Any] = {
            "enabled": parse_knowledge_backend_enabled(os.environ.get("KNOWLEDGE_BACKEND_ENABLED")),
            "admin_api_enabled": parse_knowledge_backend_enabled(
                os.environ.get("KNOWLEDGE_BACKEND_ADMIN_API_ENABLED", "true")
            ),
            "provider_api_enabled": parse_knowledge_backend_enabled(
                os.environ.get("KNOWLEDGE_BACKEND_PROVIDER_API_ENABLED")
            ),
            "fail_open": parse_knowledge_backend_enabled(os.environ.get("KNOWLEDGE_BACKEND_FAIL_OPEN", "true")),
            "data_dir": os.environ.get("KNOWLEDGE_BACKEND_DATA_DIR") or "public_protocol_knowledge",
            "sqlite_path": os.environ.get("KNOWLEDGE_BACKEND_SQLITE_PATH") or DEFAULT_DB_NAME,
            "ingest": {
                "allowed_extensions": _csv(os.environ.get("KNOWLEDGE_BACKEND_ALLOWED_EXTENSIONS"))
                or [".pdf", ".docx", ".txt", ".md"],
                "allowed_import_roots": _csv(os.environ.get("KNOWLEDGE_BACKEND_ALLOWED_IMPORT_ROOTS")),
                "max_file_size_mb": int(os.environ.get("KNOWLEDGE_BACKEND_MAX_FILE_SIZE_MB") or 500),
                "document_library_root": os.environ.get("KNOWLEDGE_BACKEND_DOCUMENT_LIBRARY_ROOT") or "",
            },
            "llm_builder": {
                "enabled": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_LLM_BUILDER_ENABLED", "true")
                ),
                "auto_generate_study_doc": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_LLM_AUTO_GENERATE_STUDY_DOC")
                ),
                "index_generated_document": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_LLM_INDEX_GENERATED_DOCUMENT", "true")
                ),
                "max_chunks": int(os.environ.get("KNOWLEDGE_BACKEND_LLM_MAX_CHUNKS") or 80),
                "max_output_tokens": int(os.environ.get("KNOWLEDGE_BACKEND_LLM_MAX_OUTPUT_TOKENS") or 6000),
            },
            "retrieval": {
                "auto_inject": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_AUTO_INJECT", "true")
                ),
                "deep_query_enabled": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_DEEP_QUERY_ENABLED", "true")
                ),
                "context_window_chunks": int(os.environ.get("KNOWLEDGE_BACKEND_DEEP_CONTEXT_WINDOW_CHUNKS") or 1),
                "deep_top_k": int(os.environ.get("KNOWLEDGE_BACKEND_DEEP_TOP_K") or 5),
                "max_evidence_chars": int(os.environ.get("KNOWLEDGE_BACKEND_MAX_EVIDENCE_CHARS") or 12000),
            },
            "visual_analysis": {
                "enabled": parse_knowledge_backend_enabled(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_ENABLED", "true")),
                "auto_build_after_upload": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_AUTO_BUILD_AFTER_UPLOAD", "true")
                ),
                "use_current_model": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_USE_CURRENT_MODEL", "true")
                ),
                "model": os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MODEL") or "gpt-5.5",
                "reasoning_effort": os.environ.get("KNOWLEDGE_BACKEND_VISUAL_REASONING_EFFORT") or "xhigh",
                "prompt_version": os.environ.get("KNOWLEDGE_BACKEND_VISUAL_PROMPT_VERSION") or "visual-v1",
                "max_items_per_request": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MAX_ITEMS_PER_REQUEST") or 1),
                "min_confidence": float(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MIN_CONFIDENCE") or 0.78),
                "min_ocr_confidence": float(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MIN_OCR_CONFIDENCE") or 0.70),
                "min_structure_confidence": float(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MIN_STRUCTURE_CONFIDENCE") or 0.75
                ),
                "min_semantic_confidence": float(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MIN_SEMANTIC_CONFIDENCE") or 0.75
                ),
                "index_low_confidence": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_INDEX_LOW_CONFIDENCE")
                ),
                "page_render_dpi": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_PAGE_RENDER_DPI") or 180),
                "crop_padding_px": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_CROP_PADDING_PX") or 12),
                "max_image_long_edge": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MAX_IMAGE_LONG_EDGE") or 1800),
                "candidate_min_area_ratio": float(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_CANDIDATE_MIN_AREA_RATIO") or 0.015
                ),
                "include_page_context": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_INCLUDE_PAGE_CONTEXT", "true")
                ),
                "context_before_chars": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_CONTEXT_BEFORE_CHARS") or 1200),
                "context_after_chars": int(os.environ.get("KNOWLEDGE_BACKEND_VISUAL_CONTEXT_AFTER_CHARS") or 1200),
                "parser_provider": os.environ.get("KNOWLEDGE_BACKEND_VISUAL_PARSER_PROVIDER") or "pymupdf",
                "mineru_api_url": os.environ.get("KNOWLEDGE_BACKEND_VISUAL_MINERU_API_URL") or "",
                "unstructured_enabled": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_VISUAL_UNSTRUCTURED_ENABLED")
                ),
            },
            "vector_store": {
                "provider": os.environ.get("KNOWLEDGE_BACKEND_VECTOR_PROVIDER") or "sqlite",
                "url": os.environ.get("KNOWLEDGE_BACKEND_QDRANT_URL") or "",
                "collection": os.environ.get("KNOWLEDGE_BACKEND_QDRANT_COLLECTION") or "cowagent_knowledge",
                "required": parse_knowledge_backend_enabled(os.environ.get("KNOWLEDGE_BACKEND_VECTOR_REQUIRED")),
            },
            "security": {
                "provider_api_token_env": os.environ.get("KNOWLEDGE_BACKEND_PROVIDER_TOKEN_ENV")
                or "KNOWLEDGE_PROVIDER_TOKEN",
                "disable_admin_api_when_web_password_empty": parse_knowledge_backend_enabled(
                    os.environ.get("KNOWLEDGE_BACKEND_DISABLE_ADMIN_WHEN_NO_PASSWORD", "true")
                ),
            },
        }
        return cls.from_mapping(mapping)

    @classmethod
    def from_project_config(cls) -> "KnowledgeBackendConfig":
        from common.utils import expand_path
        from config import conf

        raw = conf().get("knowledge_backend", {}) or {}
        if not isinstance(raw, Mapping):
            raw = {}
        merged = dict(raw)
        merged.setdefault("workspace_root", conf().get("agent_workspace", "~/cow"))
        if "data_dir" in merged:
            merged["data_dir"] = expand_path(str(merged["data_dir"]))
        if "sqlite_path" in merged:
            merged["sqlite_path"] = expand_path(str(merged["sqlite_path"]))
        return cls.from_mapping(merged)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "KnowledgeBackendConfig":
        vector_raw = _mapping(mapping.get("vector_store", {}))
        ingest_raw = _mapping(mapping.get("ingest", {}))
        llm_builder_raw = _mapping(mapping.get("llm_builder", {}))
        retrieval_raw = _mapping(mapping.get("retrieval", {}))
        visual_raw = _mapping(mapping.get("visual_analysis", {}))
        security_raw = _mapping(mapping.get("security", {}))
        provider = str(
            mapping.get("vector_provider")
            or mapping.get("backend")
            or vector_raw.get("provider")
            or "sqlite"
        ).lower()
        sqlite_path = Path(str(mapping.get("sqlite_path") or mapping.get("path") or DEFAULT_DB_NAME)).expanduser()
        workspace_root = Path(str(mapping.get("workspace_root") or ".")).expanduser()
        data_dir = Path(str(mapping.get("data_dir") or workspace_root / "public_protocol_knowledge")).expanduser()
        allowed = ingest_raw.get("allowed_extensions") or [".pdf", ".docx", ".txt", ".md"]
        allowed_import_roots = ingest_raw.get("allowed_import_roots") or []
        document_library_root = ingest_raw.get("document_library_root") or ingest_raw.get("docs_root") or ""
        return cls(
            enabled=parse_knowledge_backend_enabled(mapping.get("enabled")),
            admin_api_enabled=parse_knowledge_backend_enabled(mapping.get("admin_api_enabled", True)),
            provider_api_enabled=parse_knowledge_backend_enabled(mapping.get("provider_api_enabled")),
            sqlite_path=sqlite_path,
            workspace_root=workspace_root,
            data_dir=data_dir,
            default_kb_id=str(mapping.get("default_kb_id") or "kb_default"),
            fail_open=parse_knowledge_backend_enabled(mapping.get("fail_open", True)),
            vector_store=VectorStoreConfig(
                provider=provider,
                url=str(vector_raw.get("url") or ""),
                collection=str(vector_raw.get("collection") or "cowagent_knowledge"),
                required=parse_knowledge_backend_enabled(vector_raw.get("required")),
            ),
            ingest=IngestConfig(
                allowed_extensions=[_normalize_suffix(ext) for ext in allowed],
                allowed_import_roots=[Path(str(root)).expanduser() for root in allowed_import_roots],
                max_file_size_mb=int(ingest_raw.get("max_file_size_mb") or 500),
                document_library_root=Path(str(document_library_root)).expanduser() if document_library_root else None,
            ),
            llm_builder=dict(llm_builder_raw),
            retrieval=dict(retrieval_raw),
            visual_analysis=_normalize_visual_analysis_config(visual_raw),
            security=dict(security_raw),
        )


@dataclass(frozen=True)
class BackendStatus:
    enabled: bool
    backend: str
    reason: str = ""
    visual_analysis: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestPathResult:
    files_indexed: int = 0
    files_skipped: int = 0
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class DisabledKnowledgeBackend:
    def __init__(self, backend: str = "disabled", reason: str = "local knowledge backend is disabled"):
        self._status = BackendStatus(enabled=False, backend=backend, reason=reason)

    def status(self) -> BackendStatus:
        return self._status

    def ingest_path(self, path: Path) -> IngestPathResult:
        return IngestPathResult()

    def search(self, query: str, limit: int = 5) -> List[Any]:
        return []

    def query(self, query: str, limit: int = 5) -> Dict[str, Any]:
        return {"answer": self._status.reason, "citations": []}

    def deep_query(self, query: str, **_: Any) -> Dict[str, Any]:
        return {
            "status": "disabled",
            "message": self._status.reason,
            "query": query,
            "evidence_blocks": [],
            "table_blocks": [],
            "citations": [],
            "coverage_terms": [],
            "missing_terms": [],
            "confidence": 0.0,
        }

    def list_documents(self) -> List[Dict[str, Any]]:
        return []

    def job_status(self, job_id: str) -> Dict[str, Any]:
        return {"status": "disabled", "job": None, "message": self._status.reason}

    def ingest_upload_bytes(self, filename: str, content: bytes, title: Optional[str] = None) -> Dict[str, Any]:
        return {"status": "disabled", "message": self._status.reason}

    def generate_llm_study_document(self, document_id: str = "", **_: Any) -> Dict[str, Any]:
        return {"status": "disabled", "message": self._status.reason}


class KnowledgeBackendService:
    """Testable local backend service built on SQLite keyword search."""

    def __init__(self, config: KnowledgeBackendConfig):
        self.config = config
        self._backend = LocalKnowledgeBackend(
            workspace_root=str(config.workspace_root),
            db_path=str(config.sqlite_path),
            enabled=config.enabled,
            default_kb_id=config.default_kb_id,
        )
        self._visual_extractor = PyMuPDFVisualArtifactExtractor()
        self._visual_analyzer = VisualAnalyzer()

    def status(self) -> BackendStatus:
        visual = dict(self.config.visual_analysis or {})
        visual.pop("mineru_api_url", None)
        return BackendStatus(enabled=self.config.enabled, backend=self.config.vector_store.provider, visual_analysis=visual)

    def ingest_path(self, path: Path) -> IngestPathResult:
        if not self.config.enabled:
            return IngestPathResult()
        source_path = Path(path)
        files = self._iter_ingestable_files(source_path)
        indexed = 0
        skipped = 0
        jobs: List[Dict[str, Any]] = []
        errors: List[str] = []
        for file_path in files:
            result = self._backend.ingest_upload(str(file_path))
            jobs.append(result.get("job", {}))
            if result.get("status") == "succeeded":
                indexed += 1
            else:
                skipped += 1
                error = result.get("job", {}).get("error") if isinstance(result.get("job"), dict) else ""
                if error:
                    errors.append(str(error))
        if source_path.is_dir():
            skipped += self._count_skipped_files(source_path)
        elif source_path.is_file() and not self._is_allowed(source_path):
            skipped += 1
        return IngestPathResult(files_indexed=indexed, files_skipped=skipped, jobs=jobs, errors=errors)

    def search(
        self,
        query: str,
        limit: int = 5,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> List[Any]:
        if not self.config.enabled:
            return []
        payload = self._backend.search(
            query,
            limit=limit,
            kb_ids=kb_ids,
            visited_kb_ids=visited_kb_ids,
            trace_id=trace_id,
        )
        return payload.get("hits", []) if isinstance(payload, dict) else []

    def query(
        self,
        query: str,
        limit: int = 5,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"answer": "Knowledge backend is disabled.", "citations": []}
        return self._backend.query(
            query,
            limit=limit,
            kb_ids=kb_ids,
            visited_kb_ids=visited_kb_ids,
            trace_id=trace_id,
        )

    def deep_query(
        self,
        query: str,
        limit: int = 5,
        *,
        context_window: Optional[int] = None,
        max_evidence_chars: Optional[int] = None,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {
                "status": "disabled",
                "message": "Knowledge backend is disabled.",
                "query": query,
                "evidence_blocks": [],
                "table_blocks": [],
                "citations": [],
                "coverage_terms": [],
                "missing_terms": [],
                "confidence": 0.0,
                "trace_id": trace_id,
                "visited_kb_ids": list(visited_kb_ids or []),
            }
        retrieval = self.config.retrieval or {}
        window = context_window if context_window is not None else retrieval.get("context_window_chunks", 1)
        evidence_chars = max_evidence_chars if max_evidence_chars is not None else retrieval.get("max_evidence_chars", 12000)
        return self._backend.deep_query(
            query,
            limit=limit,
            context_window=int(window or 0),
            max_evidence_chars=int(evidence_chars or 12000),
            kb_ids=kb_ids,
            visited_kb_ids=visited_kb_ids,
            trace_id=trace_id,
        )

    def list_documents(self) -> List[Dict[str, Any]]:
        if not self.config.enabled:
            return []
        storage = self._backend._get_read_storage()
        if storage is None:
            return []
        return [
            {
                "id": document.id,
                "title": document.title,
                "source_path": document.source_path,
                "mime_type": document.mime_type,
                "size": document.size,
                "content_hash": document.content_hash,
                "status": document.status,
                "kb_id": document.kb_id,
                "doc_type": document.doc_type,
                "version_id": document.version_id,
                "document_library_path": _document_library_path(document),
                "metadata": document.metadata,
            }
            for document in storage.list_documents()
        ]

    def list_knowledge_bases(self) -> List[Dict[str, Any]]:
        if not self.config.enabled:
            return []
        storage = self._backend._get_read_storage()
        if storage is None:
            return []
        return [kb.to_dict() for kb in storage.list_knowledge_bases()]

    def prepare_visual_artifacts(self, document_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """Scan documents for visual artifact candidates without calling a model."""

        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "message": "local knowledge backend is disabled"}
        if not _visual_analysis_enabled(self.config):
            return {"ok": False, "status": "disabled", "message": "knowledge_backend.visual_analysis.enabled is false"}
        storage = self._backend._get_storage(writable=True)
        documents = storage.list_documents()
        if document_id:
            documents = [document for document in documents if document.id == document_id]
        documents = [document for document in documents if (document.doc_type or "document") == "document"]
        prepared = 0
        errors: List[str] = []
        for document in documents:
            try:
                source = _resolve_document_source_path(document, self.config)
                extracted = extract_document(source)
                extracted = ExtractedDocument(
                    title=document.title or extracted.title,
                    source_path=str(source),
                    mime_type=extracted.mime_type,
                    pages=extracted.pages,
                    metadata=extracted.metadata,
                )
                candidates = self._visual_extractor.extract_candidates(document, extracted, storage, self.config)
                for candidate in candidates:
                    storage.upsert_visual_artifact(candidate)
                    prepared += 1
            except Exception as exc:
                message = f"{document.id}: {exc}"
                logger.warning("[KnowledgeBackend] visual artifact prepare failed: %s", message)
                errors.append(message)
        version_id = _current_document_version_id(storage, document_id) if document_id else None
        stats = storage.visual_stats(document_id=document_id, version_id=version_id)
        return {
            "ok": True,
            "status": "success",
            "document_id": document_id or "",
            "prepared": prepared,
            "errors": errors,
            **stats,
        }

    def build_visual_knowledge(
        self,
        document_id: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Analyze a small batch of visual artifacts and append high-confidence chunks."""

        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "message": "local knowledge backend is disabled"}
        if not _visual_analysis_enabled(self.config):
            return {"ok": False, "status": "disabled", "message": "knowledge_backend.visual_analysis.enabled is false"}
        visual_config = self.config.visual_analysis or {}
        model = str(visual_config.get("model") or "gpt-5.5")
        prompt_version = str(visual_config.get("prompt_version") or "visual-v1")
        storage = self._backend._get_storage(writable=True)
        version_id = _current_document_version_id(storage, document_id) if document_id else None
        stats = storage.visual_stats(document_id=document_id, version_id=version_id)
        if stats["total"] == 0 or (not force and stats["pending"] == 0 and stats["failed"] == 0 and stats["running"] == 0):
            self.prepare_visual_artifacts(document_id=document_id, force=force)
            stats = storage.visual_stats(document_id=document_id, version_id=version_id)
        batch_limit = max(1, int(limit or visual_config.get("max_items_per_request") or 1))
        run_id = run_id or storage.create_visual_run(document_id=document_id, kb_id=self.config.default_kb_id)
        processed = succeeded = low_confidence = failed = 0

        for _ in range(batch_limit):
            artifact = storage.claim_next_visual_artifact(
                document_id=document_id,
                version_id=version_id,
                force=force,
                model=model,
                prompt_version=prompt_version,
            )
            if artifact is None:
                break
            processed += 1
            outcome = self._process_visual_artifact(storage, artifact, force, model, prompt_version)
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "low_confidence":
                low_confidence += 1
            else:
                failed += 1
        stats = storage.visual_stats(document_id=document_id, version_id=version_id)
        run = storage.update_visual_run_stats(run_id)
        return {
            "ok": True,
            "status": "success",
            "document_id": document_id or "",
            "processed": processed,
            "succeeded": succeeded,
            "low_confidence": low_confidence,
            "failed": failed,
            "pending": stats["pending"],
            "has_more": bool(stats["pending"] or stats["failed"] or stats["running"]),
            "run_id": run_id,
            "run": run,
            "stats": stats,
        }

    def list_visual_artifacts(self, document_id: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "message": "local knowledge backend is disabled", "artifacts": []}
        if not _visual_analysis_enabled(self.config):
            return {"ok": False, "status": "disabled", "message": "knowledge_backend.visual_analysis.enabled is false", "artifacts": []}
        storage = self._backend._get_read_storage()
        if storage is None:
            return {"ok": True, "status": "success", "artifacts": [], "stats": {}}
        version_id = _current_document_version_id(storage, document_id) if document_id else None
        return {
            "ok": True,
            "status": "success",
            "artifacts": storage.list_visual_artifacts(
                document_id=document_id,
                version_id=version_id,
                status=status,
                limit=1000,
            ),
            "stats": storage.visual_stats(
                document_id=document_id,
                version_id=version_id,
            ),
        }

    def retry_visual_artifact(self, artifact_id: str) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "message": "local knowledge backend is disabled"}
        storage = self._backend._get_storage(writable=True)
        artifact = storage.get_visual_artifact(artifact_id)
        if artifact is None:
            return {"ok": False, "status": "error", "message": "visual artifact not found"}
        visual_config = self.config.visual_analysis or {}
        model = str(visual_config.get("model") or "gpt-5.5")
        prompt_version = str(visual_config.get("prompt_version") or "visual-v1")
        outcome = self._process_visual_artifact(storage, artifact, True, model, prompt_version)
        return {
            "ok": outcome != "failed",
            "status": "success" if outcome != "failed" else "failed",
            "artifact_id": artifact_id,
            "outcome": outcome,
            "stats": storage.visual_stats(document_id=artifact["document_id"], version_id=artifact["version_id"]),
        }

    def get_visual_stats(self, document_id: Optional[str] = None) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"ok": False, "status": "disabled", "message": "local knowledge backend is disabled"}
        if not _visual_analysis_enabled(self.config):
            return {"ok": False, "status": "disabled", "message": "knowledge_backend.visual_analysis.enabled is false"}
        storage = self._backend._get_read_storage()
        stats = (
            storage.visual_stats(
                document_id=document_id,
                version_id=_current_document_version_id(storage, document_id) if document_id else None,
            )
            if storage is not None
            else {}
        )
        return {"ok": True, "status": "success", "document_id": document_id or "", **stats}

    def _process_visual_artifact(
        self,
        storage: KnowledgeStorage,
        artifact: Dict[str, Any],
        force: bool,
        model: str,
        prompt_version: str,
    ) -> str:
        visual_config = self.config.visual_analysis or {}
        try:
            document = storage.get_document(artifact["document_id"])
            if document is None:
                raise RuntimeError("source document not found")
            if force:
                storage.delete_visual_chunks_for_artifact(artifact["id"])
            candidate = _candidate_from_artifact_row(artifact, document, storage, self.config)
            result = self._visual_analyzer.analyze(candidate, self.config, document)
            if not isinstance(result, VisualAnalysisResult):
                result = validate_visual_analysis_json(result, candidate, visual_config)
            result_json = result.to_dict()
            confidence = float(result.confidence.get("overall", 0.0) or 0.0)
            if result.should_index:
                chunks, spans = visual_result_to_chunks(candidate, result, document, visual_config)
                storage.delete_visual_chunks_for_artifact(artifact["id"])
                storage.append_visual_chunks(document.id, document.version_id, artifact["id"], chunks, spans)
                storage.complete_visual_artifact_success(
                    artifact["id"],
                    result_json,
                    confidence,
                    retrievable=True,
                    model=model,
                    prompt_version=prompt_version,
                )
                return "succeeded"
            storage.delete_visual_chunks_for_artifact(artifact["id"])
            storage.complete_visual_artifact_low_confidence(
                artifact["id"],
                result_json,
                confidence,
                result.low_confidence_reason,
                model=model,
                prompt_version=prompt_version,
            )
            return "low_confidence"
        except Exception as exc:
            logger.warning("[KnowledgeBackend] visual artifact analysis failed: %s", exc)
            storage.complete_visual_artifact_failed(artifact["id"], str(exc))
            return "failed"

    def export_document_library(self, document_id: str = "") -> Dict[str, Any]:
        """Export indexed backend documents into the visible Markdown knowledge library."""

        if not self.config.enabled:
            return {"status": "disabled", "message": "local knowledge backend is disabled", "files": []}
        storage = self._backend._get_read_storage()
        if storage is None:
            return {"status": "success", "documents_exported": 0, "files": []}
        all_documents = storage.list_documents()
        documents = all_documents
        if document_id:
            documents = [document for document in documents if document.id == document_id]
        if not documents:
            return {"status": "success", "documents_exported": 0, "files": []}

        exported: List[Dict[str, Any]] = []
        document_root = _document_library_root(self.config)
        for document in documents:
            chunks = storage.list_chunks(document.id)
            visual_artifacts = storage.list_visual_artifacts(
                document_id=document.id,
                version_id=document.version_id,
                limit=1000,
            )
            rel_path = _write_protocol_document_page(document_root, document, chunks, visual_artifacts)
            item = {
                "document_id": document.id,
                "title": document.title,
                "kb_id": document.kb_id,
                "path": rel_path,
                "chunks": len(chunks),
                "visual_artifacts": len(visual_artifacts),
            }
            exported.append(item)

        index_by_kb = _protocol_index_documents_by_kb(document_root, all_documents)
        kb_index_files = []
        for kb_id, items in sorted(index_by_kb.items()):
            kb_index_files.append(_write_protocol_kb_index(document_root, kb_id, items))
        root_index = _write_protocol_root_index(document_root, index_by_kb)
        files = [item["path"] for item in exported] + kb_index_files + [root_index]
        return {
            "status": "success",
            "documents_exported": len(exported),
            "document_library_root": str(document_root),
            "files": files,
            "documents": exported,
        }

    def generate_llm_study_document(
        self,
        document_id: str = "",
        *,
        index_generated_document: Optional[bool] = None,
        max_chunks: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generate a source-grounded LLM study page for one indexed document.

        The deterministic chunks remain the authoritative index. This method
        only adds a derived study document when the LLM output cites existing
        source spans from those chunks.
        """

        if not self.config.enabled:
            return {"status": "disabled", "message": "local knowledge backend is disabled"}
        if not _llm_builder_enabled(self.config):
            return {"status": "disabled", "message": "knowledge_backend.llm_builder.enabled is false"}

        storage = self._backend._get_read_storage()
        if storage is None:
            return {"status": "error", "message": "source document not found"}
        source_document = _select_llm_source_document(storage.list_documents(), document_id=document_id)
        if source_document is None:
            return {"status": "error", "message": "source document not found"}
        if source_document.doc_type == "llm_study":
            return {"status": "error", "message": "LLM study documents cannot be used as source documents"}

        chunks = storage.list_chunks(source_document.id)
        if not chunks:
            return {"status": "error", "message": "source document has no indexed chunks"}
        if _llm_builder_bool(self.config, "require_source_spans", True):
            missing = [chunk.id for chunk in chunks if not chunk.source_span_ids]
            if missing:
                return {
                    "status": "error",
                    "message": "source chunks are missing source spans",
                    "missing_chunk_ids": missing[:10],
                }

        selected_chunks = _select_llm_chunks(chunks, max_chunks or _llm_builder_int(self.config, "max_chunks", 80))
        valid_span_ids = sorted({span_id for chunk in chunks for span_id in chunk.source_span_ids})
        prompt = _build_llm_study_prompt(source_document, selected_chunks)
        try:
            llm_text = _call_llm_for_study_document(prompt, self.config)
        except Exception as exc:
            logger.warning("[KnowledgeBackend] LLM study document generation failed: %s", exc)
            return {"status": "failed", "message": str(exc)}
        validation = _validate_llm_study_document(llm_text, valid_span_ids)
        if validation["invalid_source_span_refs"] and validation["valid_source_span_refs"]:
            llm_text = _sanitize_invalid_llm_source_refs(llm_text, validation["invalid_source_span_refs"])
            sanitized_refs = validation["invalid_source_span_refs"]
            validation = _validate_llm_study_document(llm_text, valid_span_ids)
            validation["sanitized_invalid_source_span_refs"] = sanitized_refs
        if not validation["valid"]:
            return {
                "status": "failed",
                "message": "LLM study document failed source-span validation",
                "validation": validation,
            }

        document_root = _document_library_root(self.config)
        rel_path = _write_llm_study_document_page(document_root, source_document, llm_text)
        report = {
            "status": "success",
            "source_document_id": source_document.id,
            "source_title": source_document.title,
            "study_document_path": rel_path,
            "document_library_root": str(document_root),
            "source_chunks_total": len(chunks),
            "source_chunks_used": len(selected_chunks),
            "validation": validation,
            "generated_at": int(time.time()),
        }
        report["report_path"] = _write_llm_study_report(self.config, source_document, report)

        should_index = (
            _llm_builder_bool(self.config, "index_generated_document", True)
            if index_generated_document is None
            else bool(index_generated_document)
        )
        if should_index:
            report["indexed_document"] = self._index_generated_study_document(
                source_document,
                rel_path,
                llm_text,
                validation,
            )
        return report

    def _index_generated_study_document(
        self,
        source_document: KnowledgeDocument,
        study_rel_path: str,
        markdown_text: str,
        validation: Dict[str, Any],
    ) -> Dict[str, Any]:
        derived_dir = self.config.data_dir / "derived" / source_document.id
        derived_dir.mkdir(parents=True, exist_ok=True)
        derived_name = f"{_slug(source_document.title)}-{source_document.id[:8]}-llm-study.md"
        derived_path = derived_dir / derived_name
        derived_path.write_text(markdown_text, encoding="utf-8")

        extracted = extract_document(derived_path)
        content_hash = compute_file_hash(derived_path)
        document_id = stable_document_id(str(derived_path.resolve()), content_hash)
        version_id = stable_version_id(document_id, content_hash)
        chunks = self._backend._build_chunks(
            document_id,
            extracted.pages,
            kb_id=source_document.kb_id,
            version_id=version_id,
        )
        document = KnowledgeDocument(
            id=document_id,
            title=f"{source_document.title} - LLM Study Guide",
            source_path=_portable_source_path(derived_path, self._backend.workspace_root),
            mime_type=extracted.mime_type,
            size=derived_path.stat().st_size,
            content_hash=content_hash,
            status="ready",
            kb_id=source_document.kb_id,
            doc_type="llm_study",
            version_id=version_id,
            metadata={
                "derived_from_document_id": source_document.id,
                "derived_from_title": source_document.title,
                "document_library_path": study_rel_path,
                "valid_source_span_refs": validation.get("valid_source_span_refs", []),
                "invalid_source_span_refs": validation.get("invalid_source_span_refs", []),
            },
        )
        build = self._backend._builder.build(document, chunks)
        self._backend._get_storage(writable=True).save_document(
            document,
            build.chunks,
            source_spans=build.source_spans,
            entities=build.entities,
            relations=build.relations,
        )
        return {
            "id": document.id,
            "title": document.title,
            "kb_id": document.kb_id,
            "doc_type": document.doc_type,
            "chunks": len(build.chunks),
            "source_spans": len(build.source_spans),
            "entities": len(build.entities),
            "relations": len(build.relations),
        }

    def build_knowledge_graph(self, mode: str = "heuristic") -> Dict[str, Any]:
        """Return the currently persisted entity graph.

        Ingestion builds the graph incrementally. This method exposes that
        state for tests, admin APIs and diagnostics without re-parsing files.
        """

        if not self.config.enabled:
            return {"entities": [], "relations": []}
        storage = self._backend._get_read_storage()
        if storage is None:
            return {"mode": mode, "entities": [], "relations": []}
        entities = [_entity_payload(entity) for entity in storage.list_entities()]
        relations = [_relation_payload(relation) for relation in storage.list_relations()]
        return {"mode": mode, "entities": entities, "relations": relations}

    def build_graph(self, mode: str = "heuristic") -> Dict[str, Any]:
        return self.build_knowledge_graph(mode=mode)

    def resolve_entities(
        self,
        terms: Iterable[str],
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"entities": [], "visited_kb_ids": list(visited_kb_ids or [])}
        return self._backend.resolve_entities(terms, kb_ids=kb_ids, visited_kb_ids=visited_kb_ids)

    def resolve_entity(self, terms: Iterable[str], **kwargs: Any) -> Dict[str, Any]:
        return self.resolve_entities(terms, **kwargs)

    def graph_neighbors(
        self,
        entity_id: str = "",
        term: str = "",
        kb_id: str = "",
        max_hops: int = 1,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"nodes": [], "links": [], "trace_id": trace_id, "visited_kb_ids": list(visited_kb_ids or [])}
        return self._backend.graph_neighbors(
            entity_id=entity_id,
            term=term,
            kb_id=kb_id,
            max_hops=max_hops,
            visited_kb_ids=visited_kb_ids,
            trace_id=trace_id,
        )

    def verify_source(
        self,
        claim: str,
        candidate_span_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {
                "status": "insufficient",
                "supported": False,
                "claim": claim,
                "evidence": [],
                "trace_id": trace_id,
                "visited_kb_ids": list(visited_kb_ids or []),
            }
        return self._backend.verify_source(
            claim=claim,
            candidate_span_ids=candidate_span_ids,
            visited_kb_ids=visited_kb_ids,
            trace_id=trace_id,
        )

    def job_status(self, job_id: str) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled", "job": None}
        return self._backend.job_status(job_id)

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> "KnowledgeBackendService":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def ingest_upload_bytes(self, filename: str, content: bytes, title: Optional[str] = None) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled", "message": "local knowledge backend is disabled"}
        safe_name = _safe_filename(filename)
        if not self._is_extension_allowed(Path(safe_name)):
            return {"status": "failed", "message": f"unsupported document type: {Path(safe_name).suffix}"}
        if not self._is_size_allowed(len(content)):
            return {
                "status": "failed",
                "message": f"file exceeds {self.config.ingest.max_file_size_mb} MB limit",
            }
        upload_dir = self.config.data_dir / "originals"
        upload_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()[:16]
        target = upload_dir / f"{digest}_{safe_name}"
        target.write_bytes(content)
        return self._backend.ingest_upload(str(target), title=title)

    def _iter_ingestable_files(self, path: Path) -> List[Path]:
        if path.is_file():
            return [path] if self._is_allowed(path) else []
        if not path.is_dir():
            return []
        return [candidate for candidate in sorted(path.rglob("*")) if candidate.is_file() and self._is_allowed(candidate)]

    def _count_skipped_files(self, path: Path) -> int:
        if not path.is_dir():
            return 0
        return sum(1 for candidate in path.rglob("*") if candidate.is_file() and not self._is_allowed(candidate))

    def _is_allowed(self, path: Path) -> bool:
        return self._is_extension_allowed(path) and self._is_size_allowed(path.stat().st_size)

    def _is_extension_allowed(self, path: Path) -> bool:
        return path.suffix.lower() in set(self.config.ingest.allowed_extensions)

    def _is_size_allowed(self, size_bytes: int) -> bool:
        max_size = max(0, int(self.config.ingest.max_file_size_mb or 0)) * 1024 * 1024
        return max_size <= 0 or int(size_bytes) <= max_size


def _llm_builder_enabled(config: KnowledgeBackendConfig) -> bool:
    return _llm_builder_bool(config, "enabled", False)


def _llm_builder_bool(config: KnowledgeBackendConfig, name: str, default: bool = False) -> bool:
    return parse_knowledge_backend_enabled(config.llm_builder.get(name, default))


def _llm_builder_int(config: KnowledgeBackendConfig, name: str, default: int) -> int:
    try:
        return int(config.llm_builder.get(name) or default)
    except Exception:
        return default


def _select_llm_source_document(
    documents: List[KnowledgeDocument],
    *,
    document_id: str = "",
) -> Optional[KnowledgeDocument]:
    source_documents = [document for document in documents if document.doc_type != "llm_study"]
    if document_id:
        return next((document for document in source_documents if document.id == document_id), None)
    return source_documents[0] if source_documents else None


def _select_llm_chunks(chunks: List[KnowledgeChunk], max_chunks: int) -> List[KnowledgeChunk]:
    return chunks[: max(1, int(max_chunks or 1))]


def _build_llm_study_prompt(document: KnowledgeDocument, chunks: List[KnowledgeChunk]) -> str:
    source_blocks = []
    for chunk in chunks:
        span_text = ", ".join(f"source_span:{span_id}" for span_id in chunk.source_span_ids)
        page_text = (
            f"page:{chunk.page_start}"
            if chunk.page_start == chunk.page_end
            else f"pages:{chunk.page_start}-{chunk.page_end}"
        )
        source_blocks.append(
            "\n".join(
                [
                    f"[chunk:{chunk.ordinal} {page_text} {span_text}]",
                    _trim_for_prompt(chunk.text, 1400),
                ]
            )
        )
    source_packet = "\n\n---\n\n".join(source_blocks)
    return f"""你是协议知识库的学习文档生成器。请只基于下面的 SOURCE PACKET 生成中文 Markdown 学习文档，禁止补充来源中没有的信息。

目标文档：{document.title}
知识库：{document.kb_id}

硬性要求：
1. 每一个事实性要点都必须引用至少一个原始 source span，格式必须原样写成 `source_span:<id>`。
2. 不要把 source span 当成装饰；如果某个结论不能从 SOURCE PACKET 支撑，请写“来源片段未覆盖”。
3. 输出结构必须包含：协议定位、信号速查、握手与传输规则、Packet/Byte Qualifier、互连与路由、验证关注点、微信问答高频问题、Source Map。
4. 保留英文术语，例如 TVALID、TREADY、TDATA、TKEEP、TSTRB、TLAST、TID、TDEST、TUSER。
5. 语言要像给硬件验证工程师看的学习文档：清楚、分层、可直接用于提问和复习。

SOURCE PACKET:
{source_packet}
"""


def _call_llm_for_study_document(prompt: str, config: KnowledgeBackendConfig) -> str:
    from models.openai.open_ai_bot import OpenAIBot

    bot = OpenAIBot()
    llm_config = config.llm_builder or {}
    model_override = str(llm_config.get("model") or "").strip()
    if not model_override and not _llm_builder_bool(config, "use_current_model", True):
        model_override = str(llm_config.get("fallback_model") or "").strip()
    kwargs: Dict[str, Any] = {
        "max_tokens": _llm_builder_int(config, "max_output_tokens", 6000),
        "temperature": float(llm_config.get("temperature", 0.2)),
        "channel_type": "knowledge_backend_llm_builder",
        "session_id": f"llm-study-{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:16]}",
    }
    if model_override:
        kwargs["model"] = model_override
    reasoning_effort = str(llm_config.get("reasoning_effort") or "").strip()
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    response = bot.call_with_tools(
        messages=[
            {
                "role": "system",
                "content": (
                    "Generate source-grounded protocol study notes. "
                    "Never invent facts outside the provided source packet."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        stream=False,
        **kwargs,
    )
    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(str(response.get("message") or "LLM request failed"))
    choices = response.get("choices") if isinstance(response, dict) else None
    if not choices:
        raise RuntimeError("LLM response did not contain choices")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        raise RuntimeError("LLM response was empty")
    return content


def _validate_llm_study_document(markdown_text: str, valid_span_ids: List[str]) -> Dict[str, Any]:
    valid_span_set = set(valid_span_ids)
    refs = sorted(set(re.findall(r"source_span:([A-Za-z0-9_-]+)", markdown_text or "")))
    valid_refs = [ref for ref in refs if ref in valid_span_set]
    invalid_refs = [ref for ref in refs if ref not in valid_span_set]
    required_terms = [
        "AXI4-Stream",
        "TVALID",
        "TREADY",
        "TDATA",
        "TKEEP",
        "TSTRB",
        "TLAST",
        "TID",
        "TDEST",
        "TUSER",
    ]
    text_upper = (markdown_text or "").upper()
    covered_terms = [term for term in required_terms if term.upper() in text_upper]
    min_refs = min(8, max(1, len(valid_span_set) // 12))
    valid = bool(markdown_text and len(markdown_text.strip()) >= 120 and len(valid_refs) >= min_refs and not invalid_refs)
    return {
        "valid": valid,
        "source_span_refs": refs,
        "valid_source_span_refs": valid_refs,
        "invalid_source_span_refs": invalid_refs,
        "required_source_span_refs": min_refs,
        "source_span_ref_count": len(valid_refs),
        "term_coverage": round(len(covered_terms) / max(1, len(required_terms)), 3),
        "covered_terms": covered_terms,
        "char_count": len(markdown_text or ""),
    }


def _sanitize_invalid_llm_source_refs(markdown_text: str, invalid_refs: List[str]) -> str:
    text = markdown_text or ""
    for span_id in invalid_refs:
        text = text.replace(f"source_span:{span_id}", f"removed_invalid_span:{span_id}")
    note = (
        "\n\n## Validation Notes\n\n"
        "The generator produced invalid source-span IDs that were removed before indexing: "
        + ", ".join(f"`{span_id}`" for span_id in invalid_refs)
        + "\n"
    )
    return text.rstrip() + note


def _write_llm_study_document_page(workspace_root: Path, document: KnowledgeDocument, markdown_text: str) -> str:
    rel_path = _llm_study_rel_path(document)
    target_dir = _workspace_path(workspace_root, rel_path.parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    _workspace_path(workspace_root, rel_path).write_text(markdown_text.rstrip() + "\n", encoding="utf-8")
    return rel_path.as_posix()


def _llm_study_rel_path(document: KnowledgeDocument) -> Path:
    return (
        Path("knowledge")
        / "protocols"
        / _slug(document.kb_id or "kb_default")
        / f"{_slug(document.title or document.id)}-{document.id[:8]}-llm-study.md"
    )


def _document_library_path(document: KnowledgeDocument) -> str:
    metadata_path = ""
    if isinstance(document.metadata, dict):
        metadata_path = str(document.metadata.get("document_library_path") or "")
    if metadata_path:
        return metadata_path
    return _protocol_document_rel_path(document).as_posix()


def _write_llm_study_report(
    config: KnowledgeBackendConfig,
    document: KnowledgeDocument,
    report: Dict[str, Any],
) -> str:
    report_dir = config.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{document.id[:8]}-llm-study-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(report_path)


def _trim_for_prompt(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 24)].rstrip() + "\n[...truncated...]"


def _write_protocol_document_page(
    workspace_root: Path,
    document: KnowledgeDocument,
    chunks: List[KnowledgeChunk],
    visual_artifacts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    rel_path = _protocol_document_rel_path(document)
    rel_dir = rel_path.parent
    target_dir = _workspace_path(workspace_root, rel_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _workspace_path(workspace_root, rel_path)
    target_path.write_text(
        _render_protocol_document_markdown(document, chunks, visual_artifacts or []),
        encoding="utf-8",
    )
    return rel_path.as_posix()


def _write_protocol_kb_index(workspace_root: Path, kb_id: str, documents: List[Dict[str, Any]]) -> str:
    rel_dir = Path("knowledge") / "protocols" / _slug(kb_id or "kb_default")
    target_dir = _workspace_path(workspace_root, rel_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / "index.md"
    lines = [
        f"# Protocol Knowledge Base: {kb_id or 'kb_default'}",
        "",
        "This page is generated from the local structured knowledge backend.",
        "",
        "## Documents",
        "",
    ]
    for document in sorted(documents, key=lambda item: item.get("title", "")):
        local_name = Path(str(document["path"])).name
        lines.append(f"- [{_escape_markdown(document['title'])}]({local_name})")
    lines.append("")
    _workspace_path(workspace_root, rel_path).write_text("\n".join(lines), encoding="utf-8")
    return rel_path.as_posix()


def _protocol_index_documents_by_kb(workspace_root: Path, documents: List[KnowledgeDocument]) -> Dict[str, List[Dict[str, Any]]]:
    documents_by_kb: Dict[str, List[Dict[str, Any]]] = {}
    for document in documents:
        rel_path = _protocol_index_document_path(workspace_root, document)
        item = {
            "document_id": document.id,
            "title": document.title,
            "kb_id": document.kb_id,
            "path": rel_path,
        }
        documents_by_kb.setdefault(document.kb_id or "kb_default", []).append(item)
    return documents_by_kb


def _protocol_index_document_path(workspace_root: Path, document: KnowledgeDocument) -> str:
    metadata_path = _document_library_path(document)
    if metadata_path and _workspace_path(workspace_root, Path(metadata_path)).is_file():
        return metadata_path
    return _protocol_document_rel_path(document).as_posix()


def _write_protocol_root_index(workspace_root: Path, documents_by_kb: Dict[str, List[Dict[str, Any]]]) -> str:
    rel_dir = Path("knowledge") / "protocols"
    target_dir = _workspace_path(workspace_root, rel_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / "index.md"
    lines = [
        "# Protocol Knowledge Libraries",
        "",
        "This page is generated from the local structured knowledge backend.",
        "",
    ]
    for kb_id, documents in sorted(documents_by_kb.items()):
        lines.append(f"- [{_escape_markdown(kb_id)}]({_slug(kb_id)}/index.md) - {len(documents)} document(s)")
    lines.append("")
    _workspace_path(workspace_root, rel_path).write_text("\n".join(lines), encoding="utf-8")
    return rel_path.as_posix()


def _protocol_document_rel_path(document: KnowledgeDocument) -> Path:
    return (
        Path("knowledge")
        / "protocols"
        / _slug(document.kb_id or "kb_default")
        / f"{_slug(document.title or document.id)}-{document.id[:8]}.md"
    )


def _document_library_root(config: KnowledgeBackendConfig) -> Path:
    try:
        from common.utils import expand_path
        from config import conf

        configured = config.ingest.document_library_root or conf().get("agent_workspace") or "~/cow"
        return Path(expand_path(str(configured))).expanduser()
    except Exception:
        pass
    return Path("~/cow").expanduser()


def _render_protocol_document_markdown(
    document: KnowledgeDocument,
    chunks: List[KnowledgeChunk],
    visual_artifacts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    lines = [
        f"# {_escape_markdown(document.title)}",
        "",
        "> Generated from CowAgent local structured knowledge backend.",
        "",
        "## Metadata",
        "",
        f"- Knowledge base: `{document.kb_id}`",
        f"- Document ID: `{document.id}`",
        f"- Version ID: `{document.version_id}`",
        f"- Source: `{document.source_path}`",
        f"- Content hash: `{document.content_hash}`",
        f"- Chunks: `{len(chunks)}`",
        "",
        "## Query Hints",
        "",
        "Ask the Agent protocol-specific questions and require source-backed answers with page references.",
        "",
        "## Source Chunks",
        "",
    ]
    for chunk in chunks:
        page = f"Page {chunk.page_start}" if chunk.page_start == chunk.page_end else f"Pages {chunk.page_start}-{chunk.page_end}"
        entities = ", ".join(chunk.entities[:16])
        heading = chunk.section_path or chunk.clause_title or page
        lines.extend(
            [
                f"### Chunk {chunk.ordinal}: {_escape_markdown(heading)}",
                "",
                f"- Source range: `{page}`",
                f"- Source span IDs: `{', '.join(chunk.source_span_ids)}`",
            ]
        )
        if entities:
            lines.append(f"- Entities: `{entities}`")
        lines.extend(["", _normalize_markdown_text(chunk.text), ""])
    visual_artifacts = visual_artifacts or []
    retrievable = [item for item in visual_artifacts if item.get("analysis_status") == "succeeded" and item.get("retrievable")]
    low_confidence = [item for item in visual_artifacts if item.get("analysis_status") == "low_confidence"]
    if retrievable:
        lines.extend(["", "## 视觉图表补全", ""])
        for item in retrievable:
            result = item.get("result_json") or {}
            caption = result.get("caption") or item.get("caption") or ""
            title = result.get("title") or caption or item.get("label") or item.get("artifact_type")
            lines.extend(
                [
                    f"### Page {item.get('page')} - {_escape_markdown(str(item.get('artifact_type') or 'visual'))} - {_escape_markdown(str(title or ''))}",
                    "",
                    f"- Confidence: `{item.get('analysis_confidence', 0)}`",
                    f"- Caption: {_escape_markdown(str(caption or ''))}",
                    f"- BBox: `{json.dumps(item.get('bbox') or {}, ensure_ascii=False)}`",
                    f"- Summary: {_escape_markdown(str(result.get('summary') or ''))}",
                    "",
                ]
            )
            facts = result.get("key_facts") if isinstance(result.get("key_facts"), list) else []
            if facts:
                lines.extend(["- Key facts:", ""])
                for fact in facts:
                    fact_text = fact.get("fact") if isinstance(fact, dict) else str(fact)
                    lines.append(f"  - {_escape_markdown(str(fact_text or ''))}")
                lines.append("")
            structured = result.get("structured_markdown") or (result.get("table") or {}).get("markdown") or ""
            if structured:
                lines.extend(["Structured content:", "", _normalize_markdown_text(str(structured)), ""])
    if low_confidence:
        lines.extend(["", "## 未入库低置信图表", ""])
        for item in low_confidence:
            reason = item.get("error") or (item.get("result_json") or {}).get("low_confidence_reason") or ""
            lines.append(
                f"- Page {item.get('page')} `{item.get('artifact_type')}`: "
                f"{_escape_markdown(str(item.get('caption') or item.get('label') or ''))} "
                f"(reason: {_escape_markdown(str(reason))})"
            )
    return "\n".join(lines).rstrip() + "\n"


def _workspace_path(workspace_root: Path, rel_path: Path) -> Path:
    root = Path(workspace_root or ".").expanduser().resolve()
    return root / rel_path


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return normalized.lower()[:96] or "document"


def _escape_markdown(value: str) -> str:
    return str(value or "").replace("[", "\\[").replace("]", "\\]")


def _normalize_markdown_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or "_No extracted text in this chunk._"


def parse_knowledge_backend_enabled(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    value = str(raw).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return False


def _normalize_visual_analysis_config(raw: Mapping[str, Any]) -> Dict[str, Any]:
    config = dict(DEFAULT_VISUAL_ANALYSIS_CONFIG)
    config.update(dict(raw or {}))
    for key in ("enabled", "auto_build_after_upload", "use_current_model", "index_low_confidence", "include_page_context", "unstructured_enabled"):
        config[key] = parse_knowledge_backend_enabled(config.get(key))
    for key in ("max_items_per_request", "page_render_dpi", "crop_padding_px", "max_image_long_edge", "context_before_chars", "context_after_chars"):
        config[key] = int(config.get(key) or DEFAULT_VISUAL_ANALYSIS_CONFIG[key])
    for key in ("min_confidence", "min_ocr_confidence", "min_structure_confidence", "min_semantic_confidence", "candidate_min_area_ratio"):
        config[key] = float(config.get(key) or DEFAULT_VISUAL_ANALYSIS_CONFIG[key])
    config["model"] = str(config.get("model") or "gpt-5.5")
    config["reasoning_effort"] = str(config.get("reasoning_effort") or "xhigh")
    config["prompt_version"] = str(config.get("prompt_version") or "visual-v1")
    config["parser_provider"] = str(config.get("parser_provider") or "pymupdf")
    config["mineru_api_url"] = str(config.get("mineru_api_url") or "")
    return config


def _visual_analysis_enabled(config: KnowledgeBackendConfig) -> bool:
    return parse_knowledge_backend_enabled((config.visual_analysis or {}).get("enabled", True))


def _resolve_document_source_path(document: KnowledgeDocument, config: KnowledgeBackendConfig) -> Path:
    source = Path(document.source_path)
    if source.is_absolute():
        return source
    return (Path(config.workspace_root).expanduser().resolve() / source).resolve()


def _candidate_from_artifact_row(
    artifact: Dict[str, Any],
    document: KnowledgeDocument,
    storage: KnowledgeStorage,
    config: KnowledgeBackendConfig,
) -> VisualArtifactCandidate:
    chunks = storage.list_chunks(document.id)
    page_chunks = [chunk for chunk in chunks if chunk.page_start <= int(artifact["page"]) <= chunk.page_end]
    page_text = "\n\n".join(chunk.text for chunk in page_chunks)[:3000]
    ordinal = min((chunk.ordinal for chunk in page_chunks), default=0)
    before_chunks = [chunk for chunk in chunks if ordinal and max(0, ordinal - 2) <= chunk.ordinal < ordinal]
    after_chunks = [chunk for chunk in chunks if ordinal and ordinal < chunk.ordinal <= ordinal + 2]
    visual_config = config.visual_analysis or {}
    before_chars = int(visual_config.get("context_before_chars", 1200) or 1200)
    after_chars = int(visual_config.get("context_after_chars", 1200) or 1200)
    section_path = []
    for chunk in page_chunks:
        if chunk.section_path:
            section_path = [chunk.section_path]
            break
    return VisualArtifactCandidate(
        id=artifact["id"],
        document_id=artifact["document_id"],
        version_id=artifact["version_id"],
        kb_id=artifact.get("kb_id") or "kb_default",
        artifact_type=artifact.get("artifact_type") or "unknown",
        page=int(artifact.get("page") or 0),
        label=artifact.get("label") or "",
        caption=artifact.get("caption") or "",
        bbox=artifact.get("bbox") or {},
        image_path=artifact.get("image_path") or "",
        image_hash=artifact.get("image_hash") or "",
        context_hash=artifact.get("context_hash") or "",
        parser=artifact.get("parser") or "",
        parser_confidence=float(artifact.get("parser_confidence") or 0),
        section_path=section_path,
        context_before="\n\n".join(chunk.text for chunk in before_chunks)[-before_chars:],
        context_after="\n\n".join(chunk.text for chunk in after_chunks)[:after_chars],
        page_text=page_text,
    )


def _current_document_version_id(storage: KnowledgeStorage, document_id: Optional[str]) -> Optional[str]:
    if not document_id:
        return None
    document = storage.get_document(document_id)
    return document.version_id if document is not None else None


def require_provider_token(provider: str, token: Optional[str], env_var: str) -> str:
    value = token or os.environ.get(env_var)
    if value:
        return value
    raise MissingProviderTokenError(f"{provider} provider token is required; set {env_var}")


def build_knowledge_backend(config: KnowledgeBackendConfig):
    if not config.enabled:
        return DisabledKnowledgeBackend(config.vector_store.provider)
    if config.vector_store.provider == "qdrant":
        try:
            import qdrant_client  # noqa: F401
        except Exception as exc:
            if config.vector_store.required:
                if not config.fail_open:
                    raise
                return DisabledKnowledgeBackend("qdrant", f"qdrant client unavailable: {exc}")
            logger.warning("[KnowledgeBackend] qdrant client unavailable, falling back to SQLite FTS: %s", exc)
            return KnowledgeBackendService(config)
        # The first implementation keeps authoritative metadata/search in SQLite.
        # qdrant-client presence means the deployment can add vector indexing
        # without making startup depend on a live Qdrant server.
        return KnowledgeBackendService(config)
    return KnowledgeBackendService(config)


def get_backend_service() -> Any:
    return build_knowledge_backend(KnowledgeBackendConfig.from_project_config())


def get_provider_bearer_token() -> str:
    config = KnowledgeBackendConfig.from_project_config()
    token_env = config.security.get("provider_api_token_env") or "KNOWLEDGE_PROVIDER_TOKEN"
    return os.environ.get(str(token_env), "")


def verify_provider_bearer_token(token: str) -> bool:
    expected = get_provider_bearer_token()
    if not expected:
        return False
    import hmac

    return hmac.compare_digest(str(token), expected)


def dispatch_admin_request(method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    config = KnowledgeBackendConfig.from_project_config()
    disabled_reason = _admin_api_disabled_reason(config)
    if disabled_reason:
        return {"status": "disabled", "message": disabled_reason}
    service = build_knowledge_backend(config)
    method = method.upper()
    path = _clean_route_path(path)
    payload = payload or {}

    if method == "GET" and path in ("", "status"):
        return _jsonable_status(service.status())
    if method == "GET" and path in ("dependencies", "health"):
        backend = getattr(service, "_backend", None)
        if backend is not None:
            return backend.dependency_check()
        return _jsonable_status(service.status())
    if method == "GET" and path in ("docs", "documents"):
        return {"status": "success", "documents": _call_or_default(service, "list_documents", [])}
    if method == "GET" and path in ("kbs", "knowledge-bases", "knowledge_bases"):
        return {"status": "success", "knowledge_bases": _call_or_default(service, "list_knowledge_bases", [])}
    if method in ("GET", "POST") and path in ("graph", "knowledge-graph"):
        return {"status": "success", **_to_jsonable(service.build_knowledge_graph())}
    if method in ("GET", "POST") and path.endswith("/graph"):
        kb_id = path.split("/", 1)[0] if "/" in path else ""
        return {"status": "success", **_to_jsonable(service.graph_neighbors(kb_id=kb_id, max_hops=2))}
    if method in ("GET", "POST") and path in ("entities/resolve", "entity/resolve"):
        terms = payload.get("terms") or payload.get("term") or []
        if isinstance(terms, str):
            terms = [terms]
        return {"status": "success", **_to_jsonable(service.resolve_entities(terms))}
    if method in ("GET", "POST") and path == "search":
        query = _payload_query(payload)
        return {"status": "success", "results": _to_jsonable(service.search(query, limit=_payload_limit(payload)))}
    if method in ("GET", "POST") and path == "query":
        query = _payload_query(payload)
        return {"status": "success", **_to_jsonable(service.query(query, limit=_payload_limit(payload)))}
    if method == "POST" and path in ("export", "docs/export"):
        document_id = str(payload.get("document_id") or "")
        return _to_jsonable(service.export_document_library(document_id=document_id))
    if method == "POST" and path in ("llm-study", "docs/llm-study", "enrich", "docs/enrich"):
        document_id = str(payload.get("document_id") or "")
        index_generated = payload.get("index_generated_document")
        max_chunks = payload.get("max_chunks")
        return _to_jsonable(
            service.generate_llm_study_document(
                document_id=document_id,
                index_generated_document=None if index_generated is None else parse_knowledge_backend_enabled(index_generated),
                max_chunks=int(max_chunks) if max_chunks else None,
            )
        )
    if method == "POST" and path in ("visual/prepare", "visual-prepare"):
        return _to_jsonable(
            service.prepare_visual_artifacts(
                document_id=str(payload.get("document_id") or "") or None,
                force=parse_knowledge_backend_enabled(payload.get("force")),
            )
        )
    if method == "POST" and path in ("visual/build", "visual-build"):
        return _to_jsonable(
            service.build_visual_knowledge(
                document_id=str(payload.get("document_id") or "") or None,
                limit=int(payload.get("limit") or 0) or None,
                force=parse_knowledge_backend_enabled(payload.get("force")),
                run_id=str(payload.get("run_id") or "") or None,
            )
        )
    if method == "GET" and path in ("visual/status", "visual-stats"):
        return _to_jsonable(service.get_visual_stats(document_id=str(payload.get("document_id") or "") or None))
    if method == "GET" and path in ("visual/artifacts", "visual-artifacts"):
        return _to_jsonable(
            service.list_visual_artifacts(
                document_id=str(payload.get("document_id") or "") or None,
                status=str(payload.get("status") or "") or None,
            )
        )
    if method == "POST" and path in ("visual/retry", "visual-retry"):
        artifact_id = str(payload.get("artifact_id") or payload.get("id") or "")
        if not artifact_id:
            return {"status": "error", "message": "artifact_id is required"}
        return _to_jsonable(service.retry_visual_artifact(artifact_id))
    if method == "POST" and path in ("upload", "docs/upload"):
        return _handle_upload_payload(service, payload)
    if method == "POST" and path in ("ingest", "rebuild", "docs/import-folder"):
        source_path = payload.get("path") or payload.get("file_path")
        if not source_path:
            return {"status": "error", "message": "path is required"}
        if not _is_import_path_allowed(Path(source_path), config):
            return {
                "status": "error",
                "message": "path import is disabled or outside knowledge_backend.ingest.allowed_import_roots",
            }
        result = {"status": "success", **_to_jsonable(service.ingest_path(Path(source_path)))}
        result["document_library"] = _to_jsonable(service.export_document_library())
        return result
    if path.startswith("jobs/"):
        job_id = path.split("/", 1)[1]
        return _to_jsonable(service.job_status(job_id))
    return {"status": "error", "message": f"unsupported knowledge admin route: {method} {path}"}


def dispatch_provider_request(method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    config = KnowledgeBackendConfig.from_project_config()
    if not config.provider_api_enabled:
        return {"status": "disabled", "message": "knowledge provider API is disabled"}
    service = build_knowledge_backend(config)
    method = method.upper()
    path = _clean_route_path(path)
    payload = payload or {}
    trace_id = str(payload.get("trace_id") or "")
    visited_kb_ids = [str(item) for item in (payload.get("visited_kb_ids") or [])]
    max_hops = int(payload.get("max_hops") or 1)
    if max_hops < 0:
        return {"status": "error", "message": "max_hops must be non-negative", "trace_id": trace_id}
    if "cowagent-local-kb" in [str(item) for item in (payload.get("visited_provider_ids") or [])]:
        return {
            "status": "ok",
            "results": [],
            "trace_id": trace_id,
            "visited_kb_ids": visited_kb_ids,
            "recursion_prevented": True,
        }

    if method == "GET" and path in ("", "capabilities"):
        status = _jsonable_status(service.status())
        return {
            "provider_id": "cowagent-local-kb",
            "version": "1.0",
            "supported_methods": ["search", "query", "deep_query", "resolve_entity", "graph_neighbors", "verify_source"],
            "auth_required": True,
            "status": status,
            "knowledge_bases": _to_jsonable(_call_or_default(service, "list_knowledge_bases", [])),
        }
    if method == "POST" and path == "search":
        query = _payload_query(payload)
        return {
            "results": _to_jsonable(
                service.search(
                    query,
                    limit=_payload_limit(payload),
                    kb_ids=payload.get("kb_ids"),
                    visited_kb_ids=visited_kb_ids,
                    trace_id=trace_id,
                )
            ),
            "trace_id": trace_id,
            "visited_kb_ids": visited_kb_ids,
        }
    if method == "POST" and path == "query":
        query = _payload_query(payload)
        result = _to_jsonable(
            service.query(
                query,
                limit=_payload_limit(payload),
                kb_ids=payload.get("kb_ids"),
                visited_kb_ids=visited_kb_ids,
                trace_id=trace_id,
            )
        )
        result["trace_id"] = trace_id
        result["visited_kb_ids"] = visited_kb_ids
        return result
    if method == "POST" and path == "deep_query":
        query = _payload_query(payload)
        result = _to_jsonable(
            service.deep_query(
                query,
                limit=_payload_limit(payload),
                context_window=payload.get("context_window"),
                max_evidence_chars=payload.get("max_evidence_chars"),
                kb_ids=payload.get("kb_ids"),
                visited_kb_ids=visited_kb_ids,
                trace_id=trace_id,
            )
        )
        result["trace_id"] = trace_id
        result["visited_kb_ids"] = visited_kb_ids
        return result
    if method == "POST" and path in ("entities/resolve", "entity/resolve"):
        terms = payload.get("terms") or []
        if isinstance(terms, str):
            terms = [terms]
        result = _to_jsonable(
            service.resolve_entities(
                terms,
                kb_ids=payload.get("kb_ids"),
                visited_kb_ids=visited_kb_ids,
            )
        )
        result["trace_id"] = trace_id
        return result
    if method == "GET" and path.startswith("graph/neighbors"):
        return _to_jsonable(
            service.graph_neighbors(
                entity_id=str(payload.get("entity_id") or ""),
                term=str(payload.get("term") or ""),
                kb_id=str(payload.get("kb_id") or ""),
                max_hops=max_hops,
                visited_kb_ids=visited_kb_ids,
                trace_id=trace_id,
            )
        )
    if method == "POST" and path == "verify":
        return _to_jsonable(
            service.verify_source(
                claim=str(payload.get("claim") or payload.get("query") or ""),
                candidate_span_ids=payload.get("candidate_span_ids"),
                visited_kb_ids=visited_kb_ids,
                trace_id=trace_id,
            )
        )
    return {"status": "error", "message": f"unsupported knowledge provider route: {method} {path}"}


def _handle_upload_payload(service: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    files = payload.get("files") or []
    fields = payload.get("fields") or {}
    if files:
        results = []
        for file_info in files:
            ingest_result = service.ingest_upload_bytes(
                filename=file_info.get("filename") or "upload.bin",
                content=file_info.get("content") or b"",
                title=fields.get("title") or fields.get("name"),
            )
            document_id = ""
            if isinstance(ingest_result, dict):
                document = ingest_result.get("document") or {}
                if isinstance(document, dict):
                    document_id = str(document.get("id") or "")
            if ingest_result.get("status") == "succeeded" and hasattr(service, "export_document_library"):
                ingest_result["document_library"] = service.export_document_library(document_id=document_id)
                config = getattr(service, "config", None)
                if config is not None and _llm_builder_bool(config, "auto_generate_study_doc", False):
                    ingest_result["llm_study"] = service.generate_llm_study_document(document_id=document_id)
            results.append(ingest_result)
        return {"status": "success", "uploads": _to_jsonable(results)}

    source_path = payload.get("path") or payload.get("file_path")
    if source_path:
        config = getattr(service, "config", None) or KnowledgeBackendConfig.from_project_config()
        if not _is_import_path_allowed(Path(source_path), config):
            return {
                "status": "error",
                "message": "path import is disabled or outside knowledge_backend.ingest.allowed_import_roots",
            }
        result = {"status": "success", **_to_jsonable(service.ingest_path(Path(source_path)))}
        if hasattr(service, "export_document_library"):
            result["document_library"] = _to_jsonable(service.export_document_library())
        config = getattr(service, "config", None)
        if config is not None and _llm_builder_bool(config, "auto_generate_study_doc", False):
            result["llm_study"] = _to_jsonable(service.generate_llm_study_document())
        return result
    return {"status": "error", "message": "No files uploaded"}


def _admin_api_disabled_reason(config: KnowledgeBackendConfig) -> str:
    if not config.admin_api_enabled:
        return "knowledge admin API is disabled"
    disable_without_password = parse_knowledge_backend_enabled(
        config.security.get("disable_admin_api_when_web_password_empty", True)
    )
    if not disable_without_password:
        return ""
    try:
        from config import conf

        if not conf().get("web_password", ""):
            return "knowledge admin API requires web_password when exposed through Web"
    except Exception:
        return "knowledge admin API could not validate web_password"
    return ""


def _is_import_path_allowed(path: Path, config: KnowledgeBackendConfig) -> bool:
    roots = list(config.ingest.allowed_import_roots)
    if not roots:
        return False
    try:
        resolved_path = Path(path).expanduser().resolve()
    except Exception:
        return False
    for root in roots:
        try:
            resolved_root = Path(root).expanduser().resolve()
        except Exception:
            continue
        if resolved_path == resolved_root or resolved_root in resolved_path.parents:
            return True
    return False


def _payload_query(payload: Dict[str, Any]) -> str:
    return str(payload.get("query") or payload.get("question") or "")


def _payload_limit(payload: Dict[str, Any]) -> int:
    return int(payload.get("limit") or payload.get("top_k") or 5)


def _clean_route_path(path: str) -> str:
    return (path or "").strip("/")


def _call_or_default(target: Any, name: str, default: Any) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return default
    return method()


def _jsonable_status(status: Any) -> Dict[str, Any]:
    return _to_jsonable(status)


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.bin").name
    safe = "".join(ch for ch in name if ch.isalnum() or ch in (" ", ".", "-", "_")).strip()
    return safe or "upload.bin"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_suffix(value: Any) -> str:
    suffix = str(value).strip().lower()
    if not suffix:
        return suffix
    return suffix if suffix.startswith(".") else f".{suffix}"


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _entity_payload(entity: Any) -> Dict[str, Any]:
    data = _to_jsonable(entity)
    status = "verified" if data.get("defining_doc_id") else "candidate"
    data.update(
        {
            "name": data.get("canonical_name", ""),
            "status": status,
        }
    )
    return data


def _relation_payload(relation: Any) -> Dict[str, Any]:
    data = _to_jsonable(relation)
    status = data.get("status") or "candidate"
    if status == "active":
        status = "verified"
    data.update(
        {
            "source": data.get("subject", "") or data.get("subject_entity_id", ""),
            "target": data.get("object", "") or data.get("object_entity_id", ""),
            "source_id": data.get("subject_entity_id", ""),
            "target_id": data.get("object_entity_id", ""),
            "status": status,
        }
    )
    return data


def _deep_query_answer_policy() -> str:
    return (
        "Use evidence_blocks as source evidence only. Distinguish directly supported facts, "
        "inferences from those facts, and insufficient evidence. For layered technical concepts, "
        "separate protocol/logical behavior, physical mapping, implementation or monitor view, "
        "and state/step boundaries."
    )


def _build_deep_evidence_blocks(
    chunks: List[KnowledgeChunk],
    documents: Mapping[str, KnowledgeDocument],
    source_spans: Mapping[str, Any],
    hit_chunk_ids: Iterable[str],
    hit_scores: Mapping[str, float],
    max_evidence_chars: int,
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    used_chars = 0
    hit_ids = set(hit_chunk_ids)
    last_sections: Dict[str, str] = {}
    for chunk in chunks:
        document = documents.get(chunk.document_id)
        source_path = document.source_path if document else ""
        title = document.title if document else chunk.document_id
        section = chunk.section_path or _infer_section_path(chunk.text)
        if section:
            last_sections[chunk.document_id] = section
        else:
            section = last_sections.get(chunk.document_id, "")
        span_payloads = []
        for span_id in chunk.source_span_ids:
            span = source_spans.get(span_id)
            if span is not None:
                span_payloads.append(
                    {
                        "id": span.id,
                        "page_start": span.page_start,
                        "page_end": span.page_end,
                        "section": span.section_path or section,
                    }
                )
        text = _compact_block_text(chunk.text)
        remaining = max_evidence_chars - used_chars
        if remaining <= 0:
            break
        truncated = False
        if len(text) > remaining:
            text = text[: max(0, remaining)].rstrip()
            truncated = True
        block = {
            "chunk_id": chunk.id,
            "document_id": chunk.document_id,
            "hit": chunk.id in hit_ids,
            "ordinal": chunk.ordinal,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "score": round(float(hit_scores.get(chunk.id, 0.0)), 3) if chunk.id in hit_ids else None,
            "section": section,
            "source": source_path,
            "source_span_ids": list(chunk.source_span_ids),
            "source_spans": span_payloads,
            "title": title,
            "truncated": truncated,
            "text": text,
        }
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        if metadata.get("source") == "visual_analysis":
            block["visual_artifact_type"] = metadata.get("visual_artifact_type")
            block["visual_confidence"] = metadata.get("visual_confidence")
            block["caption"] = metadata.get("caption")
            block["page"] = metadata.get("page")
            block["bbox"] = metadata.get("bbox")
            block["source_span"] = span_payloads[0] if span_payloads else None
        blocks.append(block)
        used_chars += len(text)
        if truncated:
            break
    return blocks


def _deep_query_citations(hits: List[Any], documents: Mapping[str, KnowledgeDocument]) -> List[Dict[str, Any]]:
    citations = []
    for index, hit in enumerate(hits, start=1):
        document = documents.get(hit.document_id)
        citations.append(
            {
                "index": index,
                "document_id": hit.document_id,
                "chunk_id": hit.chunk_id,
                "ordinal": hit.ordinal,
                "title": document.title if document else hit.title,
                "source_path": document.source_path if document else hit.source_path,
                "page_start": hit.page_start,
                "page_end": hit.page_end,
                "score": round(float(hit.score), 3),
                "source_span_ids": list(hit.source_span_ids),
            }
        )
    return citations


def _matched_terms(terms: Iterable[str], text: str) -> List[str]:
    text_lower = (text or "").lower()
    seen = []
    for term in terms:
        normalized = str(term or "").strip()
        if normalized and normalized.lower() in text_lower and normalized not in seen:
            seen.append(normalized)
    return seen


def _deep_query_confidence(hits: List[Any], coverage_terms: List[str], claim_terms: List[str]) -> float:
    if not hits:
        return 0.0
    hit_score = max(float(getattr(hit, "score", 0.0) if not isinstance(hit, dict) else hit.get("score", 0.0)) for hit in hits)
    coverage = len(coverage_terms) / max(1, len(claim_terms)) if claim_terms else 1.0
    return round(max(0.0, min(1.0, (hit_score * 0.7) + (coverage * 0.3))), 3)


def _deep_query_supplemental_terms(question: str) -> List[str]:
    text = str(question or "")
    terms = re.findall(r"\b[A-Z][A-Z0-9_.-]{2,}\b", text)
    terms.extend(re.findall(r"\b(?:Table|Figure)\s+\d+(?:-\d+)+\b", text, flags=re.IGNORECASE))
    terms.extend(re.findall(r"\{[^{}]{3,80}\}", text))
    lowered = text.lower()
    if "encoding" in lowered or "编码" in text:
        terms.extend(["encoding", "MsgInfo", "Table"])
    if "field" in lowered or "字段" in text or "table" in lowered or "表格" in text:
        terms.extend(["field", "Data Field", "MsgCode", "Table"])
    if "configuration req" in lowered or "configuration resp" in lowered or "mbinit.param" in lowered:
        terms.extend(["MBINIT.PARAM", "configuration req", "configuration resp", "Max IO Link Speed"])
    if "phyretrain" in lowered or "retrain" in lowered:
        terms.extend(["PHYRETRAIN", "retrain start req", "retrain start resp", "Table 4-10", "Table 4-11", "Table 4-12"])
    if "repairval" in lowered:
        terms.extend(["MBINIT.REPAIRVAL", "Step 1", "Step 4", "Step 7", "Step 10", "Step 12", "TVLD_L", "TRDVLD_L"])
    visual_markers = [
        "图",
        "图表",
        "表",
        "表格",
        "时序",
        "状态机",
        "位域",
        "waveform",
        "timing",
        "diagram",
        "figure",
        "table",
        "chart",
    ]
    if any(marker in lowered or marker in text for marker in visual_markers):
        terms.extend(visual_markers)
    return _unique_strings(terms)


def _deep_query_required_terms(question: str) -> List[str]:
    lowered = str(question or "").lower()
    required: List[str] = []
    if "repairval" in lowered:
        required.extend(["MBINIT.REPAIRVAL", "Step 12"])
    if "mbinit.param" in lowered or ("configuration req" in lowered and "configuration resp" in lowered):
        required.extend(["configuration req", "configuration resp", "Max IO Link Speed"])
    if "phyretrain" in lowered or "retrain encoding" in lowered:
        required.extend(["PHYRETRAIN", "retrain start req", "encoding"])
    if "table" in lowered or "表格" in str(question or "") or "encoding" in lowered:
        required.append("Table")
    return _unique_strings(required)


def _extract_table_blocks(evidence_blocks: List[Dict[str, Any]], max_blocks: int = 12) -> List[Dict[str, Any]]:
    table_blocks: List[Dict[str, Any]] = []
    for block in evidence_blocks:
        text = str(block.get("text") or "")
        table_text = _table_like_excerpt(text)
        if not table_text:
            continue
        table_blocks.append(
            {
                "id": f"table:{block.get('chunk_id')}",
                "chunk_id": block.get("chunk_id"),
                "document_id": block.get("document_id"),
                "page_start": block.get("page_start"),
                "page_end": block.get("page_end"),
                "section": block.get("section"),
                "source": block.get("source"),
                "source_span_ids": block.get("source_span_ids") or [],
                "title": block.get("title"),
                "text": table_text,
            }
        )
        if len(table_blocks) >= max_blocks:
            break
    return table_blocks


def _table_like_excerpt(text: str, max_chars: int = 1800) -> str:
    lines = [line.strip() for line in str(text or "").splitlines()]
    if len(lines) <= 1:
        compact = " ".join(str(text or "").split())
        if _looks_table_like(compact):
            return compact[:max_chars]
        return ""

    selected = []
    for line in lines:
        if _looks_table_like(line):
            selected.append(line)
    if not selected:
        return ""
    excerpt = "\n".join(selected)
    return excerpt[:max_chars].rstrip()


def _looks_table_like(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    if re.search(r"\btable\s+\d+(?:-\d+)+\b", lowered):
        return True
    if "msgcode" in lowered or "msginfo" in lowered or "data field" in lowered:
        return True
    if re.search(r"\[[0-9]+(?::[0-9]+)?\]", value):
        return True
    if re.search(r"\b[01]{3,}b\b", lowered):
        return True
    if "reserved" in lowered and ("encoding" in lowered or "field" in lowered):
        return True
    return False


def _infer_section_path(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > 140:
            continue
        heading = re.match(r"^(\d+(?:\.\d+){1,8})\s+([A-Z][A-Za-z0-9_.() /\-:]+)$", stripped)
        if heading:
            return stripped
        markdown = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        if markdown:
            return markdown.group(1).strip()
    return ""


def _compact_block_text(value: Any) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "").strip())


def _display_canonical_name(entity: Any, requested_term: str) -> str:
    canonical = getattr(entity, "canonical_name", "") or ""
    aliases = list(getattr(entity, "aliases", []) or [])
    if requested_term and requested_term.upper() == requested_term and len(requested_term) <= 6:
        expanded = [alias for alias in aliases if len(alias) > len(requested_term) and " " in alias]
        if expanded:
            return expanded[0]
    return canonical


def _claim_terms(text: str) -> List[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "called",
        "does",
        "for",
        "how",
        "is",
        "of",
        "or",
        "the",
        "to",
        "uses",
        "what",
        "with",
    }
    import re

    terms = [term for term in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", text or "") if term.lower() not in stopwords]
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    return _unique_strings([*terms, *cjk_terms])


def _section_path(text: str) -> str:
    import re

    for line in (text or "").splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def _unique_strings(values: Iterable[Any]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _portable_source_path(path: Path, workspace_root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    except Exception:
        return str(Path(path).resolve())


class LocalKnowledgeBackend:
    """Optional local document-ingestion and search backend."""

    def __init__(
        self,
        workspace_root: str,
        db_path: Optional[str] = None,
        enabled: bool = True,
        default_kb_id: str = "kb_default",
        chunk_chars: int = 1800,
        overlap_chars: int = 200,
    ):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.enabled = bool(enabled)
        self.default_kb_id = default_kb_id or "kb_default"
        self.chunk_chars = max(500, int(chunk_chars))
        self.overlap_chars = max(0, min(int(overlap_chars), self.chunk_chars // 2))
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.workspace_root / "knowledge" / ".backend" / DEFAULT_DB_NAME
        self._storage: Optional[KnowledgeStorage] = None
        self._builder = HeuristicKnowledgeBuilder()

    def dependency_check(self) -> Dict[str, Any]:
        """Return backend capability status without raising on optional deps."""

        statuses = dependency_status()
        sqlite_status = statuses.get("sqlite3")
        fts5_available = False
        if self.enabled and sqlite_status and sqlite_status.available:
            try:
                storage = self._get_read_storage()
                fts5_available = bool(storage.fts5_available) if storage is not None else False
            except Exception as exc:
                statuses["sqlite3"] = type(sqlite_status)(
                    name="sqlite3",
                    available=False,
                    detail=str(exc),
                )

        return {
            "enabled": self.enabled,
            "dependencies": {name: status.to_dict() for name, status in statuses.items()},
            "fts5": {"available": fts5_available, "detail": "SQLite FTS5 keyword search"},
            "supported_types": [".pdf", ".docx", ".txt", ".md", ".markdown"],
        }

    def ingest_upload(self, file_path: str, title: Optional[str] = None) -> Dict[str, Any]:
        """Ingest an uploaded document synchronously and return job status."""

        if not self.enabled:
            return self._disabled_response("ingest")

        source = Path(file_path).expanduser().resolve()
        storage = self._get_storage(writable=True)
        stored_source_path = _portable_source_path(source, self.workspace_root)
        job = storage.create_job(uuid.uuid4().hex, stored_source_path)
        try:
            if not source.is_file():
                job = storage.update_job(job.id, "failed", error=f"file not found: {source}")
                return {"status": "failed", "job": job.to_dict()}

            extracted = extract_document(source)
            content_hash = compute_file_hash(source)
            document_id = stable_document_id(str(source), content_hash)
            version_id = stable_version_id(document_id, content_hash)
            chunks = self._build_chunks(document_id, extracted.pages, kb_id=self.default_kb_id, version_id=version_id)
            document = KnowledgeDocument(
                id=document_id,
                title=title or extracted.title,
                source_path=stored_source_path,
                mime_type=extracted.mime_type,
                size=source.stat().st_size,
                content_hash=content_hash,
                status="ready",
                kb_id=self.default_kb_id,
                version_id=version_id,
                metadata=extracted.metadata,
            )
            build = self._builder.build(document, chunks)
            storage.save_document(
                document,
                build.chunks,
                source_spans=build.source_spans,
                entities=build.entities,
                relations=build.relations,
            )
            message = f"ingested {len(build.chunks)} chunks"
            job = storage.update_job(job.id, "succeeded", message=message, document_id=document_id)
            return {
                "status": "succeeded",
                "job": job.to_dict(),
                "document": {
                    "id": document_id,
                    "title": document.title,
                    "kb_id": document.kb_id,
                    "version_id": version_id,
                    "chunks": len(build.chunks),
                    "source_spans": len(build.source_spans),
                    "entities": len(build.entities),
                    "relations": len(build.relations),
                    "missing_prerequisites": build.missing_prerequisites,
                },
            }
        except MissingDependencyError as exc:
            job = storage.update_job(job.id, "failed", error=str(exc))
            return {"status": "failed", "job": job.to_dict(), "missing_dependency": True}
        except UnsupportedDocumentError as exc:
            job = storage.update_job(job.id, "failed", error=str(exc))
            return {"status": "failed", "job": job.to_dict()}
        except ExtractionError as exc:
            job = storage.update_job(job.id, "failed", error=str(exc))
            return {"status": "failed", "job": job.to_dict()}
        except Exception as exc:
            logger.error(f"[LocalKnowledgeBackend] ingestion failed: {exc}", exc_info=True)
            job = storage.update_job(job.id, "failed", error=str(exc))
            return {"status": "failed", "job": job.to_dict()}

    def job_status(self, job_id: str) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled_response("job_status")
        storage = self._get_read_storage()
        if storage is None:
            return {"status": "not_found", "job": None}
        job = storage.get_job(job_id)
        if not job:
            return {"status": "not_found", "job": None}
        return {"status": job.status, "job": job.to_dict()}

    def search(
        self,
        query: str,
        limit: int = 5,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled_response("search")
        expanded_query = self._expand_query_aliases(query)
        storage = self._get_read_storage()
        hits = storage.search(expanded_query, limit=max(1, int(limit or 5))) if storage is not None else []
        if kb_ids:
            kb_set = {str(kb_id) for kb_id in kb_ids}
            hits = [hit for hit in hits if hit.kb_id in kb_set]
        return {
            "status": "ok",
            "hits": [hit.to_dict() for hit in hits],
            "trace_id": trace_id,
            "visited_kb_ids": list(visited_kb_ids or []),
        }

    def query(
        self,
        question: str,
        limit: int = 5,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        """Return a simple extractive answer with citations."""

        if not self.enabled:
            return self._disabled_response("query")
        expanded_query = self._expand_query_aliases(question)
        storage = self._get_read_storage()
        hits = storage.search(expanded_query, limit=max(1, int(limit or 5))) if storage is not None else []
        if kb_ids:
            kb_set = {str(kb_id) for kb_id in kb_ids}
            hits = [hit for hit in hits if hit.kb_id in kb_set]
        if not hits:
            return QueryResult(
                answer="No relevant local knowledge found.",
                citations=[],
                trace_id=trace_id,
            ).to_dict()

        citations = [
            Citation(
                index=index,
                document_id=hit.document_id,
                title=hit.title,
                source_path=hit.source_path,
                page_start=hit.page_start,
                page_end=hit.page_end,
                snippet=hit.snippet,
                kb_id=hit.kb_id,
                source_span_ids=hit.source_span_ids,
            )
            for index, hit in enumerate(hits, start=1)
        ]
        answer = self._compose_answer(citations)
        entities = sorted({entity for hit in hits for entity in hit.entities})
        confidence = max((hit.score for hit in hits), default=0.0)
        return QueryResult(
            answer=answer,
            citations=citations,
            entities=entities,
            confidence=confidence,
            trace_id=trace_id,
        ).to_dict()

    def deep_query(
        self,
        question: str,
        limit: int = 5,
        context_window: int = 1,
        max_evidence_chars: int = 12000,
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        """Return ordered source evidence expanded around the initial hits."""

        if not self.enabled:
            return self._disabled_response("deep_query")
        expanded_query = self._expand_query_aliases(question)
        storage = self._get_read_storage()
        hits = storage.search(expanded_query, limit=max(1, int(limit or 5))) if storage is not None else []
        kb_set = {str(kb_id) for kb_id in kb_ids} if kb_ids else set()
        if kb_set:
            hits = [hit for hit in hits if hit.kb_id in kb_set]
        if hits and storage is not None:
            hits = self._supplement_deep_hits(
                storage,
                hits,
                question,
                limit=max(1, int(limit or 5)),
                kb_set=kb_set,
            )
        if not hits or storage is None:
            return {
                "status": "insufficient",
                "query": question,
                "answer_policy": _deep_query_answer_policy(),
                "evidence_blocks": [],
                "table_blocks": [],
                "citations": [],
                "coverage_terms": [],
                "missing_terms": _claim_terms(question),
                "confidence": 0.0,
                "trace_id": trace_id,
                "visited_kb_ids": list(visited_kb_ids or []),
            }

        selected_chunks: Dict[str, KnowledgeChunk] = {}
        hit_chunk_ids = {hit.chunk_id for hit in hits}
        hit_scores = {hit.chunk_id: hit.score for hit in hits}
        for hit in hits:
            for chunk in storage.get_chunks_near(hit.document_id, hit.ordinal, context_window):
                if kb_set and chunk.kb_id not in kb_set:
                    continue
                selected_chunks[chunk.id] = chunk

        ordered_chunks = sorted(selected_chunks.values(), key=lambda item: (item.document_id, item.ordinal))
        source_spans = {
            span.id: span
            for span in storage.get_source_spans(
                span_id for chunk in ordered_chunks for span_id in chunk.source_span_ids
            )
        }
        documents = {}
        for document_id in sorted({chunk.document_id for chunk in ordered_chunks}):
            document = storage.get_document(document_id)
            if document is not None:
                documents[document_id] = document
        evidence_blocks = _build_deep_evidence_blocks(
            ordered_chunks,
            documents,
            source_spans,
            hit_chunk_ids,
            hit_scores,
            max_evidence_chars=max(1, int(max_evidence_chars or 12000)),
        )
        evidence_text = " ".join(block.get("text", "") for block in evidence_blocks)
        claim_terms = _unique_strings([*_claim_terms(question), *_deep_query_required_terms(question)])
        coverage_terms = _matched_terms(claim_terms, evidence_text)
        missing_terms = [term for term in claim_terms if term not in set(coverage_terms)]
        confidence = _deep_query_confidence(hits, coverage_terms, claim_terms)
        required_missing = [term for term in _deep_query_required_terms(question) if term in set(missing_terms)]
        status = "ok" if evidence_blocks and (not claim_terms or coverage_terms) and not required_missing else "insufficient"
        citations = _deep_query_citations(hits, documents)
        table_blocks = _extract_table_blocks(evidence_blocks)
        return {
            "status": status,
            "query": question,
            "answer_policy": _deep_query_answer_policy(),
            "evidence_blocks": evidence_blocks,
            "table_blocks": table_blocks,
            "citations": citations,
            "coverage_terms": coverage_terms,
            "missing_terms": missing_terms,
            "confidence": confidence,
            "trace_id": trace_id,
            "visited_kb_ids": list(visited_kb_ids or []),
        }

    def _supplement_deep_hits(
        self,
        storage: KnowledgeStorage,
        hits: List[Any],
        question: str,
        *,
        limit: int,
        kb_set: set[str],
    ) -> List[Any]:
        by_chunk = {hit.chunk_id: hit for hit in hits}
        for term in _deep_query_supplemental_terms(question):
            supplemental_hits = storage.search(
                self._expand_query_aliases(term),
                limit=max(1, min(3, limit)),
            )
            for hit in supplemental_hits:
                if kb_set and hit.kb_id not in kb_set:
                    continue
                by_chunk.setdefault(hit.chunk_id, hit)
        target_limit = max(limit, min(len(by_chunk), limit * 2))
        return sorted(by_chunk.values(), key=lambda item: (-float(item.score), item.document_id, item.ordinal))[
            :target_limit
        ]

    def resolve_entities(
        self,
        terms: Iterable[str],
        kb_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        storage = self._get_read_storage()
        if storage is None:
            return {
                "entities": [
                    {"term": str(term), "resolved": False, "visited_kb_ids": list(visited_kb_ids or kb_ids or [])}
                    for term in terms or []
                ],
                "visited_kb_ids": list(visited_kb_ids or kb_ids or []),
            }
        visited = list(visited_kb_ids or kb_ids or [])
        allowed_kbs = {str(kb_id) for kb_id in (kb_ids or []) if kb_id}
        entities = []
        for term in terms or []:
            entity = storage.resolve_entity(str(term))
            if entity and allowed_kbs and entity.defining_kb_id and entity.defining_kb_id not in allowed_kbs:
                entity = None
            if entity:
                canonical_name = _display_canonical_name(entity, str(term))
                entities.append(
                    {
                        "term": str(term),
                        "resolved": True,
                        "entity_id": entity.id,
                        "canonical_name": canonical_name,
                        "kb_id": entity.defining_kb_id,
                        "aliases": entity.aliases,
                        "confidence": entity.confidence,
                        "visited_kb_ids": visited,
                    }
                )
            else:
                entities.append({"term": str(term), "resolved": False, "visited_kb_ids": visited})
        return {"entities": entities, "visited_kb_ids": visited}

    def graph_neighbors(
        self,
        entity_id: str = "",
        term: str = "",
        kb_id: str = "",
        max_hops: int = 1,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        storage = self._get_read_storage()
        if storage is None:
            return {"nodes": [], "links": [], "trace_id": trace_id, "visited_kb_ids": list(visited_kb_ids or [])}
        max_hops = max(1, int(max_hops or 1))
        start = storage.resolve_entity(term or entity_id)
        if start is None:
            return {"nodes": [], "links": [], "trace_id": trace_id, "visited_kb_ids": list(visited_kb_ids or [])}

        nodes: Dict[str, Dict[str, Any]] = {start.id: _entity_payload(start)}
        links: Dict[str, Dict[str, Any]] = {}
        frontier = {start.id}
        visited_entities = set()
        for hop in range(1, max_hops + 1):
            next_frontier = set()
            for current_id in frontier:
                if current_id in visited_entities:
                    continue
                visited_entities.add(current_id)
                for relation in storage.list_relations(entity_id=current_id, kb_id=kb_id):
                    links[relation.id] = {**_relation_payload(relation), "hop": hop}
                    for related_id in (relation.subject_entity_id, relation.object_entity_id):
                        if related_id not in nodes:
                            related = storage.list_entities([relation.subject if related_id == relation.subject_entity_id else relation.object])
                            if related:
                                nodes[related_id] = _entity_payload(related[0])
                        if related_id not in visited_entities:
                            next_frontier.add(related_id)
            frontier = next_frontier
        return {
            "nodes": list(nodes.values()),
            "links": list(links.values()),
            "trace_id": trace_id,
            "visited_kb_ids": list(visited_kb_ids or []),
        }

    def verify_source(
        self,
        claim: str,
        candidate_span_ids: Optional[Iterable[str]] = None,
        visited_kb_ids: Optional[Iterable[str]] = None,
        trace_id: str = "",
    ) -> Dict[str, Any]:
        storage = self._get_read_storage()
        if storage is None:
            return {
                "status": "insufficient",
                "supported": False,
                "claim": claim,
                "evidence": [],
                "trace_id": trace_id,
                "visited_kb_ids": list(visited_kb_ids or []),
            }
        span_ids = [span_id for span_id in (candidate_span_ids or []) if span_id]
        spans = storage.get_source_spans(span_ids) if span_ids else []
        if not spans:
            hits = storage.search(self._expand_query_aliases(claim), limit=3)
            spans = storage.get_source_spans(span_id for hit in hits for span_id in hit.source_span_ids)

        claim_terms = _claim_terms(claim)
        evidence_text = " ".join(span.text for span in spans).lower()
        matched = [term for term in claim_terms if term.lower() in evidence_text]
        coverage = len(matched) / max(1, len(claim_terms))
        status = "supported" if spans and coverage >= 0.5 else "insufficient"
        return {
            "status": status,
            "supported": status == "supported",
            "claim": claim,
            "confidence": round(coverage, 3),
            "matched_terms": matched,
            "evidence": [span.to_dict() for span in spans],
            "trace_id": trace_id,
            "visited_kb_ids": list(visited_kb_ids or []),
        }

    def stats(self) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled_response("stats")
        try:
            storage = self._get_read_storage()
            if storage is None:
                return {"status": "ok", **self._empty_health()}
            return {"status": "ok", **storage.health()}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def close(self) -> None:
        """Close the SQLite connection if it has been opened."""

        if self._storage is not None:
            self._storage.close()
            self._storage = None

    def __enter__(self) -> "LocalKnowledgeBackend":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def dispatch(self, action: str, payload: Optional[dict] = None) -> Dict[str, Any]:
        """Protocol-friendly action dispatcher."""

        payload = payload or {}
        try:
            if action in ("dependencies", "dependency_check"):
                return {"action": action, "code": 200, "message": "success", "payload": self.dependency_check()}
            if action in ("ingest", "upload"):
                file_path = payload.get("path") or payload.get("file_path")
                if not file_path:
                    return {"action": action, "code": 400, "message": "path is required", "payload": None}
                return {"action": action, "code": 200, "message": "success", "payload": self.ingest_upload(file_path, payload.get("title"))}
            if action in ("job", "job_status"):
                job_id = payload.get("job_id") or payload.get("id")
                if not job_id:
                    return {"action": action, "code": 400, "message": "job_id is required", "payload": None}
                return {"action": action, "code": 200, "message": "success", "payload": self.job_status(job_id)}
            if action == "search":
                return {"action": action, "code": 200, "message": "success", "payload": self.search(payload.get("query", ""), payload.get("limit", 5))}
            if action == "query":
                return {"action": action, "code": 200, "message": "success", "payload": self.query(payload.get("query", "") or payload.get("question", ""), payload.get("limit", 5))}
            if action == "deep_query":
                return {
                    "action": action,
                    "code": 200,
                    "message": "success",
                    "payload": self.deep_query(
                        payload.get("query", "") or payload.get("question", ""),
                        limit=payload.get("limit", 5),
                        context_window=payload.get("context_window", 1),
                        max_evidence_chars=payload.get("max_evidence_chars", 12000),
                    ),
                }
            if action == "stats":
                return {"action": action, "code": 200, "message": "success", "payload": self.stats()}
            if action.startswith("visual"):
                return {"action": action, "code": 400, "message": "visual actions require KnowledgeBackendService", "payload": None}
            return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
        except Exception as exc:
            logger.error(f"[LocalKnowledgeBackend] dispatch error: action={action}, error={exc}", exc_info=True)
            return {"action": action, "code": 500, "message": str(exc), "payload": None}

    def _get_read_storage(self) -> Optional[KnowledgeStorage]:
        if self._storage is None and not self.db_path.is_file():
            return None
        return self._get_storage(writable=False)

    def _get_storage(self, writable: bool = False) -> KnowledgeStorage:
        if self._storage is not None and writable and self._storage.read_only:
            self._storage.close()
            self._storage = None
        if self._storage is None:
            self._storage = KnowledgeStorage(self.db_path, read_only=not writable)
        return self._storage

    def _build_chunks(
        self,
        document_id: str,
        pages: List[Any],
        kb_id: str = "kb_default",
        version_id: str = "",
    ) -> List[KnowledgeChunk]:
        chunks: List[KnowledgeChunk] = []
        ordinal = 0
        for page in pages:
            section_path = _section_path(page.text)
            for text in self._split_text(page.text):
                if not text.strip():
                    continue
                ordinal += 1
                chunks.append(
                    KnowledgeChunk(
                        id=stable_chunk_id(document_id, ordinal, text),
                        document_id=document_id,
                        ordinal=ordinal,
                        page_start=page.page,
                        page_end=page.page,
                        text=text,
                        kb_id=kb_id,
                        version_id=version_id,
                        section_path=section_path,
                        clause_title=section_path.split("/")[-1] if section_path else "",
                    )
                )
        return chunks

    def _expand_query_aliases(self, query: str) -> str:
        query = str(query or "")
        storage = self._get_read_storage()
        if storage is None:
            return query
        additions: List[str] = []
        for term in _claim_terms(query):
            entity = storage.resolve_entity(term)
            if entity:
                additions.extend([entity.canonical_name, *entity.aliases])
        if not additions:
            return query
        return " ".join([query, *_unique_strings(additions)])

    def _split_text(self, text: str) -> List[str]:
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.chunk_chars:
            return [text]
        chunks: List[str] = []
        start = 0
        while start < len(text):
            end = min(len(text), start + self.chunk_chars)
            chunks.append(text[start:end].strip())
            if end == len(text):
                break
            start = max(end - self.overlap_chars, start + 1)
        return chunks

    @staticmethod
    def _compose_answer(citations: List[Citation]) -> str:
        lines = ["Relevant local knowledge:"]
        for citation in citations:
            page = f"p. {citation.page_start}" if citation.page_start == citation.page_end else f"pp. {citation.page_start}-{citation.page_end}"
            lines.append(f"[{citation.index}] {citation.title} ({page}): {citation.snippet}")
        return "\n".join(lines)

    @staticmethod
    def _disabled_response(action: str) -> Dict[str, Any]:
        return {"status": "disabled", "action": action, "message": "local knowledge backend is disabled"}

    def _empty_health(self) -> Dict[str, Any]:
        return {
            "sqlite": True,
            "fts5": False,
            "db_path": str(self.db_path),
            **{
                key: 0
                for key in (
                    "knowledge_bases",
                    "documents",
                    "chunks",
                    "source_spans",
                    "entities",
                    "relations",
                    "jobs",
                )
            },
        }
