import re
from dataclasses import replace
from pathlib import Path

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import KnowledgeChunk, KnowledgeDocument
from agent.knowledge.backend.models import SourceSpan, VisualAnalysisResult, VisualArtifactCandidate
import agent.knowledge.backend.service as backend_service
from agent.knowledge.backend.service import _render_protocol_document_markdown
from agent.knowledge.backend.storage import KnowledgeStorage, stable_chunk_id, stable_span_id


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


def _source_document(document_id, tmp_path, *, kb_id="kb_default", doc_type="document"):
    pdf_path = tmp_path / f"{document_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% fake service test pdf\n")
    return KnowledgeDocument(
        id=document_id,
        title=document_id,
        source_path=str(pdf_path),
        mime_type="application/pdf",
        size=pdf_path.stat().st_size,
        content_hash=f"hash-{document_id}",
        status="ready",
        kb_id=kb_id,
        doc_type=doc_type,
        version_id=f"version-{document_id}",
    )


def _polluted_chunk(document):
    text = "L f( ) 20 10 Vr f( ) Vs f( ) log ="
    return KnowledgeChunk(
        id=f"chunk-{document.id}-ordinary",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text=text,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[f"span-{document.id}-ordinary"],
    )


def _polluted_span(document):
    return SourceSpan(
        id=f"span-{document.id}-ordinary",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="L f( ) 20 10 Vr f( ) Vs f( ) log =",
    )


def _add_visual_replacement(storage, document, *, confidence=0.91, retrievable=True):
    artifact = VisualArtifactCandidate(
        id=f"artifact-{document.id}",
        document_id=document.id,
        version_id=document.version_id,
        kb_id=document.kb_id,
        artifact_type="formula",
        page=1,
        label="Equation 1",
        caption="Equation 1. Formula",
        bbox={"x0": 1, "y0": 2, "x1": 100, "y1": 120},
        image_hash=f"hash-{document.id}",
        context_hash=f"context-{document.id}",
        parser="test",
        parser_confidence=0.9,
        source_path=document.source_path,
    )
    chunk = KnowledgeChunk(
        id=f"chunk-{document.id}-visual",
        document_id=document.id,
        ordinal=99,
        page_start=1,
        page_end=1,
        text="High confidence visual formula replacement.",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[f"span-{document.id}-visual"],
        metadata={"source": "visual_analysis", "visual_confidence": confidence, "retrievable": retrievable},
    )
    span = SourceSpan(
        id=f"span-{document.id}-visual",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text=chunk.text,
    )
    storage.upsert_visual_artifact(artifact)
    storage.append_visual_chunks(document.id, document.version_id, artifact.id, [chunk], [span])
    if retrievable:
        storage.complete_visual_artifact_success(
            artifact.id,
            {"artifact_type": "formula", "summary": chunk.text, "confidence": {"overall": confidence}, "should_index": True},
            confidence,
            retrievable=True,
        )
    else:
        storage.complete_visual_artifact_low_confidence(
            artifact.id,
            {"reason": "test low confidence"},
            confidence,
            "test low confidence",
        )


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
    assert (tmp_path / "knowledge" / "documents" / "axi4_stream" / "index.md").is_file()


def test_upload_bytes_can_target_explicit_kb_id(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "kb_default",
                "ingest": {"allowed_extensions": [".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    result = service.ingest_upload_bytes(
        "new-protocol.md",
        b"# New Protocol\n\nTVALID and TREADY define the transfer handshake.",
        kb_id="new_protocol",
    )
    document_id = result["document"]["id"]
    storage = KnowledgeStorage(tmp_path / "knowledge.sqlite3")
    try:
        assert result["document"]["kb_id"] == "new_protocol"
        assert storage.get_document(document_id).kb_id == "new_protocol"
        assert {chunk.kb_id for chunk in storage.list_chunks(document_id)} == {"new_protocol"}
        assert {row["document_id"] for row in storage.conn.execute("SELECT document_id FROM source_spans").fetchall()} == {document_id}
        assert {
            row["source_kb_id"]
            for row in storage.conn.execute("SELECT source_kb_id FROM knowledge_relations").fetchall()
            if row["source_kb_id"]
        } <= {"new_protocol"}
    finally:
        storage.close()


def test_upload_bytes_without_kb_id_keeps_default_kb_id(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "default_protocol",
                "ingest": {"allowed_extensions": [".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    result = service.ingest_upload_bytes("default.md", b"# Default\n\nAXI4-Stream is a protocol.")

    assert result["status"] == "succeeded"
    assert result["document"]["kb_id"] == "default_protocol"


def test_upload_rejects_invalid_kb_id(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "ingest": {"allowed_extensions": [".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    result = service.ingest_upload_bytes("bad.md", b"# Bad", kb_id="../bad")

    assert result["status"] == "failed"
    assert "kb_id" in result["message"]


def test_search_kb_id_filter_is_pushed_before_limit(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "alpha",
                "ingest": {"allowed_extensions": [".md"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    service.ingest_upload_bytes("alpha.md", b"# Alpha\n\nSharedNeedle appears in alpha.", kb_id="alpha")
    service.ingest_upload_bytes("beta.md", b"# Beta\n\nSharedNeedle appears in beta.", kb_id="beta")

    hits = service.search("SharedNeedle", limit=1, kb_ids=["beta"])
    query = service.query("SharedNeedle", limit=1, kb_ids=["beta"])

    assert len(hits) == 1
    assert hits[0]["kb_id"] == "beta"
    assert query["citations"][0]["kb_id"] == "beta"


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
    root_index = (tmp_path / "knowledge" / "documents" / "index.md").read_text(encoding="utf-8")
    assert "[axi4_stream](axi4_stream/index.md) - 1 document(s)" in root_index
    assert "[ucie_1_1](ucie_1_1/index.md) - 1 document(s)" in root_index

    axi_index = (tmp_path / "knowledge" / "documents" / "axi4_stream" / "index.md").read_text(encoding="utf-8")
    ucie_index = (tmp_path / "knowledge" / "documents" / "ucie_1_1" / "index.md").read_text(encoding="utf-8")
    assert "AMBA AXI4-Stream Test" in axi_index
    assert "UCIe Test" in ucie_index


def test_export_uses_documents_category_not_protocols(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "default_kb_id": "sv_book",
                "ingest": {"allowed_extensions": [".md"], "document_library_root": str(tmp_path)},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    result = service.ingest_upload_bytes(
        "systemverilog.md",
        b"# SystemVerilog Guide\n\nInterfaces and classes support verification code reuse.",
        title="SystemVerilog Guide",
    )
    document_id = result["document"]["id"]

    export = service.export_document_library(document_id=document_id)

    assert export["status"] == "success"
    assert export["documents"][0]["path"].startswith("knowledge/documents/sv_book/")
    exported_text = (tmp_path / export["documents"][0]["path"]).read_text(encoding="utf-8")
    root_index = (tmp_path / "knowledge" / "documents" / "index.md").read_text(encoding="utf-8")
    kb_index = (tmp_path / "knowledge" / "documents" / "sv_book" / "index.md").read_text(encoding="utf-8")
    combined = "\n".join([exported_text, root_index, kb_index])
    assert "Protocol Knowledge Libraries" not in combined
    assert "Protocol Knowledge Base" not in combined
    assert "protocol-specific" not in combined
    assert "# Local Document Knowledge Libraries" in root_index
    assert "# Local Document Knowledge Base: sv_book" in kb_index
    assert "document-specific questions" in exported_text
    assert not (tmp_path / "knowledge" / "protocols").exists()


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


def test_reingest_preserves_visual_chunks_and_fts(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "ingest": {"allowed_extensions": [".md"]},
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    result = service.ingest_upload_bytes(
        "visual-source.md",
        b"# Visual Source\n\nOriginal ordinary text.",
        title="Visual Source",
    )
    document_id = result["document"]["id"]
    version_id = result["document"]["version_id"]
    storage = service._backend._get_storage(writable=True)
    artifact = VisualArtifactCandidate(
        id="visual_preserve_1",
        document_id=document_id,
        version_id=version_id,
        kb_id="kb_default",
        artifact_type="figure",
        page=1,
        label="Figure 1",
        caption="Figure 1. Preserved visual",
        bbox={"x0": 1, "y0": 2, "x1": 3, "y1": 4},
        image_path="",
        image_hash="prehash",
        context_hash="ctx",
        parser="fake",
        parser_confidence=0.9,
    )
    storage.upsert_visual_artifact(artifact)
    visual_chunk = KnowledgeChunk(
        id="visual_chunk_preserve_1",
        document_id=document_id,
        ordinal=99,
        page_start=1,
        page_end=1,
        text="[视觉图表]\nPreserved visual key fact: fts_visual_preserve_fact",
        kb_id="kb_default",
        version_id=version_id,
        source_span_ids=["visual_span_preserve_1"],
        metadata={"source": "visual_analysis"},
    )
    visual_span = SourceSpan(
        id="visual_span_preserve_1",
        document_id=document_id,
        version_id=version_id,
        source_file="visual-source.md",
        page_start=1,
        page_end=1,
        text="fts_visual_preserve_fact",
    )
    storage.append_visual_chunks(document_id, version_id, artifact.id, [visual_chunk], [visual_span])
    storage.complete_visual_artifact_success(
        artifact.id,
        VisualAnalysisResult(
            artifact_type="figure",
            title="Preserved",
            summary="fts_visual_preserve_fact",
            structured_markdown="",
            key_facts=[{"fact": "fts_visual_preserve_fact", "confidence": 0.9}],
            confidence={"overall": 0.9, "ocr": 0.9, "structure": 0.9, "semantic": 0.9},
            should_index=True,
        ).to_dict(),
        0.9,
        retrievable=True,
    )

    replacement_chunk = KnowledgeChunk(
        id=stable_chunk_id(document_id, 1, "Replacement ordinary text."),
        document_id=document_id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="Replacement ordinary text.",
        kb_id="kb_default",
        version_id=version_id,
        source_span_ids=[stable_span_id(document_id, 1, "Replacement ordinary text.")],
    )
    replacement_span = SourceSpan(
        id=replacement_chunk.source_span_ids[0],
        document_id=document_id,
        version_id=version_id,
        source_file="visual-source.md",
        page_start=1,
        page_end=1,
        text="Replacement ordinary text.",
    )
    document = storage.get_document(document_id)
    storage.save_document(
        replace(document, metadata={**document.metadata, "page_count": 1}),
        [replacement_chunk],
        source_spans=[replacement_span],
    )

    chunks = storage.list_chunks(document_id)
    assert any(chunk.metadata.get("source") == "visual_analysis" for chunk in chunks)
    assert not any("Original ordinary text" in chunk.text for chunk in chunks if chunk.metadata.get("source") != "visual_analysis")
    assert any("Replacement ordinary text" in chunk.text for chunk in chunks)
    assert storage.list_visual_artifacts(document_id=document_id)[0]["analysis_status"] == "succeeded"
    assert storage.conn.execute("SELECT COUNT(*) FROM visual_artifact_chunks").fetchone()[0] >= 1
    assert service.search("fts_visual_preserve_fact", limit=5)


def test_reingest_same_path_new_version_removes_old_visual_chunks(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "ingest": {"allowed_extensions": [".md"]},
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    doc_path = tmp_path / "doc.md"
    doc_path.write_text("# Doc\n\nOriginal ordinary text.", encoding="utf-8")
    service.ingest_path(doc_path)
    document = next(item for item in service.list_documents() if item["title"] == "doc")
    document_id = document["id"]
    old_version_id = document["version_id"]
    storage = service._backend._get_storage(writable=True)
    artifact = VisualArtifactCandidate(
        id="visual_old_version_1",
        document_id=document_id,
        version_id=old_version_id,
        kb_id="kb_default",
        artifact_type="figure",
        page=1,
        label="Figure 1",
        caption="Figure 1. Old visual",
        bbox={"x0": 1, "y0": 2, "x1": 3, "y1": 4},
        image_path="",
        image_hash="old-prehash",
        context_hash="ctx",
        parser="fake",
        parser_confidence=0.9,
    )
    storage.upsert_visual_artifact(artifact)
    visual_chunk = KnowledgeChunk(
        id="visual_chunk_old_version_1",
        document_id=document_id,
        ordinal=99,
        page_start=1,
        page_end=1,
        text="[视觉图表]\nold_visual_fact",
        kb_id="kb_default",
        version_id=old_version_id,
        source_span_ids=["visual_span_old_version_1"],
        metadata={"source": "visual_analysis"},
    )
    visual_span = SourceSpan(
        id="visual_span_old_version_1",
        document_id=document_id,
        version_id=old_version_id,
        source_file=str(doc_path),
        page_start=1,
        page_end=1,
        text="old_visual_fact",
    )
    storage.append_visual_chunks(document_id, old_version_id, artifact.id, [visual_chunk], [visual_span])
    storage.complete_visual_artifact_success(
        artifact.id,
        VisualAnalysisResult(
            artifact_type="figure",
            title="Old",
            summary="old_visual_fact",
            structured_markdown="",
            key_facts=[{"fact": "old_visual_fact", "confidence": 0.9}],
            confidence={"overall": 0.9, "ocr": 0.9, "structure": 0.9, "semantic": 0.9},
            should_index=True,
        ).to_dict(),
        0.9,
        retrievable=True,
    )
    assert service.search("old_visual_fact", limit=5)

    doc_path.write_text("# Doc\n\nReplacement ordinary text.", encoding="utf-8")
    service.ingest_path(doc_path)
    updated = next(item for item in service.list_documents() if item["id"] == document_id)

    assert updated["version_id"] != old_version_id
    assert service.search("old_visual_fact", limit=5) == []
    chunks = storage.list_chunks(document_id)
    assert not any(chunk.version_id == old_version_id and chunk.metadata.get("source") == "visual_analysis" for chunk in chunks)
    old_artifact = storage.get_visual_artifact(artifact.id)
    assert old_artifact["retrievable"] is False
    assert any("Replacement ordinary text" in chunk.text for chunk in chunks if chunk.metadata.get("source") != "visual_analysis")


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


def test_complete_visual_knowledge_all_kbs_and_kb_filter(monkeypatch, tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    docs = [
        KnowledgeDocument(id="doc-a", title="A", source_path="a.pdf", mime_type="application/pdf", size=1, content_hash="a", status="ready", kb_id="kb_a", version_id="v-a"),
        KnowledgeDocument(id="doc-b", title="B", source_path="b.pdf", mime_type="application/pdf", size=1, content_hash="b", status="ready", kb_id="kb_b", version_id="v-b"),
        KnowledgeDocument(id="doc-generated", title="Generated", source_path="g.md", mime_type="text/markdown", size=1, content_hash="g", status="ready", kb_id="kb_a", doc_type="llm_study", version_id="v-g"),
    ]
    for document in docs:
        storage.save_document(document, [])

    calls = []

    def fake_build(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "has_more": False,
            "prepare": {"status": "done"},
            "group_succeeded": 1,
            "group_merge_strategy": "codex_text_merge",
            "group_merge_fallback_reason": "multi image unavailable",
            "group_merge_backend": "codex",
            "group_merge_model": "gpt-5.5",
        }

    monkeypatch.setattr(service, "build_visual_knowledge", fake_build)

    all_result = service.complete_visual_knowledge(max_steps=10, export=False)
    calls.clear()
    kb_result = service.complete_visual_knowledge(kb_id="kb_b", max_steps=10, export=False)

    assert all_result["scope"] == "all_source_documents"
    assert all_result["kb_id"] == ""
    assert all_result["documents_processed"] == 2
    assert {item["document_id"] for item in all_result["results"]} == {"doc-a", "doc-b"}
    assert all_result["group_merge_strategy"] == "codex_text_merge"
    assert all_result["group_merge_fallback_reason"] == "multi image unavailable"
    assert kb_result["scope"] == "kb"
    assert kb_result["documents_processed"] == 1
    assert calls[0]["document_id"] == "doc-b"


def test_complete_visual_knowledge_rejects_generated_document_id(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    generated = KnowledgeDocument(
        id="generated-doc",
        title="Generated",
        source_path="generated.md",
        mime_type="text/markdown",
        size=1,
        content_hash="generated",
        status="ready",
        kb_id="kb_default",
        doc_type="codex_analysis",
        version_id="generated-v1",
    )
    storage.save_document(generated, [])

    result = service.complete_visual_knowledge(document_id=generated.id, max_steps=5, export=False)

    assert result["ok"] is False
    assert "source document not found" in result["message"]


def test_legacy_visual_repair_all_source_documents_excludes_generated_and_returns_summaries(monkeypatch, tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    docs = [
        _source_document("legacy-a", tmp_path, kb_id="kb_a"),
        _source_document("legacy-b", tmp_path, kb_id="kb_b"),
        _source_document("legacy-generated", tmp_path, kb_id="kb_a", doc_type="llm_study"),
    ]
    for document in docs:
        storage.save_document(document, [_polluted_chunk(document)], source_spans=[_polluted_span(document)])

    monkeypatch.setattr(
        service,
        "complete_visual_knowledge",
        lambda **kwargs: {"ok": True, "stopped_reason": "completed", "errors": [], "results": []},
    )

    result = service.complete_and_repair_legacy_visual_knowledge(max_steps=5)

    assert result["scope"] == "all_source_documents"
    assert {item["document_id"] for item in result["source_documents"]} == {"legacy-a", "legacy-b"}
    assert result["visual_completion"]["stopped_reason"] == "completed"
    assert result["repair_summary"]["selected_documents"] == 2
    assert result["repair_summary"]["skipped_chunks_without_visual_replacement"] == 2


def test_legacy_visual_repair_dry_run_default_does_not_write_db(monkeypatch, tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    document = _source_document("legacy-dry", tmp_path)
    storage.save_document(document, [_polluted_chunk(document)], source_spans=[_polluted_span(document)])
    _add_visual_replacement(storage, document)
    monkeypatch.setattr(
        service,
        "complete_visual_knowledge",
        lambda **kwargs: {"ok": True, "stopped_reason": "completed", "errors": [], "results": []},
    )

    result = service.complete_and_repair_legacy_visual_knowledge(document_id=document.id)

    assert result["dry_run"] is True
    assert result["repair_summary"]["candidate_chunks_to_strip"] == 1
    storage = service._backend._get_storage(writable=True)
    assert storage.search("Vr", limit=5)
    assert any("L f( )" in chunk.text for chunk in storage.list_chunks(document.id) if chunk.metadata.get("source") != "visual_analysis")


def test_legacy_visual_repair_apply_strip_requires_high_confidence_visual_replacement(monkeypatch, tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    high_doc = _source_document("legacy-high", tmp_path, kb_id="kb_high")
    low_doc = _source_document("legacy-low", tmp_path, kb_id="kb_low")
    for document in (high_doc, low_doc):
        storage.save_document(document, [_polluted_chunk(document)], source_spans=[_polluted_span(document)])
    _add_visual_replacement(storage, high_doc)
    _add_visual_replacement(storage, low_doc, confidence=0.3, retrievable=False)
    monkeypatch.setattr(
        service,
        "complete_visual_knowledge",
        lambda **kwargs: {"ok": True, "stopped_reason": "completed", "errors": [], "results": []},
    )

    high_result = service.complete_and_repair_legacy_visual_knowledge(
        document_id=high_doc.id,
        apply=True,
        strip_completed_visual_regions=True,
    )
    low_result = service.complete_and_repair_legacy_visual_knowledge(
        document_id=low_doc.id,
        apply=True,
        strip_completed_visual_regions=True,
    )

    assert high_result["applied"] is True
    assert high_result["repair_summary"]["stripped_completed_visual_chunks"] == 1
    storage = service._backend._get_storage(writable=True)
    assert not any("L f( )" in chunk.text for chunk in storage.list_chunks(high_doc.id) if chunk.metadata.get("source") != "visual_analysis")
    assert low_result["repair_summary"]["stripped_completed_visual_chunks"] == 0
    assert low_result["repair_summary"]["skipped_chunks_without_visual_replacement"] == 1
    assert any("L f( )" in chunk.text for chunk in storage.list_chunks(low_doc.id) if chunk.metadata.get("source") != "visual_analysis")


def test_legacy_visual_repair_blocks_destructive_apply_when_visual_completion_incomplete(monkeypatch, tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "visual_analysis": {"enabled": True},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    document = _source_document("legacy-blocked", tmp_path)
    storage.save_document(document, [_polluted_chunk(document)], source_spans=[_polluted_span(document)])
    _add_visual_replacement(storage, document)
    monkeypatch.setattr(
        service,
        "complete_visual_knowledge",
        lambda **kwargs: {"ok": True, "stopped_reason": "max_steps", "errors": [], "results": []},
    )

    result = service.complete_and_repair_legacy_visual_knowledge(
        document_id=document.id,
        apply=True,
        strip_completed_visual_regions=True,
        max_steps=1,
    )

    assert result["destructive_repair_blocked"] is True
    assert result["dry_run"] is True
    assert result["repair"]["mode"] == "dry-run"
    storage = service._backend._get_storage(writable=True)
    assert any("L f( )" in chunk.text for chunk in storage.list_chunks(document.id) if chunk.metadata.get("source") != "visual_analysis")


def test_analyze_visual_artifact_group_returns_resolved_backend_metadata(tmp_path):
    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "visual_analysis": {"enabled": True, "use_current_model": False, "model": "fixed-vision-model"},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )
    storage = service._backend._get_storage(writable=True)
    document = KnowledgeDocument(id="doc-group", title="Group", source_path="group.pdf", mime_type="application/pdf", size=1, content_hash="group", status="ready", kb_id="kb_default", version_id="v-group")
    storage.save_document(document, [])
    group_id = "visual_group_meta"
    storage.upsert_visual_artifact_group(
        {
            "id": group_id,
            "document_id": document.id,
            "version_id": document.version_id,
            "kb_id": "kb_default",
            "group_type": "table",
            "title": "meta",
            "caption": "Table 1. meta",
            "source_pages": [1],
            "status": "skipped",
            "confidence": 0.0,
            "result_json": {},
        }
    )

    result = service.analyze_visual_artifact_group(group_id, analysis_backend="codex")

    assert result["outcome"] == "skipped"
    assert result["analysis_backend"] == "codex"
    assert result["requested_analysis_backend"] == "codex"
    assert result["analysis_model"] == "fixed-vision-model"


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
