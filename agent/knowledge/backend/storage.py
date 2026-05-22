"""SQLite metadata and full-text storage for local knowledge backend."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    IngestionJob,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeRelation,
    SearchHit,
    SourceSpan,
)


class KnowledgeStorage:
    """SQLite-backed document metadata and chunk search."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.fts5_available = self._check_fts5_support()
        self._init_schema()

    def close(self) -> None:
        if self.conn:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def health(self) -> Dict[str, Any]:
        return {
            "sqlite": True,
            "fts5": self.fts5_available,
            "db_path": str(self.db_path),
            **self.stats(),
        }

    def stats(self) -> Dict[str, int]:
        documents = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        jobs = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        source_spans = self.conn.execute("SELECT COUNT(*) FROM source_spans").fetchone()[0]
        entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relations = self.conn.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0]
        knowledge_bases = self.conn.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0]
        return {
            "knowledge_bases": int(knowledge_bases),
            "documents": int(documents),
            "chunks": int(chunks),
            "source_spans": int(source_spans),
            "entities": int(entities),
            "relations": int(relations),
            "jobs": int(jobs),
        }

    def create_job(self, job_id: str, source_path: str) -> IngestionJob:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO jobs(id, document_id, source_path, status, message, error, created_at, updated_at)
            VALUES (?, NULL, ?, 'queued', '', '', ?, ?)
            """,
            (job_id, source_path, now, now),
        )
        self.conn.commit()
        return self.get_job(job_id)

    def update_job(
        self,
        job_id: str,
        status: str,
        message: str = "",
        error: str = "",
        document_id: Optional[str] = None,
    ) -> IngestionJob:
        self.conn.execute(
            """
            UPDATE jobs
            SET status = ?, message = ?, error = ?, document_id = COALESCE(?, document_id),
                updated_at = ?
            WHERE id = ?
            """,
            (status, message, error, document_id, _now(), job_id),
        )
        self.conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[IngestionJob]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def save_document(
        self,
        document: KnowledgeDocument,
        chunks: Iterable[KnowledgeChunk],
        source_spans: Iterable[SourceSpan] = (),
        entities: Iterable[KnowledgeEntity] = (),
        relations: Iterable[KnowledgeRelation] = (),
    ) -> None:
        chunks = list(chunks)
        source_spans = list(source_spans)
        entities = list(entities)
        relations = list(relations)
        kb_id = document.kb_id or "kb_default"
        version_id = document.version_id or stable_version_id(document.id, document.content_hash)
        self.ensure_knowledge_base(KnowledgeBase(id=kb_id, name=kb_id))
        self.conn.execute(
            """
            INSERT OR REPLACE INTO documents(
                id, title, source_path, mime_type, size, content_hash, status, error,
                kb_id, doc_type, version_id, metadata, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.id,
                document.title,
                document.source_path,
                document.mime_type,
                document.size,
                document.content_hash,
                document.status,
                document.error,
                kb_id,
                document.doc_type or "document",
                version_id,
                json.dumps(document.metadata, ensure_ascii=False),
                _now(),
            ),
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO document_versions(id, document_id, version_label, content_hash, source_path, created_at)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM document_versions WHERE id = ?), ?))
            """,
            (version_id, document.id, version_id, document.content_hash, document.source_path, version_id, _now()),
        )
        self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document.id,))
        self.conn.execute("DELETE FROM source_spans WHERE document_id = ?", (document.id,))
        self.conn.executemany(
            """
            INSERT INTO chunks(
                id, document_id, ordinal, page_start, page_end, text,
                kb_id, version_id, section_path, clause_title, source_span_ids, entities, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chunk.id,
                    chunk.document_id,
                    chunk.ordinal,
                    chunk.page_start,
                    chunk.page_end,
                    chunk.text,
                    chunk.kb_id or kb_id,
                    chunk.version_id or version_id,
                    chunk.section_path,
                    chunk.clause_title,
                    _json(chunk.source_span_ids),
                    _json(chunk.entities),
                    _json(chunk.metadata),
                )
                for chunk in chunks
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO source_spans(
                id, document_id, version_id, source_file, page_start, page_end, section_path,
                paragraph_index_start, paragraph_index_end, char_start, char_end, bbox, text_hash, text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    span.id,
                    span.document_id,
                    span.version_id or version_id,
                    span.source_file,
                    span.page_start,
                    span.page_end,
                    span.section_path,
                    span.paragraph_index_start,
                    span.paragraph_index_end,
                    span.char_start,
                    span.char_end,
                    _json(span.bbox or {}),
                    span.text_hash,
                    span.text,
                )
                for span in source_spans
            ],
        )
        for entity in entities:
            self.upsert_entity(entity)
        self.conn.execute("DELETE FROM knowledge_relations WHERE source_doc_id = ?", (document.id,))
        for relation in relations:
            self.upsert_relation(relation, source_doc_id=document.id)
        if self.fts5_available:
            self._rebuild_fts()
        self.conn.commit()

    def get_document(self, document_id: str) -> Optional[KnowledgeDocument]:
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return _row_to_document(row) if row else None

    def list_documents(self) -> List[KnowledgeDocument]:
        rows = self.conn.execute("SELECT * FROM documents ORDER BY updated_at DESC, title ASC").fetchall()
        return [_row_to_document(row) for row in rows]

    def ensure_knowledge_base(self, kb: KnowledgeBase) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO knowledge_bases(id, name, description, domains, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = COALESCE(NULLIF(excluded.description, ''), knowledge_bases.description),
                domains = COALESCE(NULLIF(excluded.domains, '[]'), knowledge_bases.domains),
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (kb.id, kb.name, kb.description, _json(kb.domains), kb.status, now, now),
        )

    def list_knowledge_bases(self) -> List[KnowledgeBase]:
        rows = self.conn.execute("SELECT * FROM knowledge_bases ORDER BY name ASC").fetchall()
        return [
            KnowledgeBase(
                id=row["id"],
                name=row["name"],
                description=row["description"] or "",
                domains=_loads(row["domains"]),
                status=row["status"] or "active",
            )
            for row in rows
        ]

    def get_source_span(self, span_id: str) -> Optional[SourceSpan]:
        row = self.conn.execute("SELECT * FROM source_spans WHERE id = ?", (span_id,)).fetchone()
        return _row_to_span(row) if row else None

    def get_source_spans(self, span_ids: Iterable[str]) -> List[SourceSpan]:
        ids = [span_id for span_id in span_ids if span_id]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM source_spans WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return [_row_to_span(row) for row in rows]

    def list_entities(self, terms: Optional[Iterable[str]] = None) -> List[KnowledgeEntity]:
        if terms is None:
            rows = self.conn.execute("SELECT * FROM entities ORDER BY canonical_name ASC").fetchall()
            return [self._row_to_entity(row) for row in rows]
        seen = {}
        for term in terms:
            entity = self.resolve_entity(str(term))
            if entity:
                seen[entity.id] = entity
        return list(seen.values())

    def resolve_entity(self, term: str) -> Optional[KnowledgeEntity]:
        normalized = _normalize_alias(term)
        if not normalized:
            return None
        row = self.conn.execute(
            """
            SELECT e.*
            FROM entity_aliases a
            JOIN entities e ON e.id = a.entity_id
            WHERE a.normalized_alias = ?
            ORDER BY a.confidence DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if row:
            return self._row_to_entity(row)
        row = self.conn.execute(
            "SELECT * FROM entities WHERE lower(canonical_name) = lower(?) LIMIT 1",
            (term,),
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def upsert_entity(self, entity: KnowledgeEntity) -> KnowledgeEntity:
        existing = self.resolve_entity(entity.canonical_name)
        entity_id = existing.id if existing else entity.id
        now = _now()
        if existing:
            defining_kb_id = entity.defining_kb_id or existing.defining_kb_id
            defining_doc_id = entity.defining_doc_id or existing.defining_doc_id
            confidence = max(float(entity.confidence), float(existing.confidence))
            description = entity.description or existing.description
        else:
            defining_kb_id = entity.defining_kb_id
            defining_doc_id = entity.defining_doc_id
            confidence = float(entity.confidence)
            description = entity.description
        self.conn.execute(
            """
            INSERT INTO entities(
                id, canonical_name, entity_type, description, defining_kb_id, defining_doc_id,
                confidence, source_span_ids, metadata, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                entity_type = excluded.entity_type,
                description = excluded.description,
                defining_kb_id = excluded.defining_kb_id,
                defining_doc_id = excluded.defining_doc_id,
                confidence = excluded.confidence,
                source_span_ids = excluded.source_span_ids,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                entity_id,
                entity.canonical_name,
                entity.entity_type,
                description,
                defining_kb_id,
                defining_doc_id,
                confidence,
                _json(_unique((existing.source_span_ids if existing else []) + entity.source_span_ids)),
                _json({**(existing.metadata if existing else {}), **entity.metadata}),
                now,
                now,
            ),
        )
        aliases = _unique([entity.canonical_name, *entity.aliases])
        for alias in aliases:
            self._upsert_alias(entity_id, alias, confidence)
        return self._row_to_entity(self.conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone())

    def upsert_relation(self, relation: KnowledgeRelation, source_doc_id: str = "") -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO knowledge_relations(
                id, subject_entity_id, predicate, object_entity_id, subject, object,
                source_kb_id, target_kb_id, source_doc_id, evidence_span_ids, confidence, status, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM knowledge_relations WHERE id = ?), ?))
            """,
            (
                relation.id,
                relation.subject_entity_id,
                relation.predicate,
                relation.object_entity_id,
                relation.subject,
                relation.object,
                relation.source_kb_id,
                relation.target_kb_id,
                source_doc_id,
                _json(relation.evidence_span_ids),
                float(relation.confidence),
                relation.status,
                _json(relation.metadata),
                relation.id,
                _now(),
            ),
        )

    def list_relations(self, entity_id: str = "", kb_id: str = "", include_candidates: bool = True) -> List[KnowledgeRelation]:
        clauses = []
        params: List[Any] = []
        if entity_id:
            clauses.append("(subject_entity_id = ? OR object_entity_id = ?)")
            params.extend([entity_id, entity_id])
        if kb_id:
            clauses.append("(source_kb_id = ? OR target_kb_id = ?)")
            params.extend([kb_id, kb_id])
        if not include_candidates:
            clauses.append("status = 'active'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM knowledge_relations {where} ORDER BY confidence DESC, created_at DESC",
            params,
        ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def chunks_for_entities(self, terms: Iterable[str], limit: int = 5) -> List[SearchHit]:
        normalized_terms = [_normalize_alias(term) for term in terms if _normalize_alias(term)]
        if not normalized_terms:
            return []
        where = " OR ".join("LOWER(c.entities) LIKE ?" for _ in normalized_terms)
        params = [f"%{term}%" for term in normalized_terms]
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT c.*, d.title, d.source_path
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where}
            ORDER BY c.ordinal ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_hit(row, 0.55) for row in rows]

    def save_query_trace(self, trace_id: str, query: str, route: Dict[str, Any], retrieved: List[Dict[str, Any]], verification: Dict[str, Any], confidence: float, latency_ms: int) -> None:
        if not trace_id:
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO query_traces(id, query, route, retrieved_chunks, source_verification, final_confidence, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM query_traces WHERE id = ?), ?))
            """,
            (trace_id, query, _json(route), _json(retrieved), _json(verification), confidence, latency_ms, trace_id, _now()),
        )
        self.conn.commit()

    def _upsert_alias(self, entity_id: str, alias: str, confidence: float) -> None:
        normalized = _normalize_alias(alias)
        if not normalized:
            return
        alias_id = hashlib.sha256(f"alias:{normalized}".encode("utf-8")).hexdigest()[:24]
        self.conn.execute(
            """
            INSERT INTO entity_aliases(id, entity_id, alias, normalized_alias, confidence)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(normalized_alias) DO UPDATE SET
                entity_id = excluded.entity_id,
                alias = excluded.alias,
                confidence = MAX(entity_aliases.confidence, excluded.confidence)
            """,
            (alias_id, entity_id, alias, normalized, float(confidence)),
        )

    def _row_to_entity(self, row: sqlite3.Row) -> KnowledgeEntity:
        aliases = [
            alias_row["alias"]
            for alias_row in self.conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias ASC",
                (row["id"],),
            ).fetchall()
        ]
        return KnowledgeEntity(
            id=row["id"],
            canonical_name=row["canonical_name"],
            entity_type=row["entity_type"],
            description=row["description"] or "",
            defining_kb_id=row["defining_kb_id"],
            defining_doc_id=row["defining_doc_id"],
            confidence=float(row["confidence"] or 0),
            aliases=aliases,
            source_span_ids=_loads(row["source_span_ids"]),
            metadata=_loads(row["metadata"]),
        )

    def search(self, query: str, limit: int = 5) -> List[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        if self.fts5_available:
            hits = self._search_fts(query, limit)
            if hits:
                return hits
        return self._search_like(query, limit)

    def _check_fts5_support(self) -> bool:
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_probe USING fts5(text)")
            self.conn.execute("DROP TABLE IF EXISTS knowledge_fts_probe")
            return True
        except sqlite3.OperationalError:
            return False

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_bases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                domains TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                metadata TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
            """
        )
        self._ensure_column("documents", "kb_id", "TEXT NOT NULL DEFAULT 'kb_default'")
        self._ensure_column("documents", "doc_type", "TEXT NOT NULL DEFAULT 'document'")
        self._ensure_column("documents", "version_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("documents", "original_filename", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_kb ON documents(kb_id, title)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_versions (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                version_label TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                source_path TEXT NOT NULL,
                parsed_path TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
            """
        )
        self._ensure_column("chunks", "kb_id", "TEXT NOT NULL DEFAULT 'kb_default'")
        self._ensure_column("chunks", "version_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("chunks", "section_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("chunks", "clause_title", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("chunks", "source_span_ids", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("chunks", "entities", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("chunks", "metadata", "TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id, ordinal)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_kb ON chunks(kb_id, document_id)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_spans (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                source_file TEXT NOT NULL,
                page_start INTEGER NOT NULL,
                page_end INTEGER NOT NULL,
                section_path TEXT NOT NULL DEFAULT '',
                paragraph_index_start INTEGER NOT NULL DEFAULT 0,
                paragraph_index_end INTEGER NOT NULL DEFAULT 0,
                char_start INTEGER NOT NULL DEFAULT 0,
                char_end INTEGER NOT NULL DEFAULT 0,
                bbox TEXT NOT NULL DEFAULT '{}',
                text_hash TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_source_spans_document ON source_spans(document_id, page_start)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                defining_kb_id TEXT,
                defining_doc_id TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                source_span_ids TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_aliases (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                normalized_alias TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(entity_id)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_relations (
                id TEXT PRIMARY KEY,
                subject_entity_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_entity_id TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                object TEXT NOT NULL DEFAULT '',
                source_kb_id TEXT NOT NULL DEFAULT '',
                target_kb_id TEXT NOT NULL DEFAULT '',
                source_doc_id TEXT NOT NULL DEFAULT '',
                evidence_span_ids TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_subject ON knowledge_relations(subject_entity_id, predicate)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_object ON knowledge_relations(object_entity_id, predicate)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_kb_registry (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                auth_type TEXT NOT NULL DEFAULT 'bearer',
                token_ref TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '{}',
                domains TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                document_id TEXT,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_traces (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                route TEXT NOT NULL DEFAULT '{}',
                retrieved_chunks TEXT NOT NULL DEFAULT '[]',
                source_verification TEXT NOT NULL DEFAULT '{}',
                final_confidence REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        if self.fts5_available:
            self._init_fts()
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_fts(self) -> None:
        self.conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
        self.conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
        self.conn.execute("DROP TRIGGER IF EXISTS chunks_au")
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        if row and "content='chunks'" in (row["sql"] or ""):
            self.conn.execute("DROP TABLE IF EXISTS chunks_fts")
        if row and "kb_id" not in (row["sql"] or ""):
            self.conn.execute("DROP TABLE IF EXISTS chunks_fts")
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                chunk_id UNINDEXED,
                document_id UNINDEXED,
                kb_id UNINDEXED,
                title UNINDEXED,
                section_path UNINDEXED
            )
            """
        )
        self._rebuild_fts()

    def _rebuild_fts(self) -> None:
        try:
            self.conn.execute("DELETE FROM chunks_fts")
            self.conn.execute(
                """
                INSERT INTO chunks_fts(text, chunk_id, document_id, kb_id, title, section_path)
                SELECT c.text, c.id, c.document_id, c.kb_id, d.title, c.section_path
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                """
            )
        except sqlite3.DatabaseError:
            self.fts5_available = False

    def _search_fts(self, query: str, limit: int) -> List[SearchHit]:
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        try:
            rows = self.conn.execute(
                """
                SELECT c.*, d.title, d.source_path, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [_row_to_hit(row, _rank_to_score(row["rank"])) for row in rows]

    def _search_like(self, query: str, limit: int) -> List[SearchHit]:
        terms = _search_terms(query)
        if not terms:
            return []
        where = " OR ".join("LOWER(c.text) LIKE ?" for _ in terms)
        params = [f"%{term.lower()}%" for term in terms]
        params.append(limit)
        rows = self.conn.execute(
            f"""
                SELECT c.*, d.title, d.source_path
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where}
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_row_to_hit(row, 0.5) for row in rows]


def stable_document_id(source_path: str, content_hash: str) -> str:
    raw = Path(source_path).expanduser().resolve().as_posix()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stable_version_id(document_id: str, content_hash: str) -> str:
    raw = f"{document_id}:{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stable_chunk_id(document_id: str, ordinal: int, text: str) -> str:
    raw = f"{document_id}:{ordinal}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stable_span_id(document_id: str, ordinal: int, text: str) -> str:
    raw = f"span:{document_id}:{ordinal}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def stable_entity_id(canonical_name: str) -> str:
    return hashlib.sha256(f"entity:{_normalize_alias(canonical_name)}".encode("utf-8")).hexdigest()[:24]


def stable_relation_id(subject_id: str, predicate: str, object_id: str, evidence_span_ids: Iterable[str]) -> str:
    raw = f"relation:{subject_id}:{predicate}:{object_id}:{','.join(evidence_span_ids)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def compute_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _row_to_document(row: sqlite3.Row) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=row["id"],
        title=row["title"],
        source_path=row["source_path"],
        mime_type=row["mime_type"],
        size=int(row["size"]),
        content_hash=row["content_hash"],
        status=row["status"],
        error=row["error"] or "",
        kb_id=row["kb_id"] or "kb_default",
        doc_type=row["doc_type"] or "document",
        version_id=row["version_id"] or "",
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _row_to_job(row: sqlite3.Row) -> IngestionJob:
    return IngestionJob(
        id=row["id"],
        document_id=row["document_id"],
        source_path=row["source_path"],
        status=row["status"],
        message=row["message"] or "",
        error=row["error"] or "",
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _row_to_hit(row: sqlite3.Row, score: float) -> SearchHit:
    return SearchHit(
        document_id=row["document_id"],
        chunk_id=row["id"],
        title=row["title"],
        source_path=row["source_path"],
        page_start=int(row["page_start"]),
        page_end=int(row["page_end"]),
        score=score,
        snippet=_snippet(row["text"]),
        kb_id=row["kb_id"] or "kb_default",
        section_path=row["section_path"] or "",
        source_span_ids=_loads(row["source_span_ids"]),
        entities=_loads(row["entities"]),
    )


def _row_to_span(row: sqlite3.Row) -> SourceSpan:
    return SourceSpan(
        id=row["id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        source_file=row["source_file"],
        page_start=int(row["page_start"]),
        page_end=int(row["page_end"]),
        section_path=row["section_path"] or "",
        paragraph_index_start=int(row["paragraph_index_start"] or 0),
        paragraph_index_end=int(row["paragraph_index_end"] or 0),
        char_start=int(row["char_start"] or 0),
        char_end=int(row["char_end"] or 0),
        bbox=_loads(row["bbox"]) or None,
        text_hash=row["text_hash"] or "",
        text=row["text"] or "",
    )


def _row_to_relation(row: sqlite3.Row) -> KnowledgeRelation:
    return KnowledgeRelation(
        id=row["id"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        object_entity_id=row["object_entity_id"],
        subject=row["subject"] or "",
        object=row["object"] or "",
        source_kb_id=row["source_kb_id"] or "",
        target_kb_id=row["target_kb_id"] or "",
        evidence_span_ids=_loads(row["evidence_span_ids"]),
        confidence=float(row["confidence"] or 0),
        status=row["status"] or "active",
        metadata=_loads(row["metadata"]),
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any) -> Any:
    if not value:
        return [] if value == "[]" else {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []


def _normalize_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _unique(values: Iterable[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        key = str(value).lower()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _build_fts_query(raw_query: str) -> Optional[str]:
    tokens = re.findall(r"[A-Za-z0-9_]+", raw_query)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def _search_terms(raw_query: str) -> List[str]:
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", raw_query)
    ascii_terms = [term for term in re.findall(r"[A-Za-z0-9_]+", raw_query) if len(term) >= 2]
    return cjk + ascii_terms


def _rank_to_score(rank: Optional[float]) -> float:
    if rank is None:
        return 0.0
    return 1 / (1 + max(0.0, rank))


def _snippet(text: str, max_chars: int = 500) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def _now() -> int:
    return int(time.time())
