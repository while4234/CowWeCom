"""SQLite metadata and full-text storage for local knowledge backend."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import (
    IngestionJob,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeRelation,
    SearchHit,
    SourceSpan,
    VisualArtifactCandidate,
)


class KnowledgeStorage:
    """SQLite-backed document metadata and chunk search."""

    def __init__(self, db_path: Path, read_only: bool = False, immutable_read: bool = True):
        self.db_path = Path(db_path)
        self.read_only = bool(read_only)
        self.immutable_read = bool(immutable_read)
        self.conn = self._connect()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.read_only:
            self.fts5_available = self._detect_existing_fts()
        else:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.fts5_available = self._check_fts5_support()
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri_path = self.db_path.resolve().as_posix()
            # Published indexes are usually static artifacts; repair dry-runs need WAL visibility.
            immutable_suffix = "&immutable=1" if self.immutable_read else ""
            return sqlite3.connect(
                f"file:{uri_path}?mode=ro{immutable_suffix}",
                uri=True,
                check_same_thread=False,
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.db_path), check_same_thread=False)

    def close(self) -> None:
        if self.conn:
            if not self.read_only:
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
        *,
        commit: bool = True,
    ) -> None:
        if not commit:
            self._save_document_impl(document, chunks, source_spans, entities, relations)
            return

        savepoint_name = f"save_document_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            self._save_document_impl(document, chunks, source_spans, entities, relations)
        except Exception:
            self._rollback_save_document_savepoint(savepoint_name)
            raise

        try:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _rollback_save_document_savepoint(self, savepoint_name: str) -> None:
        self._rollback_savepoint(savepoint_name)

    def _rollback_savepoint(self, savepoint_name: str) -> None:
        try:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        except Exception:
            pass
        try:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception:
            pass

    def _save_document_impl(
        self,
        document: KnowledgeDocument,
        chunks: Iterable[KnowledgeChunk],
        source_spans: Iterable[SourceSpan],
        entities: Iterable[KnowledgeEntity],
        relations: Iterable[KnowledgeRelation],
    ) -> None:
        chunks = list(chunks)
        source_spans = list(source_spans)
        entities = list(entities)
        relations = list(relations)
        old_document = self.get_document(document.id)
        old_version_id = old_document.version_id if old_document else ""
        kb_id = document.kb_id or "kb_default"
        version_id = document.version_id or stable_version_id(document.id, document.content_hash)
        self.ensure_knowledge_base(KnowledgeBase(id=kb_id, name=kb_id))
        if old_version_id and old_version_id != version_id:
            self._delete_stale_visual_chunks(document.id, version_id)
            self.conn.execute(
                """
                UPDATE visual_artifacts
                SET retrievable = 0,
                    updated_at = ?
                WHERE document_id = ?
                  AND version_id != ?
                """,
                (_now(), document.id, version_id),
            )
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
        ordinary_chunk_rows = self.conn.execute(
            """
            SELECT id, source_span_ids
            FROM chunks
            WHERE document_id = ?
              AND COALESCE(
                    CASE
                        WHEN json_valid(metadata) THEN json_extract(metadata, '$.source')
                        ELSE ''
                    END,
                    ''
                  ) != 'visual_analysis'
            """,
            (document.id,),
        ).fetchall()
        ordinary_chunk_ids = [row["id"] for row in ordinary_chunk_rows]
        ordinary_span_ids: List[str] = []
        for row in ordinary_chunk_rows:
            ordinary_span_ids.extend(_loads(row["source_span_ids"]) or [])
        preserved_span_ids = self._source_span_ids_for_preserved_chunks(document.id, ordinary_chunk_ids)
        incoming_span_ids = {span.id for span in source_spans if span.id}
        protected_span_ids = preserved_span_ids | self._source_span_ids_for_other_documents(document.id, incoming_span_ids)
        chunks, source_spans, entities, relations = self._remap_incoming_source_span_conflicts(
            chunks,
            source_spans,
            entities,
            relations,
            protected_span_ids,
        )
        incoming_span_ids = {span.id for span in source_spans if span.id}
        if ordinary_span_ids:
            ordinary_span_ids = [
                span_id
                for span_id in dict.fromkeys(ordinary_span_ids)
                if span_id and span_id not in preserved_span_ids
            ]
        self._delete_current_document_conflicting_source_spans(
            document.id,
            incoming_span_ids,
            preserved_span_ids,
        )
        if ordinary_chunk_ids:
            placeholders = ",".join("?" for _ in ordinary_chunk_ids)
            self.conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", ordinary_chunk_ids)
            if self.fts5_available:
                self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", ordinary_chunk_ids)
        if ordinary_span_ids:
            self.prune_source_span_references(ordinary_span_ids)
            placeholders = ",".join("?" for _ in ordinary_span_ids)
            self.conn.execute(
                f"DELETE FROM source_spans WHERE document_id = ? AND id IN ({placeholders})",
                [document.id, *ordinary_span_ids],
            )
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
        self.conn.execute(
            """
            DELETE FROM visual_artifact_chunks
            WHERE chunk_id NOT IN (SELECT id FROM chunks)
            """
        )

    def _source_span_ids_for_preserved_chunks(self, document_id: str, deleted_chunk_ids: List[str]) -> Set[str]:
        if not deleted_chunk_ids:
            rows = self.conn.execute(
                "SELECT source_span_ids FROM chunks WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in deleted_chunk_ids)
            rows = self.conn.execute(
                f"""
                SELECT source_span_ids
                FROM chunks
                WHERE document_id = ?
                  AND id NOT IN ({placeholders})
                """,
                [document_id, *deleted_chunk_ids],
            ).fetchall()
        preserved: Set[str] = set()
        for row in rows:
            preserved.update(_loads(row["source_span_ids"]) or [])
        return preserved

    def _source_span_ids_for_other_documents(self, document_id: str, span_ids: Iterable[str]) -> Set[str]:
        ids = [span_id for span_id in dict.fromkeys(span_ids) if span_id]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT id
            FROM source_spans
            WHERE id IN ({placeholders})
              AND document_id != ?
            """,
            [*ids, document_id],
        ).fetchall()
        return {row["id"] for row in rows if row["id"]}

    def _delete_current_document_conflicting_source_spans(
        self,
        document_id: str,
        incoming_span_ids: Iterable[str],
        protected_span_ids: Set[str],
    ) -> None:
        ids = [span_id for span_id in dict.fromkeys(incoming_span_ids) if span_id and span_id not in protected_span_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""
            SELECT id
            FROM source_spans
            WHERE document_id = ?
              AND id IN ({placeholders})
            """,
            [document_id, *ids],
        ).fetchall()
        delete_ids = [row["id"] for row in rows if row["id"]]
        if not delete_ids:
            return
        self.prune_source_span_references(delete_ids)
        placeholders = ",".join("?" for _ in delete_ids)
        self.conn.execute(
            f"DELETE FROM source_spans WHERE document_id = ? AND id IN ({placeholders})",
            [document_id, *delete_ids],
        )

    def _remap_incoming_source_span_conflicts(
        self,
        chunks: List[KnowledgeChunk],
        source_spans: List[SourceSpan],
        entities: List[KnowledgeEntity],
        relations: List[KnowledgeRelation],
        protected_span_ids: Set[str],
    ) -> Tuple[List[KnowledgeChunk], List[SourceSpan], List[KnowledgeEntity], List[KnowledgeRelation]]:
        if not protected_span_ids or not source_spans:
            return chunks, source_spans, entities, relations
        incoming_span_ids = {span.id for span in source_spans if span.id}
        conflicts = incoming_span_ids & protected_span_ids
        if not conflicts:
            return chunks, source_spans, entities, relations

        used_span_ids = self._all_source_span_ids() | incoming_span_ids
        remap: Dict[str, str] = {}
        for old_id in sorted(conflicts):
            index = 1
            while True:
                candidate = f"{old_id}-repair-{index}"
                if candidate not in used_span_ids:
                    remap[old_id] = candidate
                    used_span_ids.add(candidate)
                    break
                index += 1

        def remap_ids(values: Iterable[str]) -> List[str]:
            return [remap.get(value, value) for value in (values or [])]

        remapped_spans = [replace(span, id=remap.get(span.id, span.id)) for span in source_spans]
        remapped_chunks = [
            replace(chunk, source_span_ids=remap_ids(chunk.source_span_ids))
            for chunk in chunks
        ]
        remapped_entities = [
            replace(entity, source_span_ids=remap_ids(entity.source_span_ids))
            for entity in entities
        ]
        remapped_relations: List[KnowledgeRelation] = []
        for relation in relations:
            evidence_span_ids = remap_ids(relation.evidence_span_ids)
            remapped_relations.append(
                replace(
                    relation,
                    id=stable_relation_id(
                        relation.subject_entity_id,
                        relation.predicate,
                        relation.object_entity_id,
                        evidence_span_ids,
                    ),
                    evidence_span_ids=evidence_span_ids,
                )
            )
        return remapped_chunks, remapped_spans, remapped_entities, remapped_relations

    def _all_source_span_ids(self) -> Set[str]:
        rows = self.conn.execute("SELECT id FROM source_spans").fetchall()
        return {row["id"] for row in rows if row["id"]}

    def prune_source_span_references(self, span_ids: Iterable[str]) -> Dict[str, int]:
        remove_ids = {str(span_id) for span_id in span_ids if span_id}
        if not remove_ids:
            return {"entities": 0, "relations": 0}

        entity_updates = self._prune_entity_source_span_references(remove_ids)
        relation_updates = self._prune_relation_source_span_references(remove_ids)
        return {"entities": entity_updates, "relations": relation_updates}

    def _prune_entity_source_span_references(self, remove_ids: Set[str]) -> int:
        updates = 0
        rows = self.conn.execute("SELECT id, source_span_ids FROM entities").fetchall()
        for row in rows:
            current_ids = _json_list(row["source_span_ids"])
            filtered_ids = [span_id for span_id in current_ids if str(span_id) not in remove_ids]
            if filtered_ids == current_ids:
                continue
            self.conn.execute(
                "UPDATE entities SET source_span_ids = ?, updated_at = ? WHERE id = ?",
                (_json(filtered_ids), _now(), row["id"]),
            )
            updates += 1
        return updates

    def _prune_relation_source_span_references(self, remove_ids: Set[str]) -> int:
        updates = 0
        rows = self.conn.execute("SELECT id, evidence_span_ids, metadata FROM knowledge_relations").fetchall()
        for row in rows:
            current_ids = _json_list(row["evidence_span_ids"])
            removed_ids = [span_id for span_id in current_ids if str(span_id) in remove_ids]
            if not removed_ids:
                continue

            filtered_ids = [span_id for span_id in current_ids if str(span_id) not in remove_ids]
            metadata = _loads(row["metadata"])
            if not isinstance(metadata, dict):
                metadata = {}
            if not filtered_ids:
                metadata["evidence_pruned"] = True
                metadata["pruned_source_span_ids"] = _unique(
                    [
                        *(_json_list(metadata.get("pruned_source_span_ids"))),
                        *removed_ids,
                    ]
                )
            self.conn.execute(
                "UPDATE knowledge_relations SET evidence_span_ids = ?, metadata = ? WHERE id = ?",
                (_json(filtered_ids), _json(metadata), row["id"]),
            )
            updates += 1
        return updates

    def get_document(self, document_id: str) -> Optional[KnowledgeDocument]:
        row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        return _row_to_document(row) if row else None

    def list_documents(self) -> List[KnowledgeDocument]:
        rows = self.conn.execute("SELECT * FROM documents ORDER BY updated_at DESC, title ASC").fetchall()
        return [_row_to_document(row) for row in rows]

    def list_chunks(self, document_id: str = "") -> List[KnowledgeChunk]:
        params: List[Any] = []
        where = ""
        if document_id:
            where = "WHERE document_id = ?"
            params.append(document_id)
        rows = self.conn.execute(
            f"SELECT * FROM chunks {where} ORDER BY document_id ASC, ordinal ASC",
            params,
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def get_chunks_near(self, document_id: str, ordinal: int, window: int = 1) -> List[KnowledgeChunk]:
        """Return chunks around an ordinal in document order."""

        if not document_id or ordinal <= 0:
            return []
        radius = max(0, int(window or 0))
        rows = self.conn.execute(
            """
            SELECT *
            FROM chunks
            WHERE document_id = ?
              AND ordinal BETWEEN ? AND ?
            ORDER BY ordinal ASC
            """,
            (document_id, max(1, ordinal - radius), ordinal + radius),
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

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

    def upsert_visual_artifact(self, candidate: VisualArtifactCandidate) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO visual_artifacts(
                id, document_id, version_id, kb_id, artifact_type, page, label, caption, bbox,
                image_path, image_hash, context_hash, pipeline_version, parser, parser_confidence,
                source_path, crop_dpi, crop_padding_px,
                context_before, context_after, page_text,
                group_id, part_index, continuation_role, continuation_confidence, group_retrievable,
                analysis_status, analysis_confidence, retrievable, analysis_model, analysis_backend, prompt_version,
                result_json, error, created_at, updated_at, analyzed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 'single', 0, 0, 'pending', 0, 0, '', '', '', '{}', '', ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                kb_id = excluded.kb_id,
                label = excluded.label,
                caption = excluded.caption,
                bbox = excluded.bbox,
                image_path = CASE
                    WHEN excluded.image_path != '' THEN excluded.image_path
                    ELSE visual_artifacts.image_path
                END,
                image_hash = CASE
                    WHEN visual_artifacts.image_path != ''
                     AND visual_artifacts.context_hash = excluded.context_hash
                     AND COALESCE(visual_artifacts.pipeline_version, '') = COALESCE(excluded.pipeline_version, '')
                    THEN visual_artifacts.image_hash
                    ELSE excluded.image_hash
                END,
                context_hash = excluded.context_hash,
                pipeline_version = excluded.pipeline_version,
                parser = excluded.parser,
                parser_confidence = excluded.parser_confidence,
                source_path = excluded.source_path,
                crop_dpi = excluded.crop_dpi,
                crop_padding_px = excluded.crop_padding_px,
                context_before = excluded.context_before,
                context_after = excluded.context_after,
                page_text = excluded.page_text,
                updated_at = excluded.updated_at,
                analysis_status = CASE
                    WHEN visual_artifacts.analysis_status IN ('succeeded', 'low_confidence', 'failed')
                     AND visual_artifacts.context_hash = excluded.context_hash
                     AND COALESCE(visual_artifacts.pipeline_version, '') = COALESCE(excluded.pipeline_version, '')
                    THEN visual_artifacts.analysis_status
                    ELSE 'pending'
                END
            """,
            (
                candidate.id,
                candidate.document_id,
                candidate.version_id,
                candidate.kb_id or "kb_default",
                candidate.artifact_type,
                int(candidate.page),
                candidate.label,
                candidate.caption,
                _json(candidate.bbox or {}),
                candidate.image_path,
                candidate.image_hash,
                candidate.context_hash,
                candidate.pipeline_version or "",
                candidate.parser,
                float(candidate.parser_confidence or 0),
                candidate.source_path,
                int(candidate.crop_dpi or 180),
                int(candidate.crop_padding_px or 12),
                candidate.context_before,
                candidate.context_after,
                candidate.page_text,
                now,
                now,
            ),
        )
        self.conn.commit()

    def _delete_stale_visual_chunks(self, document_id: str, current_version_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT id, source_span_ids
            FROM chunks
            WHERE document_id = ?
              AND version_id != ?
              AND COALESCE(
                    CASE
                        WHEN json_valid(metadata) THEN json_extract(metadata, '$.source')
                        ELSE ''
                    END,
                    ''
                  ) = 'visual_analysis'
            """,
            (document_id, current_version_id),
        ).fetchall()
        chunk_ids = [row["id"] for row in rows]
        span_ids: List[str] = []
        for row in rows:
            span_ids.extend(_loads(row["source_span_ids"]) or [])
        self._delete_chunks_and_unreferenced_source_spans(chunk_ids, span_ids, commit=False)

    def _source_span_ids_referenced_by_chunks(self, span_ids: Iterable[str]) -> Set[str]:
        candidate_ids = {span_id for span_id in span_ids if span_id}
        if not candidate_ids:
            return set()
        rows = self.conn.execute("SELECT source_span_ids FROM chunks").fetchall()
        referenced: Set[str] = set()
        for row in rows:
            for span_id in _loads(row["source_span_ids"]) or []:
                if span_id in candidate_ids:
                    referenced.add(span_id)
        return referenced

    def _existing_source_span_ids(self, span_ids: Iterable[str]) -> Set[str]:
        ids = [span_id for span_id in dict.fromkeys(span_ids) if span_id]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id FROM source_spans WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return {row["id"] for row in rows if row["id"]}

    def _all_chunk_ids(self) -> Set[str]:
        rows = self.conn.execute("SELECT id FROM chunks").fetchall()
        return {row["id"] for row in rows if row["id"]}

    def _existing_chunk_ids(self, chunk_ids: Iterable[str]) -> Set[str]:
        ids = [chunk_id for chunk_id in dict.fromkeys(chunk_ids) if chunk_id]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(f"SELECT id FROM chunks WHERE id IN ({placeholders})", ids).fetchall()
        return {row["id"] for row in rows if row["id"]}

    def _chunk_ids_for_artifact(self, artifact_id: str) -> Set[str]:
        if not artifact_id:
            return set()
        rows = self.conn.execute(
            "SELECT chunk_id FROM visual_artifact_chunks WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchall()
        return {row["chunk_id"] for row in rows if row["chunk_id"]}

    def _visual_artifact_chunk_owners(self, chunk_ids: Iterable[str]) -> Dict[str, Set[str]]:
        ids = [chunk_id for chunk_id in dict.fromkeys(chunk_ids) if chunk_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT artifact_id, chunk_id FROM visual_artifact_chunks WHERE chunk_id IN ({placeholders})",
            ids,
        ).fetchall()
        owners: Dict[str, Set[str]] = {}
        for row in rows:
            owners.setdefault(row["chunk_id"], set()).add(row["artifact_id"])
        return owners

    def _existing_chunk_rows(self, chunk_ids: Iterable[str]) -> Dict[str, sqlite3.Row]:
        ids = [chunk_id for chunk_id in dict.fromkeys(chunk_ids) if chunk_id]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, document_id, source_span_ids, metadata FROM chunks WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return {row["id"]: row for row in rows if row["id"]}

    def _delete_existing_visual_chunks_for_same_artifact_if_idempotent(
        self,
        artifact_id: str,
        chunks: List[KnowledgeChunk],
    ) -> Set[str]:
        incoming_ids = [chunk.id for chunk in chunks if chunk.id]
        existing_rows = self._existing_chunk_rows(incoming_ids)
        if not existing_rows:
            return set()
        owner_rows = self._visual_artifact_chunk_owners(existing_rows)
        direct_artifact_chunk_ids = self._chunk_ids_for_artifact(artifact_id)
        delete_ids: List[str] = []
        span_ids: List[str] = []
        for chunk_id, row in existing_rows.items():
            owners = owner_rows.get(chunk_id, set())
            metadata = _loads(row["metadata"])
            if not self._is_idempotent_visual_chunk_for_artifact(
                chunk_id,
                artifact_id,
                metadata,
                owners,
                direct_artifact_chunk_ids,
            ):
                continue
            delete_ids.append(chunk_id)
            span_ids.extend(_loads(row["source_span_ids"]) or [])
        if delete_ids:
            self._delete_chunks_and_unreferenced_source_spans(delete_ids, span_ids, commit=False)
        return set(existing_rows) - set(delete_ids)

    def _is_idempotent_visual_chunk_for_artifact(
        self,
        chunk_id: str,
        artifact_id: str,
        metadata: Any,
        owners: Set[str],
        direct_artifact_chunk_ids: Set[str],
    ) -> bool:
        if not artifact_id or chunk_id not in direct_artifact_chunk_ids:
            return False
        if not isinstance(metadata, dict) or metadata.get("source") != "visual_analysis":
            return False
        scope = str(metadata.get("visual_scope") or "")
        if scope == "group":
            return str(metadata.get("visual_group_id") or "") == artifact_id
        if scope == "page":
            return str(metadata.get("visual_artifact_id") or "") == artifact_id
        return owners == {artifact_id}

    def _remap_incoming_chunk_id_conflicts(
        self,
        chunks: List[KnowledgeChunk],
        protected_chunk_ids: Set[str],
    ) -> List[KnowledgeChunk]:
        seen: Set[str] = set()
        incoming_ids = [chunk.id for chunk in chunks]
        if not protected_chunk_ids and all(incoming_ids) and len(set(incoming_ids)) == len(incoming_ids):
            return chunks
        used_chunk_ids = self._all_chunk_ids()
        remapped: List[KnowledgeChunk] = []
        for chunk in chunks:
            chunk_id = chunk.id
            if chunk_id and chunk_id not in protected_chunk_ids and chunk_id not in seen:
                remapped.append(chunk)
                seen.add(chunk_id)
                used_chunk_ids.add(chunk_id)
                continue
            base_id = chunk_id or f"visual-chunk-{uuid.uuid4().hex}"
            index = 1
            while True:
                candidate = f"{base_id}-repair-{index}"
                if candidate not in used_chunk_ids and candidate not in seen:
                    break
                index += 1
            remapped.append(replace(chunk, id=candidate))
            seen.add(candidate)
            used_chunk_ids.add(candidate)
        return remapped

    def _delete_chunks_and_unreferenced_source_spans(
        self,
        chunk_ids: Iterable[str],
        span_ids: Iterable[str],
        *,
        commit: bool = True,
    ) -> Dict[str, int]:
        chunk_ids = _unique([chunk_id for chunk_id in chunk_ids if chunk_id])
        span_ids = _unique([span_id for span_id in span_ids if span_id])
        deleted_chunks = 0
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            deleted_chunks = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM chunks WHERE id IN ({placeholders})",
                    chunk_ids,
                ).fetchone()[0]
            )
            self.conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", chunk_ids)
            self.conn.execute(f"DELETE FROM visual_artifact_chunks WHERE chunk_id IN ({placeholders})", chunk_ids)
            if self.fts5_available:
                self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids)

        referenced_span_ids = self._source_span_ids_referenced_by_chunks(span_ids)
        unreferenced_span_ids = [span_id for span_id in span_ids if span_id not in referenced_span_ids]
        deleted_source_spans = 0
        pruned_counts = {"entities": 0, "relations": 0}
        if unreferenced_span_ids:
            pruned_counts = self.prune_source_span_references(unreferenced_span_ids)
            placeholders = ",".join("?" for _ in unreferenced_span_ids)
            deleted_source_spans = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM source_spans WHERE id IN ({placeholders})",
                    unreferenced_span_ids,
                ).fetchone()[0]
            )
            self.conn.execute(f"DELETE FROM source_spans WHERE id IN ({placeholders})", unreferenced_span_ids)

        if commit:
            self.conn.commit()
        return {
            "deleted_chunks": deleted_chunks,
            "deleted_source_spans": deleted_source_spans,
            "preserved_source_spans": len(referenced_span_ids),
            "pruned_entities": int(pruned_counts.get("entities", 0)),
            "pruned_relations": int(pruned_counts.get("relations", 0)),
        }

    def update_visual_artifact_image(self, artifact_id: str, image_path: str, image_hash: str) -> None:
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET image_path = ?,
                image_hash = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (image_path or "", image_hash or "", _now(), artifact_id),
        )
        self.conn.commit()

    def list_visual_artifacts(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
        status: Optional[str] = None,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        if status:
            clauses.append("analysis_status = ?")
            params.append(status)
        if page_start is not None:
            clauses.append("page >= ?")
            params.append(max(1, int(page_start)))
        if page_end is not None:
            clauses.append("page <= ?")
            params.append(max(1, int(page_end)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, int(limit or 100)), max(0, int(offset or 0))])
        try:
            rows = self.conn.execute(
                f"""
                SELECT *
                FROM visual_artifacts
                {where}
                ORDER BY document_id ASC, version_id ASC, page ASC, created_at ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [_row_to_visual_artifact(row) for row in rows]

    def get_visual_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self.conn.execute("SELECT * FROM visual_artifacts WHERE id = ?", (artifact_id,)).fetchone()
        except sqlite3.DatabaseError:
            return None
        return _row_to_visual_artifact(row) if row else None

    def has_visual_artifacts_with_pipeline_version(
        self,
        *,
        document_id: str,
        version_id: str,
        pipeline_version: str,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM visual_artifacts
            WHERE document_id = ?
              AND version_id = ?
              AND COALESCE(pipeline_version, '') != ?
            LIMIT 1
            """,
            (document_id, version_id, pipeline_version or ""),
        ).fetchone()
        return bool(row)

    def claim_next_visual_artifact(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
        force: bool = False,
        retry_failed: bool = False,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
        exclude_ids: Optional[Iterable[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        now = _now()
        stale_before = now - 30 * 60
        params: List[Any] = []
        doc_clause = ""
        if document_id:
            doc_clause = "AND document_id = ?"
            params.append(document_id)
        kb_clause = ""
        if kb_id:
            kb_clause = "AND kb_id = ?"
            params.append(kb_id)
        version_clause = ""
        if version_id:
            version_clause = "AND version_id = ?"
            params.append(version_id)
        excluded = [artifact_id for artifact_id in (exclude_ids or []) if artifact_id]
        exclude_clause = ""
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"AND id NOT IN ({placeholders})"
            params.extend(excluded)
        if force:
            status_clause = "analysis_status IN ('pending', 'failed', 'low_confidence', 'succeeded', 'running')"
        else:
            status_parts = [
                "analysis_status = 'pending'",
                "(analysis_status = 'running' AND updated_at < ?)",
            ]
            params = [stale_before, *params]
            if retry_failed:
                status_parts.append("analysis_status = 'failed'")
            status_clause = " OR ".join(status_parts)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM visual_artifacts
            WHERE ({status_clause}) {doc_clause} {kb_clause} {version_clause} {exclude_clause}
            ORDER BY CASE analysis_status
                WHEN 'pending' THEN 0
                WHEN 'running' THEN 1
                WHEN 'failed' THEN 2
                WHEN 'low_confidence' THEN 3
                WHEN 'succeeded' THEN 4
                ELSE 4
            END, page ASC, updated_at ASC
            LIMIT 1
            """,
            params,
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        if (
            not force
            and row["analysis_status"] == "succeeded"
            and row["analysis_model"] == model
            and row["prompt_version"] == prompt_version
        ):
            return None
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET analysis_status = 'running',
                analysis_model = ?,
                analysis_backend = ?,
                prompt_version = ?,
                error = '',
                updated_at = ?
            WHERE id = ?
            """,
            (model, analysis_backend, prompt_version, now, row["id"]),
        )
        self.conn.commit()
        return self.get_visual_artifact(row["id"])

    def get_visual_prepare_state(self, document_id: str, version_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM visual_prepare_states WHERE document_id = ? AND version_id = ?",
            (document_id, version_id),
        ).fetchone()
        return _row_to_visual_prepare_state(row) if row else None

    def upsert_visual_prepare_state(
        self,
        *,
        document_id: str,
        version_id: str,
        kb_id: str,
        source_path: str,
        total_pages: int,
        next_page: int = 1,
        prepared_pages: int = 0,
        prepared_artifacts: int = 0,
        status: str = "pending",
        error: str = "",
        pipeline_version: str = "",
    ) -> Dict[str, Any]:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO visual_prepare_states(
                document_id, version_id, kb_id, source_path, total_pages, next_page,
                prepared_pages, prepared_artifacts, status, error, pipeline_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, version_id) DO UPDATE SET
                kb_id = excluded.kb_id,
                source_path = excluded.source_path,
                total_pages = excluded.total_pages,
                next_page = excluded.next_page,
                prepared_pages = excluded.prepared_pages,
                prepared_artifacts = excluded.prepared_artifacts,
                status = excluded.status,
                error = excluded.error,
                pipeline_version = excluded.pipeline_version,
                updated_at = excluded.updated_at
            """,
            (
                document_id,
                version_id,
                kb_id or "kb_default",
                source_path or "",
                int(total_pages or 0),
                max(1, int(next_page or 1)),
                max(0, int(prepared_pages or 0)),
                max(0, int(prepared_artifacts or 0)),
                status or "pending",
                error or "",
                pipeline_version or "",
                now,
                now,
            ),
        )
        self.conn.commit()
        return self.get_visual_prepare_state(document_id, version_id) or {}

    def update_visual_prepare_state(self, document_id: str, version_id: str, **updates: Any) -> Dict[str, Any]:
        allowed = {
            "kb_id",
            "source_path",
            "total_pages",
            "next_page",
            "prepared_pages",
            "prepared_artifacts",
            "status",
            "error",
            "pipeline_version",
        }
        fields = [key for key in updates if key in allowed]
        if not fields:
            return self.get_visual_prepare_state(document_id, version_id) or {}
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [updates[field] for field in fields]
        values.extend([_now(), document_id, version_id])
        self.conn.execute(
            f"""
            UPDATE visual_prepare_states
            SET {assignments},
                updated_at = ?
            WHERE document_id = ? AND version_id = ?
            """,
            values,
        )
        self.conn.commit()
        return self.get_visual_prepare_state(document_id, version_id) or {}

    def visual_prepare_stats(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses: List[str] = []
        params: List[Any] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM visual_prepare_states
            {where}
            ORDER BY updated_at DESC
            """,
            params,
        ).fetchall()
        states = [_row_to_visual_prepare_state(row) for row in rows]
        total_pages = sum(int(state.get("total_pages") or 0) for state in states)
        prepared_pages = sum(int(state.get("prepared_pages") or 0) for state in states)
        prepared_artifacts = sum(int(state.get("prepared_artifacts") or 0) for state in states)
        status = "pending"
        if states:
            if any(state.get("status") == "failed" for state in states):
                status = "failed"
            elif all(state.get("status") == "done" for state in states):
                status = "done"
            elif any(state.get("status") == "running" for state in states):
                status = "running"
        return {
            "status": status,
            "total_pages": total_pages,
            "prepared_pages": prepared_pages,
            "prepared_artifacts": prepared_artifacts,
            "next_page": min((int(state.get("next_page") or 1) for state in states), default=1),
            "states": states,
        }

    def complete_visual_artifact_success(
        self,
        artifact_id: str,
        result_json: Dict[str, Any],
        confidence: float,
        retrievable: bool,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET analysis_status = 'succeeded',
                analysis_confidence = ?,
                retrievable = ?,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                result_json = ?,
                error = '',
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (
                float(confidence or 0),
                1 if retrievable else 0,
                model,
                analysis_backend,
                prompt_version,
                _json(result_json),
                now,
                now,
                artifact_id,
            ),
        )
        self.conn.commit()

    def complete_visual_artifact_low_confidence(
        self,
        artifact_id: str,
        result_json: Dict[str, Any],
        confidence: float,
        reason: str,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET analysis_status = 'low_confidence',
                analysis_confidence = ?,
                retrievable = 0,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                result_json = ?,
                error = ?,
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (
                float(confidence or 0),
                model,
                analysis_backend,
                prompt_version,
                _json(result_json),
                str(reason or ""),
                now,
                now,
                artifact_id,
            ),
        )
        self.conn.commit()

    def complete_visual_artifact_failed(
        self,
        artifact_id: str,
        error: str,
        analysis_backend: str = "",
        model: str = "",
        prompt_version: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET analysis_status = 'failed',
                retrievable = 0,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                error = ?,
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (model, analysis_backend, prompt_version, str(error or ""), now, now, artifact_id),
        )
        self.conn.commit()

    def mark_visual_artifact_group_membership(
        self,
        artifact_id: str,
        group_id: str,
        part_index: int,
        role: str,
        confidence: float,
        *,
        group_retrievable: int = 0,
    ) -> None:
        previous_rows = self.conn.execute(
            """
            SELECT DISTINCT group_id
            FROM visual_artifact_group_members
            WHERE artifact_id = ? AND group_id != ?
            """,
            (artifact_id, group_id or ""),
        ).fetchall()
        previous_group_ids = [row["group_id"] for row in previous_rows if row["group_id"]]
        if previous_group_ids:
            placeholders = ",".join("?" for _ in previous_group_ids)
            self.conn.execute(
                f"DELETE FROM visual_artifact_group_members WHERE artifact_id = ? AND group_id IN ({placeholders})",
                [artifact_id, *previous_group_ids],
            )
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET group_id = ?,
                part_index = ?,
                continuation_role = ?,
                continuation_confidence = ?,
                group_retrievable = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                group_id or "",
                max(0, int(part_index or 0)),
                role or "unknown",
                float(confidence or 0),
                1 if group_retrievable else 0,
                _now(),
                artifact_id,
            ),
        )
        self.conn.commit()
        self.delete_visual_page_chunks_for_artifact(artifact_id)
        for previous_group_id in previous_group_ids:
            self._repair_visual_group_after_member_change(previous_group_id)

    def upsert_visual_artifact_group(self, group: Dict[str, Any]) -> None:
        now = _now()
        result_json = group.get("result_json") if isinstance(group.get("result_json"), dict) else {}
        existing = self.get_visual_artifact_group(group["id"])
        if existing:
            old_pages = _merge_int_pages(existing.get("source_pages") or [], [])
            new_pages = _merge_int_pages(group.get("source_pages") or [], [])
            merged_pages = _merge_int_pages(old_pages, new_pages)
            dirty = _group_pages_changed(old_pages, merged_pages) or _group_identity_changed(existing, group)
            merged_result_json = _merge_visual_group_result_json(existing.get("result_json") or {}, result_json, merged_pages)
            incoming_status = group.get("status") or existing.get("status") or "pending"
            status = "pending" if dirty else _preserved_visual_group_status(existing.get("status"), incoming_status)
            retrievable = 0 if dirty else (1 if group.get("retrievable") else int(bool(existing.get("retrievable"))))
            analyzed_at = 0 if dirty else int(existing.get("analyzed_at") or 0)
            self.conn.execute(
                """
                UPDATE visual_artifact_groups
                SET kb_id = ?,
                    group_type = ?,
                    title = ?,
                    caption = ?,
                    source_pages = ?,
                    status = ?,
                    confidence = ?,
                    retrievable = ?,
                    analysis_model = ?,
                    analysis_backend = ?,
                    prompt_version = ?,
                    result_json = ?,
                    error = ?,
                    updated_at = ?,
                    analyzed_at = ?
                WHERE id = ?
                """,
                (
                    group.get("kb_id") or existing.get("kb_id") or "kb_default",
                    group.get("group_type") or existing.get("group_type") or "unknown",
                    group.get("title") or existing.get("title") or "",
                    group.get("caption") or existing.get("caption") or "",
                    _json(merged_pages),
                    status,
                    max(float(existing.get("confidence") or 0), float(group.get("confidence") or 0)),
                    retrievable,
                    group.get("analysis_model") or existing.get("analysis_model") or "",
                    group.get("analysis_backend") or existing.get("analysis_backend") or "",
                    group.get("prompt_version") or existing.get("prompt_version") or "",
                    _json(merged_result_json),
                    "" if dirty else (group.get("error") or existing.get("error") or ""),
                    now,
                    analyzed_at,
                    group["id"],
                ),
            )
            self.conn.commit()
            if dirty:
                self._mark_visual_group_not_retrievable(group["id"])
            return
        self.conn.execute(
            """
            INSERT INTO visual_artifact_groups(
                id, document_id, version_id, kb_id, group_type, title, caption, source_pages,
                status, confidence, retrievable, analysis_model, analysis_backend, prompt_version,
                result_json, error, created_at, updated_at, analyzed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                group["id"],
                group["document_id"],
                group["version_id"],
                group.get("kb_id") or "kb_default",
                group.get("group_type") or "unknown",
                group.get("title") or "",
                group.get("caption") or "",
                _json(group.get("source_pages") or []),
                group.get("status") or "pending",
                float(group.get("confidence") or 0),
                1 if group.get("retrievable") else 0,
                group.get("analysis_model") or "",
                group.get("analysis_backend") or "",
                group.get("prompt_version") or "",
                _json(result_json),
                group.get("error") or "",
                now,
                now,
            ),
        )
        self.conn.commit()

    def add_visual_artifact_group_member(
        self,
        group_id: str,
        artifact_id: str,
        part_index: int,
        page: int,
        role: str,
        confidence: float,
    ) -> None:
        existing = self.conn.execute(
            """
            SELECT *
            FROM visual_artifact_group_members
            WHERE group_id = ? AND artifact_id = ?
            """,
            (group_id, artifact_id),
        ).fetchone()
        normalized_part_index = max(1, int(part_index or 1))
        normalized_page = int(page or 0)
        normalized_role = role or "unknown"
        normalized_confidence = float(confidence or 0)
        changed = existing is None or any(
            [
                int(existing["part_index"] or 0) != normalized_part_index,
                int(existing["page"] or 0) != normalized_page,
                (existing["role"] or "") != normalized_role,
                abs(float(existing["confidence"] or 0) - normalized_confidence) > 0.0001,
            ]
        )
        self.conn.execute(
            """
            INSERT INTO visual_artifact_group_members(group_id, artifact_id, part_index, page, role, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, artifact_id) DO UPDATE SET
                part_index = excluded.part_index,
                page = excluded.page,
                role = excluded.role,
                confidence = excluded.confidence
            """,
            (
                group_id,
                artifact_id,
                normalized_part_index,
                normalized_page,
                normalized_role,
                normalized_confidence,
            ),
        )
        self.conn.commit()
        if changed:
            self._mark_visual_group_member_change(group_id, normalized_page)

    def resolve_visual_group_id_for_members(
        self,
        *,
        document_id: str,
        version_id: str,
        member_artifact_ids: Iterable[str],
        preferred_group_id: str,
    ) -> str:
        """Resolve overlapping groups using only current artifact->group links.

        Legacy membership rows whose artifact has moved to another group are ignored
        by the join and can be removed later by cleanup_stale_visual_artifact_group_members().
        """
        artifact_ids = _unique([artifact_id for artifact_id in member_artifact_ids if artifact_id])
        if not artifact_ids:
            return preferred_group_id
        placeholders = ",".join("?" for _ in artifact_ids)
        rows = self.conn.execute(
            f"""
            SELECT
                gm.group_id AS group_id,
                COUNT(DISTINCT gm.artifact_id) AS overlap_count,
                MIN(gm.page) AS min_page,
                COUNT(DISTINCT all_member_artifacts.id) AS member_count,
                MIN(g.created_at) AS created_at
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            JOIN visual_artifact_groups g ON g.id = gm.group_id
            LEFT JOIN visual_artifact_group_members all_members ON all_members.group_id = gm.group_id
            LEFT JOIN visual_artifacts all_member_artifacts
              ON all_member_artifacts.id = all_members.artifact_id
             AND all_member_artifacts.group_id = all_members.group_id
            WHERE gm.artifact_id IN ({placeholders})
              AND g.document_id = ?
              AND g.version_id = ?
            GROUP BY gm.group_id
            ORDER BY min_page ASC, member_count DESC, overlap_count DESC, created_at ASC, gm.group_id ASC
            """,
            [*artifact_ids, document_id, version_id],
        ).fetchall()
        if not rows:
            return preferred_group_id
        winner = str(rows[0]["group_id"] or preferred_group_id)
        loser_ids = [str(row["group_id"]) for row in rows[1:] if row["group_id"] and str(row["group_id"]) != winner]
        for loser_id in loser_ids:
            self.mark_visual_artifact_group_skipped(loser_id, "merged into overlapping visual group")
        return winner

    def cleanup_stale_visual_artifact_group_members(self) -> int:
        """Prune stale member rows, then repair affected groups from current joins."""
        affected_rows = self.conn.execute(
            """
            SELECT DISTINCT gm.group_id
            FROM visual_artifact_group_members gm
            WHERE gm.group_id != ''
              AND NOT EXISTS (
                SELECT 1
                FROM visual_artifacts va
                WHERE va.id = gm.artifact_id
                  AND va.group_id = gm.group_id
              )
            """
        ).fetchall()
        affected_group_ids = [str(row["group_id"]) for row in affected_rows if row["group_id"]]
        cursor = self.conn.execute(
            """
            DELETE FROM visual_artifact_group_members
            WHERE NOT EXISTS (
              SELECT 1
              FROM visual_artifacts va
              WHERE va.id = visual_artifact_group_members.artifact_id
                AND va.group_id = visual_artifact_group_members.group_id
            )
            """
        )
        self.conn.commit()
        deleted = int(cursor.rowcount or 0)
        for group_id in affected_group_ids:
            self._repair_visual_group_after_member_change(group_id)
        return deleted

    def list_visual_artifact_groups(
        self,
        document_id: Optional[str] = None,
        version_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM visual_artifact_groups
            {where}
            ORDER BY document_id ASC, version_id ASC, updated_at ASC
            LIMIT ?
            """,
            [*params, max(1, int(limit or 1000))],
        ).fetchall()
        return [_row_to_visual_artifact_group(row) for row in rows]

    def get_visual_artifact_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM visual_artifact_groups WHERE id = ?", (group_id,)).fetchone()
        return _row_to_visual_artifact_group(row) if row else None

    def get_visual_artifact_group_members(self, group_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT gm.*, va.artifact_type, va.caption, va.label, va.bbox, va.result_json,
                   va.analysis_status, va.analysis_confidence, va.source_path, va.image_path,
                   va.context_before, va.context_after, va.page_text, va.group_id AS current_group_id
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            WHERE gm.group_id = ?
            ORDER BY gm.part_index ASC, gm.page ASC
            """,
            (group_id,),
        ).fetchall()
        return [_row_to_visual_group_member(row) for row in rows]

    def complete_visual_artifact_group_success(
        self,
        group_id: str,
        result_json: Dict[str, Any],
        confidence: float,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'succeeded',
                confidence = ?,
                retrievable = 1,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                result_json = ?,
                error = '',
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (float(confidence or 0), model, analysis_backend, prompt_version, _json(result_json), now, now, group_id),
        )
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET group_retrievable = 1,
                retrievable = 0,
                updated_at = ?
            WHERE group_id = ?
              AND id IN (
                SELECT artifact_id
                FROM visual_artifact_group_members
                WHERE group_id = ?
              )
            """,
            (now, group_id, group_id),
        )
        self.conn.commit()

    def complete_visual_artifact_group_low_confidence(
        self,
        group_id: str,
        result_json: Dict[str, Any],
        confidence: float,
        reason: str,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'low_confidence',
                confidence = ?,
                retrievable = 0,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                result_json = ?,
                error = ?,
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (
                float(confidence or 0),
                model,
                analysis_backend,
                prompt_version,
                _json(result_json),
                str(reason or ""),
                now,
                now,
                group_id,
            ),
        )
        self.conn.commit()
        self._mark_visual_group_not_retrievable(group_id)

    def complete_visual_artifact_group_failed(
        self,
        group_id: str,
        error: str,
        analysis_backend: str = "",
        model: str = "",
        prompt_version: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'failed',
                retrievable = 0,
                analysis_model = COALESCE(NULLIF(?, ''), analysis_model),
                analysis_backend = COALESCE(NULLIF(?, ''), analysis_backend),
                prompt_version = COALESCE(NULLIF(?, ''), prompt_version),
                error = ?,
                updated_at = ?,
                analyzed_at = ?
            WHERE id = ?
            """,
            (model, analysis_backend, prompt_version, str(error or ""), now, now, group_id),
        )
        self.conn.commit()
        self._mark_visual_group_not_retrievable(group_id)

    def mark_visual_artifact_group_skipped(self, group_id: str, reason: str = "") -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'skipped',
                retrievable = 0,
                error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (str(reason or "visual group skipped"), now, group_id),
        )
        self.conn.commit()
        self._mark_visual_group_not_retrievable(group_id)

    def reset_visual_artifact_group_pending(self, group_id: str, reason: str = "") -> None:
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'pending',
                retrievable = 0,
                error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (str(reason or ""), now, group_id),
        )
        self.conn.commit()

    def claim_next_visual_artifact_group(
        self,
        group_id: Optional[str] = None,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
        force: bool = False,
        retry_failed: bool = False,
        model: str = "",
        prompt_version: str = "",
        analysis_backend: str = "",
    ) -> Optional[Dict[str, Any]]:
        now = _now()
        stale_before = now - 30 * 60
        clauses: List[str] = []
        params: List[Any] = []
        if group_id:
            clauses.append("id = ?")
            params.append(group_id)
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        if force:
            status_clause = "status IN ('pending', 'failed', 'low_confidence', 'succeeded', 'running')"
        else:
            status_parts = ["status = 'pending'", "(status = 'running' AND updated_at < ?)"]
            params = [stale_before, *params]
            if retry_failed:
                status_parts.append("status = 'failed'")
            status_clause = " OR ".join(status_parts)
        where = f"WHERE ({status_clause})"
        if clauses:
            where += " AND " + " AND ".join(clauses)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM visual_artifact_groups
            {where}
            ORDER BY CASE status
                WHEN 'pending' THEN 0
                WHEN 'running' THEN 1
                WHEN 'failed' THEN 2
                WHEN 'low_confidence' THEN 3
                WHEN 'succeeded' THEN 4
                ELSE 4
            END, updated_at ASC
            LIMIT 1
            """,
            params,
        ).fetchall()
        if not rows:
            return None
        group = rows[0]
        if (
            not force
            and group["status"] == "succeeded"
            and group["analysis_model"] == model
            and group["prompt_version"] == prompt_version
        ):
            return None
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET status = 'running',
                analysis_model = ?,
                analysis_backend = ?,
                prompt_version = ?,
                error = '',
                updated_at = ?
            WHERE id = ?
            """,
            (model, analysis_backend, prompt_version, now, group["id"]),
        )
        self.conn.commit()
        return self.get_visual_artifact_group(group["id"])

    def append_visual_chunks(
        self,
        document_id: str,
        version_id: str,
        artifact_id: str,
        chunks: Iterable[KnowledgeChunk],
        spans: Iterable[SourceSpan],
    ) -> List[str]:
        savepoint_name = f"append_visual_chunks_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            chunk_ids = self._append_visual_chunks_impl(document_id, version_id, artifact_id, chunks, spans)
        except Exception:
            self._rollback_savepoint(savepoint_name)
            raise
        try:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return chunk_ids

    def _append_visual_chunks_impl(
        self,
        document_id: str,
        version_id: str,
        artifact_id: str,
        chunks: Iterable[KnowledgeChunk],
        spans: Iterable[SourceSpan],
    ) -> List[str]:
        chunks = list(chunks)
        spans = list(spans)
        if not chunks:
            return []
        row = self.conn.execute("SELECT COALESCE(MAX(ordinal), 0) AS max_ordinal FROM chunks WHERE document_id = ?", (document_id,)).fetchone()
        next_ordinal = int(row["max_ordinal"] or 0) + 1
        appended: List[KnowledgeChunk] = []
        for offset, chunk in enumerate(chunks):
            appended.append(replace(chunk, ordinal=next_ordinal + offset, version_id=chunk.version_id or version_id))

        protected_chunk_ids = self._delete_existing_visual_chunks_for_same_artifact_if_idempotent(artifact_id, appended)
        existing_span_ids = self._existing_source_span_ids(span.id for span in spans)
        if existing_span_ids:
            appended, spans, _, _ = self._remap_incoming_source_span_conflicts(
                appended,
                spans,
                [],
                [],
                existing_span_ids,
            )
        protected_chunk_ids |= self._existing_chunk_ids(chunk.id for chunk in appended)
        appended = self._remap_incoming_chunk_id_conflicts(appended, protected_chunk_ids)

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
                for span in spans
            ],
        )
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
                    chunk.kb_id or "kb_default",
                    chunk.version_id or version_id,
                    chunk.section_path,
                    chunk.clause_title,
                    _json(chunk.source_span_ids),
                    _json(chunk.entities),
                    _json(chunk.metadata),
                )
                for chunk in appended
            ],
        )
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO visual_artifact_chunks(artifact_id, chunk_id)
            VALUES (?, ?)
            """,
            [(artifact_id, chunk.id) for chunk in appended],
        )
        if self.fts5_available:
            document = self.get_document(document_id)
            title = document.title if document else ""
            chunk_ids = [chunk.id for chunk in appended if chunk.id]
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", chunk_ids)
            self.conn.executemany(
                """
                INSERT INTO chunks_fts(text, chunk_id, document_id, kb_id, title, section_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (chunk.text, chunk.id, chunk.document_id, chunk.kb_id or "kb_default", title, chunk.section_path)
                    for chunk in appended
                ],
            )
        return [chunk.id for chunk in appended]

    def append_visual_group_chunks(
        self,
        document_id: str,
        version_id: str,
        group_id: str,
        artifact_ids: Iterable[str],
        chunks: Iterable[KnowledgeChunk],
        spans: Iterable[SourceSpan],
    ) -> List[str]:
        savepoint_name = f"append_visual_group_chunks_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint_name}")
        try:
            chunk_ids = self._append_visual_chunks_impl(document_id, version_id, group_id, chunks, spans)
            artifact_ids = [artifact_id for artifact_id in artifact_ids if artifact_id]
            if chunk_ids and artifact_ids:
                self.conn.executemany(
                    """
                    INSERT OR IGNORE INTO visual_artifact_chunks(artifact_id, chunk_id)
                    VALUES (?, ?)
                    """,
                    [(artifact_id, chunk_id) for artifact_id in artifact_ids for chunk_id in chunk_ids],
                )
        except Exception:
            self._rollback_savepoint(savepoint_name)
            raise
        try:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return chunk_ids

    def delete_visual_chunks_for_artifact(self, artifact_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT c.id, c.source_span_ids
            FROM visual_artifact_chunks vac
            JOIN chunks c ON c.id = vac.chunk_id
            WHERE vac.artifact_id = ?
            """,
            (artifact_id,),
        ).fetchall()
        chunk_ids = [row["id"] for row in rows]
        span_ids: List[str] = []
        for row in rows:
            span_ids.extend(_loads(row["source_span_ids"]) or [])
        self._delete_chunks_and_unreferenced_source_spans(chunk_ids, span_ids, commit=False)
        self.conn.execute("DELETE FROM visual_artifact_chunks WHERE artifact_id = ?", (artifact_id,))
        self.conn.commit()

    def delete_visual_page_chunks_for_artifact(self, artifact_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT c.id, c.source_span_ids, c.metadata
            FROM visual_artifact_chunks vac
            JOIN chunks c ON c.id = vac.chunk_id
            WHERE vac.artifact_id = ?
            """,
            (artifact_id,),
        ).fetchall()
        chunk_ids: List[str] = []
        span_ids: List[str] = []
        for row in rows:
            metadata = _loads(row["metadata"])
            if not _is_visual_page_chunk_metadata(metadata):
                continue
            chunk_ids.append(row["id"])
            span_ids.extend(_loads(row["source_span_ids"]) or [])
        self._delete_chunks_and_spans(chunk_ids, span_ids)

    def delete_visual_page_chunks_for_group_members(self, group_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT gm.artifact_id
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            WHERE gm.group_id = ?
            """,
            (group_id,),
        ).fetchall()
        artifact_ids = [row["artifact_id"] for row in rows if row["artifact_id"]]
        for artifact_id in artifact_ids:
            self.delete_visual_page_chunks_for_artifact(artifact_id)

    def _delete_chunks_and_spans(self, chunk_ids: Iterable[str], span_ids: Iterable[str]) -> Dict[str, int]:
        return self._delete_chunks_and_unreferenced_source_spans(chunk_ids, span_ids)

    def delete_visual_chunks_for_group(self, group_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT c.id, c.source_span_ids
            FROM visual_artifact_chunks vac
            JOIN chunks c ON c.id = vac.chunk_id
            WHERE vac.artifact_id = ?
            """,
            (group_id,),
        ).fetchall()
        chunk_ids = [row["id"] for row in rows]
        span_ids: List[str] = []
        for row in rows:
            span_ids.extend(_loads(row["source_span_ids"]) or [])
        self._delete_chunks_and_unreferenced_source_spans(chunk_ids, span_ids, commit=False)
        self.conn.execute("DELETE FROM visual_artifact_chunks WHERE artifact_id = ?", (group_id,))
        self.conn.commit()

    def _mark_visual_group_not_retrievable(self, group_id: str) -> None:
        """Clear group/member retrieval flags and delete current group/page chunks.

        Member page chunk cleanup is scoped through current artifact/group joins so stale
        membership rows do not make old groups delete chunks for artifacts that moved.
        """
        now = _now()
        self.conn.execute(
            "UPDATE visual_artifact_groups SET retrievable = 0, updated_at = ? WHERE id = ?",
            (now, group_id),
        )
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET group_retrievable = 0,
                retrievable = 0,
                updated_at = ?
            WHERE group_id = ?
            """,
            (now, group_id),
        )
        self.conn.commit()
        self.delete_visual_chunks_for_group(group_id)
        self.delete_visual_page_chunks_for_group_members(group_id)

    def invalidate_visual_group_for_member_analysis_change(
        self,
        group_id: str,
        artifact_id: str = "",
        reason: str = "",
    ) -> None:
        """Invalidate a group after a current member changes analysis state.

        Stale membership rows are pruned before recomputing pages, then group chunks and
        current member page chunks are removed so retry/force cannot leave retrievable residue.
        """
        if not group_id or not self.get_visual_artifact_group(group_id):
            return
        self.cleanup_stale_visual_artifact_group_members()
        rows = self.conn.execute(
            """
            SELECT gm.page
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            WHERE gm.group_id = ?
            ORDER BY gm.page ASC
            """,
            (group_id,),
        ).fetchall()
        pages = _merge_int_pages([row["page"] for row in rows], [])
        status = "pending" if len(pages) >= 2 else "skipped"
        error = str(reason or "visual group member analysis changed")
        if status == "skipped":
            error = "group has fewer than 2 current members"
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET source_pages = ?,
                status = ?,
                retrievable = 0,
                analyzed_at = 0,
                error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (_json(pages), status, error, now, group_id),
        )
        self.conn.execute(
            """
            UPDATE visual_artifacts
            SET group_retrievable = 0,
                retrievable = 0,
                updated_at = ?
            WHERE group_id = ?
            """,
            (now, group_id),
        )
        self.conn.commit()
        self.delete_visual_chunks_for_group(group_id)
        self.delete_visual_page_chunks_for_group_members(group_id)

    def _mark_visual_group_member_change(self, group_id: str, page: int) -> None:
        """Mark a group dirty from current joined rows plus the newly changed page."""
        group = self.get_visual_artifact_group(group_id)
        if not group:
            return
        member_rows = self.conn.execute(
            """
            SELECT gm.page
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            WHERE gm.group_id = ?
            ORDER BY gm.page ASC
            """,
            (group_id,),
        ).fetchall()
        member_pages = [row["page"] for row in member_rows]
        pages = _merge_int_pages(member_pages, [page] if page else [])
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET source_pages = ?,
                status = 'pending',
                retrievable = 0,
                analyzed_at = 0,
                updated_at = ?,
                error = ''
            WHERE id = ?
            """,
            (_json(pages), now, group_id),
        )
        self.conn.commit()
        self._mark_visual_group_not_retrievable(group_id)

    def _repair_visual_group_after_member_change(self, group_id: str) -> None:
        """Recompute group pages/status from current joined members and drop stale chunks."""
        group = self.get_visual_artifact_group(group_id)
        if not group:
            return
        rows = self.conn.execute(
            """
            SELECT gm.page
            FROM visual_artifact_group_members gm
            JOIN visual_artifacts va ON va.id = gm.artifact_id AND va.group_id = gm.group_id
            WHERE gm.group_id = ?
            ORDER BY gm.page ASC
            """,
            (group_id,),
        ).fetchall()
        pages = _merge_int_pages([row["page"] for row in rows], [])
        status = "pending" if len(pages) >= 2 else "skipped"
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_artifact_groups
            SET source_pages = ?,
                status = ?,
                retrievable = 0,
                analyzed_at = CASE WHEN ? = 'pending' THEN 0 ELSE analyzed_at END,
                error = CASE WHEN ? = 'skipped' THEN 'group has fewer than 2 current members' ELSE '' END,
                updated_at = ?
            WHERE id = ?
            """,
            (_json(pages), status, status, status, now, group_id),
        )
        self.conn.commit()
        self._mark_visual_group_not_retrievable(group_id)

    def upsert_visual_artifact_tile(self, tile: Dict[str, Any]) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO visual_artifact_tiles(
                id, artifact_id, tile_index, bbox, image_path, image_hash, status,
                confidence, result_json, error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                bbox = excluded.bbox,
                image_path = excluded.image_path,
                image_hash = excluded.image_hash,
                status = excluded.status,
                confidence = excluded.confidence,
                result_json = excluded.result_json,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                tile["id"],
                tile["artifact_id"],
                int(tile.get("tile_index") or 0),
                _json(tile.get("bbox") or {}),
                tile.get("image_path") or "",
                tile.get("image_hash") or "",
                tile.get("status") or "pending",
                float(tile.get("confidence") or 0),
                _json(tile.get("result_json") or {}),
                tile.get("error") or "",
                now,
                now,
            ),
        )
        self.conn.commit()

    def list_visual_artifact_tiles(self, artifact_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM visual_artifact_tiles
            WHERE artifact_id = ?
            ORDER BY tile_index ASC
            """,
            (artifact_id,),
        ).fetchall()
        return [_row_to_visual_artifact_tile(row) for row in rows]

    def delete_visual_artifact_tiles(self, artifact_id: str) -> None:
        self.conn.execute("DELETE FROM visual_artifact_tiles WHERE artifact_id = ?", (artifact_id,))
        self.conn.commit()


    def reset_visual_cache(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Delete visual artifacts, visual chunks, and prepare state for a document or KB."""

        clauses: List[str] = []
        params: List[Any] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        artifact_rows = self.conn.execute(f"SELECT id FROM visual_artifacts {where}", params).fetchall()
        artifact_ids = [row["id"] for row in artifact_rows]

        group_clauses: List[str] = []
        group_params: List[Any] = []
        if document_id:
            group_clauses.append("document_id = ?")
            group_params.append(document_id)
        if kb_id:
            group_clauses.append("kb_id = ?")
            group_params.append(kb_id)
        if version_id:
            group_clauses.append("version_id = ?")
            group_params.append(version_id)
        group_where = f"WHERE {' AND '.join(group_clauses)}" if group_clauses else ""
        group_rows = self.conn.execute(f"SELECT id FROM visual_artifact_groups {group_where}", group_params).fetchall()
        group_ids = [row["id"] for row in group_rows]

        chunk_ids: List[str] = []
        span_ids: List[str] = []
        delete_stats = {
            "deleted_chunks": 0,
            "deleted_source_spans": 0,
            "preserved_source_spans": 0,
            "pruned_entities": 0,
            "pruned_relations": 0,
        }
        visual_mapping_owner_ids = _unique([*artifact_ids, *group_ids])
        if visual_mapping_owner_ids:
            placeholders = ",".join("?" for _ in visual_mapping_owner_ids)
            rows = self.conn.execute(
                f"""
                SELECT DISTINCT c.id, c.source_span_ids
                FROM visual_artifact_chunks vac
                JOIN chunks c ON c.id = vac.chunk_id
                WHERE vac.artifact_id IN ({placeholders})
                """,
                visual_mapping_owner_ids,
            ).fetchall()
            for row in rows:
                chunk_ids.append(row["id"])
                span_ids.extend(_loads(row["source_span_ids"]) or [])
            delete_stats = self._delete_chunks_and_unreferenced_source_spans(chunk_ids, span_ids, commit=False)
            self.conn.execute(
                f"DELETE FROM visual_artifact_chunks WHERE artifact_id IN ({placeholders})",
                visual_mapping_owner_ids,
            )
        deleted_groups = int(
            self.conn.execute(f"SELECT COUNT(*) FROM visual_artifact_groups {group_where}", group_params).fetchone()[0]
        )
        self.conn.execute(f"DELETE FROM visual_artifact_group_members WHERE group_id IN ({','.join('?' for _ in group_ids)})", group_ids) if group_ids else None
        self.conn.execute(f"DELETE FROM visual_artifact_groups {group_where}", group_params)

        deleted_artifacts = 0
        if where:
            deleted_artifacts = int(self.conn.execute(f"SELECT COUNT(*) FROM visual_artifacts {where}", params).fetchone()[0])
            self.conn.execute(f"DELETE FROM visual_artifacts {where}", params)
        else:
            deleted_artifacts = int(self.conn.execute("SELECT COUNT(*) FROM visual_artifacts").fetchone()[0])
            self.conn.execute("DELETE FROM visual_artifacts")

        prepare_clauses: List[str] = []
        prepare_params: List[Any] = []
        if document_id:
            prepare_clauses.append("document_id = ?")
            prepare_params.append(document_id)
        if kb_id:
            prepare_clauses.append("kb_id = ?")
            prepare_params.append(kb_id)
        if version_id:
            prepare_clauses.append("version_id = ?")
            prepare_params.append(version_id)
        prepare_where = f"WHERE {' AND '.join(prepare_clauses)}" if prepare_clauses else ""
        deleted_prepare_states = int(
            self.conn.execute(f"SELECT COUNT(*) FROM visual_prepare_states {prepare_where}", prepare_params).fetchone()[0]
        )
        self.conn.execute(f"DELETE FROM visual_prepare_states {prepare_where}", prepare_params)
        deleted_tiles = 0
        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            deleted_tiles = int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM visual_artifact_tiles WHERE artifact_id IN ({placeholders})",
                    artifact_ids,
                ).fetchone()[0]
            )
            self.conn.execute(f"DELETE FROM visual_artifact_tiles WHERE artifact_id IN ({placeholders})", artifact_ids)
        self.conn.commit()
        return {
            "artifacts": deleted_artifacts,
            "groups": deleted_groups,
            "tiles": deleted_tiles,
            "chunks": delete_stats["deleted_chunks"],
            "source_spans": delete_stats["deleted_source_spans"],
            "prepare_states": deleted_prepare_states,
        }

    def visual_stats(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: List[Any] = []
        clauses: List[str] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = self.conn.execute(
                f"""
                SELECT analysis_status, COUNT(*) AS count
                FROM visual_artifacts
                {where}
                GROUP BY analysis_status
                """,
                params,
            ).fetchall()
        except sqlite3.DatabaseError:
            return {
                "total": 0,
                "pending": 0,
                "running": 0,
                "succeeded": 0,
                "low_confidence": 0,
                "failed": 0,
                "skipped": 0,
                "retrievable": 0,
            }
        counts = {str(row["analysis_status"]): int(row["count"]) for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "low_confidence": counts.get("low_confidence", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "retrievable": int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM visual_artifacts {where} {'AND' if where else 'WHERE'} retrievable = 1",
                    params,
                ).fetchone()[0]
            ),
        }

    def visual_group_stats(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: List[Any] = []
        clauses: List[str] = []
        if document_id:
            clauses.append("document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("version_id = ?")
            params.append(version_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = self.conn.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM visual_artifact_groups
                {where}
                GROUP BY status
                """,
                params,
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "succeeded": counts.get("succeeded", 0),
            "low_confidence": counts.get("low_confidence", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "retrievable": int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM visual_artifact_groups {where} {'AND' if where else 'WHERE'} retrievable = 1",
                    params,
                ).fetchone()[0]
            ),
        }

    def visual_tile_stats(
        self,
        document_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clauses = ["1 = 1"]
        params: List[Any] = []
        if document_id:
            clauses.append("va.document_id = ?")
            params.append(document_id)
        if kb_id:
            clauses.append("va.kb_id = ?")
            params.append(kb_id)
        if version_id:
            clauses.append("va.version_id = ?")
            params.append(version_id)
        where = " AND ".join(clauses)
        tile_row = self.conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT vt.artifact_id) AS tile_artifacts,
                COUNT(*) AS total_tiles
            FROM visual_artifact_tiles vt
            JOIN visual_artifacts va ON va.id = vt.artifact_id
            WHERE {where}
            """,
            params,
        ).fetchone()
        high_res_row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS high_res_retries
            FROM visual_artifacts va
            WHERE {where}
              AND COALESCE(json_extract(va.result_json, '$.processing.high_res_retry'), 0) != 0
            """,
            params,
        ).fetchone()
        return {
            "tile_artifacts": int(tile_row["tile_artifacts"] or 0) if tile_row else 0,
            "total_tiles": int(tile_row["total_tiles"] or 0) if tile_row else 0,
            "high_res_retries": int(high_res_row["high_res_retries"] or 0) if high_res_row else 0,
        }

    def create_visual_run(
        self,
        document_id: Optional[str] = None,
        kb_id: str = "kb_default",
        analysis_backend: str = "",
    ) -> str:
        run_id = f"visual_run_{uuid.uuid4().hex[:16]}"
        now = _now()
        self.conn.execute(
            """
            INSERT INTO visual_analysis_runs(id, document_id, kb_id, analysis_backend, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (run_id, document_id or "", kb_id or "kb_default", analysis_backend or "", now, now),
        )
        self.conn.commit()
        self.update_visual_run_stats(run_id)
        return run_id

    def update_visual_run_stats(self, run_id: str) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM visual_analysis_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return {}
        document_id = row["document_id"] or None
        kb_id = None if document_id else (row["kb_id"] or None)
        stats = self.visual_stats(document_id=document_id, kb_id=kb_id)
        status = "done" if stats["pending"] == 0 and stats["running"] == 0 else "running"
        now = _now()
        self.conn.execute(
            """
            UPDATE visual_analysis_runs
            SET status = ?,
                total = ?,
                pending = ?,
                running = ?,
                succeeded = ?,
                low_confidence = ?,
                failed = ?,
                skipped = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                stats["total"],
                stats["pending"],
                stats["running"],
                stats["succeeded"],
                stats["low_confidence"],
                stats["failed"],
                stats["skipped"],
                now,
                run_id,
            ),
        )
        self.conn.commit()
        return {"id": run_id, "status": status, **stats}

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

    def search(self, query: str, limit: int = 5, kb_ids: Optional[Iterable[str]] = None) -> List[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        kb_filter = _normalize_kb_filter(kb_ids)
        if self.fts5_available:
            hits = self._search_fts(query, limit, kb_filter)
            if hits:
                return hits
        return self._search_like(query, limit, kb_filter)

    def _check_fts5_support(self) -> bool:
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_probe USING fts5(text)")
            self.conn.execute("DROP TABLE IF EXISTS knowledge_fts_probe")
            return True
        except sqlite3.OperationalError:
            return False

    def _detect_existing_fts(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
            ).fetchone()
            if not row:
                return False
            self.conn.execute("SELECT rowid FROM chunks_fts LIMIT 1").fetchone()
            return True
        except sqlite3.DatabaseError:
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
            CREATE TABLE IF NOT EXISTS visual_artifacts (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                kb_id TEXT NOT NULL DEFAULT 'kb_default',
                artifact_type TEXT NOT NULL,
                page INTEGER NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                bbox TEXT NOT NULL DEFAULT '{}',
                image_path TEXT NOT NULL DEFAULT '',
                image_hash TEXT NOT NULL DEFAULT '',
                context_hash TEXT NOT NULL DEFAULT '',
                pipeline_version TEXT NOT NULL DEFAULT '',
                parser TEXT NOT NULL DEFAULT '',
                parser_confidence REAL NOT NULL DEFAULT 0,
                source_path TEXT NOT NULL DEFAULT '',
                crop_dpi INTEGER NOT NULL DEFAULT 180,
                crop_padding_px INTEGER NOT NULL DEFAULT 12,
                context_before TEXT NOT NULL DEFAULT '',
                context_after TEXT NOT NULL DEFAULT '',
                page_text TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                part_index INTEGER NOT NULL DEFAULT 0,
                continuation_role TEXT NOT NULL DEFAULT '',
                continuation_confidence REAL NOT NULL DEFAULT 0,
                group_retrievable INTEGER NOT NULL DEFAULT 0,
                analysis_status TEXT NOT NULL DEFAULT 'pending',
                analysis_confidence REAL NOT NULL DEFAULT 0,
                retrievable INTEGER NOT NULL DEFAULT 0,
                analysis_model TEXT NOT NULL DEFAULT '',
                analysis_backend TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                analyzed_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(document_id, version_id, page, image_hash, artifact_type)
            )
            """
        )
        self._ensure_column("visual_artifacts", "analysis_backend", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "pipeline_version", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "source_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "crop_dpi", "INTEGER NOT NULL DEFAULT 180")
        self._ensure_column("visual_artifacts", "crop_padding_px", "INTEGER NOT NULL DEFAULT 12")
        self._ensure_column("visual_artifacts", "context_before", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "context_after", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "page_text", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "group_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "part_index", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("visual_artifacts", "continuation_role", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("visual_artifacts", "continuation_confidence", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("visual_artifacts", "group_retrievable", "INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifacts_doc
            ON visual_artifacts(document_id, version_id, analysis_status)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifacts_group
            ON visual_artifacts(group_id, part_index)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_artifact_groups (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                kb_id TEXT NOT NULL DEFAULT 'kb_default',
                group_type TEXT NOT NULL DEFAULT 'unknown',
                title TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                source_pages TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                confidence REAL NOT NULL DEFAULT 0,
                retrievable INTEGER NOT NULL DEFAULT 0,
                analysis_model TEXT NOT NULL DEFAULT '',
                analysis_backend TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                analyzed_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifact_groups_doc
            ON visual_artifact_groups(document_id, version_id, status)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifact_groups_kb
            ON visual_artifact_groups(kb_id, status, retrievable)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_artifact_group_members (
                group_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                page INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(group_id, artifact_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_group_members_artifact
            ON visual_artifact_group_members(artifact_id)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_artifact_tiles (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                tile_index INTEGER NOT NULL,
                bbox TEXT NOT NULL DEFAULT '{}',
                image_path TEXT NOT NULL DEFAULT '',
                image_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                confidence REAL NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifact_tiles_artifact
            ON visual_artifact_tiles(artifact_id, tile_index, status)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_artifacts_kb
            ON visual_artifacts(kb_id, analysis_status, retrievable)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_analysis_runs (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL DEFAULT '',
                kb_id TEXT NOT NULL DEFAULT 'kb_default',
                analysis_backend TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0,
                running INTEGER NOT NULL DEFAULT 0,
                succeeded INTEGER NOT NULL DEFAULT 0,
                low_confidence INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._ensure_column("visual_analysis_runs", "analysis_backend", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_prepare_states (
                document_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                kb_id TEXT NOT NULL DEFAULT 'kb_default',
                source_path TEXT NOT NULL DEFAULT '',
                total_pages INTEGER NOT NULL DEFAULT 0,
                next_page INTEGER NOT NULL DEFAULT 1,
                prepared_pages INTEGER NOT NULL DEFAULT 0,
                prepared_artifacts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '',
                pipeline_version TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(document_id, version_id)
            )
            """
        )
        self._ensure_column("visual_prepare_states", "pipeline_version", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_visual_prepare_states_kb
            ON visual_prepare_states(kb_id, status)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS visual_artifact_chunks (
                artifact_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                PRIMARY KEY(artifact_id, chunk_id)
            )
            """
        )
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

    def _search_fts(self, query: str, limit: int, kb_ids: List[str]) -> List[SearchHit]:
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []
        kb_clause, kb_params = _kb_where_clause("c.kb_id", kb_ids)
        try:
            rows = self.conn.execute(
                f"""
                SELECT c.*, d.title, d.source_path, bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ?
                {kb_clause}
                ORDER BY rank
                LIMIT ?
                """,
                [fts_query, *kb_params, limit],
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        return [_row_to_hit(row, _rank_to_score(row["rank"])) for row in rows]

    def _search_like(self, query: str, limit: int, kb_ids: List[str]) -> List[SearchHit]:
        terms = _search_terms(query)
        if not terms:
            return []
        where = "(" + " OR ".join("LOWER(c.text) LIKE ?" for _ in terms) + ")"
        params = [f"%{term.lower()}%" for term in terms]
        kb_clause, kb_params = _kb_where_clause("c.kb_id", kb_ids)
        params.extend(kb_params)
        params.append(limit)
        rows = self.conn.execute(
            f"""
                SELECT c.*, d.title, d.source_path
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where}
            {kb_clause}
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


def stable_visual_artifact_id(
    document_id: str,
    version_id: str,
    page: int,
    image_hash: str,
    artifact_type: str,
    bbox: Dict[str, Any],
) -> str:
    bbox_json = json.dumps(bbox or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{document_id}|{version_id}|{page}|{artifact_type}|{image_hash}|{bbox_json}"
    return "visual_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_visual_chunk_id(document_id: str, version_id: str, artifact_id: str, chunk_kind: str, text: str) -> str:
    raw = f"{document_id}|{version_id}|{artifact_id}|{chunk_kind}|{text}"
    return "chunk_visual_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_visual_span_id(document_id: str, version_id: str, artifact_id: str, text: str) -> str:
    raw = f"span_visual|{document_id}|{version_id}|{artifact_id}|{text}"
    return "span_visual_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_visual_group_chunk_id(document_id: str, version_id: str, group_id: str, chunk_kind: str, text: str) -> str:
    raw = f"{document_id}|{version_id}|{group_id}|{chunk_kind}|{text}"
    return "chunk_visual_group_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_visual_group_span_id(document_id: str, version_id: str, group_id: str, text: str) -> str:
    raw = f"span_visual_group|{document_id}|{version_id}|{group_id}|{text}"
    return "span_visual_group_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
        ordinal=int(row["ordinal"]),
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


def _row_to_chunk(row: sqlite3.Row) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=row["id"],
        document_id=row["document_id"],
        ordinal=int(row["ordinal"]),
        page_start=int(row["page_start"]),
        page_end=int(row["page_end"]),
        text=row["text"] or "",
        kb_id=row["kb_id"] or "kb_default",
        version_id=row["version_id"] or "",
        section_path=row["section_path"] or "",
        clause_title=row["clause_title"] or "",
        source_span_ids=_loads(row["source_span_ids"]),
        entities=_loads(row["entities"]),
        metadata=_loads(row["metadata"]),
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


def _row_to_visual_artifact(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "kb_id": row["kb_id"] or "kb_default",
        "artifact_type": row["artifact_type"],
        "page": int(row["page"]),
        "label": row["label"] or "",
        "caption": row["caption"] or "",
        "bbox": _loads(row["bbox"]) or {},
        "image_path": row["image_path"] or "",
        "image_hash": row["image_hash"] or "",
        "context_hash": row["context_hash"] or "",
        "pipeline_version": _row_value(row, "pipeline_version", ""),
        "parser": row["parser"] or "",
        "parser_confidence": float(row["parser_confidence"] or 0),
        "source_path": _row_value(row, "source_path", ""),
        "crop_dpi": int(_row_value(row, "crop_dpi", 180) or 180),
        "crop_padding_px": int(_row_value(row, "crop_padding_px", 12) or 12),
        "context_before": _row_value(row, "context_before", ""),
        "context_after": _row_value(row, "context_after", ""),
        "page_text": _row_value(row, "page_text", ""),
        "group_id": _row_value(row, "group_id", ""),
        "part_index": int(_row_value(row, "part_index", 0) or 0),
        "continuation_role": _row_value(row, "continuation_role", ""),
        "continuation_confidence": float(_row_value(row, "continuation_confidence", 0) or 0),
        "group_retrievable": bool(_row_value(row, "group_retrievable", 0)),
        "analysis_status": row["analysis_status"] or "pending",
        "analysis_confidence": float(row["analysis_confidence"] or 0),
        "retrievable": bool(row["retrievable"]),
        "analysis_model": row["analysis_model"] or "",
        "analysis_backend": _row_value(row, "analysis_backend", ""),
        "prompt_version": row["prompt_version"] or "",
        "result_json": _loads(row["result_json"]) or {},
        "error": row["error"] or "",
        "pipeline_version": _row_value(row, "pipeline_version", ""),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "analyzed_at": int(row["analyzed_at"] or 0),
    }


def _row_to_visual_artifact_group(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "kb_id": row["kb_id"] or "kb_default",
        "group_type": row["group_type"] or "unknown",
        "title": row["title"] or "",
        "caption": row["caption"] or "",
        "source_pages": _loads(row["source_pages"]) or [],
        "status": row["status"] or "pending",
        "confidence": float(row["confidence"] or 0),
        "retrievable": bool(row["retrievable"]),
        "analysis_model": row["analysis_model"] or "",
        "analysis_backend": row["analysis_backend"] or "",
        "prompt_version": row["prompt_version"] or "",
        "result_json": _loads(row["result_json"]) or {},
        "error": row["error"] or "",
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "analyzed_at": int(row["analyzed_at"] or 0),
    }


def _row_to_visual_group_member(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "group_id": row["group_id"],
        "artifact_id": row["artifact_id"],
        "part_index": int(row["part_index"] or 0),
        "page": int(row["page"] or 0),
        "role": row["role"] or "",
        "confidence": float(row["confidence"] or 0),
        "artifact_type": _row_value(row, "artifact_type", ""),
        "caption": _row_value(row, "caption", ""),
        "label": _row_value(row, "label", ""),
        "bbox": _loads(_row_value(row, "bbox", "{}")) or {},
        "result_json": _loads(_row_value(row, "result_json", "{}")) or {},
        "analysis_status": _row_value(row, "analysis_status", ""),
        "analysis_confidence": float(_row_value(row, "analysis_confidence", 0) or 0),
        "source_path": _row_value(row, "source_path", ""),
        "image_path": _row_value(row, "image_path", ""),
        "context_before": _row_value(row, "context_before", ""),
        "context_after": _row_value(row, "context_after", ""),
        "page_text": _row_value(row, "page_text", ""),
        "current_group_id": _row_value(row, "current_group_id", ""),
    }


def _row_to_visual_artifact_tile(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "artifact_id": row["artifact_id"],
        "tile_index": int(row["tile_index"] or 0),
        "bbox": _loads(row["bbox"]) or {},
        "image_path": row["image_path"] or "",
        "image_hash": row["image_hash"] or "",
        "status": row["status"] or "pending",
        "confidence": float(row["confidence"] or 0),
        "result_json": _loads(row["result_json"]) or {},
        "error": row["error"] or "",
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def _row_to_visual_prepare_state(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "document_id": row["document_id"],
        "version_id": row["version_id"],
        "kb_id": row["kb_id"] or "kb_default",
        "source_path": row["source_path"] or "",
        "total_pages": int(row["total_pages"] or 0),
        "next_page": int(row["next_page"] or 1),
        "prepared_pages": int(row["prepared_pages"] or 0),
        "prepared_artifacts": int(row["prepared_artifacts"] or 0),
        "status": row["status"] or "pending",
        "error": row["error"] or "",
        "pipeline_version": _row_value(row, "pipeline_version", ""),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


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


def _json_list(value: Any) -> List[Any]:
    loaded = _loads(value)
    return loaded if isinstance(loaded, list) else []


def _merge_int_pages(old_pages: Iterable[Any], new_pages: Iterable[Any]) -> List[int]:
    values: set[int] = set()
    for page in [*(old_pages or []), *(new_pages or [])]:
        try:
            number = int(page)
        except (TypeError, ValueError):
            continue
        if number > 0:
            values.add(number)
    return sorted(values)


def _group_pages_changed(old_pages: Iterable[Any], new_pages: Iterable[Any]) -> bool:
    return _merge_int_pages(old_pages, []) != _merge_int_pages(new_pages, [])


def _group_identity_changed(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    for key in ("group_type", "title", "caption"):
        incoming_value = str(incoming.get(key) or "").strip()
        if incoming_value and incoming_value != str(existing.get(key) or "").strip():
            return True
    return False


def _preserved_visual_group_status(existing_status: Any, incoming_status: str) -> str:
    existing = str(existing_status or "pending")
    if existing in {"succeeded", "low_confidence", "failed"}:
        return existing
    return incoming_status or existing or "pending"


def _merge_visual_group_result_json(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
    source_pages: List[int],
) -> Dict[str, Any]:
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key == "continuation_evidence":
            continue
        if value not in ("", None, [], {}):
            merged[key] = value
    evidence = []
    for payload in (existing or {}, incoming or {}):
        values = payload.get("continuation_evidence") if isinstance(payload, dict) else []
        if isinstance(values, list):
            evidence.extend(str(item) for item in values)
    if evidence:
        merged["continuation_evidence"] = _unique(evidence)
    merged["source_pages"] = source_pages
    return merged


def _is_visual_page_chunk_metadata(metadata: Any) -> bool:
    return (
        isinstance(metadata, dict)
        and metadata.get("source") == "visual_analysis"
        and metadata.get("visual_scope") == "page"
    )


def _row_value(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    return default


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


def _normalize_kb_filter(kb_ids: Optional[Iterable[str]]) -> List[str]:
    if not kb_ids:
        return []
    return [str(kb_id) for kb_id in dict.fromkeys(kb_ids) if str(kb_id or "").strip()]


def _kb_where_clause(column: str, kb_ids: List[str]) -> Tuple[str, List[str]]:
    if not kb_ids:
        return "", []
    placeholders = ",".join("?" for _ in kb_ids)
    return f"AND {column} IN ({placeholders})", kb_ids


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
