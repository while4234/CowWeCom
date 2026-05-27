"""Data models for the optional local knowledge backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class DependencyStatus:
    """Availability of an optional backend capability."""

    name: str
    available: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentPage:
    """Extracted text for one source page or logical section."""

    page: int
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    """Normalized document text extracted from an uploaded file."""

    title: str
    source_path: str
    mime_type: str
    pages: List[DocumentPage]
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text.strip())


@dataclass(frozen=True)
class KnowledgeChunk:
    """Searchable chunk stored in SQLite."""

    id: str
    document_id: str
    ordinal: int
    page_start: int
    page_end: int
    text: str
    kb_id: str = "kb_default"
    version_id: str = ""
    section_path: str = ""
    clause_title: str = ""
    source_span_ids: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeDocument:
    """Document metadata stored in SQLite."""

    id: str
    title: str
    source_path: str
    mime_type: str
    size: int
    content_hash: str
    status: str
    error: str = ""
    kb_id: str = "kb_default"
    doc_type: str = "document"
    version_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeBase:
    """Knowledge-base namespace for documents and graph entities."""

    id: str
    name: str
    description: str = ""
    domains: List[str] = field(default_factory=list)
    status: str = "active"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceSpan:
    """Traceable source range backing chunks, entities and relations."""

    id: str
    document_id: str
    version_id: str
    source_file: str
    page_start: int
    page_end: int
    text: str
    section_path: str = ""
    paragraph_index_start: int = 0
    paragraph_index_end: int = 0
    char_start: int = 0
    char_end: int = 0
    bbox: Optional[Dict[str, Any]] = None
    text_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeEntity:
    """Canonical entity with aliases and optional defining source."""

    id: str
    canonical_name: str
    entity_type: str
    description: str = ""
    defining_kb_id: Optional[str] = None
    defining_doc_id: Optional[str] = None
    confidence: float = 0.0
    aliases: List[str] = field(default_factory=list)
    source_span_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeRelation:
    """Relationship between two canonical entities."""

    id: str
    subject_entity_id: str
    predicate: str
    object_entity_id: str
    subject: str
    object: str
    source_kb_id: str = ""
    target_kb_id: str = ""
    evidence_span_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "active"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchHit:
    """Knowledge search hit with enough metadata for citation rendering."""

    document_id: str
    chunk_id: str
    ordinal: int
    title: str
    source_path: str
    page_start: int
    page_end: int
    score: float
    snippet: str
    kb_id: str = "kb_default"
    section_path: str = ""
    source_span_ids: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["path"] = self.source_path
        data["text"] = self.snippet
        return data

    @property
    def path(self) -> str:
        return self.source_path

    @property
    def text(self) -> str:
        return self.snippet


@dataclass(frozen=True)
class Citation:
    """Citation returned by query()."""

    index: int
    document_id: str
    title: str
    source_path: str
    page_start: int
    page_end: int
    snippet: str
    kb_id: str = "kb_default"
    source_span_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueryResult:
    """Simple extractive answer plus citations."""

    answer: str
    citations: List[Citation]
    entities: List[str] = field(default_factory=list)
    confidence: float = 0.0
    unresolved: List[Dict[str, Any]] = field(default_factory=list)
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": [citation.to_dict() for citation in self.citations],
            "entities": self.entities,
            "confidence": self.confidence,
            "unresolved": self.unresolved,
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True)
class IngestionJob:
    """Synchronous ingestion job state persisted in SQLite."""

    id: str
    document_id: Optional[str]
    source_path: str
    status: str
    message: str = ""
    error: str = ""
    created_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationResult:
    """Source-verification outcome for one claim."""

    status: str
    supported: bool
    claim: str
    evidence: List[SourceSpan] = field(default_factory=list)
    confidence: float = 0.0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [span.to_dict() for span in self.evidence]
        return data


@dataclass(frozen=True)
class VisualArtifactCandidate:
    """Visual artifact discovered from a source document."""

    id: str
    document_id: str
    version_id: str
    kb_id: str
    artifact_type: str
    page: int
    label: str = ""
    caption: str = ""
    bbox: Dict[str, Any] = field(default_factory=dict)
    image_path: str = ""
    image_hash: str = ""
    context_hash: str = ""
    pipeline_version: str = ""
    parser: str = ""
    parser_confidence: float = 0.0
    section_path: List[str] = field(default_factory=list)
    context_before: str = ""
    context_after: str = ""
    page_text: str = ""
    source_path: str = ""
    crop_dpi: int = 180
    crop_padding_px: int = 12

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VisualAnalysisResult:
    """Validated model output for one visual artifact."""

    artifact_type: str
    title: str
    summary: str
    structured_markdown: str
    key_facts: List[Dict[str, Any]]
    table: Dict[str, Any] = field(default_factory=dict)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    state_machine: Dict[str, Any] = field(default_factory=dict)
    chart: Dict[str, Any] = field(default_factory=dict)
    uncertain_fields: List[str] = field(default_factory=list)
    readability: str = "unknown"
    confidence: Dict[str, Any] = field(default_factory=dict)
    should_index: bool = False
    low_confidence_reason: str = ""
    caption: str = ""
    page: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
