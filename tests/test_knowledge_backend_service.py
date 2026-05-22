import re
from pathlib import Path

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
import agent.knowledge.backend.service as backend_service


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _result_text(result):
    parts = [
        str(_field(result, "path", "")),
        str(_field(result, "title", "")),
        str(_field(result, "snippet", "")),
        str(_field(result, "text", "")),
    ]
    return "\n".join(parts)


def test_sqlite_backend_ingests_and_searches_markdown_and_text_files(tmp_path):
    workspace = tmp_path / "workspace"
    knowledge = workspace / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "concept.md").write_text(
        "# Retrieval Notes\n\nVector search can combine keyword fallback with embeddings.",
        encoding="utf-8",
    )
    (knowledge / "plain.txt").write_text(
        "SQLite full text search should index ordinary text notes about durable storage.",
        encoding="utf-8",
    )
    (knowledge / "ignored.json").write_text('{"term": "jsonsentinel"}', encoding="utf-8")

    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(workspace),
                "ingest": {"allowed_extensions": [".txt", ".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    ingest_result = service.ingest_path(knowledge)

    assert _field(ingest_result, "files_indexed") == 2
    assert _field(ingest_result, "files_skipped", 0) >= 1

    markdown_results = service.search("keyword fallback", limit=5)
    text_results = service.search("durable storage", limit=5)
    ignored_results = service.search("jsonsentinel", limit=5)

    assert any("concept.md" in _result_text(result) for result in markdown_results)
    assert any("plain.txt" in _result_text(result) for result in text_results)
    assert ignored_results == []


def test_sqlite_backend_reingest_updates_search_index(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    note = knowledge / "topic.md"
    note.write_text("# Topic\n\nalpha beta gamma", encoding="utf-8")

    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "ingest": {"allowed_extensions": [".txt", ".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    service.ingest_path(note)
    assert service.search("alpha", limit=5)

    note.write_text("# Topic\n\nreplacement delta epsilon", encoding="utf-8")
    service.ingest_path(note)

    assert service.search("alpha", limit=5) == []
    assert any("topic.md" in _result_text(result) for result in service.search("delta", limit=5))


def test_backend_exports_indexed_document_to_visible_markdown_library(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "axi4_stream",
                "ingest": {"allowed_extensions": [".md"], "document_library_root": str(tmp_path)},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    result = service.ingest_upload_bytes(
        "axi-stream.md",
        b"# AXI4-Stream\n\nTVALID and TREADY define the transfer handshake.",
        title="AMBA AXI4-Stream Test",
    )
    document_id = result["document"]["id"]

    export = service.export_document_library(document_id=document_id)

    assert export["status"] == "success"
    assert export["documents_exported"] == 1
    exported_path = tmp_path / export["documents"][0]["path"]
    assert exported_path.is_file()
    exported_text = exported_path.read_text(encoding="utf-8")
    assert "AMBA AXI4-Stream Test" in exported_text
    assert "TVALID and TREADY" in exported_text
    assert "Source span IDs" in exported_text
    assert (tmp_path / "knowledge" / "protocols" / "axi4_stream" / "index.md").is_file()


def test_backend_generates_validated_llm_study_document(tmp_path, monkeypatch):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "axi4_stream",
                "ingest": {"allowed_extensions": [".md"], "document_library_root": str(tmp_path)},
                "llm_builder": {"enabled": True, "index_generated_document": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    result = service.ingest_upload_bytes(
        "axi-stream.md",
        (
            b"# AXI4-Stream\n\n"
            b"TVALID and TREADY define the transfer handshake. "
            b"TDATA carries data, TKEEP and TSTRB qualify bytes, TLAST marks packets, "
            b"and TID TDEST TUSER provide sideband information."
        ),
        title="AMBA AXI4-Stream Test",
    )
    document_id = result["document"]["id"]

    def fake_llm(prompt, config):
        span_id = re.search(r"source_span:([A-Za-z0-9_-]+)", prompt).group(1)
        return (
            "# AXI4-Stream LLM Study\n\n"
            f"AXI4-Stream uses TVALID and TREADY for handshake. `source_span:{span_id}`\n\n"
            f"TDATA carries payload data. `source_span:{span_id}`\n\n"
            f"TKEEP and TSTRB are byte qualifier signals. `source_span:{span_id}`\n\n"
            f"TLAST marks packet boundary. `source_span:{span_id}`\n\n"
            f"TID, TDEST, and TUSER are sideband signals. `source_span:{span_id}`\n\n"
            "## Source Map\n\n"
            f"- chunk references `source_span:{span_id}` for every factual section."
        )

    monkeypatch.setattr(backend_service, "_call_llm_for_study_document", fake_llm)

    llm_result = service.generate_llm_study_document(document_id=document_id)

    assert llm_result["status"] == "success"
    assert llm_result["validation"]["valid"] is True
    study_path = tmp_path / llm_result["study_document_path"]
    assert study_path.is_file()
    assert "AXI4-Stream LLM Study" in study_path.read_text(encoding="utf-8")
    indexed = llm_result["indexed_document"]
    assert indexed["doc_type"] == "llm_study"
    assert any(document["doc_type"] == "llm_study" for document in service.list_documents())


def test_sqlite_backend_skips_files_over_size_limit(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    large_note = knowledge / "large.txt"
    large_note.write_bytes(b"x" * (1024 * 1024 + 1))

    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "ingest": {"allowed_extensions": [".txt"], "max_file_size_mb": 1},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    ingest_result = service.ingest_path(knowledge)

    assert _field(ingest_result, "files_indexed") == 0
    assert _field(ingest_result, "files_skipped") == 1
    assert service.search("xxx", limit=5) == []


def test_service_is_safe_when_backend_is_disabled(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": False,
                "sqlite_path": str(tmp_path / "disabled.sqlite3"),
                "workspace_root": str(tmp_path),
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    assert _field(service.status(), "enabled") is False
    assert service.ingest_path(Path(tmp_path / "missing")) is not None
    assert service.search("anything") == []
