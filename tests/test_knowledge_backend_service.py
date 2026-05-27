import re
from dataclasses import replace
from pathlib import Path

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import KnowledgeChunk, KnowledgeDocument
import agent.knowledge.backend.service as backend_service
from agent.knowledge.backend.service import _render_protocol_document_markdown


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


def test_single_document_export_keeps_all_protocol_indexes(tmp_path):
    config = KnowledgeBackendConfig.from_mapping(
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
    service = KnowledgeBackendService(config)

    axi_result = service.ingest_upload_bytes(
        "axi-stream.md",
        b"# AXI4-Stream\n\nTVALID and TREADY define the transfer handshake.",
        title="AMBA AXI4-Stream Test",
    )
    axi_document_id = axi_result["document"]["id"]
    service.export_document_library(document_id=axi_document_id)
    service.close()

    ucie_service = KnowledgeBackendService(replace(config, default_kb_id="ucie_1_1"))
    ucie_result = ucie_service.ingest_upload_bytes(
        "ucie.md",
        b"# UCIe\n\nFLIT transfer uses protocol-layer flow control.",
        title="UCIe Test",
    )
    ucie_document_id = ucie_result["document"]["id"]

    export = ucie_service.export_document_library(document_id=ucie_document_id)
    ucie_service.close()

    assert export["documents_exported"] == 1
    root_index = (tmp_path / "knowledge" / "protocols" / "index.md").read_text(encoding="utf-8")
    assert "[axi4_stream](axi4_stream/index.md) - 1 document(s)" in root_index
    assert "[ucie_1_1](ucie_1_1/index.md) - 1 document(s)" in root_index

    axi_index = (tmp_path / "knowledge" / "protocols" / "axi4_stream" / "index.md").read_text(encoding="utf-8")
    ucie_index = (tmp_path / "knowledge" / "protocols" / "ucie_1_1" / "index.md").read_text(encoding="utf-8")
    assert "AMBA AXI4-Stream Test" in axi_index
    assert "UCIe Test" in ucie_index


def test_protocol_markdown_source_chunks_skip_visual_analysis_chunks():
    document = KnowledgeDocument(
        id="doc1",
        title="Protocol Doc",
        source_path="protocol.pdf",
        mime_type="application/pdf",
        size=123,
        content_hash="hash",
        status="ready",
        kb_id="ucie",
        version_id="v1",
    )
    chunks = [
        KnowledgeChunk(
            id="chunk1",
            document_id="doc1",
            ordinal=1,
            page_start=1,
            page_end=1,
            text="Ordinary source prose.",
        ),
        KnowledgeChunk(
            id="chunk2",
            document_id="doc1",
            ordinal=2,
            page_start=2,
            page_end=2,
            text="[视觉图表]\nVisual chunk text should not appear in Source Chunks.",
            metadata={"source": "visual_analysis"},
        ),
    ]
    visual_artifacts = [
        {
            "analysis_status": "succeeded",
            "retrievable": True,
            "page": 2,
            "artifact_type": "figure",
            "analysis_confidence": 0.9,
            "caption": "Figure 1. Visual caption",
            "bbox": {},
            "result_json": {
                "caption": "Figure 1. Visual caption",
                "summary": "High-confidence visual summary.",
                "key_facts": [{"fact": "Visual fact", "confidence": 0.9}],
            },
        }
    ]

    markdown = _render_protocol_document_markdown(document, chunks, visual_artifacts)
    source_section = markdown.split("## 视觉图表补全", 1)[0]

    assert "Ordinary source prose." in source_section
    assert "[视觉图表]" not in source_section
    assert "High-confidence visual summary." in markdown


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


def test_deep_query_expands_adjacent_chunks_for_step_context(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "ingest": {"allowed_extensions": [".md"]},
                "retrieval": {"context_window_chunks": 1, "max_evidence_chars": 12000},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    service._backend.chunk_chars = 360
    service._backend.overlap_chars = 0
    service.ingest_upload_bytes(
        "ucie-repairval.md",
        (
            b"# UCIe MBINIT.REPAIRVAL\n\n"
            b"Step 1. The UCIe Module sends MBINIT.REPAIRVAL init req and waits for init resp. "
            b"Step 2. The UCIe Module must send 128 iterations of VALTRAIN pattern on TVLD_L "
            b"along with the forwarded clock. Step 3. Partner detects pattern on RVLD_L and "
            b"RRDVLD_L. Step 4. After pattern transmission the module sends result req.\n\n"
            b"Step 7. The UCIe Module sends 128 iterations of VALTRAIN repair pattern on "
            b"TRDVLD_L along with the forwarded clock. This step checks the redundant Valid "
            b"repair resource path, not the post-repair success check.\n\n"
            b"Step 12. If a repair is applied, device must check the repair success by "
            b"repeating Step 1 through Step 4. The logical TVLD_L path may be remapped through "
            b"the repair mux to the TRDVLD_P/RRDVLD_P physical path after repair."
        ),
        title="UCIe RepairVAL Mini Spec",
    )

    result = service.deep_query(
        "UCIe MBINIT.REPAIRVAL after repair repeat Step 1 through Step 4 TVLD_L TRDVLD_L repair mux",
        limit=1,
        context_window=2,
        max_evidence_chars=12000,
    )

    evidence = "\n".join(block["text"] for block in result["evidence_blocks"])
    assert result["status"] == "ok"
    assert "Step 2" in evidence
    assert "TVLD_L" in evidence
    assert "Step 7" in evidence
    assert "TRDVLD_L" in evidence
    assert "Step 12" in evidence
    assert "repeating Step 1 through Step 4" in evidence
    assert "TRDVLD_P/RRDVLD_P physical path" in evidence
    assert "Step 12" in result["coverage_terms"]
    assert all(block["source_span_ids"] for block in result["evidence_blocks"])
    assert result["citations"][0]["source_span_ids"]


def test_deep_query_returns_table_blocks_for_protocol_tables(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "ingest": {"allowed_extensions": [".md"]},
                "retrieval": {"context_window_chunks": 1, "max_evidence_chars": 12000},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    service.ingest_upload_bytes(
        "phyretrain-table.md",
        (
            b"# PHYRETRAIN\n\n"
            b"PHYRETRAIN sends a retrain start req and retrain start resp before using the resolved encoding. "
            b"Table 4-11 defines Retrain Encoding. MsgInfo[2:0] carries Retrain Encoding "
            b"and MsgInfo[15:3] is Reserved. 001b maps to TXSELFCAL, 010b maps to "
            b"SPEEDIDLE, and 100b maps to REPAIR."
        ),
        title="UCIe PHYRETRAIN Table Mini Spec",
    )

    result = service.deep_query(
        "PHYRETRAIN retrain encoding Table 4-11 MsgInfo[2:0]",
        limit=1,
        context_window=1,
        max_evidence_chars=12000,
    )

    assert result["status"] == "ok"
    assert result["table_blocks"]
    table_text = "\n".join(block["text"] for block in result["table_blocks"])
    assert "Table 4-11" in table_text
    assert "MsgInfo[2:0]" in table_text
    assert "001b" in table_text


def test_deep_query_truncates_evidence_without_breaking_records(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "ingest": {"allowed_extensions": [".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    service.ingest_upload_bytes(
        "long-spec.md",
        ("# Long Spec\n\n" + "Alpha pattern evidence. " * 300).encode("utf-8"),
        title="Long Evidence Spec",
    )

    result = service.deep_query("Alpha pattern evidence", limit=1, context_window=1, max_evidence_chars=1000)

    assert result["evidence_blocks"]
    assert sum(len(block["text"]) for block in result["evidence_blocks"]) <= 1000
    assert result["evidence_blocks"][-1]["truncated"] is True
    assert result["coverage_terms"]


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
