"""Optional local knowledge backend.

The package is intentionally self-contained so the existing filesystem
knowledge service can keep working when this backend is disabled or optional
document parsing dependencies are absent.
"""

from .service import (
    KnowledgeBackendConfig,
    KnowledgeBackendService,
    LocalKnowledgeBackend,
    MissingProviderTokenError,
    build_knowledge_backend,
    dispatch_admin_request,
    dispatch_provider_request,
    get_backend_service,
    get_provider_bearer_token,
    parse_knowledge_backend_enabled,
    require_provider_token,
    verify_provider_bearer_token,
)

__all__ = [
    "KnowledgeBackendConfig",
    "KnowledgeBackendService",
    "LocalKnowledgeBackend",
    "MissingProviderTokenError",
    "build_knowledge_backend",
    "dispatch_admin_request",
    "dispatch_provider_request",
    "get_backend_service",
    "get_provider_bearer_token",
    "parse_knowledge_backend_enabled",
    "require_provider_token",
    "verify_provider_bearer_token",
]
