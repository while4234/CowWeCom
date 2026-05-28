import json
import sqlite3
from pathlib import Path

import pytest

from agent.knowledge.backend import KnowledgeBackendConfig
from agent.knowledge.backend.models import (
    DocumentPage,
    ExtractedDocument,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeRelation,
    SourceSpan,
    VisualAnalysisResult,
    VisualArtifactCandidate,
)
from agent.knowledge.backend.storage import KnowledgeStorage, stable_entity_id, stable_relation_id
from scripts import repair_knowledge_text_chunks as repair_script


POLLUTION = "L a y e r 1 0 1 2 3 rxdatasbtxdatasb txcksb rxcksb"
CAPTION = "Figure 5-34. Standard Package x16 interface: Signal exit order"
CLEAN_TEXT = f"{CAPTION}\nClean package prose about UCIe sideband initialization."


class FailingStorage(KnowledgeStorage):
    def upsert_entity(self, entity):
        raise RuntimeError("forced entity failure")


def _options(tmp_path, db_path, **overrides):
    values = {
        "db_path": Path(db_path),
        "workspace_root": tmp_path,
        "data_dir": tmp_path / "public_protocol_knowledge",
        "document_id": "",
        "kb_id": "",
        "all_documents": False,
        "apply": False,
        "export": False,
        "backup_path": None,
    }
    values.update(overrides)
    return repair_script.RepairOptions(**values)


def _pdf(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% fake test pdf\n")
    return path


def _document(document_id, pdf_path, kb_id="ucie"):
    return KnowledgeDocument(
        id=document_id,
        title=f"{kb_id.upper()} Legacy PDF",
        source_path=str(pdf_path),
        mime_type="application/pdf",
        size=pdf_path.stat().st_size,
        content_hash=f"hash-{document_id}",
        status="ready",
        kb_id=kb_id,
        version_id=f"version-{document_id}",
    )


def _ordinary_chunk(document, text=POLLUTION):
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


def _ordinary_span(document, text=POLLUTION):
    return SourceSpan(
        id=f"span-{document.id}-ordinary",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text=text,
    )


def _visual_candidate(document, artifact_id="visual-artifact-1", status_page=2):
    return VisualArtifactCandidate(
        id=artifact_id,
        document_id=document.id,
        version_id=document.version_id,
        kb_id=document.kb_id,
        artifact_type="figure",
        page=status_page,
        label="Figure 5-34",
        caption=CAPTION,
        bbox={"x0": 1, "y0": 2, "x1": 100, "y1": 120},
        image_hash=f"image-{artifact_id}",
        context_hash=f"context-{artifact_id}",
        parser="test",
        parser_confidence=0.9,
        source_path=document.source_path,
    )


def _visual_chunk(document, text="High-confidence visual summary."):
    return KnowledgeChunk(
        id=f"chunk-{document.id}-visual",
        document_id=document.id,
        ordinal=99,
        page_start=2,
        page_end=2,
        text=text,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[f"span-{document.id}-visual"],
        metadata={"source": "visual_analysis"},
    )


def _visual_span(document, text="High-confidence visual summary."):
    return SourceSpan(
        id=f"span-{document.id}-visual",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=2,
        page_end=2,
        text=text,
    )


def _visual_chunk_with_ids(document, chunk_id, span_id, artifact_id, text="Visual append text"):
    return KnowledgeChunk(
        id=chunk_id,
        document_id=document.id,
        ordinal=99,
        page_start=2,
        page_end=2,
        text=text,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[span_id],
        metadata={"source": "visual_analysis", "visual_scope": "page", "visual_artifact_id": artifact_id},
    )


def _visual_span_with_id(document, span_id, text="Visual append text"):
    return SourceSpan(
        id=span_id,
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=2,
        page_end=2,
        text=text,
    )


def _upsert_graph_refs(storage, document, entity_id, canonical_name, span_id):
    storage.upsert_entity(
        KnowledgeEntity(
            id=entity_id,
            canonical_name=canonical_name,
            entity_type="test",
            source_span_ids=[span_id],
        )
    )
    storage.upsert_relation(
        KnowledgeRelation(
            id=stable_relation_id(entity_id, "mentions", "entity-target", [span_id]),
            subject_entity_id=entity_id,
            predicate="mentions",
            object_entity_id="entity-target",
            subject=canonical_name,
            object="Target",
            source_kb_id=document.kb_id,
            evidence_span_ids=[span_id],
            metadata={"kept": True},
        ),
        source_doc_id=document.id,
    )
    storage.conn.commit()


def _seed_document(db_path, document, *, with_visual=True, with_low_confidence=False):
    storage = KnowledgeStorage(db_path)
    try:
        chunk = _ordinary_chunk(document)
        storage.save_document(document, [chunk], source_spans=[_ordinary_span(document)])
        if with_visual:
            artifact = _visual_candidate(document)
            visual_chunk = _visual_chunk(document)
            storage.upsert_visual_artifact(artifact)
            storage.append_visual_chunks(document.id, document.version_id, artifact.id, [visual_chunk], [_visual_span(document)])
            storage.complete_visual_artifact_success(
                artifact.id,
                VisualAnalysisResult(
                    artifact_type="figure",
                    title="Signal exit order",
                    caption=CAPTION,
                    page=2,
                    summary="High-confidence visual summary.",
                    structured_markdown="",
                    key_facts=[{"fact": "Visual fact remains available", "confidence": 0.91}],
                    confidence={"overall": 0.91, "ocr": 0.91, "structure": 0.91, "semantic": 0.91},
                    should_index=True,
                ).to_dict(),
                0.91,
                retrievable=True,
            )
        if with_low_confidence:
            low = _visual_candidate(document, artifact_id=f"low-{document.id}", status_page=3)
            storage.upsert_visual_artifact(low)
            storage.complete_visual_artifact_low_confidence(
                low.id,
                {"low_confidence_reason": "test low confidence"},
                0.3,
                "test low confidence",
            )
        storage.conn.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        storage.close()


def _patch_extract_and_sanitize(monkeypatch, sanitizer_text=CLEAN_TEXT):
    def fake_extract(path):
        return ExtractedDocument(
            title=Path(path).stem,
            source_path=str(path),
            mime_type="application/pdf",
            pages=[DocumentPage(page=1, text=POLLUTION)],
        )

    def fake_sanitize(source_path, pages, **kwargs):
        return [DocumentPage(page=1, text=sanitizer_text)], {"removed_total_lines": 2, "pages": [{"page": 1}]}

    monkeypatch.setattr(repair_script, "extract_document", fake_extract)
    monkeypatch.setattr(repair_script, "sanitize_pages_for_knowledge_chunks", fake_sanitize)


def _chunk_texts(db_path, document_id):
    storage = KnowledgeStorage(db_path)
    try:
        return [chunk.text for chunk in storage.list_chunks(document_id)]
    finally:
        storage.close()


def _chunks(db_path, document_id):
    storage = KnowledgeStorage(db_path)
    try:
        return storage.list_chunks(document_id)
    finally:
        storage.close()


def test_dry_run_does_not_modify_db(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-dry", _pdf(tmp_path, "dry.pdf"))
    _seed_document(db_path, document)
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id))

    assert report["mode"] == "dry-run"
    assert report["summary"]["old_text_chunks"] == 1
    assert report["summary"]["new_text_chunks"] == 1
    assert any("L a y e r 1 0 1 2 3" in item for item in report["documents"][0]["removed_noise_line_examples"])
    assert any(POLLUTION in text for text in _chunk_texts(db_path, document.id))
    assert report["backup_path"] == ""


def test_apply_removes_polluted_text_keeps_caption_visual_chunk_and_mapping(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-apply", _pdf(tmp_path, "apply.pdf"))
    _seed_document(db_path, document)
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id, apply=True))

    assert report["mode"] == "apply"
    assert Path(report["backup_path"]).is_file()
    chunks = _chunks(db_path, document.id)
    ordinary_text = "\n".join(chunk.text for chunk in chunks if chunk.metadata.get("source") != "visual_analysis")
    assert "L a y e r 1 0 1 2 3" not in ordinary_text
    assert "rxdatasbtxdatasb" not in ordinary_text
    assert CAPTION in ordinary_text

    visual = next(chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis")
    assert visual.id == f"chunk-{document.id}-visual"
    assert visual.text == "High-confidence visual summary."
    storage = KnowledgeStorage(db_path)
    try:
        mapping_count = storage.conn.execute(
            "SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?",
            (visual.id,),
        ).fetchone()[0]
        assert mapping_count == 1
        assert storage.conn.execute("SELECT COUNT(*) FROM visual_artifacts").fetchone()[0] == 1
    finally:
        storage.close()


def test_apply_handles_legacy_invalid_json_chunk_metadata(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-bad-json", _pdf(tmp_path, "bad-json.pdf"))
    _seed_document(db_path, document)
    storage = KnowledgeStorage(db_path)
    try:
        storage.conn.execute(
            "UPDATE chunks SET metadata = ? WHERE id = ?",
            ("not-json", f"chunk-{document.id}-ordinary"),
        )
        storage.conn.commit()
    finally:
        storage.close()
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id, apply=True))

    assert report["ok"] is True
    ordinary = [chunk for chunk in _chunks(db_path, document.id) if chunk.metadata.get("source") != "visual_analysis"]
    assert ordinary
    assert not any(POLLUTION in chunk.text for chunk in ordinary)


def test_apply_rebuilds_fts_without_pollution(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-fts", _pdf(tmp_path, "fts.pdf"))
    _seed_document(db_path, document)
    _patch_extract_and_sanitize(monkeypatch)

    repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id, apply=True))

    storage = KnowledgeStorage(db_path)
    try:
        assert storage.search("rxdatasbtxdatasb", limit=5) == []
        assert storage.search("Standard Package x16 interface", limit=5)
    finally:
        storage.close()


def test_export_markdown_omits_pollution_and_keeps_visual_sections(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-export", _pdf(tmp_path, "export.pdf"))
    _seed_document(db_path, document, with_low_confidence=True)
    _patch_extract_and_sanitize(monkeypatch)
    monkeypatch.setattr(
        repair_script,
        "load_project_backend_config",
        lambda: KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(db_path),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "public_protocol_knowledge"),
                "ingest": {"document_library_root": str(tmp_path), "document_library_category": "documents"},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        ),
    )

    report = repair_script.run_repair(
        _options(tmp_path, db_path, document_id=document.id, apply=True, export=True)
    )

    export = report["documents"][0]["export"]
    exported_path = tmp_path / export["documents"][0]["path"]
    markdown = exported_path.read_text(encoding="utf-8")
    source_section = markdown.split("## Source Chunks", 1)[1].split("\n## ", 1)[0]
    assert "L a y e r 1 0 1 2 3" not in source_section
    assert "rxdatasbtxdatasb" not in source_section
    assert CAPTION in source_section
    assert "High-confidence visual summary." in markdown
    assert "test low confidence" in markdown


def test_document_id_repairs_only_selected_document(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    doc1 = _document("doc-one", _pdf(tmp_path, "one.pdf"))
    doc2 = _document("doc-two", _pdf(tmp_path, "two.pdf"))
    _seed_document(db_path, doc1, with_visual=False)
    _seed_document(db_path, doc2, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)

    repair_script.run_repair(_options(tmp_path, db_path, document_id=doc2.id, apply=True))

    assert any(POLLUTION in text for text in _chunk_texts(db_path, doc1.id))
    assert not any(POLLUTION in text for text in _chunk_texts(db_path, doc2.id))


def test_kb_id_repairs_only_matching_kb(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    ucie_doc = _document("doc-ucie", _pdf(tmp_path, "ucie.pdf"), kb_id="ucie")
    pcie_doc = _document("doc-pcie", _pdf(tmp_path, "pcie.pdf"), kb_id="pcie")
    _seed_document(db_path, ucie_doc, with_visual=False)
    _seed_document(db_path, pcie_doc, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, kb_id="ucie", apply=True))

    assert report["summary"]["selected_documents"] == 1
    assert not any(POLLUTION in text for text in _chunk_texts(db_path, ucie_doc.id))
    assert any(POLLUTION in text for text in _chunk_texts(db_path, pcie_doc.id))


def test_apply_creates_backup_and_report(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-backup", _pdf(tmp_path, "backup.pdf"))
    _seed_document(db_path, document, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id, apply=True))

    backup_path = Path(report["backup_path"])
    report_path = Path(report["report_path"])
    assert backup_path.name.startswith("kb.sqlite.backup-")
    assert backup_path.is_file()
    assert report_path.is_file()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["backup_path"] == str(backup_path)
    assert payload["report_path"] == str(report_path)


def test_write_report_avoids_overwriting_same_second_path(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    options = _options(tmp_path, db_path)
    started_at = 1_800_000_000

    first = repair_script.write_report(options, {"ok": True, "summary": {}}, started_at)
    second_report = {"ok": True, "summary": {}}
    second = repair_script.write_report(options, second_report, started_at)

    assert first != second
    assert first.is_file()
    assert second.is_file()
    payload = json.loads(second.read_text(encoding="utf-8"))
    assert payload["report_path"] == str(second)
    assert second_report["report_path"] == str(second)


def test_report_data_dir_prefers_db_layout_over_mismatched_config(monkeypatch, tmp_path):
    protocol_dir = tmp_path / "public_protocol_knowledge"
    db_path = protocol_dir / "indexes" / "kb.sqlite"
    document = _document("doc-report-dir", _pdf(tmp_path, "report-dir.pdf"))
    db_path.parent.mkdir(parents=True)
    _seed_document(db_path, document, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)
    monkeypatch.setattr(
        repair_script,
        "load_project_backend_config",
        lambda: KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "public_document_knowledge" / "indexes" / "kb.sqlite"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "public_document_knowledge"),
                "vector_store": {"provider": "sqlite", "required": False},
            }
        ),
    )

    options = repair_script.repair_options_from_args(
        repair_script.parse_args(
            [
                "--db",
                str(db_path),
                "--document-id",
                document.id,
                "--dry-run",
                "--workspace-root",
                str(tmp_path),
            ]
        ),
        tmp_path,
    )
    report = repair_script.run_repair(options)

    assert report["data_dir"] == str(protocol_dir.resolve())
    assert Path(report["report_path"]).parent == protocol_dir.resolve() / "reports"


def test_report_data_dir_prefers_db_layout_even_when_config_sqlite_matches_but_data_dir_wrong(monkeypatch, tmp_path):
    protocol_dir = tmp_path / "public_protocol_knowledge"
    db_path = protocol_dir / "indexes" / "kb.sqlite"
    document = _document("doc-config-mismatch-data-dir", _pdf(tmp_path, "config-mismatch-data-dir.pdf"))
    db_path.parent.mkdir(parents=True)
    _seed_document(db_path, document, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)
    monkeypatch.setattr(
        repair_script,
        "load_project_backend_config",
        lambda: KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(db_path),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "wrong_data_dir"),
                "vector_store": {"provider": "sqlite", "required": False},
            }
        ),
    )

    options = repair_script.repair_options_from_args(
        repair_script.parse_args(
            [
                "--db",
                str(db_path),
                "--document-id",
                document.id,
                "--dry-run",
                "--workspace-root",
                str(tmp_path),
            ]
        ),
        tmp_path,
    )
    report = repair_script.run_repair(options)

    assert report["data_dir"] == str(protocol_dir.resolve())
    assert Path(report["report_path"]).parent == protocol_dir.resolve() / "reports"


def test_explicit_data_dir_wins_over_db_layout(monkeypatch, tmp_path):
    protocol_dir = tmp_path / "public_protocol_knowledge"
    explicit_dir = tmp_path / "explicit-data"
    db_path = protocol_dir / "indexes" / "kb.sqlite"
    document = _document("doc-explicit-dir", _pdf(tmp_path, "explicit-dir.pdf"))
    db_path.parent.mkdir(parents=True)
    _seed_document(db_path, document, with_visual=False)
    _patch_extract_and_sanitize(monkeypatch)

    options = repair_script.repair_options_from_args(
        repair_script.parse_args(
            [
                "--db",
                str(db_path),
                "--document-id",
                document.id,
                "--dry-run",
                "--workspace-root",
                str(tmp_path),
                "--data-dir",
                str(explicit_dir),
            ]
        ),
        tmp_path,
    )
    report = repair_script.run_repair(options)

    assert report["data_dir"] == str(explicit_dir.resolve())
    assert Path(report["report_path"]).parent == explicit_dir.resolve() / "reports"


def test_missing_document_id_fails_before_backup(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-present", _pdf(tmp_path, "present.pdf"))
    _seed_document(db_path, document, with_visual=False)

    report = repair_script.run_repair(
        _options(tmp_path, db_path, document_id="missing-doc", apply=True)
    )

    assert report["ok"] is False
    assert "document not found" in report["error"]
    assert report["backup_path"] == ""
    assert not list(tmp_path.glob("kb.sqlite.backup-*"))


def test_main_prints_report_error_for_missing_selector(monkeypatch, capsys, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-present-cli", _pdf(tmp_path, "present-cli.pdf"))
    _seed_document(db_path, document, with_visual=False)
    original_parse_args = repair_script.parse_args
    monkeypatch.setattr(
        repair_script,
        "parse_args",
        lambda: original_parse_args(
            [
                "--db",
                str(db_path),
                "--document-id",
                "missing-doc",
                "--dry-run",
                "--workspace-root",
                str(tmp_path),
            ]
        ),
    )

    exit_code = repair_script.main()

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output["ok"] is False
    assert output["message"] == ""
    assert output["error"] == "document not found: missing-doc"
    assert output["report_path"]


def test_missing_kb_id_fails_before_backup(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-present-kb", _pdf(tmp_path, "present-kb.pdf"), kb_id="ucie")
    _seed_document(db_path, document, with_visual=False)

    report = repair_script.run_repair(
        _options(tmp_path, db_path, kb_id="missing-kb", apply=True)
    )

    assert report["ok"] is False
    assert "no documents found for kb_id" in report["error"]
    assert report["backup_path"] == ""
    assert not list(tmp_path.glob("kb.sqlite.backup-*"))


def test_dry_run_reads_committed_wal_changes(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    first = _document("doc-first", _pdf(tmp_path, "first.pdf"))
    _seed_document(db_path, first, with_visual=False)
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        second_pdf = _pdf(tmp_path, "wal.pdf")
        writer.execute(
            """
            INSERT INTO documents(
                id, title, source_path, mime_type, size, content_hash, status, error,
                kb_id, doc_type, version_id, metadata, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'ready', '', 'ucie', 'document', ?, '{}', 1)
            """,
            (
                "doc-wal",
                "WAL Doc",
                str(second_pdf),
                "application/pdf",
                second_pdf.stat().st_size,
                "hash-doc-wal",
                "version-doc-wal",
            ),
        )
        writer.commit()
    finally:
        writer.close()
    _patch_extract_and_sanitize(monkeypatch)

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id="doc-wal"))

    assert report["ok"] is True
    assert report["summary"]["selected_documents"] == 1
    assert report["documents"][0]["document_id"] == "doc-wal"


def test_save_document_preserves_span_referenced_by_visual_chunk(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-shared-span", _pdf(tmp_path, "shared-span.pdf"))
    shared_span = SourceSpan(
        id="span-shared",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="shared span text",
    )
    ordinary = KnowledgeChunk(
        id="chunk-shared-ordinary",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text=POLLUTION,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    visual = KnowledgeChunk(
        id="chunk-shared-visual",
        document_id=document.id,
        ordinal=2,
        page_start=1,
        page_end=1,
        text="visual text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
        metadata={"source": "visual_analysis"},
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [ordinary], source_spans=[shared_span])
        storage.append_visual_chunks(document.id, document.version_id, "artifact-shared", [visual], [])
        replacement = KnowledgeChunk(
            id="chunk-shared-replacement",
            document_id=document.id,
            ordinal=1,
            page_start=1,
            page_end=1,
            text=CLEAN_TEXT,
            kb_id=document.kb_id,
            version_id=document.version_id,
            source_span_ids=[],
        )
        storage.save_document(document, [replacement], source_spans=[])

        assert storage.get_source_span(shared_span.id) is not None
        visual_chunks = [chunk for chunk in storage.list_chunks(document.id) if chunk.metadata.get("source") == "visual_analysis"]
        assert visual_chunks[0].source_span_ids == [shared_span.id]
    finally:
        storage.close()


def test_save_document_remaps_new_span_when_preserved_visual_uses_same_span_id(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-shared-span-remap", _pdf(tmp_path, "shared-span-remap.pdf"))
    shared_span = SourceSpan(
        id="span-shared",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="old shared span text",
    )
    ordinary = KnowledgeChunk(
        id="chunk-shared-ordinary",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text=POLLUTION,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    visual = KnowledgeChunk(
        id="chunk-shared-visual",
        document_id=document.id,
        ordinal=2,
        page_start=1,
        page_end=1,
        text="visual text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
        metadata={"source": "visual_analysis"},
    )
    replacement_span = SourceSpan(
        id=shared_span.id,
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="new ordinary repaired span text",
    )
    replacement = KnowledgeChunk(
        id="chunk-shared-replacement",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text=CLEAN_TEXT,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    entity = KnowledgeEntity(
        id="entity-shared",
        canonical_name="SharedSignal",
        entity_type="signal",
        source_span_ids=[shared_span.id],
    )
    relation = KnowledgeRelation(
        id=stable_relation_id("entity-shared", "mentions", "entity-target", [shared_span.id]),
        subject_entity_id="entity-shared",
        predicate="mentions",
        object_entity_id="entity-target",
        subject="SharedSignal",
        object="Target",
        source_kb_id=document.kb_id,
        evidence_span_ids=[shared_span.id],
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [ordinary], source_spans=[shared_span])
        storage.append_visual_chunks(document.id, document.version_id, "artifact-shared", [visual], [])

        storage.save_document(
            document,
            [replacement],
            source_spans=[replacement_span],
            entities=[entity],
            relations=[relation],
        )

        chunks = storage.list_chunks(document.id)
        visual_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
        ordinary_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") != "visual_analysis"]
        remapped_span_id = ordinary_chunks[0].source_span_ids[0]

        assert visual_chunks[0].source_span_ids == [shared_span.id]
        assert storage.get_source_span(shared_span.id) is not None
        assert storage.get_source_span(shared_span.id).text == "old shared span text"
        assert remapped_span_id != shared_span.id
        assert storage.get_source_span(remapped_span_id) is not None
        assert storage.get_source_span(remapped_span_id).text == "new ordinary repaired span text"
        entity_rows = storage.list_entities(["SharedSignal"])
        assert entity_rows[0].source_span_ids == [remapped_span_id]
        relations = storage.list_relations(entity_id="entity-shared")
        assert relations[0].evidence_span_ids == [remapped_span_id]
        assert relations[0].id == stable_relation_id("entity-shared", "mentions", "entity-target", [remapped_span_id])
    finally:
        storage.close()


def test_save_document_deletes_unreferenced_conflicting_source_span_before_insert(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-orphan-conflict", _pdf(tmp_path, "orphan-conflict.pdf"))
    orphan_span = SourceSpan(
        id="new-span",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=7,
        page_end=7,
        text="orphan old span text",
    )
    visual_span = SourceSpan(
        id="visual-preserved-span",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=2,
        page_end=2,
        text="preserved visual span text",
    )
    visual_chunk = KnowledgeChunk(
        id="chunk-orphan-conflict-visual",
        document_id=document.id,
        ordinal=2,
        page_start=2,
        page_end=2,
        text="preserved visual chunk text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[visual_span.id],
        metadata={"source": "visual_analysis"},
    )
    replacement_span = SourceSpan(
        id=orphan_span.id,
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="new repaired span text",
    )
    replacement_chunk = KnowledgeChunk(
        id="chunk-orphan-conflict-new",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="new ordinary repaired text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[replacement_span.id],
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [_ordinary_chunk(document)], source_spans=[_ordinary_span(document)])
        storage.conn.execute(
            """
            INSERT INTO source_spans(
                id, document_id, version_id, source_file, page_start, page_end, section_path,
                paragraph_index_start, paragraph_index_end, char_start, char_end, bbox, text_hash, text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                orphan_span.id,
                orphan_span.document_id,
                orphan_span.version_id,
                orphan_span.source_file,
                orphan_span.page_start,
                orphan_span.page_end,
                orphan_span.section_path,
                orphan_span.paragraph_index_start,
                orphan_span.paragraph_index_end,
                orphan_span.char_start,
                orphan_span.char_end,
                json.dumps(orphan_span.bbox or {}, ensure_ascii=False),
                orphan_span.text_hash,
                orphan_span.text,
            ),
        )
        storage.append_visual_chunks(document.id, document.version_id, "artifact-orphan-conflict", [visual_chunk], [visual_span])

        storage.save_document(document, [replacement_chunk], source_spans=[replacement_span])

        chunks = storage.list_chunks(document.id)
        ordinary_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") != "visual_analysis"]
        visual_chunks = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
        assert [chunk.id for chunk in ordinary_chunks] == [replacement_chunk.id]
        assert storage.get_source_span(orphan_span.id).text == replacement_span.text
        assert visual_chunks[0].source_span_ids == [visual_span.id]
        assert storage.get_source_span(visual_span.id).text == visual_span.text
    finally:
        storage.close()


def test_save_document_remaps_new_span_when_other_document_uses_same_span_id(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    other_document = _document("doc-global-other", _pdf(tmp_path, "global-other.pdf"))
    document = _document("doc-global-current", _pdf(tmp_path, "global-current.pdf"))
    global_span = SourceSpan(
        id="global-conflict",
        document_id=other_document.id,
        version_id=other_document.version_id,
        source_file=other_document.source_path,
        page_start=1,
        page_end=1,
        text="other document span text",
    )
    other_chunk = KnowledgeChunk(
        id="chunk-global-other",
        document_id=other_document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="other document text",
        kb_id=other_document.kb_id,
        version_id=other_document.version_id,
        source_span_ids=[global_span.id],
    )
    replacement_span = SourceSpan(
        id=global_span.id,
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="current document repaired text",
    )
    replacement_chunk = KnowledgeChunk(
        id="chunk-global-current-new",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="current document repaired text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[replacement_span.id],
    )
    entity = KnowledgeEntity(
        id="entity-global-current",
        canonical_name="GlobalCurrent",
        entity_type="test",
        source_span_ids=[replacement_span.id],
    )
    relation = KnowledgeRelation(
        id=stable_relation_id("entity-global-current", "mentions", "entity-target", [replacement_span.id]),
        subject_entity_id="entity-global-current",
        predicate="mentions",
        object_entity_id="entity-target",
        subject="GlobalCurrent",
        object="Target",
        source_kb_id=document.kb_id,
        evidence_span_ids=[replacement_span.id],
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(other_document, [other_chunk], source_spans=[global_span])
        storage.save_document(document, [_ordinary_chunk(document)], source_spans=[_ordinary_span(document)])

        storage.save_document(
            document,
            [replacement_chunk],
            source_spans=[replacement_span],
            entities=[entity],
            relations=[relation],
        )

        assert storage.get_source_span(global_span.id).document_id == other_document.id
        assert storage.get_source_span(global_span.id).text == global_span.text
        current_chunks = [chunk for chunk in storage.list_chunks(document.id) if chunk.metadata.get("source") != "visual_analysis"]
        remapped_span_id = current_chunks[0].source_span_ids[0]
        assert remapped_span_id != global_span.id
        assert storage.get_source_span(remapped_span_id).document_id == document.id
        assert storage.get_source_span(remapped_span_id).text == replacement_span.text
        entity_rows = storage.list_entities(["GlobalCurrent"])
        assert entity_rows[0].source_span_ids == [remapped_span_id]
        relations = storage.list_relations(entity_id="entity-global-current")
        assert relations[0].evidence_span_ids == [remapped_span_id]
        assert relations[0].id == stable_relation_id("entity-global-current", "mentions", "entity-target", [remapped_span_id])
    finally:
        storage.close()


def test_save_document_commit_true_rolls_back_partial_writes_on_failure(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-atomic-save", _pdf(tmp_path, "atomic-save.pdf"))
    old_chunk = _ordinary_chunk(document)
    old_span = _ordinary_span(document)
    new_chunk = KnowledgeChunk(
        id="chunk-doc-atomic-save-new",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text=CLEAN_TEXT,
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=["span-doc-atomic-save-new"],
    )
    new_span = SourceSpan(
        id="span-doc-atomic-save-new",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text=CLEAN_TEXT,
    )
    entity = KnowledgeEntity(
        id="entity-forced-failure",
        canonical_name="ForcedFailure",
        entity_type="test",
        source_span_ids=[new_span.id],
    )

    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [old_chunk], source_spans=[old_span])
    finally:
        storage.close()

    storage = FailingStorage(db_path)
    try:
        try:
            storage.save_document(document, [new_chunk], source_spans=[new_span], entities=[entity])
            assert False, "save_document should raise the injected entity failure"
        except RuntimeError as exc:
            assert str(exc) == "forced entity failure"
    finally:
        storage.close()

    storage = KnowledgeStorage(db_path)
    try:
        chunks = storage.list_chunks(document.id)
        assert [chunk.id for chunk in chunks] == [old_chunk.id]
        assert storage.get_source_span(old_span.id) is not None
        assert storage.get_source_span(new_span.id) is None
        assert not storage.list_entities(["ForcedFailure"])
    finally:
        storage.close()


def test_append_visual_chunks_repeated_same_artifact_is_idempotent_without_orphans_or_duplicate_fts(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-visual-repeat", _pdf(tmp_path, "visual-repeat.pdf"))
    artifact_id = "artifact-visual-repeat"
    visual_chunk = _visual_chunk_with_ids(document, "chunk1", "span1", artifact_id, text="repeat visual text")
    visual_span = _visual_span_with_id(document, "span1", text="repeat visual span")
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [], source_spans=[])
        storage.upsert_visual_artifact(_visual_candidate(document, artifact_id=artifact_id))

        first_ids = storage.append_visual_chunks(document.id, document.version_id, artifact_id, [visual_chunk], [visual_span])
        second_ids = storage.append_visual_chunks(document.id, document.version_id, artifact_id, [visual_chunk], [visual_span])

        assert first_ids == ["chunk1"]
        assert second_ids == ["chunk1"]
        chunk = storage.list_chunks(document.id)[0]
        assert chunk.id == "chunk1"
        assert chunk.source_span_ids == ["span1"]
        assert storage.get_source_span("span1") is not None
        assert storage.get_source_span("span1-repair-1") is None
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM chunks WHERE id = 'chunk1'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM source_spans WHERE id = 'span1'").fetchone()[0] == 1
            assert conn.execute(
                """
                SELECT COUNT(*)
                FROM source_spans s
                WHERE NOT EXISTS (
                  SELECT 1
                  FROM chunks c, json_each(c.source_span_ids) j
                  WHERE j.value = s.id
                )
                """
            ).fetchone()[0] == 0
            if storage.fts5_available:
                assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunk_id = 'chunk1'").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM visual_artifact_chunks WHERE artifact_id = ? AND chunk_id = 'chunk1'",
                (artifact_id,),
            ).fetchone()[0] == 1
    finally:
        storage.close()


def test_append_visual_chunks_remaps_conflict_without_overwriting_ordinary_chunk(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-visual-ordinary-conflict", _pdf(tmp_path, "visual-ordinary-conflict.pdf"))
    ordinary_span = _ordinary_span(document, text="ordinary span text")
    ordinary_chunk = KnowledgeChunk(
        id="shared-chunk-id",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="ordinary stable text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[ordinary_span.id],
        metadata={"kind": "ordinary"},
    )
    artifact_id = "artifact-ordinary-conflict"
    visual_span = _visual_span_with_id(document, "visual-span-shared-chunk", text="visual span text")
    visual_chunk = _visual_chunk_with_ids(
        document,
        "shared-chunk-id",
        visual_span.id,
        artifact_id,
        text="visual remapped text",
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [ordinary_chunk], source_spans=[ordinary_span])
        storage.upsert_visual_artifact(_visual_candidate(document, artifact_id=artifact_id))

        chunk_ids = storage.append_visual_chunks(document.id, document.version_id, artifact_id, [visual_chunk], [visual_span])

        assert chunk_ids == ["shared-chunk-id-repair-1"]
        chunks = {chunk.id: chunk for chunk in storage.list_chunks(document.id)}
        assert chunks["shared-chunk-id"].text == "ordinary stable text"
        assert chunks["shared-chunk-id"].source_span_ids == [ordinary_span.id]
        assert chunks["shared-chunk-id"].metadata == {"kind": "ordinary"}
        assert chunks["shared-chunk-id-repair-1"].text == "visual remapped text"
        assert chunks["shared-chunk-id-repair-1"].source_span_ids == [visual_span.id]
        assert storage.get_source_span(ordinary_span.id) is not None
        assert storage.get_source_span(visual_span.id) is not None
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute(
                "SELECT chunk_id FROM visual_artifact_chunks WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchall() == [("shared-chunk-id-repair-1",)]
            if storage.fts5_available:
                ordinary_fts = conn.execute(
                    "SELECT text FROM chunks_fts WHERE chunk_id = 'shared-chunk-id'",
                ).fetchall()
                assert ordinary_fts == [("ordinary stable text",)]
    finally:
        storage.close()


def test_append_visual_chunks_remaps_conflict_with_other_document_chunk(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    doc_a = _document("doc-chunk-conflict-a", _pdf(tmp_path, "chunk-conflict-a.pdf"))
    doc_b = _document("doc-chunk-conflict-b", _pdf(tmp_path, "chunk-conflict-b.pdf"))
    span_a = _ordinary_span(doc_a, text="doc a span text")
    chunk_a = KnowledgeChunk(
        id="shared-chunk-id",
        document_id=doc_a.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="doc a ordinary text",
        kb_id=doc_a.kb_id,
        version_id=doc_a.version_id,
        source_span_ids=[span_a.id],
    )
    artifact_id = "artifact-doc-b-conflict"
    visual_span = _visual_span_with_id(doc_b, "span-doc-b-visual", text="doc b visual span")
    visual_chunk = _visual_chunk_with_ids(doc_b, "shared-chunk-id", visual_span.id, artifact_id, text="doc b visual text")
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(doc_a, [chunk_a], source_spans=[span_a])
        storage.save_document(doc_b, [], source_spans=[])
        storage.upsert_visual_artifact(_visual_candidate(doc_b, artifact_id=artifact_id))

        chunk_ids = storage.append_visual_chunks(doc_b.id, doc_b.version_id, artifact_id, [visual_chunk], [visual_span])

        assert chunk_ids == ["shared-chunk-id-repair-1"]
        assert storage.list_chunks(doc_a.id)[0].id == "shared-chunk-id"
        assert storage.list_chunks(doc_a.id)[0].text == "doc a ordinary text"
        doc_b_chunks = storage.list_chunks(doc_b.id)
        assert [chunk.id for chunk in doc_b_chunks] == ["shared-chunk-id-repair-1"]
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute(
                "SELECT chunk_id FROM visual_artifact_chunks WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchall() == [("shared-chunk-id-repair-1",)]
    finally:
        storage.close()


def test_append_visual_chunks_rolls_back_source_spans_when_chunk_insert_fails(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-visual-chunk-rollback", _pdf(tmp_path, "visual-chunk-rollback.pdf"))
    artifact_id = "artifact-chunk-rollback"
    visual_span = _visual_span_with_id(document, "span-rollback-visual", text="rollback span")
    visual_chunk = _visual_chunk_with_ids(document, "chunk-rollback-visual", visual_span.id, artifact_id, text=None)
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [], source_spans=[])
        storage.upsert_visual_artifact(_visual_candidate(document, artifact_id=artifact_id))

        with pytest.raises(sqlite3.IntegrityError):
            storage.append_visual_chunks(document.id, document.version_id, artifact_id, [visual_chunk], [visual_span])

        assert storage.get_source_span(visual_span.id) is None
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM chunks WHERE id = ?", (visual_chunk.id,)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?",
                (visual_chunk.id,),
            ).fetchone()[0] == 0
            if storage.fts5_available:
                assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunk_id = ?", (visual_chunk.id,)).fetchone()[0] == 0
    finally:
        storage.close()


def test_append_visual_group_chunks_rolls_back_when_member_mapping_fails(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-visual-group-rollback", _pdf(tmp_path, "visual-group-rollback.pdf"))
    group_id = "group-rollback"
    member_id = "member-artifact-fail"
    group_span = _visual_span_with_id(document, "span-group-rollback", text="group rollback span")
    group_chunk = KnowledgeChunk(
        id="chunk-group-rollback",
        document_id=document.id,
        ordinal=99,
        page_start=1,
        page_end=2,
        text="group rollback chunk",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[group_span.id],
        metadata={"source": "visual_analysis", "visual_scope": "group", "visual_group_id": group_id},
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [], source_spans=[])
        storage.conn.execute(
            """
            CREATE TRIGGER fail_visual_group_member_mapping
            BEFORE INSERT ON visual_artifact_chunks
            WHEN NEW.artifact_id = 'member-artifact-fail'
            BEGIN
              SELECT RAISE(ABORT, 'forced visual group mapping failure');
            END
            """
        )
        storage.conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced visual group mapping failure"):
            storage.append_visual_group_chunks(
                document.id,
                document.version_id,
                group_id,
                [member_id],
                [group_chunk],
                [group_span],
            )

        assert storage.get_source_span(group_span.id) is None
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM chunks WHERE id = ?", (group_chunk.id,)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?",
                (group_chunk.id,),
            ).fetchone()[0] == 0
            if storage.fts5_available:
                assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunk_id = ?", (group_chunk.id,)).fetchone()[0] == 0
    finally:
        storage.close()


def test_repair_apply_prunes_entity_references_to_deleted_old_span(monkeypatch, tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-entity-prune", _pdf(tmp_path, "entity-prune.pdf"))
    old_span = _ordinary_span(document)
    _seed_document(db_path, document, with_visual=True)
    storage = KnowledgeStorage(db_path)
    try:
        storage.upsert_entity(
            KnowledgeEntity(
                id=stable_entity_id("UCIe"),
                canonical_name="UCIe",
                entity_type="protocol",
                source_span_ids=[old_span.id],
            )
        )
        storage.conn.commit()
    finally:
        storage.close()
    _patch_extract_and_sanitize(monkeypatch, sanitizer_text="UCIe is a die-to-die interconnect standard.")

    report = repair_script.run_repair(_options(tmp_path, db_path, document_id=document.id, apply=True))

    assert report["ok"] is True
    storage = KnowledgeStorage(db_path)
    try:
        entity = storage.resolve_entity("UCIe")
        assert entity is not None
        assert old_span.id not in entity.source_span_ids
        assert entity.source_span_ids
        for span_id in entity.source_span_ids:
            assert storage.get_source_span(span_id) is not None
        chunks = storage.list_chunks(document.id)
        ordinary = [chunk for chunk in chunks if chunk.metadata.get("source") != "visual_analysis"]
        visual = [chunk for chunk in chunks if chunk.metadata.get("source") == "visual_analysis"]
        assert ordinary
        assert visual
        assert not any(POLLUTION in chunk.text for chunk in ordinary)
    finally:
        storage.close()


def test_delete_unreferenced_source_spans_deletes_all_when_no_chunks(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-no-chunks", _pdf(tmp_path, "no-chunks.pdf"))
    storage = KnowledgeStorage(db_path)
    try:
        span_id = f"span-{document.id}-ordinary"
        storage.save_document(document, [], source_spans=[_ordinary_span(document)])
        storage.upsert_entity(
            KnowledgeEntity(
                id="entity-no-chunks",
                canonical_name="NoChunks",
                entity_type="test",
                source_span_ids=[span_id],
            )
        )
        storage.upsert_relation(
            KnowledgeRelation(
                id=stable_relation_id("entity-no-chunks", "mentions", "entity-target", [span_id]),
                subject_entity_id="entity-no-chunks",
                predicate="mentions",
                object_entity_id="entity-target",
                subject="NoChunks",
                object="Target",
                source_kb_id=document.kb_id,
                evidence_span_ids=[span_id],
                metadata={"kept": True},
            ),
            source_doc_id=document.id,
        )
        storage.conn.commit()
        assert storage.get_source_span(span_id) is not None

        deleted = repair_script.delete_unreferenced_source_spans_for_document(storage, document.id)

        assert deleted == 1
        assert storage.get_source_span(span_id) is None
        entity = storage.resolve_entity("NoChunks")
        assert entity is not None
        assert span_id not in entity.source_span_ids
        relations = storage.list_relations(entity_id="entity-no-chunks")
        assert relations[0].evidence_span_ids == []
        assert relations[0].metadata["kept"] is True
        assert relations[0].metadata["evidence_pruned"] is True
        assert relations[0].metadata["pruned_source_span_ids"] == [span_id]
    finally:
        storage.close()


def test_save_document_prunes_entity_refs_when_deleting_stale_visual_spans(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document_v1 = _document("doc-stale-visual-prune", _pdf(tmp_path, "stale-visual-prune.pdf"))
    document_v2 = KnowledgeDocument(
        id=document_v1.id,
        title=document_v1.title,
        source_path=document_v1.source_path,
        mime_type=document_v1.mime_type,
        size=document_v1.size,
        content_hash=f"{document_v1.content_hash}-v2",
        status="ready",
        kb_id=document_v1.kb_id,
        version_id=f"{document_v1.version_id}-v2",
    )
    visual_span = _visual_span(document_v1, text="stale visual span text")
    visual_chunk = _visual_chunk(document_v1, text="stale visual chunk text")
    replacement_span = SourceSpan(
        id="span-doc-stale-visual-prune-new",
        document_id=document_v2.id,
        version_id=document_v2.version_id,
        source_file=document_v2.source_path,
        page_start=1,
        page_end=1,
        text="replacement ordinary text",
    )
    replacement_chunk = KnowledgeChunk(
        id="chunk-doc-stale-visual-prune-new",
        document_id=document_v2.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="replacement ordinary text",
        kb_id=document_v2.kb_id,
        version_id=document_v2.version_id,
        source_span_ids=[replacement_span.id],
    )
    entity_id = "entity-stale-visual"
    relation_id = stable_relation_id(entity_id, "mentions", "entity-target", [visual_span.id])
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document_v1, [_ordinary_chunk(document_v1)], source_spans=[_ordinary_span(document_v1)])
        storage.upsert_visual_artifact(_visual_candidate(document_v1))
        storage.append_visual_chunks(document_v1.id, document_v1.version_id, "artifact-stale-visual", [visual_chunk], [visual_span])
        storage.upsert_entity(
            KnowledgeEntity(
                id=entity_id,
                canonical_name="StaleVisual",
                entity_type="test",
                source_span_ids=[visual_span.id],
            )
        )
        storage.upsert_relation(
            KnowledgeRelation(
                id=relation_id,
                subject_entity_id=entity_id,
                predicate="mentions",
                object_entity_id="entity-target",
                subject="StaleVisual",
                object="Target",
                source_kb_id=document_v1.kb_id,
                evidence_span_ids=[visual_span.id],
                metadata={"kept": True},
            )
        )

        storage.save_document(document_v2, [replacement_chunk], source_spans=[replacement_span])

        chunks = storage.list_chunks(document_v1.id)
        assert not any(chunk.id == visual_chunk.id for chunk in chunks)
        assert storage.get_source_span(visual_span.id) is None
        entity = storage.resolve_entity("StaleVisual")
        assert entity is not None
        assert visual_span.id not in entity.source_span_ids
        relations = storage.list_relations(entity_id=entity_id)
        assert relations[0].evidence_span_ids == []
        assert relations[0].metadata["kept"] is True
        assert relations[0].metadata["evidence_pruned"] is True
        assert relations[0].metadata["pruned_source_span_ids"] == [visual_span.id]
    finally:
        storage.close()


def test_delete_stale_visual_chunks_keeps_span_still_referenced_by_remaining_chunk(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-stale-visual-shared", _pdf(tmp_path, "stale-visual-shared.pdf"))
    shared_span = SourceSpan(
        id="shared-stale-visual-span",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="shared source span text",
    )
    remaining_chunk = KnowledgeChunk(
        id="chunk-stale-visual-shared-remaining",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="remaining ordinary chunk text",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    stale_chunk = KnowledgeChunk(
        id="chunk-stale-visual-shared-old",
        document_id=document.id,
        ordinal=2,
        page_start=1,
        page_end=1,
        text="stale visual chunk text",
        kb_id=document.kb_id,
        version_id=f"{document.version_id}-old",
        source_span_ids=[shared_span.id],
        metadata={"source": "visual_analysis"},
    )
    entity_id = "entity-shared-stale-visual"
    relation_id = stable_relation_id(entity_id, "mentions", "entity-target", [shared_span.id])
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [remaining_chunk], source_spans=[shared_span])
        storage.append_visual_chunks(document.id, f"{document.version_id}-old", "artifact-stale-shared", [stale_chunk], [])
        storage.upsert_entity(
            KnowledgeEntity(
                id=entity_id,
                canonical_name="SharedStaleVisual",
                entity_type="test",
                source_span_ids=[shared_span.id],
            )
        )
        storage.upsert_relation(
            KnowledgeRelation(
                id=relation_id,
                subject_entity_id=entity_id,
                predicate="mentions",
                object_entity_id="entity-target",
                subject="SharedStaleVisual",
                object="Target",
                source_kb_id=document.kb_id,
                evidence_span_ids=[shared_span.id],
                metadata={"kept": True},
            )
        )

        storage._delete_stale_visual_chunks(document.id, document.version_id)

        chunks = storage.list_chunks(document.id)
        assert not any(chunk.id == stale_chunk.id for chunk in chunks)
        assert storage.get_source_span(shared_span.id) is not None
        assert any(shared_span.id in chunk.source_span_ids for chunk in chunks)
        entity = storage.resolve_entity("SharedStaleVisual")
        assert entity is not None
        assert entity.source_span_ids == [shared_span.id]
        relations = storage.list_relations(entity_id=entity_id)
        assert relations[0].evidence_span_ids == [shared_span.id]
        assert relations[0].metadata == {"kept": True}
    finally:
        storage.close()


def test_append_visual_chunks_remaps_span_conflict_with_other_document(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    doc_a = _document("doc-visual-conflict-a", _pdf(tmp_path, "visual-conflict-a.pdf"))
    doc_b = _document("doc-visual-conflict-b", _pdf(tmp_path, "visual-conflict-b.pdf"))
    shared_span = SourceSpan(
        id="shared-id",
        document_id=doc_a.id,
        version_id=doc_a.version_id,
        source_file=doc_a.source_path,
        page_start=1,
        page_end=1,
        text="doc a original source span",
    )
    ordinary_chunk = KnowledgeChunk(
        id="chunk-visual-conflict-a-ordinary",
        document_id=doc_a.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="doc a ordinary chunk",
        kb_id=doc_a.kb_id,
        version_id=doc_a.version_id,
        source_span_ids=[shared_span.id],
    )
    visual_span = SourceSpan(
        id=shared_span.id,
        document_id=doc_b.id,
        version_id=doc_b.version_id,
        source_file=doc_b.source_path,
        page_start=2,
        page_end=2,
        text="doc b conflicting visual source span",
    )
    visual_chunk = KnowledgeChunk(
        id="chunk-visual-conflict-b-visual",
        document_id=doc_b.id,
        ordinal=99,
        page_start=2,
        page_end=2,
        text="doc b visual chunk",
        kb_id=doc_b.kb_id,
        version_id=doc_b.version_id,
        source_span_ids=[visual_span.id],
        metadata={"source": "visual_analysis"},
    )
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(doc_a, [ordinary_chunk], source_spans=[shared_span])
        storage.save_document(doc_b, [], source_spans=[])
        artifact = _visual_candidate(doc_b, artifact_id="artifact-visual-conflict-b")
        storage.upsert_visual_artifact(artifact)

        storage.append_visual_chunks(doc_b.id, doc_b.version_id, artifact.id, [visual_chunk], [visual_span])

        assert storage.get_source_span(shared_span.id).document_id == doc_a.id
        assert storage.get_source_span(shared_span.id).text == shared_span.text
        assert storage.list_chunks(doc_a.id)[0].source_span_ids == [shared_span.id]
        visual_chunks = [chunk for chunk in storage.list_chunks(doc_b.id) if chunk.metadata.get("source") == "visual_analysis"]
        remapped_span_id = visual_chunks[0].source_span_ids[0]
        assert remapped_span_id != shared_span.id
        assert storage.get_source_span(remapped_span_id).document_id == doc_b.id
        assert storage.get_source_span(remapped_span_id).text == visual_span.text
    finally:
        storage.close()


def test_delete_visual_chunks_for_artifact_preserves_shared_ordinary_span_refs(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-delete-visual-shared", _pdf(tmp_path, "delete-visual-shared.pdf"))
    shared_span = SourceSpan(
        id="span-delete-visual-shared",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="shared ordinary and visual span",
    )
    ordinary_chunk = KnowledgeChunk(
        id="chunk-delete-visual-shared-ordinary",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="ordinary chunk keeps shared span",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    visual_chunk = KnowledgeChunk(
        id="chunk-delete-visual-shared-visual",
        document_id=document.id,
        ordinal=2,
        page_start=1,
        page_end=1,
        text="visual chunk shares span",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
        metadata={"source": "visual_analysis"},
    )
    entity_id = "entity-delete-visual-shared"
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [ordinary_chunk], source_spans=[shared_span])
        artifact = _visual_candidate(document, artifact_id="artifact-delete-visual-shared", status_page=1)
        storage.upsert_visual_artifact(artifact)
        storage.append_visual_chunks(document.id, document.version_id, artifact.id, [visual_chunk], [])
        _upsert_graph_refs(storage, document, entity_id, "SharedDeleteVisual", shared_span.id)

        storage.delete_visual_chunks_for_artifact(artifact.id)

        chunks = storage.list_chunks(document.id)
        assert not any(chunk.id == visual_chunk.id for chunk in chunks)
        assert any(chunk.id == ordinary_chunk.id for chunk in chunks)
        assert storage.get_source_span(shared_span.id) is not None
        entity = storage.resolve_entity("SharedDeleteVisual")
        assert entity is not None
        assert entity.source_span_ids == [shared_span.id]
        relations = storage.list_relations(entity_id=entity_id)
        assert relations[0].evidence_span_ids == [shared_span.id]
        assert relations[0].metadata == {"kept": True}
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?",
                (visual_chunk.id,),
            ).fetchone()[0] == 0
    finally:
        storage.close()


def test_delete_visual_chunks_for_artifact_prunes_unreferenced_visual_span_refs(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-delete-visual-prune", _pdf(tmp_path, "delete-visual-prune.pdf"))
    visual_span = SourceSpan(
        id="span-delete-visual-prune",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=2,
        page_end=2,
        text="visual only source span",
    )
    visual_chunk = KnowledgeChunk(
        id="chunk-delete-visual-prune",
        document_id=document.id,
        ordinal=1,
        page_start=2,
        page_end=2,
        text="visual only chunk",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[visual_span.id],
        metadata={"source": "visual_analysis"},
    )
    entity_id = "entity-delete-visual-prune"
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [], source_spans=[])
        artifact = _visual_candidate(document, artifact_id="artifact-delete-visual-prune")
        storage.upsert_visual_artifact(artifact)
        storage.append_visual_chunks(document.id, document.version_id, artifact.id, [visual_chunk], [visual_span])
        _upsert_graph_refs(storage, document, entity_id, "VisualOnlyPrune", visual_span.id)

        storage.delete_visual_chunks_for_artifact(artifact.id)

        assert not any(chunk.id == visual_chunk.id for chunk in storage.list_chunks(document.id))
        assert storage.get_source_span(visual_span.id) is None
        entity = storage.resolve_entity("VisualOnlyPrune")
        assert entity is not None
        assert visual_span.id not in entity.source_span_ids
        relations = storage.list_relations(entity_id=entity_id)
        assert relations[0].evidence_span_ids == []
        assert relations[0].metadata["kept"] is True
        assert relations[0].metadata["evidence_pruned"] is True
        assert relations[0].metadata["pruned_source_span_ids"] == [visual_span.id]
    finally:
        storage.close()


def test_reset_visual_cache_preserves_shared_ordinary_span_and_reports_actual_deleted_spans(tmp_path):
    db_path = tmp_path / "kb.sqlite"
    document = _document("doc-reset-visual-shared", _pdf(tmp_path, "reset-visual-shared.pdf"))
    shared_span = SourceSpan(
        id="span-reset-visual-shared",
        document_id=document.id,
        version_id=document.version_id,
        source_file=document.source_path,
        page_start=1,
        page_end=1,
        text="shared span survives reset",
    )
    ordinary_chunk = KnowledgeChunk(
        id="chunk-reset-visual-shared-ordinary",
        document_id=document.id,
        ordinal=1,
        page_start=1,
        page_end=1,
        text="ordinary chunk keeps shared span on reset",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
    )
    visual_chunk = KnowledgeChunk(
        id="chunk-reset-visual-shared-visual",
        document_id=document.id,
        ordinal=2,
        page_start=1,
        page_end=1,
        text="visual chunk shares span on reset",
        kb_id=document.kb_id,
        version_id=document.version_id,
        source_span_ids=[shared_span.id],
        metadata={"source": "visual_analysis"},
    )
    entity_id = "entity-reset-visual-shared"
    storage = KnowledgeStorage(db_path)
    try:
        storage.save_document(document, [ordinary_chunk], source_spans=[shared_span])
        artifact = _visual_candidate(document, artifact_id="artifact-reset-visual-shared", status_page=1)
        storage.upsert_visual_artifact(artifact)
        storage.append_visual_chunks(document.id, document.version_id, artifact.id, [visual_chunk], [])
        _upsert_graph_refs(storage, document, entity_id, "ResetSharedVisual", shared_span.id)

        reset = storage.reset_visual_cache(document_id=document.id, version_id=document.version_id)

        assert reset["chunks"] == 1
        assert reset["source_spans"] == 0
        chunks = storage.list_chunks(document.id)
        assert not any(chunk.id == visual_chunk.id for chunk in chunks)
        assert any(chunk.id == ordinary_chunk.id for chunk in chunks)
        assert storage.get_source_span(shared_span.id) is not None
        entity = storage.resolve_entity("ResetSharedVisual")
        assert entity is not None
        assert entity.source_span_ids == [shared_span.id]
        relations = storage.list_relations(entity_id=entity_id)
        assert relations[0].evidence_span_ids == [shared_span.id]
        assert relations[0].metadata == {"kept": True}
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM visual_artifact_chunks WHERE chunk_id = ?",
                (visual_chunk.id,),
            ).fetchone()[0] == 0
    finally:
        storage.close()
