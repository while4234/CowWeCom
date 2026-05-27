import sqlite3
import json

import pytest

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import ExtractedDocument, DocumentPage, KnowledgeChunk, KnowledgeDocument, VisualAnalysisResult, VisualArtifactCandidate
from agent.knowledge.backend.service import dispatch_admin_request
from agent.knowledge.backend.storage import KnowledgeStorage
from agent.knowledge.backend.visual_analyzer import (
    VisualAnalyzer,
    validate_visual_analysis_json,
    validate_visual_group_analysis_json,
    visual_result_to_chunks,
)
from agent.knowledge.backend.visual_extractors import PyMuPDFVisualArtifactExtractor, is_strict_caption_block
from agent.knowledge.backend.visual_grouping import VisualArtifactGrouper


def _config(tmp_path, **overrides):
    mapping = {
        "enabled": True,
        "provider_api_enabled": True,
        "admin_api_enabled": True,
        "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
        "workspace_root": str(tmp_path),
        "data_dir": str(tmp_path / "backend-data"),
        "default_kb_id": "kb_default",
        "ingest": {
            "allowed_extensions": [".md"],
            "max_file_size_mb": 5,
            "document_library_root": str(tmp_path),
        },
        "visual_analysis": {
            "enabled": True,
            "auto_build_after_upload": True,
            "model": "gpt-5.5",
            "reasoning_effort": "xhigh",
            "prompt_version": "visual-v1",
            "max_items_per_request": 1,
        },
        "vector_store": {"provider": "sqlite", "required": False},
        "security": {"disable_admin_api_when_web_password_empty": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(mapping.get(key), dict):
            mapping[key] = {**mapping[key], **value}
        else:
            mapping[key] = value
    return KnowledgeBackendConfig.from_mapping(mapping)


def _service(tmp_path, **overrides):
    return KnowledgeBackendService(_config(tmp_path, **overrides))


def _ingest(service):
    result = service.ingest_upload_bytes(
        "visual-source.md",
        b"# Visual Source\n\nFigure 1 shows a protocol timing table with visual facts.",
        title="Visual Source",
    )
    assert result["status"] == "succeeded", result
    return result["document"]["id"], result["document"]["version_id"]


def _candidate(document_id, version_id, index, artifact_type="table"):
    return VisualArtifactCandidate(
        id=f"visual_test_{index}",
        document_id=document_id,
        version_id=version_id,
        kb_id="kb_default",
        artifact_type=artifact_type,
        page=index,
        label=f"Figure {index}",
        caption=f"Figure {index}. Visual caption",
        bbox={"x0": 10, "y0": 20 + index, "x1": 200, "y1": 180, "unit": "pdf_points"},
        image_path=f"artifact-{index}.png",
        image_hash=f"image-hash-{index}",
        context_hash=f"context-hash-{index}",
        parser="fake",
        parser_confidence=0.9,
        section_path=["1 Visual"],
        context_before="before",
        context_after="after",
        page_text="page text",
    )


class FakeExtractor:
    def __init__(self, candidates):
        self.candidates = candidates

    def extract_candidates(self, document, extracted_document, storage, config):
        return list(self.candidates)


class FakeRangeExtractor(FakeExtractor):
    def __init__(self, candidates, pages_per_call=1):
        super().__init__(candidates)
        self.calls = []
        self.pages_per_call = pages_per_call

    def extract_candidates_for_page_range(self, document, extracted_document, storage, config, start_page, max_pages):
        self.calls.append((start_page, max_pages))
        end_page = start_page + max_pages - 1
        candidates = [candidate for candidate in self.candidates if start_page <= candidate.page <= end_page]
        return list(candidates), {"pages_scanned": self.pages_per_call, "candidates": len(candidates)}

    def ensure_visual_artifact_image(self, candidate, config):
        return candidate


class FakeRangeSequenceExtractor:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def extract_candidates_for_page_range(self, document, extracted_document, storage, config, start_page, max_pages):
        self.calls.append((start_page, max_pages))
        if self.batches:
            pages_scanned, candidates = self.batches.pop(0)
        else:
            pages_scanned, candidates = 0, []
        return list(candidates), {"pages_scanned": pages_scanned, "candidates": len(candidates)}

    def ensure_visual_artifact_image(self, candidate, config):
        return candidate


class QueueAnalyzer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.backends = []

    def analyze(self, candidate, config, document=None, analysis_backend=None):
        self.calls.append(candidate.id)
        self.backends.append(analysis_backend)
        item = self.results.pop(0)
        return item(candidate) if callable(item) else item


class RecordingAnalyzer(QueueAnalyzer):
    def __init__(self, results):
        super().__init__(results)
        self.candidates = []

    def analyze(self, candidate, config, document=None, analysis_backend=None):
        self.candidates.append(candidate)
        return super().analyze(candidate, config, document=document, analysis_backend=analysis_backend)


class QueueVisualAnalyzer(VisualAnalyzer):
    skip_backend_availability_check = True

    def __init__(self, results, group_results=None):
        self.results = list(results)
        self.group_results = list(group_results or [])
        self.calls = []
        self.group_calls = []
        self.backends = []
        self.max_image_long_edges = []
        self.contexts_before = []

    def analyze(self, candidate, config, document=None, analysis_backend=None):
        self.calls.append(candidate.id)
        self.backends.append(analysis_backend)
        self.max_image_long_edges.append((config.visual_analysis or {}).get("max_image_long_edge"))
        self.contexts_before.append(candidate.context_before)
        item = self.results.pop(0)
        return item(candidate) if callable(item) else item

    def analyze_group(self, group, members, config, document=None, analysis_backend=None):
        self.group_calls.append(group["id"])
        self.backends.append(analysis_backend)
        assert self.group_results, "no queued group result"
        item = self.group_results.pop(0)
        return item(group, members) if callable(item) else item


class ImageRecordingGroupAnalyzer(VisualAnalyzer):
    skip_backend_availability_check = True

    def __init__(self):
        self.image_url_count = 0

    def _call_group_vision_model(self, group, members, config, document=None, analysis_backend=None):
        self.image_url_count = len([member for member in members if member.get("image_path")])
        return json.dumps(
            {
                "artifact_type": "table",
                "title": "image group",
                "caption": "image group",
                "is_multipage": True,
                "source_pages": [member["page"] for member in members],
                "summary": "image_group_merge_fact",
                "key_facts": [{"fact": "image_group_merge_fact", "confidence": 0.9}],
                "parts": [{"page": member["page"], "artifact_id": member["artifact_id"], "role": member["role"], "confidence": 0.9} for member in members],
                "merged_table": {"headers": ["Signal"], "rows": [{"Signal": "IMG"}], "markdown": "| Signal |\n| --- |\n| IMG |"},
                "confidence": {"ocr": 0.9, "structure": 0.9, "semantic": 0.9, "continuation": 0.9, "overall": 0.9},
                "should_index": True,
            }
        )


class CountingExtractor(FakeRangeExtractor):
    def __init__(self, candidates):
        super().__init__(candidates)
        self.ensure_calls = []

    def ensure_visual_artifact_image(self, candidate, config):
        self.ensure_calls.append((candidate.id, candidate.crop_dpi, candidate.bbox))
        return candidate


def _high_result(text="visualfact", artifact_type="table"):
    return VisualAnalysisResult(
        artifact_type=artifact_type,
        title="Timing Table",
        caption="Figure 1. Visual caption",
        summary=f"High confidence summary with {text}.",
        structured_markdown=f"| Signal | Meaning |\n| --- | --- |\n| TVALID | {text} |",
        key_facts=[{"fact": f"{text} is extracted from the visual artifact", "confidence": 0.9}],
        table={"markdown": f"| Signal | Meaning |\n| --- | --- |\n| TVALID | {text} |"},
        readability="good",
        confidence={"ocr": 0.9, "structure": 0.9, "semantic": 0.9, "overall": 0.9},
        should_index=True,
    )


def _table_result(text, page=1, confidence=0.9):
    return VisualAnalysisResult(
        artifact_type="table",
        title="Signal List",
        caption="Table 5-1. Signal List",
        summary=f"Rows from page {page} include {text}.",
        structured_markdown=f"| Signal | Direction | Description |\n| --- | --- | --- |\n| {text} | input | page {page} |",
        key_facts=[{"fact": f"{text} appears on page {page}", "confidence": confidence}],
        table={
            "headers": ["Signal", "Direction", "Description"],
            "rows": [{"Signal": text, "Direction": "input", "Description": f"page {page}"}],
            "markdown": f"| Signal | Direction | Description |\n| --- | --- | --- |\n| {text} | input | page {page} |",
        },
        readability="good" if confidence >= 0.7 else "poor",
        confidence={"ocr": confidence, "structure": confidence, "semantic": confidence, "overall": confidence},
        should_index=confidence >= 0.7,
    )


def _low_result(text="lowfact"):
    return VisualAnalysisResult(
        artifact_type="figure",
        title="Blurred Figure",
        caption="Figure 1. Visual caption",
        summary=f"Low confidence summary with {text}.",
        structured_markdown="",
        key_facts=[{"fact": text, "confidence": 0.4}],
        readability="poor",
        confidence={"ocr": 0.4, "structure": 0.4, "semantic": 0.4, "overall": 0.4},
        should_index=False,
        low_confidence_reason="readability is poor",
    )


def _raw_low_table_result(text="LOW_RAW_INDEXED", page=1, confidence=0.4, *, should_index=True):
    return {
        "artifact_type": "table",
        "title": "Low Raw Table",
        "caption": f"Table {page}. Low raw table",
        "page": page,
        "summary": f"Low confidence summary with {text}.",
        "structured_markdown": f"| Signal | Meaning |\n| --- | --- |\n| {text} | low confidence |",
        "key_facts": [{"fact": text, "confidence": confidence}],
        "table": {
            "headers": ["Signal", "Meaning"],
            "rows": [{"Signal": text, "Meaning": "low confidence"}],
            "markdown": f"| Signal | Meaning |\n| --- | --- |\n| {text} | low confidence |",
        },
        "readability": "poor",
        "confidence": {"ocr": confidence, "structure": confidence, "semantic": confidence, "overall": confidence},
        "should_index": should_index,
    }


def _append_page_visual_chunk(service, storage, candidate, result):
    document = storage.get_document(candidate.document_id)
    chunks, spans = visual_result_to_chunks(
        candidate,
        result,
        document,
        service.config.visual_analysis,
        analysis_backend="codex",
        analysis_model="gpt-5.5",
    )
    storage.append_visual_chunks(document.id, document.version_id, candidate.id, chunks, spans)


def _seed_ready_group(service, document_id, version_id, group_id="visual_group_ready", pages=(1, 2), *, image_paths=None):
    storage = service._backend._get_storage(writable=True)
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": group_id,
            "caption": group_id,
            "source_pages": list(pages),
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    image_paths = image_paths or {}
    for index, page in enumerate(pages, start=1):
        candidate = VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, page).to_dict(),
                "page": page,
                "image_path": str(image_paths.get(page) or f"artifact-{page}.png"),
                "pipeline_version": "visual-pipeline-v1",
            }
        )
        storage.upsert_visual_artifact(candidate)
        role = "first" if index == 1 else ("last" if index == len(pages) else "middle")
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, page, role, 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, role, 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"READY_{page}", page=page).to_dict(), 0.9, retrievable=False)
    return storage


def _group_chunk_refs(storage, document_id, group_id):
    chunks = [
        chunk
        for chunk in storage.list_chunks(document_id)
        if chunk.metadata.get("visual_scope") == "group"
        and chunk.metadata.get("visual_group_id") == group_id
    ]
    span_ids = sorted({span_id for chunk in chunks for span_id in chunk.source_span_ids})
    return chunks, span_ids


def _count_rows(conn, table, column, values):
    values = [value for value in values if value]
    if not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", values).fetchone()[0]


def _upsert_visual_group(storage, document_id, version_id, group_id, source_pages=None, *, status="pending", retrievable=0):
    pages = [1, 2] if source_pages is None else list(source_pages)
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": group_id,
            "caption": group_id,
            "source_pages": pages,
            "status": status,
            "confidence": 0.9,
            "retrievable": retrievable,
            "result_json": {},
        }
    )


def _pollute_visual_group_source_pages(storage, group_id, source_pages):
    storage.conn.execute(
        """
        UPDATE visual_artifact_groups
        SET source_pages = ?,
            status = 'succeeded',
            retrievable = 1,
            analyzed_at = 123,
            error = 'legacy polluted source_pages'
        WHERE id = ?
        """,
        (json.dumps(source_pages), group_id),
    )
    storage.conn.commit()


def _insert_legacy_stale_group_member(storage, document_id, version_id, group_id, *, stale_page=99, other_group_id=None):
    other_group_id = other_group_id or f"{group_id}_other"
    _upsert_visual_group(storage, document_id, version_id, other_group_id, [stale_page], status="pending")
    stale = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, stale_page).to_dict(),
            "page": stale_page,
            "caption": f"Figure {stale_page}. Legacy stale member",
            "label": f"Figure {stale_page}",
        }
    )
    storage.upsert_visual_artifact(stale)
    storage.mark_visual_artifact_group_membership(stale.id, other_group_id, 1, "first", 0.9)
    storage.conn.execute(
        """
        INSERT OR REPLACE INTO visual_artifact_group_members(group_id, artifact_id, part_index, page, role, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (group_id, stale.id, stale_page, stale_page, "stale", 0.9),
    )
    storage.conn.commit()
    return stale


def test_visual_schema_is_created(tmp_path):
    storage = KnowledgeStorage(tmp_path / "knowledge.sqlite3")
    try:
        with sqlite3.connect(str(tmp_path / "knowledge.sqlite3")) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                ).fetchall()
            }
    finally:
        storage.close()

    assert "visual_artifacts" in tables
    assert "visual_analysis_runs" in tables
    assert "visual_artifact_chunks" in tables
    assert "visual_prepare_states" in tables
    assert "visual_artifact_groups" in tables
    assert "visual_artifact_group_members" in tables
    assert "visual_artifact_tiles" in tables
    with sqlite3.connect(str(tmp_path / "knowledge.sqlite3")) as conn:
        visual_columns = {row[1] for row in conn.execute("PRAGMA table_info(visual_artifacts)").fetchall()}
        prepare_columns = {row[1] for row in conn.execute("PRAGMA table_info(visual_prepare_states)").fetchall()}
    assert "pipeline_version" in visual_columns
    assert "group_id" in visual_columns
    assert "part_index" in visual_columns
    assert "continuation_role" in visual_columns
    assert "continuation_confidence" in visual_columns
    assert "group_retrievable" in visual_columns
    assert "pipeline_version" in prepare_columns


def test_visual_build_is_artifact_level_resumable(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidates = [_candidate(document_id, version_id, index) for index in range(1, 4)]
    analyzer = QueueAnalyzer([lambda c: _high_result(f"visualfact{c.page}") for _ in candidates])
    service._visual_extractor = FakeExtractor(candidates)
    service._visual_analyzer = analyzer

    first = service.build_visual_knowledge(document_id=document_id, limit=1)
    second = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert first["processed"] == 1
    assert second["processed"] == 1
    assert analyzer.calls == ["visual_test_1", "visual_test_2"]
    assert service.get_visual_stats(document_id)["pending"] == 1


def test_high_confidence_visual_result_is_indexed(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("indexed_visualfact")])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["succeeded"] == 1
    assert service.search("indexed_visualfact", limit=5)
    storage = service._backend._get_read_storage()
    artifacts = storage.list_visual_artifacts(document_id=document_id)
    assert artifacts[0]["analysis_status"] == "succeeded"
    assert artifacts[0]["retrievable"] is True
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM visual_artifact_chunks").fetchone()[0] >= 1


def test_low_confidence_visual_result_is_not_indexed(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_low_result("low_visualfact")])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["low_confidence"] == 1
    assert service.search("low_visualfact", limit=5) == []
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["analysis_status"] == "low_confidence"
    assert artifact["retrievable"] is False


def test_index_low_confidence_does_not_index_low_confidence_page_raw_result(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True})
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_raw_low_table_result("LOW_RAW_INDEXED", page=1, should_index=True)])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["low_confidence"] == 1
    assert result["succeeded"] == 0
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["analysis_status"] == "low_confidence"
    assert artifact["retrievable"] is False
    assert service.search("LOW_RAW_INDEXED", limit=5) == []


def test_force_retry_replaces_old_visual_chunks(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("old_visualfact"), _high_result("new_visualfact")])

    assert service.build_visual_knowledge(document_id=document_id, limit=1)["succeeded"] == 1
    assert service.search("old_visualfact", limit=5)

    retry = service.build_visual_knowledge(document_id=document_id, limit=1, force=True)

    assert retry["succeeded"] == 1
    assert service.search("new_visualfact", limit=5)
    assert service.search("old_visualfact", limit=5) == []


def test_visual_admin_dispatch_and_disabled_state(monkeypatch, tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("dispatch_visualfact")])
    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: service.config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda _: service)

    response = dispatch_admin_request("POST", "visual/build", {"document_id": document_id, "limit": 1})

    assert response["ok"] is True
    assert response["processed"] == 1
    assert response["pending"] == 0
    assert response["has_more"] is False

    disabled_service = _service(tmp_path / "disabled", visual_analysis={"enabled": False})
    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: disabled_service.config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda _: disabled_service)

    disabled = dispatch_admin_request("POST", "visual/build", {})
    assert disabled["status"] == "disabled"
    assert "visual_analysis.enabled" in disabled["message"]


def test_visual_reset_api_clears_artifacts_chunks_and_prepare_state(monkeypatch, tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("reset_visualfact")])
    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: service.config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda _: service)

    assert dispatch_admin_request("POST", "visual/build", {"document_id": document_id, "limit": 1})["succeeded"] == 1
    assert service.search("reset_visualfact", limit=5)

    response = dispatch_admin_request("POST", "visual/reset", {"document_id": document_id})

    assert response["ok"] is True
    assert response["reset"]["artifacts"] == 1
    assert response["reset"]["chunks"] >= 1
    assert response["reset"]["prepare_states"] == 1
    assert service.search("reset_visualfact", limit=5) == []
    assert service.get_visual_stats(document_id)["total"] == 0


def test_visual_pipeline_version_change_resets_stale_cache(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("old_pipeline_visualfact")])

    assert service.build_visual_knowledge(document_id=document_id, limit=1)["succeeded"] == 1
    assert service.search("old_pipeline_visualfact", limit=5)

    service._visual_analyzer = QueueAnalyzer([_high_result("new_pipeline_visualfact")])
    service.config.visual_analysis["pipeline_version"] = "visual-pipeline-v2"
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    rebuilt = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert rebuilt["succeeded"] == 1
    assert service.search("old_pipeline_visualfact", limit=5) == []
    assert service.search("new_pipeline_visualfact", limit=5)
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["pipeline_version"] == "visual-pipeline-v2"


def test_visual_pipeline_reset_checks_beyond_first_artifact_page(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    for index in range(1, 1003):
        candidate = _candidate(document_id, version_id, index)
        pipeline_version = "visual-pipeline-v1" if index <= 1001 else "visual-pipeline-v0"
        storage.upsert_visual_artifact(
            VisualArtifactCandidate(**{**candidate.to_dict(), "pipeline_version": pipeline_version})
        )

    service.config.visual_analysis["pipeline_version"] = "visual-pipeline-v1"
    reset = service._reset_stale_visual_pipeline(
        storage,
        document_id=document_id,
        pipeline_version="visual-pipeline-v1",
    )

    assert reset["artifacts"] == 1002
    assert storage.visual_stats(document_id=document_id, version_id=version_id)["total"] == 0


def test_visual_model_json_validation_failures_do_not_index(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidates = [_candidate(document_id, version_id, 1), _candidate(document_id, version_id, 2), _candidate(document_id, version_id, 3)]
    service._visual_extractor = FakeExtractor(candidates)
    service._visual_analyzer = QueueAnalyzer(
        [
            "not json",
            {"summary": "missing confidence", "should_index": True},
            {
                "artifact_type": "figure",
                "summary": "poor_but_claimed_indexable",
                "key_facts": [{"fact": "poor_but_claimed_indexable", "confidence": 0.9}],
                "readability": "poor",
                "confidence": {"ocr": 0.9, "structure": 0.9, "semantic": 0.9, "overall": 0.9},
                "should_index": True,
            },
        ]
    )

    first = service.build_visual_knowledge(document_id=document_id, limit=1)
    second = service.build_visual_knowledge(document_id=document_id, limit=1)
    third = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert first["failed"] == 1
    assert second["failed"] == 1
    assert third["low_confidence"] == 1
    retry_failed = service.build_visual_knowledge(document_id=document_id, limit=1, retry_failed=True)
    assert retry_failed["failed"] == 1
    assert service.search("poor_but_claimed_indexable", limit=5) == []


def test_visual_build_filters_to_single_document(tmp_path):
    service = _service(tmp_path)
    doc1_id, doc1_version = _ingest(service)
    doc2 = service.ingest_upload_bytes(
        "visual-source-2.md",
        b"# Visual Source 2\n\nFigure 2 shows a protocol timing table.",
        title="Visual Source 2",
    )
    doc2_id = doc2["document"]["id"]
    doc2_version = doc2["document"]["version_id"]
    service._visual_extractor = FakeExtractor(
        [
            _candidate(doc1_id, doc1_version, 1),
            _candidate(doc2_id, doc2_version, 2),
        ]
    )
    analyzer = QueueAnalyzer([_high_result("doc2_visualfact")])
    service._visual_analyzer = analyzer

    result = service.build_visual_knowledge(document_id=doc2_id, limit=1)

    assert result["processed"] == 1
    assert analyzer.calls == ["visual_test_2"]
    assert service.search("doc2_visualfact", limit=5)
    assert service.get_visual_stats(doc1_id)["pending"] == 1


def test_visual_build_records_selected_analysis_backend(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    analyzer = QueueAnalyzer([_high_result("codex_visualfact")])
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = analyzer

    result = service.build_visual_knowledge(document_id=document_id, limit=1, analysis_backend="codex")

    assert result["analysis_backend"] == "codex"
    assert analyzer.backends == ["codex"]
    storage = service._backend._get_read_storage()
    artifact = storage.list_visual_artifacts(document_id=document_id)[0]
    assert artifact["analysis_backend"] == "codex"
    chunks = storage.list_chunks(document_id)
    visual_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
    assert visual_chunks
    assert visual_chunks[0].metadata["analysis_backend"] == "codex"
    assert visual_chunks[0].metadata["analysis_model"] == "gpt-5.5"


def test_visual_backends_admin_api_lists_supported_ids(monkeypatch, tmp_path):
    service = _service(tmp_path)
    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: service.config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda _: service)

    response = dispatch_admin_request("GET", "visual/backends", {})

    assert response["ok"] is True
    assert {item["id"] for item in response["backends"]} == {"current", "capi", "capi_monthly", "codex"}


def test_visual_backends_current_availability_reflects_resolved_backend(monkeypatch, tmp_path):
    service = _service(tmp_path)
    from common import llm_backend_router

    monkeypatch.setattr(llm_backend_router, "get_current_backend", lambda: "capi")
    monkeypatch.setattr(llm_backend_router, "get_effective_model", lambda: "gpt-5.5")
    monkeypatch.setattr(llm_backend_router, "get_effective_openai_api_config", lambda backend: {"api_key": "", "model": ""})
    monkeypatch.setattr(llm_backend_router, "get_codex_provider_config", lambda: {"model": "gpt-5.5", "auth_file": ""})
    monkeypatch.setattr("agent.knowledge.backend.service._visual_backend_available", lambda backend: False if backend == "capi" else True)

    response = service.get_visual_analysis_backends()
    current = next(item for item in response["backends"] if item["id"] == "current")

    assert current["available"] is False


def test_injected_visual_analyzer_skips_real_backend_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.knowledge.backend.service._visual_backend_available", lambda backend: False)
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    analyzer = QueueVisualAnalyzer([_high_result("fake_backend_result")])
    service._visual_extractor = FakeRangeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = analyzer

    result = service.build_visual_knowledge(document_id=document_id, limit=1, analysis_backend="codex")

    assert result["ok"] is True
    assert result["succeeded"] == 1
    assert analyzer.backends == ["codex"]
    assert service.search("fake_backend_result", limit=5)


def test_default_visual_analyzer_still_requires_available_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.knowledge.backend.service._visual_backend_available", lambda backend: False)
    service = _service(tmp_path)

    result = service.build_visual_knowledge(document_id="missing-doc", limit=1, analysis_backend="codex")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "selected visual analysis backend is unavailable" in result["message"]
    assert result["processed"] == 0
    assert result["succeeded"] == 0
    assert result["stats"]["pending"] == 0


def test_visual_build_progress_fields_and_failed_not_has_more(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer(["not json"])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)
    repeat = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["failed"] == 1
    assert repeat["processed"] == 0
    assert result["has_more"] is False
    assert result["has_retryable_failed"] is True
    assert result["stats"]["total"] == 1
    assert result["stats"]["pending"] == 0
    assert result["stats"]["failed"] == 1


def test_visual_build_prepares_pages_incrementally(tmp_path):
    service = _service(tmp_path, visual_analysis={"prepare_pages_per_request": 1})
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    storage.conn.execute(
        "UPDATE documents SET metadata = ? WHERE id = ?",
        (json.dumps({"page_count": 2}), document_id),
    )
    storage.conn.commit()
    extractor = FakeRangeExtractor(
        [
            _candidate(document_id, version_id, 1),
            _candidate(document_id, version_id, 2),
        ]
    )
    service._visual_extractor = extractor
    service._visual_analyzer = QueueAnalyzer([_high_result("page1_visualfact"), _high_result("page2_visualfact")])

    first = service.build_visual_knowledge(document_id=document_id, limit=1)
    second = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert extractor.calls[:2] == [(1, 1), (2, 1)]
    assert first["prepare"]["prepared_pages"] == 1
    assert first["prepare"]["prepared_artifacts"] == 1
    assert second["prepare"]["prepared_pages"] >= 2
    assert first["processed"] == 1
    assert second["processed"] == 1


def test_visual_build_continues_when_prepare_scans_pages_without_candidates(tmp_path):
    service = _service(tmp_path, visual_analysis={"prepare_pages_per_request": 3})
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    storage.conn.execute(
        "UPDATE documents SET metadata = ? WHERE id = ?",
        (json.dumps({"page_count": 6}), document_id),
    )
    storage.conn.commit()
    candidate = _candidate(document_id, version_id, 4)
    service._visual_extractor = FakeRangeSequenceExtractor([(3, []), (3, [candidate])])
    service._visual_analyzer = QueueAnalyzer([_high_result("late_candidate_visualfact")])

    first = service.build_visual_knowledge(document_id=document_id, limit=1)
    advanced_prepare = (
        first["prepared_pages_delta"] > 0
        or first["scanned_pages_delta"] > 0
        or first["prepare"]["prepared_pages"] > 0
    )
    second = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert first["has_more"] is True
    assert first["processed"] == 0
    assert first["prepared_pages_delta"] == 3
    assert first["prepared_artifacts_delta"] == 0
    assert advanced_prepare is True
    assert second["processed"] == 1
    assert second["succeeded"] == 1
    assert service.search("late_candidate_visualfact", limit=5)


def test_force_build_does_not_claim_same_artifact_twice_in_one_batch(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    service._visual_extractor = FakeExtractor([_candidate(document_id, version_id, 1)])
    service._visual_analyzer = QueueAnalyzer([_high_result("first_forcefact"), _high_result("second_forcefact")])

    assert service.build_visual_knowledge(document_id=document_id, limit=1)["succeeded"] == 1
    retry = service.build_visual_knowledge(document_id=document_id, limit=2, force=True)

    assert retry["processed"] == 1


def test_group_continued_table_pages(tmp_path):
    service = _service(tmp_path, visual_analysis={"prepare_pages_per_request": 3, "tile_large_artifacts": False})
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    storage.conn.execute(
        "UPDATE documents SET metadata = ? WHERE id = ?",
        (json.dumps({"page_count": 12}), document_id),
    )
    storage.conn.commit()
    candidates = [
        VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, 10).to_dict(),
                "caption": "Table 5-1. Signal List",
                "label": "Table 5-1",
                "page": 10,
                "bbox": {"x0": 20, "y0": 100, "x1": 560, "y1": 780, "page_width": 600, "page_height": 800},
            }
        ),
        VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, 11).to_dict(),
                "caption": "Table 5-1. Signal List (continued)",
                "label": "Table 5-1",
                "page": 11,
                "bbox": {"x0": 20, "y0": 20, "x1": 560, "y1": 780, "page_width": 600, "page_height": 800},
            }
        ),
        VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, 12).to_dict(),
                "caption": "Table 5-1. Signal List (continued)",
                "label": "Table 5-1",
                "page": 12,
                "bbox": {"x0": 20, "y0": 20, "x1": 560, "y1": 720, "page_width": 600, "page_height": 800},
            }
        ),
    ]
    service._visual_extractor = FakeRangeSequenceExtractor([(3, candidates)])
    group_result = {
        "artifact_type": "table",
        "title": "Signal List",
        "caption": "Table 5-1. Signal List",
        "is_multipage": True,
        "source_pages": [10, 11, 12],
        "summary": "Merged signal list covers SIG_A, SIG_B and SIG_C.",
        "key_facts": [{"fact": "SIG_A/SIG_B/SIG_C are in the multipage signal list", "confidence": 0.9}],
        "parts": [
            {"page": 10, "artifact_id": candidates[0].id, "role": "first", "summary": "SIG_A rows", "confidence": 0.9},
            {"page": 11, "artifact_id": candidates[1].id, "role": "middle", "summary": "SIG_B rows", "confidence": 0.9},
            {"page": 12, "artifact_id": candidates[2].id, "role": "last", "summary": "SIG_C rows", "confidence": 0.9},
        ],
        "merged_table": {
            "headers": ["Signal", "Direction", "Description"],
            "rows": [
                {"Signal": "SIG_A", "Direction": "input", "Description": "page 10"},
                {"Signal": "SIG_B", "Direction": "input", "Description": "page 11"},
                {"Signal": "SIG_C", "Direction": "input", "Description": "page 12"},
            ],
            "markdown": (
                "| Signal | Direction | Description |\n"
                "| --- | --- | --- |\n"
                "| SIG_A | input | page 10 |\n"
                "| SIG_B | input | page 11 |\n"
                "| SIG_C | input | page 12 |"
            ),
            "html": "",
            "row_page_map": [
                {"row_index": 0, "page": 10},
                {"row_index": 1, "page": 11},
                {"row_index": 2, "page": 12},
            ],
        },
        "continuation_evidence": ["Table 5-1 continued across pages"],
        "uncertain_continuations": [],
        "confidence": {"ocr": 0.9, "structure": 0.9, "semantic": 0.9, "continuation": 0.9, "overall": 0.9},
        "should_index": True,
    }
    analyzer = QueueVisualAnalyzer(
        [
            _table_result("SIG_A", page=10),
            _table_result("SIG_B", page=11),
            _table_result("SIG_C", page=12),
        ],
        group_results=[group_result],
    )
    service._visual_analyzer = analyzer

    first = service.build_visual_knowledge(document_id=document_id, limit=1)
    second = service.build_visual_knowledge(document_id=document_id, limit=1)
    third = service.build_visual_knowledge(document_id=document_id, limit=1)
    fourth = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert first["processed"] == 1
    assert third["processed"] == 1
    assert third["group_succeeded"] == 0
    assert fourth["group_succeeded"] == 1
    groups = storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id)
    assert len(groups) == 1
    assert analyzer.group_calls == [groups[0]["id"]]
    assert groups[0]["source_pages"] == [10, 11, 12]
    members = storage.get_visual_artifact_group_members(groups[0]["id"])
    assert [member["part_index"] for member in members] == [1, 2, 3]
    assert [member["role"] for member in members] == ["first", "middle", "last"]
    artifacts = storage.list_visual_artifacts(document_id=document_id, version_id=version_id)
    assert all(artifact["group_id"] == groups[0]["id"] for artifact in artifacts)
    assert service.search("SIG_A", limit=5)
    visual_chunks = [chunk for chunk in storage.list_chunks(document_id) if chunk.metadata.get("source") == "visual_analysis"]
    assert any(chunk.metadata.get("visual_scope") == "group" for chunk in visual_chunks)
    assert not any(chunk.metadata.get("visual_scope") == "page" and chunk.metadata.get("visual_artifact_id") in {item.id for item in candidates} for chunk in visual_chunks)
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        page_scope_fts = conn.execute(
            """
            SELECT COUNT(*)
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.chunk_id
            WHERE json_extract(c.metadata, '$.visual_scope') = 'page'
              AND f.text MATCH ?
            """,
            ('"SIG_A" OR "SIG_B" OR "SIG_C"',),
        ).fetchone()[0]
    assert page_scope_fts == 0


def test_no_group_for_body_references(tmp_path):
    storage = KnowledgeStorage(tmp_path / "knowledge.sqlite3")
    try:
        assert storage.list_visual_artifact_groups() == []
        assert storage.visual_group_stats()["total"] == 0
    finally:
        storage.close()


def test_explicit_caption_noncontiguous_pages_are_split(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    for page in (10, 11, 100):
        candidate = VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, page).to_dict(),
                "page": page,
                "caption": "Table 5-1. Signal List" if page == 10 else "Table 5-1. Signal List (continued)",
                "label": "Table 5-1",
                "page_text": "Signal | Direction | Description",
            }
        )
        storage.upsert_visual_artifact(candidate)

    result = VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id)

    groups = storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id)
    assert result["groups"] == 1
    assert len(groups) == 1
    assert groups[0]["source_pages"] == [10, 11]
    assert [member["page"] for member in storage.get_visual_artifact_group_members(groups[0]["id"])] == [10, 11]


def test_group_source_pages_union_across_windows(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_union"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "union group",
            "caption": "Table 10. union group",
            "source_pages": [10, 11],
            "status": "succeeded",
            "confidence": 0.9,
            "retrievable": 1,
            "result_json": {"source_pages": [10, 11], "continuation_evidence": ["first window"]},
        }
    )

    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "union group",
            "caption": "Table 10. union group",
            "source_pages": [11, 12],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {"continuation_evidence": ["second window"]},
        }
    )

    group = storage.get_visual_artifact_group(group_id)
    assert group["source_pages"] == [10, 11, 12]
    assert group["status"] == "pending"
    assert "first window" in group["result_json"]["continuation_evidence"]
    assert "second window" in group["result_json"]["continuation_evidence"]


def test_no_group_for_toc_or_list_pages(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    for page in (1, 2):
        candidate = VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, page).to_dict(),
                "page": page,
                "caption": "Table 5-1. Signal List",
                "page_text": "Table of Contents\nList of Figures\nTable 5-1. Signal List ........ 10",
            }
        )
        storage.upsert_visual_artifact(candidate)

    result = VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id)

    assert result["groups"] == 0
    assert storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id) == []


def test_inferred_top_body_continuation_without_caption(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    previous = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 20).to_dict(),
            "page": 20,
            "caption": "Table 6-1. Dense Signal List",
            "label": "Table 6-1",
            "bbox": {"x0": 20, "y0": 120, "x1": 560, "y1": 790, "page_width": 600, "page_height": 800},
            "page_text": "Table 6-1. Dense Signal List\nSignal | Direction | Description\nA | input | first",
        }
    )
    current = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 21).to_dict(),
            "page": 21,
            "caption": "",
            "label": "",
            "bbox": {"x0": 20, "y0": 10, "x1": 560, "y1": 700, "page_width": 600, "page_height": 800},
            "page_text": "Signal | Direction | Description\nB | output | continued rows\nC | input | continued rows",
        }
    )
    storage.upsert_visual_artifact(previous)
    storage.upsert_visual_artifact(current)

    result = VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id)

    groups = storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id)
    assert result["groups"] == 1
    assert groups[0]["source_pages"] == [20, 21]
    members = storage.get_visual_artifact_group_members(groups[0]["id"])
    assert [member["page"] for member in members] == [20, 21]


def test_group_hard_validation_not_bypassed_by_index_low_confidence(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True})
    document_id, version_id = _ingest(service)
    members = [
        {"artifact_id": "a1", "page": 1, "analysis_status": "succeeded", "analysis_confidence": 0.9},
        {"artifact_id": "a2", "page": 2, "analysis_status": "succeeded", "analysis_confidence": 0.9},
    ]

    result = validate_visual_group_analysis_json(
        {
            "artifact_type": "table",
            "is_multipage": True,
            "source_pages": [],
            "parts": [{"page": 1, "artifact_id": "a1"}],
            "merged_table": {"rows": [{"A": "B"}]},
            "confidence": {"ocr": 0.95, "structure": 0.95, "semantic": 0.95, "continuation": 0.5, "overall": 0.95},
            "should_index": True,
        },
        {"id": "visual_group_hard", "document_id": document_id, "version_id": version_id, "source_pages": []},
        members,
        service.config.visual_analysis,
    )

    assert result["should_index"] is False
    reason = result["low_confidence_reason"]
    assert "continuation confidence below 0.70" in reason
    assert "source_pages is empty" in reason
    assert "multipage result has fewer than 2 parts" in reason
    assert "merged_table rows require headers" in reason


def test_group_threshold_validation_not_bypassed_by_index_low_confidence(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True})
    document_id, version_id = _ingest(service)
    members = [
        {"artifact_id": "a1", "page": 1, "analysis_status": "succeeded", "analysis_confidence": 0.9},
        {"artifact_id": "a2", "page": 2, "analysis_status": "succeeded", "analysis_confidence": 0.9},
    ]

    result = validate_visual_group_analysis_json(
        {
            "artifact_type": "table",
            "is_multipage": True,
            "source_pages": [1, 2],
            "summary": "LOW_GROUP_INDEXED",
            "key_facts": [{"fact": "LOW_GROUP_INDEXED", "confidence": 0.9}],
            "parts": [{"page": 1, "artifact_id": "a1"}, {"page": 2, "artifact_id": "a2"}],
            "merged_table": {"headers": ["Signal"], "rows": [{"Signal": "LOW_GROUP_INDEXED"}]},
            "confidence": {"ocr": 0.4, "structure": 0.4, "semantic": 0.4, "continuation": 0.9, "overall": 0.4},
            "should_index": True,
        },
        {"id": "visual_group_threshold", "document_id": document_id, "version_id": version_id, "source_pages": [1, 2]},
        members,
        service.config.visual_analysis,
    )

    assert result["should_index"] is False
    reason = result["low_confidence_reason"]
    assert "overall confidence 0.40 below 0.78" in reason
    assert "ocr confidence 0.40 below 0.70" in reason
    assert "structure confidence 0.40 below 0.75" in reason
    assert "semantic confidence 0.40 below 0.75" in reason


def test_group_multipage_parts_validation_uses_actual_pages(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    members = [
        {"artifact_id": "a1", "page": 1, "analysis_status": "succeeded", "analysis_confidence": 0.9},
        {"artifact_id": "a2", "page": 2, "analysis_status": "succeeded", "analysis_confidence": 0.9},
    ]

    result = validate_visual_group_analysis_json(
        {
            "artifact_type": "table",
            "is_multipage": False,
            "source_pages": [1, 2],
            "summary": "multipage fact",
            "key_facts": [{"fact": "multipage fact", "confidence": 0.9}],
            "parts": [{"page": 1, "artifact_id": "a1"}],
            "merged_table": {"headers": ["Signal"], "rows": [{"Signal": "A"}]},
            "confidence": {"ocr": 0.95, "structure": 0.95, "semantic": 0.95, "continuation": 0.9, "overall": 0.95},
            "should_index": True,
        },
        {"id": "visual_group_multipage", "document_id": document_id, "version_id": version_id, "source_pages": [1, 2]},
        members,
        service.config.visual_analysis,
    )

    assert result["should_index"] is False
    assert result["is_multipage"] is True
    assert "multipage result has fewer than 2 parts" in result["low_confidence_reason"]


def test_group_explicit_empty_source_pages_is_hard_failure(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    members = [
        {"artifact_id": "a1", "page": 1, "analysis_status": "succeeded", "analysis_confidence": 0.9},
        {"artifact_id": "a2", "page": 2, "analysis_status": "succeeded", "analysis_confidence": 0.9},
    ]

    result = validate_visual_group_analysis_json(
        {
            "artifact_type": "table",
            "is_multipage": True,
            "source_pages": [],
            "summary": "empty source pages fact",
            "key_facts": [{"fact": "empty source pages fact", "confidence": 0.9}],
            "parts": [{"page": 1, "artifact_id": "a1"}, {"page": 2, "artifact_id": "a2"}],
            "merged_table": {"headers": ["Signal"], "rows": [{"Signal": "A"}]},
            "confidence": {"ocr": 0.95, "structure": 0.95, "semantic": 0.95, "continuation": 0.9, "overall": 0.95},
            "should_index": True,
        },
        {"id": "visual_group_empty_pages", "document_id": document_id, "version_id": version_id, "source_pages": [1, 2]},
        members,
        service.config.visual_analysis,
    )

    assert result["should_index"] is False
    assert result["source_pages"] == [1, 2]
    assert "source_pages is empty" in result["low_confidence_reason"]


def test_group_missing_source_pages_fills_from_group_metadata_with_reason(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    members = [
        {"artifact_id": "a1", "page": 1, "analysis_status": "succeeded", "analysis_confidence": 0.9},
        {"artifact_id": "a2", "page": 2, "analysis_status": "succeeded", "analysis_confidence": 0.9},
    ]

    result = validate_visual_group_analysis_json(
        {
            "artifact_type": "table",
            "is_multipage": True,
            "summary": "missing source pages fact",
            "key_facts": [{"fact": "missing source pages fact", "confidence": 0.9}],
            "parts": [{"page": 1, "artifact_id": "a1"}, {"page": 2, "artifact_id": "a2"}],
            "merged_table": {"headers": ["Signal"], "rows": [{"Signal": "A"}]},
            "confidence": {"ocr": 0.95, "structure": 0.95, "semantic": 0.95, "continuation": 0.9, "overall": 0.95},
            "should_index": True,
        },
        {"id": "visual_group_missing_pages", "document_id": document_id, "version_id": version_id, "source_pages": [1, 2]},
        members,
        service.config.visual_analysis,
    )

    assert result["should_index"] is True
    assert result["source_pages"] == [1, 2]
    assert "source_pages missing in model output; filled from group metadata" in result["low_confidence_reason"]


def test_group_low_confidence_true_config_does_not_index_chunks(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True, "tile_large_artifacts": False})
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_hard_storage"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "hard storage",
            "caption": "Table hard storage",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, "first" if index == 1 else "last", 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"HARD_MEMBER_{index}", page=index).to_dict(), 0.9, retrievable=False)
    service._visual_analyzer = QueueVisualAnalyzer(
        [],
        group_results=[
            {
                "artifact_type": "table",
                "title": "hard storage",
                "caption": "Table hard storage",
                "is_multipage": True,
                "source_pages": [],
                "summary": "hard_group_should_not_index",
                "key_facts": [{"fact": "hard_group_should_not_index", "confidence": 0.9}],
                "parts": [{"page": 1, "artifact_id": "visual_test_1"}],
                "merged_table": {"rows": [{"Signal": "A"}]},
                "confidence": {"ocr": 0.95, "structure": 0.95, "semantic": 0.95, "continuation": 0.5, "overall": 0.95},
                "should_index": True,
            }
        ],
    )

    result = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert result["outcome"] == "low_confidence"
    assert storage.get_visual_artifact_group(group_id)["status"] == "low_confidence"
    assert service.search("hard_group_should_not_index", limit=5) == []


def test_group_threshold_low_confidence_true_config_does_not_index_chunks(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True, "tile_large_artifacts": False})
    document_id, version_id = _ingest(service)
    group_id = "visual_group_threshold_storage"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueVisualAnalyzer(
        [],
        group_results=[
            {
                "artifact_type": "table",
                "title": "threshold storage",
                "caption": "Table threshold storage",
                "is_multipage": True,
                "source_pages": [1, 2],
                "summary": "LOW_GROUP_INDEXED",
                "key_facts": [{"fact": "LOW_GROUP_INDEXED", "confidence": 0.9}],
                "parts": [{"page": 1, "artifact_id": "visual_test_1"}, {"page": 2, "artifact_id": "visual_test_2"}],
                "merged_table": {
                    "headers": ["Signal"],
                    "rows": [{"Signal": "LOW_GROUP_INDEXED"}],
                    "markdown": "| Signal |\n| --- |\n| LOW_GROUP_INDEXED |",
                },
                "confidence": {"ocr": 0.4, "structure": 0.4, "semantic": 0.4, "continuation": 0.9, "overall": 0.4},
                "should_index": True,
            }
        ],
    )

    result = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert result["outcome"] == "low_confidence"
    group = storage.get_visual_artifact_group(group_id)
    assert group["status"] == "low_confidence"
    assert group["retrievable"] is False
    assert service.search("LOW_GROUP_INDEXED", limit=5) == []


def test_inferred_group_growth_reuses_existing_group_id(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    pages = {
        1: {"caption": "Table 1. Long Signal List", "y0": 120, "y1": 790},
        2: {"caption": "", "y0": 10, "y1": 790},
        3: {"caption": "", "y0": 10, "y1": 700},
    }
    for page in (1, 2):
        spec = pages[page]
        storage.upsert_visual_artifact(
            VisualArtifactCandidate(
                **{
                    **_candidate(document_id, version_id, page).to_dict(),
                    "page": page,
                    "caption": spec["caption"],
                    "label": spec["caption"],
                    "bbox": {"x0": 20, "y0": spec["y0"], "x1": 560, "y1": spec["y1"], "page_width": 600, "page_height": 800},
                    "page_text": "Signal | Direction | Description\nA | input | continued",
                }
            )
        )
    VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id, page_window=(1, 2))
    first_group = storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id)[0]

    page = 3
    spec = pages[page]
    storage.upsert_visual_artifact(
        VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, page).to_dict(),
                "page": page,
                "caption": spec["caption"],
                "label": spec["caption"],
                "bbox": {"x0": 20, "y0": spec["y0"], "x1": 560, "y1": spec["y1"], "page_width": 600, "page_height": 800},
                "page_text": "Signal | Direction | Description\nB | output | continued",
            }
        )
    )
    VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id, page_window=(2, 3))

    groups = [group for group in storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id) if group["status"] != "skipped"]
    assert [group["id"] for group in groups] == [first_group["id"]]
    assert groups[0]["source_pages"] == [1, 2, 3]
    assert [member["page"] for member in storage.get_visual_artifact_group_members(first_group["id"])] == [1, 2, 3]


def test_moving_artifact_between_groups_removes_old_member_row(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    candidate = _candidate(document_id, version_id, 1)
    storage.upsert_visual_artifact(candidate)
    for group_id in ("visual_group_old", "visual_group_new"):
        storage.upsert_visual_artifact_group(
            {
                "id": group_id,
                "document_id": document_id,
                "version_id": version_id,
                "kb_id": "kb_default",
                "group_type": "table",
                "title": group_id,
                "caption": group_id,
                "source_pages": [1, 2],
                "status": "pending",
                "confidence": 0.9,
                "result_json": {},
            }
        )

    storage.add_visual_artifact_group_member("visual_group_old", candidate.id, 1, 1, "first", 0.9)
    storage.mark_visual_artifact_group_membership(candidate.id, "visual_group_old", 1, "first", 0.9)
    storage.add_visual_artifact_group_member("visual_group_new", candidate.id, 1, 1, "first", 0.9)
    storage.mark_visual_artifact_group_membership(candidate.id, "visual_group_new", 1, "first", 0.9)

    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        old_count = conn.execute(
            "SELECT COUNT(*) FROM visual_artifact_group_members WHERE group_id = ? AND artifact_id = ?",
            ("visual_group_old", candidate.id),
        ).fetchone()[0]
    assert old_count == 0


def test_stale_group_membership_cannot_index_old_group(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    old_group = "visual_group_stale_old"
    new_group = "visual_group_stale_new"
    for group_id in (old_group, new_group):
        storage.upsert_visual_artifact_group(
            {
                "id": group_id,
                "document_id": document_id,
                "version_id": version_id,
                "kb_id": "kb_default",
                "group_type": "table",
                "title": group_id,
                "caption": group_id,
                "source_pages": [1, 2],
                "status": "pending",
                "confidence": 0.9,
                "result_json": {},
            }
        )
    candidates = [_candidate(document_id, version_id, index) for index in (1, 2)]
    for index, candidate in enumerate(candidates, start=1):
        storage.upsert_visual_artifact(candidate)
        role = "first" if index == 1 else "last"
        storage.add_visual_artifact_group_member(old_group, candidate.id, index, index, role, 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, old_group, index, role, 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"STALE_{index}", page=index).to_dict(), 0.9, retrievable=False)
    moved = candidates[0]
    storage.add_visual_artifact_group_member(new_group, moved.id, 1, 1, "first", 0.9)
    storage.mark_visual_artifact_group_membership(moved.id, new_group, 1, "first", 0.9)
    service._visual_analyzer = QueueAnalyzer([])

    result = service.analyze_visual_artifact_group(old_group, analysis_backend="codex")

    assert result["outcome"] in {"low_confidence", "skipped"}
    assert service.search("STALE_1", limit=5) == []


def test_resolve_visual_group_id_ignores_legacy_stale_membership_rows(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    old_group = "visual_group_resolver_old"
    new_group = "visual_group_resolver_new"
    for group_id in (old_group, new_group):
        storage.upsert_visual_artifact_group(
            {
                "id": group_id,
                "document_id": document_id,
                "version_id": version_id,
                "kb_id": "kb_default",
                "group_type": "table",
                "title": group_id,
                "caption": group_id,
                "source_pages": [1, 2],
                "status": "pending",
                "confidence": 0.9,
                "result_json": {},
            }
        )
    candidate = _candidate(document_id, version_id, 1)
    storage.upsert_visual_artifact(candidate)
    storage.add_visual_artifact_group_member(new_group, candidate.id, 1, 2, "first", 0.9)
    storage.mark_visual_artifact_group_membership(candidate.id, new_group, 1, "first", 0.9)
    storage.conn.execute(
        """
        INSERT OR REPLACE INTO visual_artifact_group_members(group_id, artifact_id, part_index, page, role, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (old_group, candidate.id, 1, 1, "first", 0.9),
    )
    storage.conn.commit()

    winner = storage.resolve_visual_group_id_for_members(
        document_id=document_id,
        version_id=version_id,
        member_artifact_ids=[candidate.id],
        preferred_group_id="visual_group_resolver_preferred",
    )

    assert winner == new_group
    assert storage.cleanup_stale_visual_artifact_group_members() == 1


def test_cleanup_stale_visual_artifact_group_members_removes_multiple_rows(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_ids = ["visual_group_cleanup_current", "visual_group_cleanup_old_a", "visual_group_cleanup_old_b", "visual_group_cleanup_other"]
    for group_id in group_ids:
        storage.upsert_visual_artifact_group(
            {
                "id": group_id,
                "document_id": document_id,
                "version_id": version_id,
                "kb_id": "kb_default",
                "group_type": "table",
                "title": group_id,
                "caption": group_id,
                "source_pages": [1, 2],
                "status": "pending",
                "confidence": 0.9,
                "result_json": {},
            }
        )
    current = _candidate(document_id, version_id, 1)
    stale_a = _candidate(document_id, version_id, 2)
    stale_b = _candidate(document_id, version_id, 3)
    for candidate, group_id in (
        (current, "visual_group_cleanup_current"),
        (stale_a, "visual_group_cleanup_other"),
        (stale_b, "visual_group_cleanup_other"),
    ):
        storage.upsert_visual_artifact(candidate)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, candidate.page, "first", 0.9)
    storage.add_visual_artifact_group_member("visual_group_cleanup_current", current.id, 1, 1, "first", 0.9)
    storage.add_visual_artifact_group_member("visual_group_cleanup_old_a", stale_a.id, 1, 2, "first", 0.9)
    storage.add_visual_artifact_group_member("visual_group_cleanup_old_b", stale_b.id, 1, 3, "first", 0.9)

    assert storage.cleanup_stale_visual_artifact_group_members() == 2

    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM visual_artifact_group_members").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM visual_artifact_group_members WHERE group_id = ? AND artifact_id = ?",
            ("visual_group_cleanup_current", current.id),
        ).fetchone()[0] == 1


def test_cleanup_stale_visual_artifact_group_members_repairs_group_source_pages(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    group_id = "visual_group_cleanup_repair_pages"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    _insert_legacy_stale_group_member(storage, document_id, version_id, group_id, stale_page=99)
    _pollute_visual_group_source_pages(storage, group_id, [1, 2, 99])

    deleted = storage.cleanup_stale_visual_artifact_group_members()

    assert deleted == 1
    group = storage.get_visual_artifact_group(group_id)
    assert group["source_pages"] == [1, 2]
    assert group["status"] == "pending"
    assert group["retrievable"] is False
    assert [member["page"] for member in storage.get_visual_artifact_group_members(group_id)] == [1, 2]

    service._visual_analyzer = QueueAnalyzer([])
    result = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert result["outcome"] == "succeeded"
    assert storage.get_visual_artifact_group(group_id)["status"] == "succeeded"


def test_mark_visual_group_member_change_ignores_stale_group_member_pages(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_member_change_current_pages"
    _upsert_visual_group(storage, document_id, version_id, group_id, [])
    _insert_legacy_stale_group_member(storage, document_id, version_id, group_id, stale_page=99)

    page1 = _candidate(document_id, version_id, 1)
    storage.upsert_visual_artifact(page1)
    storage.add_visual_artifact_group_member(group_id, page1.id, 1, 1, "first", 0.9)
    storage.mark_visual_artifact_group_membership(page1.id, group_id, 1, "first", 0.9)

    group = storage.get_visual_artifact_group(group_id)
    assert 99 not in group["source_pages"]
    assert group["source_pages"] == [1]

    page2 = _candidate(document_id, version_id, 2)
    storage.upsert_visual_artifact(page2)
    storage.add_visual_artifact_group_member(group_id, page2.id, 2, 2, "last", 0.9)
    storage.mark_visual_artifact_group_membership(page2.id, group_id, 2, "last", 0.9)

    assert storage.get_visual_artifact_group(group_id)["source_pages"] == [1, 2]


def test_update_groups_for_document_repairs_legacy_polluted_source_pages(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_update_legacy_repair"
    _upsert_visual_group(storage, document_id, version_id, group_id, [1, 2])
    for index, page in enumerate((1, 2), start=1):
        candidate = VisualArtifactCandidate(
            **{
                **_candidate(document_id, version_id, page).to_dict(),
                "page": page,
                "caption": "Table 5-1. Legacy Repair" if page == 1 else "Table 5-1. Legacy Repair (continued)",
                "label": "Table 5-1",
                "page_text": "Signal | Direction | Description\nLEGACY | input | continued",
            }
        )
        storage.upsert_visual_artifact(candidate)
        role = "first" if index == 1 else "last"
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, page, role, 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, role, 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"LEGACY_{page}", page=page).to_dict(), 0.9, retrievable=False)
    _insert_legacy_stale_group_member(storage, document_id, version_id, group_id, stale_page=99)
    _pollute_visual_group_source_pages(storage, group_id, [1, 2, 99])

    result = VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id)

    assert result["groups"] == 1
    group = storage.get_visual_artifact_group(group_id)
    assert group["source_pages"] == [1, 2]
    assert [member["page"] for member in storage.get_visual_artifact_group_members(group_id)] == [1, 2]

    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"


def test_cleanup_stale_visual_artifact_group_members_deletes_old_group_chunk_tables(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    group_id = "visual_group_cleanup_repair_chunks"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    old_chunks, old_span_ids = _group_chunk_refs(storage, document_id, group_id)
    old_chunk_ids = [chunk.id for chunk in old_chunks]
    assert old_chunk_ids
    assert old_span_ids
    _insert_legacy_stale_group_member(storage, document_id, version_id, group_id, stale_page=99)
    _pollute_visual_group_source_pages(storage, group_id, [1, 2, 99])

    assert storage.cleanup_stale_visual_artifact_group_members() == 1

    group = storage.get_visual_artifact_group(group_id)
    assert group["source_pages"] == [1, 2]
    assert group["status"] == "pending"
    assert group["retrievable"] is False
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert _count_rows(conn, "chunks", "id", old_chunk_ids) == 0
        assert _count_rows(conn, "chunks_fts", "chunk_id", old_chunk_ids) == 0
        assert _count_rows(conn, "source_spans", "id", old_span_ids) == 0
        assert _count_rows(conn, "visual_artifact_chunks", "chunk_id", old_chunk_ids) == 0


def test_group_success_sets_retrievable_only_for_current_real_members(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_success_current_members"
    other_group_id = "visual_group_success_other_members"
    for gid in (group_id, other_group_id):
        storage.upsert_visual_artifact_group(
            {
                "id": gid,
                "document_id": document_id,
                "version_id": version_id,
                "kb_id": "kb_default",
                "group_type": "table",
                "title": gid,
                "caption": gid,
                "source_pages": [1, 2],
                "status": "pending",
                "confidence": 0.9,
                "result_json": {},
            }
        )
    current = _candidate(document_id, version_id, 1)
    stale = _candidate(document_id, version_id, 2)
    storage.upsert_visual_artifact(current)
    storage.upsert_visual_artifact(stale)
    storage.mark_visual_artifact_group_membership(current.id, group_id, 1, "first", 0.9)
    storage.mark_visual_artifact_group_membership(stale.id, other_group_id, 2, "last", 0.9)
    storage.add_visual_artifact_group_member(group_id, current.id, 1, 1, "first", 0.9)
    storage.add_visual_artifact_group_member(group_id, stale.id, 2, 2, "last", 0.9)

    storage.complete_visual_artifact_group_success(group_id, {"summary": "current only"}, 0.9)

    assert storage.get_visual_artifact(current.id)["group_retrievable"] is True
    assert storage.get_visual_artifact(stale.id)["group_retrievable"] is False


def test_group_low_confidence_not_indexed(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    storage.upsert_visual_artifact_group(
        {
            "id": "visual_group_other",
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "other group",
            "caption": "Table 0. other group",
            "source_pages": [3, 4],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    storage.upsert_visual_artifact_group(
        {
            "id": "visual_group_low",
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "low group",
            "caption": "Table 1. low group",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.5,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        candidate = VisualArtifactCandidate(**{**candidate.to_dict(), "pipeline_version": "visual-pipeline-v1"})
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member("visual_group_low", candidate.id, index, index, "first" if index == 1 else "last", 0.5)
        storage.mark_visual_artifact_group_membership(candidate.id, "visual_group_low", index, "first" if index == 1 else "last", 0.5)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"LOW_{index}", page=index).to_dict(), 0.9, retrievable=False)
        _append_page_visual_chunk(service, storage, candidate, _table_result(f"LOW_PAGE_ONLY_{index}", page=index))

    service._visual_analyzer = QueueAnalyzer([])
    result = service.analyze_visual_artifact_group("visual_group_low", analysis_backend="codex")

    assert result["outcome"] == "low_confidence"
    group = storage.get_visual_artifact_group("visual_group_low")
    assert group["status"] == "low_confidence"
    assert storage.get_visual_artifact_group("visual_group_other")["status"] == "pending"
    assert service.search("LOW_1", limit=5) == []
    assert service.search("LOW_PAGE_ONLY_1", limit=5) == []


def test_rebuild_continues_group_after_interruption(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    storage.upsert_visual_artifact_group(
        {
            "id": "visual_group_resume",
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "resume group",
            "caption": "Table 2. resume group",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        candidate = VisualArtifactCandidate(**{**candidate.to_dict(), "pipeline_version": "visual-pipeline-v1"})
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member("visual_group_resume", candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, "visual_group_resume", index, "first" if index == 1 else "last", 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"RESUME_{index}", page=index).to_dict(), 0.9, retrievable=False)
    service._visual_analyzer = QueueAnalyzer([])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["processed"] == 0
    assert result["group_succeeded"] == 1
    assert service._visual_analyzer.calls == []
    assert service.search("RESUME_1", limit=5)


def test_group_success_deletes_existing_member_page_chunks(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    candidates = [_candidate(document_id, version_id, index) for index in (1, 2)]
    for index, candidate in enumerate(candidates, start=1):
        storage.upsert_visual_artifact(candidate)
        storage.complete_visual_artifact_success(
            candidate.id,
            _table_result(f"MEMBER_{index}", page=index).to_dict(),
            0.9,
            retrievable=True,
        )
        _append_page_visual_chunk(service, storage, candidate, _table_result(f"PAGE_ONLY_{index}", page=index))
    storage.upsert_visual_artifact_group(
        {
            "id": "visual_group_cleanup",
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "cleanup group",
            "caption": "Table 7. cleanup group",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index, candidate in enumerate(candidates, start=1):
        role = "first" if index == 1 else "last"
        storage.add_visual_artifact_group_member("visual_group_cleanup", candidate.id, index, index, role, 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, "visual_group_cleanup", index, role, 0.9)
    service._visual_analyzer = QueueAnalyzer([])

    result = service.analyze_visual_artifact_group("visual_group_cleanup", analysis_backend="codex")

    assert result["outcome"] == "succeeded"
    assert service.search("PAGE_ONLY_1", limit=5) == []
    assert service.search("PAGE_ONLY_2", limit=5) == []
    assert service.search("cleanup group", limit=5)
    chunks = storage.list_chunks(document_id)
    assert any(chunk.metadata.get("visual_scope") == "group" for chunk in chunks)
    assert not any(chunk.metadata.get("visual_scope") == "page" for chunk in chunks)


def test_group_growth_resets_succeeded_group(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_growth"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "growth group",
            "caption": "Table 8. growth group",
            "source_pages": [10, 11],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for page, text in ((10, "GROW_A"), (11, "GROW_B")):
        candidate = VisualArtifactCandidate(**{**_candidate(document_id, version_id, page).to_dict(), "page": page})
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, page - 9, page, "first" if page == 10 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, page - 9, "first" if page == 10 else "last", 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(text, page=page).to_dict(), 0.9, retrievable=False)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    assert service.search("GROW_A", limit=5)

    page12 = VisualArtifactCandidate(**{**_candidate(document_id, version_id, 12).to_dict(), "page": 12})
    storage.upsert_visual_artifact(page12)
    storage.complete_visual_artifact_success(page12.id, _table_result("GROW_C", page=12).to_dict(), 0.9, retrievable=False)
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "growth group",
            "caption": "Table 8. growth group",
            "source_pages": [12],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {"continuation_evidence": ["page 12 added"]},
        }
    )
    storage.add_visual_artifact_group_member(group_id, page12.id, 3, 12, "last", 0.9)
    storage.mark_visual_artifact_group_membership(page12.id, group_id, 3, "last", 0.9)

    dirty_group = storage.get_visual_artifact_group(group_id)
    assert dirty_group["status"] == "pending"
    assert dirty_group["source_pages"] == [10, 11, 12]
    assert service.search("GROW_A", limit=5) == []

    rebuilt = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert rebuilt["outcome"] == "succeeded"
    group = storage.get_visual_artifact_group(group_id)
    assert group["source_pages"] == [10, 11, 12]
    assert service.search("GROW_C", limit=5)


def test_group_dirty_reset_clears_member_group_retrievable(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_dirty_flags"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "dirty flags",
            "caption": "Table dirty flags",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, "first" if index == 1 else "last", 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"DIRTY_{index}", page=index).to_dict(), 0.9, retrievable=False)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    assert all(artifact["group_retrievable"] for artifact in storage.list_visual_artifacts(document_id=document_id, version_id=version_id))

    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "dirty flags",
            "caption": "Table dirty flags",
            "source_pages": [1, 2, 3],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )

    assert not any(artifact["group_retrievable"] for artifact in storage.list_visual_artifacts(document_id=document_id, version_id=version_id))
    assert service.search("DIRTY_1", limit=5) == []


def test_group_low_confidence_clears_member_group_retrievable(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_low_flags"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "low flags",
            "caption": "Table low flags",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, "first" if index == 1 else "last", 0.9)
        storage.complete_visual_artifact_success(candidate.id, _table_result(f"LOWFLAGS_{index}", page=index).to_dict(), 0.9, retrievable=False)
    storage.complete_visual_artifact_group_success(group_id, {"summary": "old"}, 0.9)

    storage.complete_visual_artifact_group_low_confidence(group_id, {"should_index": False}, 0.4, "low")

    assert not any(artifact["group_retrievable"] for artifact in storage.list_visual_artifacts(document_id=document_id, version_id=version_id))


def test_group_failed_clears_member_group_retrievable(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_failed_flags"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "failed flags",
            "caption": "Table failed flags",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, "first" if index == 1 else "last", 0.9)
    storage.complete_visual_artifact_group_success(group_id, {"summary": "old"}, 0.9)

    storage.complete_visual_artifact_group_failed(group_id, "failed")

    assert not any(artifact["group_retrievable"] for artifact in storage.list_visual_artifacts(document_id=document_id, version_id=version_id))


def test_group_member_retry_low_confidence_invalidates_existing_group_chunks(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True})
    document_id, version_id = _ingest(service)
    group_id = "visual_group_retry_low"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    assert service.search("READY_1", limit=5)

    service._visual_analyzer = QueueAnalyzer([_raw_low_table_result("LOW_NEW", page=1, should_index=True)])
    retry = service.retry_visual_artifact("visual_test_1")

    assert retry["outcome"] == "low_confidence"
    page1 = storage.get_visual_artifact("visual_test_1")
    assert page1["analysis_status"] == "low_confidence"
    group = storage.get_visual_artifact_group(group_id)
    assert group["status"] in {"pending", "low_confidence"}
    assert group["status"] != "succeeded"
    assert group["retrievable"] is False
    members = storage.list_visual_artifacts(document_id=document_id, version_id=version_id)
    assert not any(artifact["group_retrievable"] for artifact in members)
    assert service.search("READY_1", limit=5) == []
    assert service.search("LOW_NEW", limit=5) == []

    service._visual_analyzer = QueueAnalyzer([])
    group_retry = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert group_retry["outcome"] == "low_confidence"
    assert storage.get_visual_artifact_group(group_id)["retrievable"] is False
    assert service.search("READY_1", limit=5) == []
    assert service.search("LOW_NEW", limit=5) == []


def test_group_member_retry_low_confidence_removes_old_group_chunk_tables(tmp_path):
    service = _service(tmp_path, visual_analysis={"index_low_confidence": True})
    document_id, version_id = _ingest(service)
    group_id = "visual_group_retry_low_tables"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    old_chunks, old_span_ids = _group_chunk_refs(storage, document_id, group_id)
    old_chunk_ids = [chunk.id for chunk in old_chunks]
    assert old_chunk_ids
    assert old_span_ids

    service._visual_analyzer = QueueAnalyzer([_raw_low_table_result("LOW_TABLES_NEW", page=1, should_index=True)])
    retry = service.retry_visual_artifact("visual_test_1")

    assert retry["outcome"] == "low_confidence"
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert _count_rows(conn, "chunks", "id", old_chunk_ids) == 0
        assert _count_rows(conn, "chunks_fts", "chunk_id", old_chunk_ids) == 0
        assert _count_rows(conn, "source_spans", "id", old_span_ids) == 0
        assert _count_rows(conn, "visual_artifact_chunks", "chunk_id", old_chunk_ids) == 0


def test_group_member_retry_success_invalidates_old_group_fact_until_remerge(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    group_id = "visual_group_retry_success"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    assert service.search("READY_1", limit=5)

    service._visual_analyzer = QueueAnalyzer([_table_result("FRESH_1", page=1)])
    retry = service.retry_visual_artifact("visual_test_1")

    assert retry["outcome"] == "succeeded"
    group = storage.get_visual_artifact_group(group_id)
    assert group["status"] == "pending"
    assert group["retrievable"] is False
    assert service.search("READY_1", limit=5) == []
    assert service.search("FRESH_1", limit=5) == []

    service._visual_analyzer = QueueAnalyzer([])
    rebuilt = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert rebuilt["outcome"] == "succeeded"
    assert service.search("FRESH_1", limit=5)
    assert service.search("READY_1", limit=5) == []


def test_group_member_retry_success_deletes_old_group_chunk_mapping(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    group_id = "visual_group_retry_success_mapping"
    storage = _seed_ready_group(service, document_id, version_id, group_id)
    service._visual_analyzer = QueueAnalyzer([])
    assert service.analyze_visual_artifact_group(group_id, analysis_backend="codex")["outcome"] == "succeeded"
    old_chunks, _old_span_ids = _group_chunk_refs(storage, document_id, group_id)
    old_chunk_ids = [chunk.id for chunk in old_chunks]
    assert old_chunk_ids

    service._visual_analyzer = QueueAnalyzer([_table_result("FRESH_MAPPING_1", page=1)])
    retry = service.retry_visual_artifact("visual_test_1")

    assert retry["outcome"] == "succeeded"
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert _count_rows(conn, "visual_artifact_chunks", "chunk_id", old_chunk_ids) == 0


def test_force_reanalysis_context_excludes_existing_visual_chunks(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    candidate = _candidate(document_id, version_id, 1)
    service._visual_extractor = FakeExtractor([candidate])
    service._visual_analyzer = QueueAnalyzer([_high_result("old_context_visualfact")])
    assert service.build_visual_knowledge(document_id=document_id, limit=1)["succeeded"] == 1
    assert service.search("old_context_visualfact", limit=5)
    analyzer = RecordingAnalyzer([_high_result("new_context_visualfact")])
    service._visual_analyzer = analyzer
    service._visual_extractor = FakeExtractor([candidate])

    retry = service.build_visual_knowledge(document_id=document_id, limit=1, force=True)

    assert retry["succeeded"] == 1
    seen = analyzer.candidates[0]
    combined_context = "\n".join([seen.page_text, seen.context_before, seen.context_after])
    assert "old_context_visualfact" not in combined_context


def test_grouping_page_window_queries_high_pages_after_many_artifacts(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    for page in range(1, 2005):
        storage.upsert_visual_artifact(VisualArtifactCandidate(**{**_candidate(document_id, version_id, page).to_dict(), "page": page, "caption": "", "label": ""}))
    for page in (3000, 3001):
        storage.upsert_visual_artifact(
            VisualArtifactCandidate(
                **{
                    **_candidate(document_id, version_id, page).to_dict(),
                    "page": page,
                    "caption": "Table 99. High Page Table",
                    "label": "Table 99",
                    "page_text": "Signal | Direction | Description",
                }
            )
        )

    result = VisualArtifactGrouper(storage).update_groups_for_document(document_id, version_id, page_window=(3000, 3001))

    assert result["groups"] == 1
    groups = storage.list_visual_artifact_groups(document_id=document_id, version_id=version_id)
    assert groups[0]["source_pages"] == [3000, 3001]


def test_group_model_merge_uses_images_when_supported(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    service = _service(tmp_path, visual_analysis={"group_model_merge_enabled": True})
    document_id, version_id = _ingest(service)
    image_paths = {}
    for page in (1, 2):
        path = tmp_path / f"group-{page}.png"
        Image.new("RGB", (8, 8), color=(255, 255, 255)).save(path)
        image_paths[page] = path
    storage = _seed_ready_group(service, document_id, version_id, "visual_group_images", image_paths=image_paths)
    analyzer = ImageRecordingGroupAnalyzer()
    service._visual_analyzer = analyzer

    result = service.analyze_visual_artifact_group("visual_group_images", analysis_backend="capi")

    assert result["outcome"] == "succeeded"
    assert analyzer.image_url_count == 2
    assert service.search("image_group_merge_fact", limit=5)


def test_group_model_merge_falls_back_to_text_when_images_unavailable(tmp_path):
    service = _service(tmp_path, visual_analysis={"group_model_merge_enabled": True})
    document_id, version_id = _ingest(service)
    _seed_ready_group(service, document_id, version_id, "visual_group_missing_images")
    service._visual_analyzer = VisualAnalyzer()
    service._visual_analyzer.skip_backend_availability_check = True

    result = service.analyze_visual_artifact_group("visual_group_missing_images", analysis_backend="capi")

    assert result["outcome"] == "succeeded"
    assert service.search("READY_1", limit=5)


def test_group_more_than_max_pages_uses_text_merge_only(tmp_path):
    service = _service(tmp_path, visual_analysis={"group_model_merge_enabled": True, "group_model_merge_max_pages": 2})
    document_id, version_id = _ingest(service)
    _seed_ready_group(service, document_id, version_id, "visual_group_many_pages", pages=(1, 2, 3))
    analyzer = ImageRecordingGroupAnalyzer()
    service._visual_analyzer = analyzer

    result = service.analyze_visual_artifact_group("visual_group_many_pages", analysis_backend="capi")

    assert result["outcome"] == "succeeded"
    assert analyzer.image_url_count == 0
    assert service.search("READY_3", limit=5)


def test_group_not_merged_before_prepare_done_or_lookahead_passed(tmp_path):
    service = _service(tmp_path, visual_analysis={"group_merge_lookahead_pages": 2})
    document_id, version_id = _ingest(service)
    storage = _seed_ready_group(service, document_id, version_id, "visual_group_unstable", pages=(5, 6))
    storage.upsert_visual_prepare_state(
        document_id=document_id,
        version_id=version_id,
        kb_id="kb_default",
        source_path="source.pdf",
        total_pages=10,
        next_page=7,
        prepared_pages=6,
        prepared_artifacts=2,
        status="pending",
        pipeline_version="visual-pipeline-v1",
    )
    service._visual_analyzer = QueueAnalyzer([])

    outcome = service._process_ready_visual_group(
        storage,
        document_id=document_id,
        kb_id=None,
        version_id=version_id,
        force=False,
        retry_failed=False,
        model="gpt-5.5",
        prompt_version="visual-group-v1",
        analysis_backend="codex",
    )

    assert outcome == ""
    assert storage.get_visual_artifact_group("visual_group_unstable")["status"] == "pending"
    assert service.search("READY_5", limit=5) == []


def test_group_merges_after_prepare_done(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = _seed_ready_group(service, document_id, version_id, "visual_group_done", pages=(5, 6))
    storage.upsert_visual_prepare_state(
        document_id=document_id,
        version_id=version_id,
        kb_id="kb_default",
        source_path="source.pdf",
        total_pages=6,
        next_page=7,
        prepared_pages=6,
        prepared_artifacts=2,
        status="done",
        pipeline_version="visual-pipeline-v1",
    )
    service._visual_analyzer = QueueAnalyzer([])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["group_succeeded"] == 1
    assert service.search("READY_5", limit=5)


def test_manual_analyze_group_can_merge_even_prepare_not_done(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = _seed_ready_group(service, document_id, version_id, "visual_group_manual", pages=(5, 6))
    storage.upsert_visual_prepare_state(
        document_id=document_id,
        version_id=version_id,
        kb_id="kb_default",
        source_path="source.pdf",
        total_pages=10,
        next_page=6,
        prepared_pages=5,
        prepared_artifacts=2,
        status="pending",
        pipeline_version="visual-pipeline-v1",
    )
    service._visual_analyzer = QueueAnalyzer([])

    result = service.analyze_visual_artifact_group("visual_group_manual", analysis_backend="codex")

    assert result["outcome"] == "succeeded"
    assert service.search("READY_5", limit=5)


def test_visual_reset_clears_visual_artifact_chunk_mapping_even_without_spans(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    candidate = _candidate(document_id, version_id, 1)
    storage.upsert_visual_artifact(candidate)
    chunk = KnowledgeChunk(
        id="visual_chunk_no_span",
        document_id=document_id,
        ordinal=99,
        page_start=1,
        page_end=1,
        text="dangling_visual_mapping_text",
        kb_id="kb_default",
        version_id=version_id,
        source_span_ids=[],
        metadata={"source": "visual_analysis", "visual_scope": "page"},
    )
    storage.append_visual_chunks(document_id, version_id, candidate.id, [chunk], [])
    assert service.search("dangling_visual_mapping_text", limit=5)

    reset = storage.reset_visual_cache(document_id=document_id, version_id=version_id)

    assert reset["chunks"] == 1
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?", (chunk.id,)).fetchone()[0] == 0
    assert service.search("dangling_visual_mapping_text", limit=5) == []


def test_high_res_retry_for_unreadable_artifact(tmp_path):
    service = _service(tmp_path, visual_analysis={"dense_text_retry_threshold": 0.72})
    document_id, version_id = _ingest(service)
    candidate = _candidate(document_id, version_id, 1)
    extractor = CountingExtractor([candidate])
    service._visual_extractor = extractor
    service._visual_analyzer = QueueVisualAnalyzer([_low_result("tiny_text"), _high_result("highres_text")])

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["succeeded"] == 1
    assert len(extractor.ensure_calls) == 2
    assert extractor.ensure_calls[-1][1] == 260
    assert service._visual_analyzer.max_image_long_edges[-1] == 3200
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["result_json"]["processing"]["high_res_retry"] is True
    assert artifact["result_json"]["processing"]["max_image_long_edge"] == 3200
    assert service.search("highres_text", limit=5)


def test_tile_large_single_page_table(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
            "pipeline_version": "visual-pipeline-v1",
        }
    )
    extractor = CountingExtractor([candidate])
    service._visual_extractor = extractor
    service._visual_analyzer = QueueVisualAnalyzer(
        [
            _table_result("TILE_A", page=1),
            _table_result("TILE_B", page=1),
            _table_result("TILE_C", page=1),
        ]
    )

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["succeeded"] == 1
    assert result["tile_artifacts"] == 1
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["result_json"]["processing"]["tile_count"] >= 3
    assert artifact["result_json"]["processing"]["tiled"] is True
    chunks = service._backend._get_read_storage().list_chunks(document_id)
    visual_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
    assert any("Signal" in chunk.text and "TILE_A" in chunk.text and "TILE_C" in chunk.text for chunk in visual_chunks)
    assert any(chunk.metadata.get("tile_count", 0) >= 3 for chunk in visual_chunks)
    assert any(chunk.metadata.get("visual_artifact_id") == candidate.id for chunk in visual_chunks)
    assert all("Tile analysis instruction" in context for context in service._visual_analyzer.contexts_before)


def test_tile_resume_reuses_succeeded_tiles(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
            "pipeline_version": "visual-pipeline-v1",
        }
    )
    storage = service._backend._get_storage(writable=True)
    storage.upsert_visual_artifact(candidate)
    storage.upsert_visual_artifact_tile(
        {
            "id": f"{candidate.id}_tile_1",
            "artifact_id": candidate.id,
            "tile_index": 1,
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 233},
            "image_path": "tile1.png",
            "image_hash": f"{candidate.image_hash}_tile_1",
            "status": "succeeded",
            "confidence": 0.9,
            "result_json": {
                **_table_result("REUSED_TILE", page=1).to_dict(),
                "tile_index": 1,
                "visible_range": "0-0.33",
                "analysis_model": "gpt-5.5",
                "prompt_version": "visual-v1",
                "image_hash": f"{candidate.image_hash}_tile_1",
                "parent_image_hash": candidate.image_hash,
            },
            "error": "",
        }
    )
    extractor = CountingExtractor([candidate])
    service._visual_extractor = extractor
    service._visual_analyzer = QueueVisualAnalyzer(
        [
            _table_result("NEW_TILE_B", page=1),
            _table_result("NEW_TILE_C", page=1),
        ]
    )

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["succeeded"] == 1
    assert f"{candidate.id}_tile_1" not in service._visual_analyzer.calls
    assert service._visual_analyzer.calls == [f"{candidate.id}_tile_2", f"{candidate.id}_tile_3"]
    assert service.search("REUSED_TILE", limit=5)
    assert service.search("NEW_TILE_C", limit=5)


def test_force_build_reruns_succeeded_tiles(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
            "pipeline_version": "visual-pipeline-v1",
        }
    )
    service._visual_extractor = CountingExtractor([candidate])
    service._visual_analyzer = QueueVisualAnalyzer(
        [_table_result("OLD_TILE_A", page=1), _table_result("OLD_TILE_B", page=1), _table_result("OLD_TILE_C", page=1)]
    )
    assert service.build_visual_knowledge(document_id=document_id, limit=1)["succeeded"] == 1
    service._visual_extractor = CountingExtractor([candidate])
    service._visual_analyzer = QueueVisualAnalyzer(
        [_table_result("NEW_TILE_A", page=1), _table_result("NEW_TILE_B", page=1), _table_result("NEW_TILE_C", page=1)]
    )

    retry = service.build_visual_knowledge(document_id=document_id, limit=1, force=True)

    assert retry["succeeded"] == 1
    assert service._visual_analyzer.calls == [f"{candidate.id}_tile_1", f"{candidate.id}_tile_2", f"{candidate.id}_tile_3"]
    assert service.search("NEW_TILE_A", limit=5)
    assert service.search("OLD_TILE_A", limit=5) == []


def test_tile_reuse_requires_matching_image_hash(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
            "pipeline_version": "visual-pipeline-v1",
        }
    )
    storage = service._backend._get_storage(writable=True)
    storage.upsert_visual_artifact(candidate)
    storage.upsert_visual_artifact_tile(
        {
            "id": f"{candidate.id}_tile_1",
            "artifact_id": candidate.id,
            "tile_index": 1,
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 233},
            "image_path": "tile1.png",
            "image_hash": "stale-hash",
            "status": "succeeded",
            "confidence": 0.9,
            "result_json": {
                **_table_result("STALE_REUSED_TILE", page=1).to_dict(),
                "tile_index": 1,
                "analysis_model": "gpt-5.5",
                "prompt_version": "visual-v1",
                "image_hash": "stale-hash",
            },
            "error": "",
        }
    )
    service._visual_extractor = CountingExtractor([candidate])
    service._visual_analyzer = QueueVisualAnalyzer(
        [_table_result("FRESH_TILE_A", page=1), _table_result("FRESH_TILE_B", page=1), _table_result("FRESH_TILE_C", page=1)]
    )

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["succeeded"] == 1
    assert f"{candidate.id}_tile_1" in service._visual_analyzer.calls
    assert service.search("FRESH_TILE_A", limit=5)
    assert service.search("STALE_REUSED_TILE", limit=5) == []


def test_any_low_confidence_tile_prevents_artifact_indexing_even_with_high_confidence(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
            "pipeline_version": "visual-pipeline-v1",
        }
    )
    low_but_high_confidence = VisualAnalysisResult(
        **{
            **_table_result("LOW_TILE_SHOULD_BLOCK", page=1, confidence=0.95).to_dict(),
            "should_index": False,
            "low_confidence_reason": "tile explicitly not indexable",
        }
    )
    service._visual_extractor = CountingExtractor([candidate])
    service._visual_analyzer = QueueVisualAnalyzer(
        [
            _table_result("OK_TILE_A", page=1, confidence=0.95),
            low_but_high_confidence,
            _table_result("OK_TILE_C", page=1, confidence=0.95),
        ]
    )

    result = service.build_visual_knowledge(document_id=document_id, limit=1)

    assert result["low_confidence"] == 1
    assert service.search("LOW_TILE_SHOULD_BLOCK", limit=5) == []
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["analysis_status"] == "low_confidence"
    assert "tile 2" in artifact["error"]


def test_visual_result_preserves_continuation_fields(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = _candidate(document_id, version_id, 1)

    result = validate_visual_analysis_json(
        {
            "artifact_type": "table",
            "title": "continued table",
            "summary": "Partial table page",
            "structured_markdown": "| Signal | Direction |\n| --- | --- |\n| A | input |",
            "key_facts": [{"fact": "A is visible", "confidence": 0.9}],
            "table": {
                "headers": ["Signal", "Direction"],
                "rows": [{"Signal": "A", "Direction": "input"}],
                "markdown": "| Signal | Direction |\n| --- | --- |\n| A | input |",
            },
            "readability": "good",
            "confidence": {"ocr": 0.9, "structure": 0.9, "semantic": 0.9, "overall": 0.9},
            "is_partial": False,
            "continuation": {
                "role": "middle",
                "belongs_to_same_artifact": True,
                "evidence": ["continued marker"],
                "confidence": 0.82,
            },
            "should_index": True,
        },
        candidate,
        service.config.visual_analysis,
    )

    assert result.is_partial is True
    assert result.continuation["role"] == "middle"
    assert result.continuation["belongs_to_same_artifact"] is True
    assert result.continuation["evidence"] == ["continued marker"]


def test_group_member_continuation_evidence_is_retained(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    storage = service._backend._get_storage(writable=True)
    group_id = "visual_group_evidence"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document_id,
            "version_id": version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "evidence group",
            "caption": "Table 9. evidence group",
            "source_pages": [1, 2],
            "status": "pending",
            "confidence": 0.9,
            "result_json": {"continuation_evidence": ["same caption"]},
        }
    )
    for index in (1, 2):
        candidate = _candidate(document_id, version_id, index)
        storage.upsert_visual_artifact(candidate)
        storage.add_visual_artifact_group_member(group_id, candidate.id, index, index, "first" if index == 1 else "last", 0.9)
        storage.mark_visual_artifact_group_membership(candidate.id, group_id, index, "first" if index == 1 else "last", 0.9)
        result = _table_result(f"EVIDENCE_{index}", page=index)
        storage.complete_visual_artifact_success(
            candidate.id,
            {
                **result.to_dict(),
                "continuation": {
                    "role": "first" if index == 1 else "last",
                    "belongs_to_same_artifact": True,
                    "evidence": [f"member evidence {index}"],
                    "confidence": 0.9,
                },
            },
            0.9,
            retrievable=False,
        )
    service._visual_analyzer = QueueAnalyzer([])

    response = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert response["outcome"] == "succeeded"
    result_json = storage.get_visual_artifact_group(group_id)["result_json"]
    assert "same caption" in result_json["continuation_evidence"]
    assert "member evidence 1" in result_json["continuation_evidence"]
    assert "member evidence 2" in result_json["continuation_evidence"]


def test_visual_extractor_skips_toc_pages_but_keeps_strict_caption(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "figures.pdf"
    pdf = fitz.open()
    toc_page = pdf.new_page(width=600, height=800)
    toc_page.insert_text((72, 72), "Table of Contents")
    toc_page.insert_text((72, 110), "Figure 1-1. Intro diagram ................ 5")
    toc_page.insert_text((72, 135), "List of Figures")
    figure_page = pdf.new_page(width=600, height=800)
    figure_page.draw_rect(fitz.Rect(80, 120, 520, 420), color=(0, 0, 0))
    figure_page.insert_text((72, 455), "Figure 5-34. Standard Package x16 interface: Signal exit order")
    pdf.save(pdf_path)
    pdf.close()

    config = _config(tmp_path, ingest={"allowed_extensions": [".pdf"]})
    document = KnowledgeDocument(
        id="doc",
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        size=pdf_path.stat().st_size,
        content_hash="hash",
        status="ready",
        version_id="v1",
    )
    extracted = ExtractedDocument(
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        pages=[
            DocumentPage(1, "Table of Contents\nFigure 1-1. Intro diagram ................ 5\nList of Figures"),
            DocumentPage(2, "Figure 5-34. Standard Package x16 interface: Signal exit order"),
        ],
    )

    candidates, report = PyMuPDFVisualArtifactExtractor().extract_candidates_for_page_range(
        document,
        extracted,
        None,
        config,
        start_page=1,
        max_pages=2,
    )

    assert report["skipped_toc_pages"] == 1
    assert all(candidate.page != 1 for candidate in candidates)
    assert any(candidate.page == 2 and "Figure 5-34" in candidate.caption for candidate in candidates)
    caption_candidate = next(candidate for candidate in candidates if candidate.page == 2 and "Figure 5-34" in candidate.caption)
    assert caption_candidate.bbox["y0"] <= 130
    assert caption_candidate.bbox["y1"] >= 420


def test_visual_extractor_rejects_body_figure_references(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "references.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((72, 72), "Figure 1-1 demonstrates an SoC package integration example.")
    page.insert_text((72, 105), "Table 1-3 gives a summary of supported modes.")
    page.insert_text((72, 138), "Figure 3-6 to Figure 3-11 represent examples of valid configurations.")
    page.insert_text((72, 455), "Figure 5-34.")
    page.insert_text((72, 478), "Standard Package x16 interface: Signal exit order")
    pdf.save(pdf_path)
    pdf.close()

    config = _config(tmp_path, ingest={"allowed_extensions": [".pdf"]})
    document = KnowledgeDocument(
        id="doc",
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        size=pdf_path.stat().st_size,
        content_hash="hash",
        status="ready",
        version_id="v1",
    )
    extracted = ExtractedDocument(
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        pages=[
            DocumentPage(
                1,
                "Figure 1-1 demonstrates an SoC package integration example.\n"
                "Table 1-3 gives a summary of supported modes.\n"
                "Figure 3-6 to Figure 3-11 represent examples of valid configurations.\n"
                "Figure 5-34.\n"
                "Standard Package x16 interface: Signal exit order",
            )
        ],
    )

    candidates, _ = PyMuPDFVisualArtifactExtractor().extract_candidates_for_page_range(
        document,
        extracted,
        None,
        config,
        start_page=1,
        max_pages=1,
    )

    captions = "\n".join(candidate.caption for candidate in candidates)
    assert not is_strict_caption_block("Figure 1-1 demonstrates an SoC package integration example.")
    assert not is_strict_caption_block("Table 1-3 gives a summary of supported modes.")
    assert not is_strict_caption_block("Figure 3-6 to Figure 3-11 represent examples of valid configurations.")
    assert "Figure 5-34" in captions
    assert "demonstrates" not in captions
    assert "gives a summary" not in captions


def test_visual_extractor_dedupes_image_inside_caption_region(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "caption-image.pdf"
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    image_doc = fitz.open()
    image_page = image_doc.new_page(width=120, height=90)
    image_page.draw_rect(fitz.Rect(10, 10, 110, 80), color=(1, 0, 0), fill=(1, 0, 0))
    image_bytes = image_doc.convert_to_pdf()
    image_doc.close()
    page.show_pdf_page(fitz.Rect(120, 230, 480, 500), fitz.open("pdf", image_bytes), 0)
    page.insert_text((72, 455), "Figure 5-34. Standard Package x16 interface: Signal exit order")
    pdf.save(pdf_path)
    pdf.close()

    config = _config(
        tmp_path,
        ingest={"allowed_extensions": [".pdf"]},
        visual_analysis={"max_image_candidates_per_page": 3},
    )
    document = KnowledgeDocument(
        id="doc",
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        size=pdf_path.stat().st_size,
        content_hash="hash",
        status="ready",
        version_id="v1",
    )
    extracted = ExtractedDocument(
        title="Doc",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        pages=[DocumentPage(1, "Figure 5-34. Standard Package x16 interface: Signal exit order")],
    )

    candidates, _ = PyMuPDFVisualArtifactExtractor().extract_candidates_for_page_range(
        document,
        extracted,
        None,
        config,
        start_page=1,
        max_pages=1,
    )

    assert any(candidate.caption for candidate in candidates)
    assert not any(candidate.artifact_type == "image" and not candidate.caption for candidate in candidates)
