import json
from pathlib import Path

from agent.knowledge.backend import KnowledgeBackendConfig
from agent.knowledge.backend.models import (
    DocumentPage,
    ExtractedDocument,
    KnowledgeChunk,
    KnowledgeDocument,
    SourceSpan,
    VisualAnalysisResult,
    VisualArtifactCandidate,
)
from agent.knowledge.backend.storage import KnowledgeStorage
from scripts import repair_knowledge_text_chunks as repair_script


POLLUTION = "L a y e r 1 0 1 2 3 rxdatasbtxdatasb txcksb rxcksb"
CAPTION = "Figure 5-34. Standard Package x16 interface: Signal exit order"
CLEAN_TEXT = f"{CAPTION}\nClean package prose about UCIe sideband initialization."


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
