import os

import pytest

from agent.knowledge.backend import (
    KnowledgeBackendConfig,
    MissingProviderTokenError,
    build_knowledge_backend,
    parse_knowledge_backend_enabled,
    require_provider_token,
)
from agent.knowledge.backend.service import _is_import_path_allowed


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("off", False),
        ("disabled", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("on", True),
        ("enabled", True),
        ("yes", True),
    ],
)
def test_parse_knowledge_backend_enabled_accepts_common_env_values(raw, expected):
    assert parse_knowledge_backend_enabled(raw) is expected


def test_config_from_env_parses_disabled_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_BACKEND_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_ADMIN_API_ENABLED", "false")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_PROVIDER_API_ENABLED", "true")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_MAX_FILE_SIZE_MB", "42")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_VECTOR_PROVIDER", "qdrant")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_SQLITE_PATH", str(tmp_path / "knowledge.db"))

    config = KnowledgeBackendConfig.from_env()

    assert _field(config, "enabled") is False
    assert _field(config, "admin_api_enabled") is False
    assert _field(config, "provider_api_enabled") is True
    assert _field(_field(config, "ingest"), "max_file_size_mb") == 42
    assert _vector_provider(config) == "qdrant"
    assert os.fspath(_sqlite_path(config)) == str(tmp_path / "knowledge.db")


def test_config_from_env_parses_enabled_sqlite_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_BACKEND_ENABLED", "yes")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_VECTOR_PROVIDER", "sqlite")
    monkeypatch.setenv("KNOWLEDGE_BACKEND_SQLITE_PATH", str(tmp_path / "knowledge.sqlite3"))

    config = KnowledgeBackendConfig.from_env()

    assert _field(config, "enabled") is True
    assert _vector_provider(config) == "sqlite"
    assert os.fspath(_sqlite_path(config)) == str(tmp_path / "knowledge.sqlite3")


def test_config_from_mapping_parses_api_flags_and_ingest_limits(tmp_path):
    config = KnowledgeBackendConfig.from_mapping(
        {
            "enabled": True,
            "admin_api_enabled": False,
            "provider_api_enabled": True,
            "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
            "ingest": {"allowed_extensions": ["txt"], "max_file_size_mb": 12},
            "vector_store": {"provider": "sqlite", "required": False},
        }
    )

    assert _field(config, "admin_api_enabled") is False
    assert _field(config, "provider_api_enabled") is True
    assert _field(_field(config, "ingest"), "allowed_extensions") == [".txt"]
    assert _field(_field(config, "ingest"), "max_file_size_mb") == 12


def test_path_import_whitelist_is_closed_by_default(tmp_path):
    document = tmp_path / "knowledge" / "note.md"
    document.parent.mkdir()
    document.write_text("hello", encoding="utf-8")
    config = _config(tmp_path, ingest={"allowed_extensions": [".md"]})

    assert _is_import_path_allowed(document, config) is False

    config = _config(
        tmp_path,
        ingest={"allowed_extensions": [".md"], "allowed_import_roots": [str(document.parent)]},
    )

    assert _is_import_path_allowed(document, config) is True
    assert _is_import_path_allowed(tmp_path.parent / "other.md", config) is False


def test_disabled_backend_does_not_import_or_require_optional_dependencies(tmp_path):
    config = _config(
        tmp_path,
        enabled=False,
        vector_provider="qdrant",
        sqlite_path=tmp_path / "disabled.db",
    )

    backend = build_knowledge_backend(config)

    assert _field(backend.status(), "enabled") is False
    assert backend.search("anything") == []


def test_qdrant_backend_falls_back_to_fts_when_client_dependency_is_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("sys").modules, "qdrant_client", None)
    config = _config(
        tmp_path,
        enabled=True,
        vector_provider="qdrant",
        sqlite_path=tmp_path / "unused.db",
        qdrant_url="http://127.0.0.1:1",
    )

    backend = build_knowledge_backend(config)
    status = backend.status()

    assert _field(status, "enabled") is True
    assert _field(status, "backend") == "qdrant"
    assert backend.search("needle") == []


def test_required_qdrant_backend_disables_when_client_dependency_is_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(__import__("sys").modules, "qdrant_client", None)
    config = _config(
        tmp_path,
        enabled=True,
        vector_provider="qdrant",
        sqlite_path=tmp_path / "unused.db",
        qdrant_url="http://127.0.0.1:1",
        vector_store={"provider": "qdrant", "url": "http://127.0.0.1:1", "required": True},
    )

    backend = build_knowledge_backend(config)
    status = backend.status()

    assert _field(status, "enabled") is False
    assert _field(status, "backend") == "qdrant"
    assert "qdrant" in _field(status, "reason", "").lower()


def test_provider_auth_helper_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MissingProviderTokenError):
        require_provider_token("openai", token=None, env_var="OPENAI_API_KEY")


def test_provider_auth_helper_accepts_explicit_or_env_token(monkeypatch):
    assert require_provider_token("openai", token="explicit", env_var="OPENAI_API_KEY") == "explicit"

    monkeypatch.setenv("OPENAI_API_KEY", "from-env")

    assert require_provider_token("openai", token=None, env_var="OPENAI_API_KEY") == "from-env"


def _config(tmp_path, **overrides):
    sqlite_path = overrides.pop("sqlite_path", tmp_path / "knowledge.sqlite3")
    vector_provider = overrides.pop("vector_provider", "sqlite")
    qdrant_url = overrides.pop("qdrant_url", "http://127.0.0.1:6333")
    enabled = overrides.pop("enabled", True)
    return KnowledgeBackendConfig.from_mapping(
        {
            "enabled": enabled,
            "sqlite_path": str(sqlite_path),
            "vector_store": {
                "provider": vector_provider,
                "url": qdrant_url,
                "collection": "knowledge_test",
                "required": False,
            },
            **overrides,
        }
    )


def _sqlite_path(config):
    return _field(config, "sqlite_path", _field(config, "path"))


def _vector_provider(config):
    vector_store = _field(config, "vector_store", {})
    return _field(config, "vector_provider", _field(config, "backend", _field(vector_store, "provider")))
