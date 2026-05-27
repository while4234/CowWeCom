import sqlite3

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import VisualAnalysisResult, VisualArtifactCandidate
from agent.knowledge.backend.service import dispatch_admin_request
from agent.knowledge.backend.storage import KnowledgeStorage


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
