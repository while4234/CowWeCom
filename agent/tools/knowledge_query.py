"""Internal tool for querying the optional structured knowledge backend."""

from __future__ import annotations

from typing import Any, Dict

from agent.tools.base_tool import BaseTool, ToolResult


class KnowledgeQueryTool(BaseTool):
    """Search or query the optional CowAgent knowledge backend."""

    name: str = "knowledge_query"
    description: str = (
        "Query the optional local structured knowledge backend. Use this for "
        "source-backed local knowledge, deep evidence bundles, citations, entity resolution and graph neighbors. "
        "For specifications, protocols, state machines, step-by-step flows, mappings, timing, registers, tables, "
        "or comparison/confirmation questions, prefer action=deep_query before answering. If deep_query returns "
        "insufficient evidence or missing key terms, say what is not proven instead of giving a certain conclusion."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Action: deep_query, query, search, resolve_entities, graph_neighbors, verify_source, status.",
            },
            "query": {
                "type": "string",
                "description": "Question or search query for query/search actions.",
            },
            "terms": {
                "type": "array",
                "description": "Terms to resolve for resolve_entities.",
                "items": {"type": "string"},
            },
            "entity_id": {
                "type": "string",
                "description": "Entity id or canonical name for graph_neighbors.",
            },
            "claim": {
                "type": "string",
                "description": "Claim to verify against source spans.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum search/query results to return.",
            },
            "context_window": {
                "type": "integer",
                "description": "For deep_query, number of adjacent chunks to include before and after each hit.",
            },
            "max_evidence_chars": {
                "type": "integer",
                "description": "For deep_query, maximum source text characters returned in evidence blocks.",
            },
            "max_hops": {
                "type": "integer",
                "description": "Maximum graph traversal hops.",
            },
        },
        "required": ["action"],
    }

    def execute(self, params: Dict[str, Any]) -> ToolResult:
        action = str(params.get("action") or "query").strip().lower()
        try:
            from agent.knowledge.backend import KnowledgeBackendConfig, build_knowledge_backend

            service = build_knowledge_backend(KnowledgeBackendConfig.from_project_config())
            if action == "status":
                return ToolResult.success(_jsonable(service.status()))
            if action == "search":
                return ToolResult.success(
                    {
                        "results": _jsonable(
                            service.search(str(params.get("query") or ""), limit=int(params.get("limit") or 5))
                        )
                    }
                )
            if action == "query":
                return ToolResult.success(
                    _jsonable(service.query(str(params.get("query") or ""), limit=int(params.get("limit") or 5)))
                )
            if action == "deep_query":
                return ToolResult.success(
                    _jsonable(
                        service.deep_query(
                            str(params.get("query") or ""),
                            limit=int(params.get("limit") or 5),
                            context_window=_optional_int(params.get("context_window")),
                            max_evidence_chars=_optional_int(params.get("max_evidence_chars")),
                        )
                    )
                )
            if action == "resolve_entities":
                terms = params.get("terms") or []
                if isinstance(terms, str):
                    terms = [terms]
                return ToolResult.success(_jsonable(service.resolve_entities(terms)))
            if action == "graph_neighbors":
                return ToolResult.success(
                    _jsonable(
                        service.graph_neighbors(
                            entity_id=str(params.get("entity_id") or ""),
                            max_hops=int(params.get("max_hops") or 1),
                        )
                    )
                )
            if action == "verify_source":
                return ToolResult.success(_jsonable(service.verify_source(str(params.get("claim") or ""))))
            return ToolResult.fail(f"Unsupported knowledge_query action: {action}")
        except Exception as exc:
            return ToolResult.fail(f"knowledge_query failed: {exc}")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _optional_int(value: Any) -> Any:
    if value in (None, ""):
        return None
    return int(value)
