import sqlite3
import json

import pytest

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import ExtractedDocument, DocumentPage, KnowledgeDocument, VisualAnalysisResult, VisualArtifactCandidate
from agent.knowledge.backend.service import dispatch_admin_request
from agent.knowledge.backend.storage import KnowledgeStorage
from agent.knowledge.backend.visual_analyzer import VisualAnalyzer
from agent.knowledge.backend.visual_extractors import PyMuPDFVisualArtifactExtractor, is_strict_caption_block


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


class QueueVisualAnalyzer(VisualAnalyzer):
    def __init__(self, results, group_results=None):
        self.results = list(results)
        self.group_results = list(group_results or [])
        self.calls = []
        self.group_calls = []
        self.backends = []

    def analyze(self, candidate, config, document=None, analysis_backend=None):
        self.calls.append(candidate.id)
        self.backends.append(analysis_backend)
        item = self.results.pop(0)
        return item(candidate) if callable(item) else item

    def analyze_group(self, group, members, config, document=None, analysis_backend=None):
        self.group_calls.append(group["id"])
        self.backends.append(analysis_backend)
        assert self.group_results, "no queued group result"
        item = self.group_results.pop(0)
        return item(group, members) if callable(item) else item


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
    service = _service(tmp_path, visual_analysis={"prepare_pages_per_request": 3})
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
    assert third["group_succeeded"] == 1
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


def test_no_group_for_body_references(tmp_path):
    storage = KnowledgeStorage(tmp_path / "knowledge.sqlite3")
    try:
        assert storage.list_visual_artifact_groups() == []
        assert storage.visual_group_stats()["total"] == 0
    finally:
        storage.close()


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

    service._visual_analyzer = QueueAnalyzer([])
    result = service.analyze_visual_artifact_group("visual_group_low", analysis_backend="codex")

    assert result["outcome"] == "low_confidence"
    group = storage.get_visual_artifact_group("visual_group_low")
    assert group["status"] == "low_confidence"
    assert storage.get_visual_artifact_group("visual_group_other")["status"] == "pending"
    assert service.search("LOW_1", limit=5) == []


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
    artifact = service._backend._get_read_storage().list_visual_artifacts(document_id=document_id)[0]
    assert artifact["result_json"]["processing"]["high_res_retry"] is True
    assert service.search("highres_text", limit=5)


def test_tile_large_single_page_table(tmp_path):
    service = _service(tmp_path)
    document_id, version_id = _ingest(service)
    candidate = VisualArtifactCandidate(
        **{
            **_candidate(document_id, version_id, 1).to_dict(),
            "bbox": {"x0": 0, "y0": 0, "x1": 600, "y1": 700, "page_width": 600, "page_height": 800},
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
    chunks = service._backend._get_read_storage().list_chunks(document_id)
    visual_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
    assert any("Signal" in chunk.text and "TILE_A" in chunk.text and "TILE_C" in chunk.text for chunk in visual_chunks)
    assert any(chunk.metadata.get("visual_artifact_id") == candidate.id for chunk in visual_chunks)


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
