from pathlib import Path

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService


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
