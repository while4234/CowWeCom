# CowAgent Knowledge Backend

This directory is the project-portable backend knowledge store. Keep it with the
repository when moving the deployment to another machine.

## What Lives Here

- `indexes/kb.sqlite`: authoritative SQLite index for parsed documents, chunks,
  source spans, entities, relations, jobs, and search metadata.
- `originals/`: copied source protocol files used to build the index.
- `manifest.json`: portable summary of indexed documents.

## Runtime Layout

The structured backend store intentionally stays in this project:

```text
D:\cowwechat\knowledge_backend
```

The Web console and WeChat-facing readable document library use the Agent
workspace knowledge directory:

```text
~/cow/knowledge
```

For this project, protocol documents are exported under:

```text
~/cow/knowledge/protocols/<kb_id>/
```

This split keeps the parsed protocol index portable with the project while
keeping Web/WeChat document browsing aligned with the rest of CowAgent's
knowledge workflow.

## Moving To Another Machine

1. Copy the project directory, including this `knowledge_backend/` directory.
2. Install normal project dependencies and optional knowledge dependencies.
3. Enable `knowledge_backend` in local `config.json`.
4. Export the readable Markdown library into the target Agent workspace:

```powershell
python scripts/export_knowledge_backend_docs.py
```

The export step is deterministic and does not re-parse the protocol PDF. It
reads `indexes/kb.sqlite` and writes Markdown pages to `~/cow/knowledge`.

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
script writes a Markdown study page to `~/cow/knowledge/protocols/<kb_id>/`,
stores a portable copy under `knowledge_backend/derived/`, indexes that derived
copy as `doc_type=llm_study`, and writes a validation report under
`knowledge_backend/reports/`.

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
