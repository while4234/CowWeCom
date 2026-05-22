"""Structured knowledge builders for the local backend.

The production path can attach an LLM-backed extractor later. This module
provides a deterministic source-bound builder that is safe for tests and for
machines where model calls are disabled or unavailable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from .models import KnowledgeChunk, KnowledgeDocument, KnowledgeEntity, KnowledgeRelation, SourceSpan
from .storage import stable_entity_id, stable_relation_id, stable_span_id


KNOWN_ALIASES = {
    "axi4-stream": ("AXI4-Stream", ["AXI4-Stream", "AXI4 Stream", "AXI Stream"]),
    "axi4 stream": ("AXI4-Stream", ["AXI4-Stream", "AXI4 Stream", "AXI Stream"]),
    "axi stream": ("AXI4-Stream", ["AXI4-Stream", "AXI4 Stream", "AXI Stream"]),
    "tvalid": ("TVALID", ["TVALID"]),
    "tready": ("TREADY", ["TREADY"]),
    "tdata": ("TDATA", ["TDATA"]),
    "tlast": ("TLAST", ["TLAST"]),
    "tkeep": ("TKEEP", ["TKEEP"]),
    "tstrb": ("TSTRB", ["TSTRB"]),
    "tuser": ("TUSER", ["TUSER"]),
    "tid": ("TID", ["TID"]),
    "tdest": ("TDEST", ["TDEST"]),
    "ucie": ("UCIe", ["UCIE", "UCIe"]),
    "ucie protocol layer": ("UCIe Protocol Layer", ["UCIE Protocol Layer", "UCIe Protocol Layer"]),
    "pci express": ("PCIe", ["PCI Express", "PCIe"]),
    "pcie": ("PCIe", ["PCI Express", "PCIe"]),
    "tlp": ("TLP", ["TLP", "Transaction Layer Packet"]),
    "transaction layer packet": ("TLP", ["TLP", "Transaction Layer Packet"]),
    "dllp": ("DLLP", ["DLLP", "Data Link Layer Packet"]),
    "data link layer packet": ("DLLP", ["DLLP", "Data Link Layer Packet"]),
    "flow control": ("Flow Control", ["Flow Control"]),
    "transaction layer": ("Transaction Layer", ["Transaction Layer"]),
}

RELATION_TYPES = {
    "defines",
    "mentions",
    "depends_on",
    "maps_to",
    "equivalent_to",
    "part_of",
    "mentions_or_depends_on",
}


@dataclass
class StructuredBuildResult:
    chunks: List[KnowledgeChunk] = field(default_factory=list)
    source_spans: List[SourceSpan] = field(default_factory=list)
    entities: List[KnowledgeEntity] = field(default_factory=list)
    relations: List[KnowledgeRelation] = field(default_factory=list)
    missing_prerequisites: List[Dict[str, Any]] = field(default_factory=list)


class HeuristicKnowledgeBuilder:
    """Extract source-bound entities and relations without external services."""

    def __init__(self, min_relation_confidence: float = 0.7):
        self.min_relation_confidence = float(min_relation_confidence)

    def build(self, document: KnowledgeDocument, chunks: Iterable[KnowledgeChunk]) -> StructuredBuildResult:
        result = StructuredBuildResult()
        entity_map: Dict[str, KnowledgeEntity] = {}
        relation_map: Dict[str, KnowledgeRelation] = {}
        version_id = document.version_id

        for chunk in chunks:
            span = self._source_span(document, version_id, chunk)
            chunk_entities = self._extract_entities(chunk.text)
            enriched_chunk = KnowledgeChunk(
                id=chunk.id,
                document_id=chunk.document_id,
                ordinal=chunk.ordinal,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                kb_id=chunk.kb_id,
                version_id=version_id,
                section_path=chunk.section_path,
                clause_title=chunk.clause_title,
                source_span_ids=[span.id],
                entities=[item["canonical_name"] for item in chunk_entities],
                metadata={**chunk.metadata, "text_hash": span.text_hash},
            )

            result.source_spans.append(span)
            result.chunks.append(enriched_chunk)

            for item in chunk_entities:
                entity = self._entity_from_candidate(document, span, item)
                existing = entity_map.get(entity.id)
                if existing:
                    entity = KnowledgeEntity(
                        id=entity.id,
                        canonical_name=entity.canonical_name,
                        entity_type=entity.entity_type,
                        description=entity.description or existing.description,
                        defining_kb_id=entity.defining_kb_id or existing.defining_kb_id,
                        defining_doc_id=entity.defining_doc_id or existing.defining_doc_id,
                        confidence=max(entity.confidence, existing.confidence),
                        aliases=sorted(set(existing.aliases + entity.aliases)),
                        source_span_ids=sorted(set(existing.source_span_ids + entity.source_span_ids)),
                        metadata={**existing.metadata, **entity.metadata},
                    )
                entity_map[entity.id] = entity

            for relation in self._relations_from_chunk(document, span, chunk_entities, chunk.text):
                relation_map[relation.id] = relation

            for item in chunk_entities:
                if item["entity_type"] == "external_dependency" and not item["is_defined"]:
                    result.missing_prerequisites.append(
                        {
                            "term": item["canonical_name"],
                            "reason": "Current document mentions the term but does not define it fully.",
                            "recommended_kb_query": f"{item['canonical_name']} definition",
                            "source_span_id": span.id,
                        }
                    )

        result.entities = list(entity_map.values())
        result.relations = list(relation_map.values())
        result.missing_prerequisites = _dedupe_missing(result.missing_prerequisites)
        return result

    def _source_span(self, document: KnowledgeDocument, version_id: str, chunk: KnowledgeChunk) -> SourceSpan:
        text_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        return SourceSpan(
            id=stable_span_id(document.id, chunk.ordinal, chunk.text),
            document_id=document.id,
            version_id=version_id,
            source_file=document.source_path,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section_path=chunk.section_path,
            paragraph_index_start=chunk.ordinal,
            paragraph_index_end=chunk.ordinal,
            char_start=0,
            char_end=len(chunk.text),
            text_hash=f"sha256:{text_hash}",
            text=chunk.text,
        )

    def _extract_entities(self, text: str) -> List[Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        lower = text.lower()
        for alias, (canonical, aliases) in KNOWN_ALIASES.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lower):
                found[canonical] = {
                    "canonical_name": canonical,
                    "aliases": aliases,
                    "entity_type": _entity_type(canonical, lower),
                    "is_defined": _looks_defined(canonical, aliases, lower),
                    "confidence": 0.9 if _looks_defined(canonical, aliases, lower) else 0.76,
                }

        for acronym in re.findall(r"\b[A-Z][A-Z0-9]{1,8}\b", text):
            canonical, aliases = KNOWN_ALIASES.get(acronym.lower(), (acronym, [acronym]))
            found.setdefault(
                canonical,
                {
                    "canonical_name": canonical,
                    "aliases": aliases,
                    "entity_type": _entity_type(canonical, lower),
                    "is_defined": _looks_defined(canonical, aliases, lower),
                    "confidence": 0.72,
                },
            )
        return list(found.values())

    def _entity_from_candidate(self, document: KnowledgeDocument, span: SourceSpan, item: Dict[str, Any]) -> KnowledgeEntity:
        defining_kb_id = document.kb_id if item["is_defined"] else None
        defining_doc_id = document.id if item["is_defined"] else None
        return KnowledgeEntity(
            id=stable_entity_id(item["canonical_name"]),
            canonical_name=item["canonical_name"],
            entity_type=item["entity_type"],
            description=_definition_sentence(item["canonical_name"], span.text) if item["is_defined"] else "",
            defining_kb_id=defining_kb_id,
            defining_doc_id=defining_doc_id,
            confidence=float(item["confidence"]),
            aliases=item["aliases"],
            source_span_ids=[span.id],
            metadata={"defined_in_current_doc": item["is_defined"]},
        )

    def _relations_from_chunk(
        self,
        document: KnowledgeDocument,
        span: SourceSpan,
        entities: List[Dict[str, Any]],
        text: str,
    ) -> List[KnowledgeRelation]:
        relations = []
        names = [item["canonical_name"] for item in entities]
        for subject in names:
            for obj in names:
                if subject == obj:
                    continue
                predicate, confidence = _infer_relation(subject, obj, text)
                if not predicate:
                    continue
                status = "active" if confidence >= self.min_relation_confidence else "candidate"
                subject_id = stable_entity_id(subject)
                object_id = stable_entity_id(obj)
                relations.append(
                    KnowledgeRelation(
                        id=stable_relation_id(subject_id, predicate, object_id, [span.id]),
                        subject_entity_id=subject_id,
                        predicate=predicate,
                        object_entity_id=object_id,
                        subject=subject,
                        object=obj,
                        source_kb_id=document.kb_id,
                        target_kb_id="",
                        evidence_span_ids=[span.id],
                        confidence=confidence,
                        status=status,
                    )
                )
        return relations


def _entity_type(canonical: str, lower_text: str) -> str:
    if canonical == "AXI4-Stream":
        return "protocol"
    if canonical.startswith("T") and canonical.isupper():
        return "interface_signal"
    if canonical in {"PCIe", "UCIe"}:
        return "external_standard" if canonical == "PCIe" else "protocol"
    if canonical in {"TLP", "DLLP", "Flow Control", "Transaction Layer"}:
        return "protocol_concept"
    if "depends" in lower_text or "requires" in lower_text:
        return "external_dependency"
    return "term"


def _looks_defined(canonical: str, aliases: List[str], lower_text: str) -> bool:
    for alias in aliases + [canonical]:
        escaped = re.escape(alias.lower())
        patterns = [
            rf"{escaped}\s+(is|are|means|refers to|defines|defined as)\b",
            rf"(definition of|defines)\s+{escaped}\b",
        ]
        if any(re.search(pattern, lower_text) for pattern in patterns):
            return True
    return False


def _definition_sentence(canonical: str, text: str) -> str:
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text.strip()):
        if canonical.lower() in sentence.lower():
            return sentence[:500]
    return ""


def _infer_relation(subject: str, obj: str, text: str) -> tuple[str, float]:
    lower = text.lower()
    axi_signals = {"TVALID", "TREADY", "TDATA", "TLAST", "TKEEP", "TSTRB", "TUSER", "TID", "TDEST"}
    if subject in axi_signals and obj == "AXI4-Stream":
        return "part_of", 0.86
    if obj in axi_signals and subject == "AXI4-Stream":
        return "defines", 0.82
    if {subject, obj} == {"TVALID", "TREADY"} and "handshake" in lower:
        return "handshake_with", 0.88
    if {subject, obj} == {"TKEEP", "TSTRB"} and any(word in lower for word in ("byte qualifier", "byte qualifiers", "qualification")):
        return "qualifies_with", 0.84
    if subject == "TLAST" and obj in {"TDATA", "AXI4-Stream"} and any(word in lower for word in ("packet", "boundary", "last transfer")):
        return "marks_boundary_of", 0.8
    if subject == "TLP" and obj in {"PCIe", "Transaction Layer"}:
        return "part_of", 0.86
    if subject == "DLLP" and obj == "PCIe":
        return "part_of", 0.84
    if subject.startswith("UCIe") and obj in {"PCIe", "TLP", "DLLP", "Flow Control", "Transaction Layer"}:
        if any(word in lower for word in ("depends", "requires", "related", "关联", "依赖")):
            return "depends_on", 0.84
        return "mentions", 0.74
    if "equivalent" in lower and {subject, obj} == {"PCIe", "PCI Express"}:
        return "equivalent_to", 0.9
    if subject in {"PCIe", "TLP", "DLLP"} and obj.startswith("UCIe"):
        return "", 0.0
    if subject != obj and any(word in lower for word in ("maps to", "mapped", "映射")):
        return "maps_to", 0.78
    return "", 0.0


def _dedupe_missing(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    seen = set()
    for item in items:
        key = item.get("term")
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
