# Public Protocol Knowledge Backend

This directory is the committed public protocol knowledge store. It is reserved
for protocol/specification documents uploaded by an administrator through the
Web backend, plus the indexed protocol chunks and model-generated study
documents derived from those uploads.

The current public protocol library contains AMBA AXI v2.0, AXI4-Stream, and
UCIe 1.1 protocol knowledge bases. Other personal knowledge bases,
conversation summaries, and knowledge-wiki outputs do not belong here and must
not be committed.

## What Lives Here

- `indexes/kb.sqlite`: generated SQLite index for public protocol documents, chunks,
  source spans, entities, relations, jobs, and search metadata.
- `originals/`: copied source files used to build the index.
- `derived/` and `reports/`: generated study documents and validation reports.
- `manifest.json`: safe portable summary of indexed documents.

Artifacts in this directory are committed to this repository only when they come
from administrator Web-backend protocol/specification ingestion. That includes
the portable SQLite index, source protocol files, derived model study documents,
validation reports, and manifest entries. This lets a fresh CowWechat deployment
reuse already parsed public protocol knowledge without uploading and parsing the
same document again.

Do not use this directory for private chat memory, credentials, local runtime
state, personal knowledge, conversation-generated summaries, knowledge-wiki
outputs, or non-protocol personal documents. Those stay in ignored runtime
locations.

## Runtime Layout

The structured backend store intentionally stays in this project:

```text
D:\cowwechat\public_protocol_knowledge
```

The Web console and WeChat-facing readable document library use the Agent
workspace knowledge directory:

```text
~/cow/knowledge
```

For this project, protocol documents are exported under:

```text
~/cow/knowledge/documents/<kb_id>/
```

This split keeps the parsed protocol index portable with the project while
keeping Web/WeChat document browsing aligned with the rest of CowAgent's
knowledge workflow.

## Moving To Another Machine

1. Copy or clone the project directory.
2. Install normal project dependencies and optional knowledge dependencies.
3. Enable `knowledge_backend` in local `config.json`.
4. Export the readable Markdown library into the target Agent workspace:

```powershell
python scripts/export_knowledge_backend_docs.py
```

The export step is deterministic after an index exists. It reads
`indexes/kb.sqlite` and writes Markdown pages to `~/cow/knowledge`.
The current canonical Web document path is `knowledge/documents/<kb_id>/`.
After pulling the repaired public protocol index on an existing machine, delete
any legacy `~/cow/knowledge/protocols/` export after refreshing the document
library; otherwise the Web console can still list stale Markdown even though
knowledge retrieval is already using the updated SQLite index.

If the target machine uses a different Agent workspace, configure:

```json
{
  "knowledge_backend": {
    "ingest": {
      "document_library_root": "~/cow"
    }
  }
}
```

or run:

```powershell
python scripts/export_knowledge_backend_docs.py --document-library-root "D:\path\to\cow-workspace"
```

## Optional LLM Study Documents

LLM-generated study notes are an optional derived layer. They should be built
from source spans already stored in `kb.sqlite`, include citations, and pass
validation. They are not required for retrieval and must not replace the
authoritative parsed chunks/source spans.

Generate or refresh the derived study page after a protocol has already been
indexed:

```powershell
python scripts/generate_knowledge_backend_llm_docs.py
```

This is a one-time operation per protocol version unless the original protocol
file changes or you want to regenerate with a different model/prompt. The
script writes a Markdown study page to `~/cow/knowledge/documents/<kb_id>/`,
stores a portable copy under `public_protocol_knowledge/derived/`, indexes that derived
copy as `doc_type=llm_study`, and writes a validation report under
`public_protocol_knowledge/reports/`.

After generation and validation, commit the updated
`public_protocol_knowledge/` files so the generated teaching layer moves with
the repository.

Quality-check the result before relying on it in WeChat Q&A:

```powershell
python scripts/validate_knowledge_backend_llm_quality.py
```

If the LLM layer is not wanted or model access is unavailable, set:

```json
{
  "knowledge_backend": {
    "llm_builder": {
      "enabled": false
    }
  }
}
```
