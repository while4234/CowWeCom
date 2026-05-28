# encoding:utf-8

"""xAI/Grok model capability metadata copied from Hermes behavior."""

_GROK_EFFORT_CAPABLE_PREFIXES = (
    "grok-3-mini",
    "grok-4.20-multi-agent",
    "grok-4.3",
)


def grok_supports_reasoning_effort(model: str) -> bool:
    """Return True only for Grok models that accept reasoning.effort."""
    name = (model or "").strip().lower()
    if not name:
        return False
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return any(name.startswith(prefix) for prefix in _GROK_EFFORT_CAPABLE_PREFIXES)
