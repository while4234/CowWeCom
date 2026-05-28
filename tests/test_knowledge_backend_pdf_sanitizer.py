import sqlite3

import pytest

from agent.knowledge.backend import KnowledgeBackendConfig, KnowledgeBackendService
from agent.knowledge.backend.models import DocumentPage
from agent.knowledge.backend.text_sanitizer import (
    is_formula_garble_block,
    is_formula_garble_line,
    is_large_table_like_block,
    sanitize_pages_for_knowledge_chunks,
)


def _make_noisy_pdf(path):
    fitz = pytest.importorskip("fitz")

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((72, 72), "This section describes the package interface and keeps normal prose.")
    page.insert_text((72, 250), "L a y e r 1 0 1 2 3 t r k v l d 1 2 1 3 1 4")
    page.insert_text((72, 280), "Sideband")
    page.insert_text((72, 305), "Tx")
    page.insert_text((72, 330), "Module")
    page.insert_text((180, 305), "Rx")
    page.insert_text((180, 330), "Module")
    page.insert_text((72, 360), "Figure 5-34. Standard Package x16 interface: Signal exit order")
    page.insert_text((72, 395), "rxdatasbtxdatasb txcksb rxcksb")
    page.insert_text((72, 620), "A receiver observes ordered signals during initialization.")
    doc.save(path)
    doc.close()


def test_pdf_text_sanitizer_keeps_caption_and_prose_but_removes_visual_noise(tmp_path):
    pdf_path = tmp_path / "visual-noise.pdf"
    _make_noisy_pdf(pdf_path)
    pages = [
        DocumentPage(
            page=1,
            text=(
                "This section describes the package interface and keeps normal prose.\n"
                "L a y e r 1 0 1 2 3 t r k v l d 1 2 1 3 1 4\n"
                "Sideband\nTx\nModule\nRx\nModule\n"
                "Figure 5-34. Standard Package x16 interface: Signal exit order\n"
                "rxdatasbtxdatasb txcksb rxcksb\n"
                "A receiver observes ordered signals during initialization."
            ),
        )
    ]

    sanitized, report = sanitize_pages_for_knowledge_chunks(pdf_path, pages)

    text = sanitized[0].text
    assert "Figure 5-34. Standard Package x16 interface: Signal exit order" in text
    assert "This section describes the package interface" in text
    assert "A receiver observes ordered signals" in text
    assert "L a y e r 1 0 1 2 3" not in text
    assert "rxdatasbtxdatasb txcksb rxcksb" not in text
    assert "Sideband" not in text
    assert report["removed_total_lines"] >= 1


def test_pdf_upload_does_not_generate_visual_noise_source_chunk(tmp_path):
    pytest.importorskip("pypdf")
    pdf_path = tmp_path / "visual-noise.pdf"
    _make_noisy_pdf(pdf_path)

    service = KnowledgeBackendService(
        KnowledgeBackendConfig.from_mapping(
            {
                "enabled": True,
                "sqlite_path": str(tmp_path / "knowledge.sqlite3"),
                "workspace_root": str(tmp_path),
                "data_dir": str(tmp_path / "backend-data"),
                "ingest": {"allowed_extensions": [".pdf"]},
                "vector_store": {"provider": "sqlite", "required": False},
            }
        )
    )

    result = service.ingest_upload_bytes("visual-noise.pdf", pdf_path.read_bytes(), title="Noisy PDF")

    assert result["status"] == "succeeded", result
    document_id = result["document"]["id"]
    with sqlite3.connect(str(service.config.sqlite_path)) as conn:
        chunk_text = "\n".join(row[0] for row in conn.execute("SELECT text FROM chunks WHERE document_id = ?", (document_id,)))
    assert "Figure 5-34. Standard Package x16 interface: Signal exit order" in chunk_text
    assert "L a y e r 1 0 1 2 3" not in chunk_text
    assert "rxdatasbtxdatasb txcksb rxcksb" not in chunk_text


def test_sanitizer_preserves_multiline_caption_label(tmp_path):
    pdf_path = tmp_path / "multiline-caption.pdf"
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((72, 250), "Figure 5-33.")
    page.insert_text((72, 275), "Standard Package Bump Map: x16 interface")
    page.insert_text((72, 310), "L a y e r 1 0 1 2 3 t r k v l d")
    page.insert_text((72, 340), "rxdatasbtxdatasb txcksb rxcksb")
    doc.save(pdf_path)
    doc.close()
    pages = [
        DocumentPage(
            page=1,
            text=(
                "Figure 5-33.\n"
                "Standard Package Bump Map: x16 interface\n"
                "L a y e r 1 0 1 2 3 t r k v l d\n"
                "rxdatasbtxdatasb txcksb rxcksb"
            ),
        )
    ]

    sanitized, _ = sanitize_pages_for_knowledge_chunks(pdf_path, pages)
    text = sanitized[0].text

    assert "Figure 5-33." in text
    assert "Standard Package Bump Map: x16 interface" in text
    assert "L a y e r 1 0 1 2 3" not in text
    assert "rxdatasbtxdatasb txcksb rxcksb" not in text


def test_formula_garble_detection_positive_and_negative_examples():
    assert is_formula_garble_line(
        "L f( ) 20 10 Vr f( ) Vs f( ) log =",
        context="Equation defines insertion loss.",
    )
    assert is_formula_garble_block(
        "The following formula defines loss:\nL f( ) 20 10 Vr f( ) Vs f( ) log ="
    )

    assert not is_formula_garble_line("TVALID and TREADY define the transfer handshake.")
    assert not is_formula_garble_line("Signal Direction Description")
    assert not is_formula_garble_line("Figure 1-1 Channel architecture of reads")


def test_signal_and_encoding_table_rows_are_not_formula_garble():
    signal_context = "Table 8-1. RDI signal list\nSignal Description"
    message_context = "Table 6-9. Link Training State Machine related Message encodings\nMessage MsgInfo MsgCode MsgSubcode Field Bits Encoding Value Reserved"

    assert not is_formula_garble_line(
        "lp_data[NBYTES-1:0][7:0] Adapter to Physical Layer data, where NBYTES equals number of bytes",
        context=signal_context,
    )
    assert not is_formula_garble_block(
        "Table 8-1. RDI signal list\n"
        "Signal Description\n"
        "lp_data[NBYTES-1:0][7:0]\n"
        "Adapter to Physical Layer data, where NBYTES equals number of bytes determined by the data width."
    )
    assert not is_formula_garble_line("MsgInfo[15:0] Message information field", context=message_context)
    assert not is_formula_garble_line("MsgCode[7:0] Message code", context=message_context)
    assert not is_formula_garble_line("MsgSubcode[7:0] Message subcode", context=message_context)
    assert not is_formula_garble_line("[15:6]: Reserved", context=message_context)


def test_real_formula_rows_still_detected_after_table_guards():
    assert is_formula_garble_line(
        "L f( ) 20 10 Vr f( ) Vs f( ) log =",
        context="Equation 5-1 defines VTF loss.",
    )
    assert is_formula_garble_block(
        "CRC polynomial is defined by the following formula:\n"
        "G x( ) x 16 x 12 x 5 1 + + + ="
    )
    assert is_formula_garble_block(
        "VTF loss is calculated using:\n"
        "L f( ) 20 10 Vr f( ) Vs f( ) log ="
    )


def test_dense_table_like_block_is_reported_without_caption_false_positive():
    table_text = "\n".join(
        ["Signal Direction Description"]
        + [f"SIG_{index} input Description_{index}" for index in range(12)]
    )
    assert is_large_table_like_block(table_text)
    assert not is_large_table_like_block("Table 2-1 Global signals\nThis table explains global signal usage.")


def test_sanitizer_does_not_default_delete_formula_or_large_table_without_visual_replacement(tmp_path):
    pdf_path = tmp_path / "quality-boundary.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n% fake\n")
    formula = "L f( ) 20 10 Vr f( ) Vs f( ) log ="
    table_text = "\n".join(["Signal Direction Description"] + [f"SIG_{i} input Description_{i}" for i in range(12)])
    pages = [DocumentPage(page=1, text=f"Equation 1 defines insertion loss.\n{formula}\n{table_text}")]

    sanitized, _ = sanitize_pages_for_knowledge_chunks(
        pdf_path,
        pages,
        strip_visual_regions=False,
        strip_visual_noise_lines=True,
    )

    assert formula in sanitized[0].text
    assert "SIG_10 input Description_10" in sanitized[0].text
