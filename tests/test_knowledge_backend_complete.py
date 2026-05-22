import inspect
import sqlite3
from pathlib import Path

import pytest

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.service import dispatch_admin_request, dispatch_provider_request


def _config(tmp_path, **overrides):
    mapping = {
        "enabled": True,
        "provider_api_enabled": True,
        "admin_api_enabled": True,
        "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
        "workspace_root": str(tmp_path),
        "data_dir": str(tmp_path / "backend-data"),
        "default_kb_id": "kb_default",
        "ingest": {"allowed_extensions": [".txt", ".md"], "max_file_size_mb": 5},
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


def _ingest_text(service, filename, text):
    result = service.ingest_upload_bytes(filename, text.encode("utf-8"))
    assert result["status"] == "succeeded", result
    return result


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _assert_trace(response, trace_id):
    assert response.get("trace_id") == trace_id
    return response


def test_ingestion_persists_source_spans_with_chunks_and_search_citations(tmp_path):
    service = _service(tmp_path)
    _ingest_text(
        service,
        "pcie-tlp.md",
        "# PCIe TLP\n\nA Transaction Layer Packet header carries routing and completion metadata.",
    )

    hit = service.search("Transaction Layer Packet", limit=1)[0]
    assert hit["document_id"]
    assert hit["chunk_id"]
    assert hit["page_start"] == 1
    assert hit["page_end"] == 1
    assert "Transaction Layer Packet" in hit["snippet"]

    db_path = service.config.sqlite_path
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, document_id, ordinal, page_start, page_end, text FROM chunks"
        ).fetchone()

    assert row is not None
    assert row["id"] == hit["chunk_id"]
    assert row["document_id"] == hit["document_id"]
    assert row["ordinal"] == 1
    assert row["page_start"] == 1
    assert row["page_end"] == 1
    assert "completion metadata" in row["text"]

    query_result = service.query("What carries completion metadata?", limit=1)
    assert query_result["citations"][0]["document_id"] == hit["document_id"]
    assert query_result["citations"][0]["page_start"] == 1
    assert query_result["citations"][0]["page_end"] == 1


def test_graph_builder_contract_extracts_entities_relations_confidence_status(tmp_path):
    service = _service(tmp_path)
    _ingest_text(
        service,
        "pcie-link.md",
        "PCIe uses Transaction Layer Packets. A TLP contains a header and payload.",
    )

    builder = getattr(service, "build_knowledge_graph", None) or getattr(service, "build_graph", None)
    assert callable(builder), "KnowledgeBackendService must expose build_knowledge_graph/build_graph"

    signature = inspect.signature(builder)
    kwargs = {"mode": "heuristic"} if "mode" in signature.parameters else {}
    graph = builder(**kwargs)
    entities = _field(graph, "entities", [])
    relations = _field(graph, "relations", [])

    assert any(_field(entity, "name") == "PCIe" for entity in entities)
    assert any(_field(entity, "name") in {"TLP", "Transaction Layer Packet"} for entity in entities)
    for entity in entities:
        assert 0 <= float(_field(entity, "confidence")) <= 1
        assert _field(entity, "status") in {"candidate", "verified", "rejected"}

    assert relations
    for relation in relations:
        assert _field(relation, "source")
        assert _field(relation, "target")
        assert _field(relation, "predicate")
        assert 0 <= float(_field(relation, "confidence")) <= 1
        assert _field(relation, "status") in {"candidate", "verified", "rejected"}


def test_cross_kb_alias_resolution_allows_tlp_query_to_find_pcie_document(tmp_path):
    service = _service(tmp_path, default_kb_id="kb_pcie")
    _ingest_text(
        service,
        "pcie-spec.md",
        "PCI Express, also called PCIe, defines Transaction Layer Packet routing rules.",
    )

    alias_resolver = getattr(service, "resolve_entities", None) or getattr(service, "resolve_entity", None)
    assert callable(alias_resolver), "KnowledgeBackendService must expose entity alias resolution"

    resolved = alias_resolver(["TLP"], kb_ids=["kb_pcie", "kb_protocols"])
    entities = _field(resolved, "entities", resolved)
    tlp = next((entity for entity in entities if _field(entity, "term") == "TLP"), None)
    assert tlp is not None
    assert _field(tlp, "resolved") is True
    assert _field(tlp, "canonical_name") in {"PCIe", "PCI Express", "Transaction Layer Packet"}
    assert "kb_pcie" in _field(tlp, "visited_kb_ids", [])

    search_signature = inspect.signature(service.search)
    kwargs = {"visited_kb_ids": ["kb_pcie"]} if "visited_kb_ids" in search_signature.parameters else {}
    hits = service.search("TLP routing", limit=5, **kwargs)
    assert any("PCI" in hit["snippet"] for hit in hits)


def test_provider_api_contract_preserves_trace_and_traversal_semantics(monkeypatch, tmp_path):
    service = _service(tmp_path, default_kb_id="kb_pcie")
    _ingest_text(
        service,
        "pcie-provider.md",
        "PCIe TLP flow control is represented as credits across linked protocol concepts.",
    )

    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: service.config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda config: service)

    trace_id = "trace-provider-1"
    capabilities = dispatch_provider_request("GET", "capabilities", {})
    assert set(capabilities["supported_methods"]) >= {
        "search",
        "query",
        "resolve_entity",
        "graph_neighbors",
        "verify_source",
    }

    search = _assert_trace(
        dispatch_provider_request(
            "POST",
            "search",
            {"query": "TLP credits", "trace_id": trace_id, "visited_kb_ids": ["kb_pcie"]},
        ),
        trace_id,
    )
    assert search["results"]
    assert search.get("visited_kb_ids") == ["kb_pcie"]

    query = _assert_trace(
        dispatch_provider_request(
            "POST",
            "query",
            {"query": "What uses TLP credits?", "trace_id": trace_id, "visited_kb_ids": ["kb_pcie"]},
        ),
        trace_id,
    )
    assert query["citations"]
    assert query.get("visited_kb_ids") == ["kb_pcie"]

    entities = _assert_trace(
        dispatch_provider_request(
            "POST",
            "entities/resolve",
            {"terms": ["TLP"], "trace_id": trace_id, "visited_kb_ids": ["kb_pcie"]},
        ),
        trace_id,
    )
    assert entities["entities"][0]["resolved"] is True
    assert entities.get("visited_kb_ids") == ["kb_pcie"]

    graph = _assert_trace(
        dispatch_provider_request(
            "GET",
            "graph/neighbors",
            {"entity_id": "PCIe", "max_hops": 1, "trace_id": trace_id, "visited_kb_ids": ["kb_pcie"]},
        ),
        trace_id,
    )
    assert graph["nodes"]
    assert all(int(edge.get("hop", 1)) <= 1 for edge in graph.get("links", []))
    assert graph.get("visited_kb_ids") == ["kb_pcie"]

    verify = _assert_trace(
        dispatch_provider_request(
            "POST",
            "verify",
            {"claim": "PCIe uses TLP credits", "trace_id": trace_id, "visited_kb_ids": ["kb_pcie"]},
        ),
        trace_id,
    )
    assert verify["status"] in {"supported", "contradicted", "insufficient"}
    assert verify["status"] == "supported"
    assert verify.get("visited_kb_ids") == ["kb_pcie"]


def test_admin_path_import_is_whitelist_gated_through_public_dispatch(monkeypatch, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    allowed_doc = allowed_root / "allowed.md"
    blocked_doc = blocked_root / "blocked.md"
    allowed_doc.write_text("PCIe allowed import", encoding="utf-8")
    blocked_doc.write_text("PCIe blocked import", encoding="utf-8")

    config = _config(
        tmp_path,
        ingest={"allowed_extensions": [".md"], "allowed_import_roots": [str(allowed_root)]},
    )
    service = KnowledgeBackendService(config)
    monkeypatch.setattr(KnowledgeBackendConfig, "from_project_config", classmethod(lambda cls: config))
    monkeypatch.setattr("agent.knowledge.backend.service.build_knowledge_backend", lambda _: service)

    blocked = dispatch_admin_request("POST", "ingest", {"path": str(blocked_doc)})
    assert blocked["status"] == "error"
    assert "allowed_import_roots" in blocked["message"]
    assert service.search("blocked import", limit=5) == []

    allowed = dispatch_admin_request("POST", "ingest", {"path": str(allowed_doc)})
    assert allowed["status"] == "success"
    assert allowed["files_indexed"] == 1
    assert service.search("allowed import", limit=5)
