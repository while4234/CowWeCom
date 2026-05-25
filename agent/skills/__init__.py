"""
Skills module for agent system.

This module provides the framework for loading, managing, and executing skills.
Skills are markdown files with frontmatter that provide specialized instructions
for specific tasks.
"""

from agent.skills.types import (
    Skill,
    SkillEntry,
    SkillMetadata,
    SkillInstallSpec,
    LoadSkillsResult,
)
from agent.skills.loader import SkillLoader
from agent.skills.manager import SkillManager
from agent.skills.service import SkillService
from agent.skills.formatter import format_skills_for_prompt
from agent.skills.cache import SkillCatalogCache, SkillCatalogEntry, get_skill_catalog_cache

__all__ = [
    "Skill",
    "SkillEntry",
    "SkillMetadata",
    "SkillInstallSpec",
    "LoadSkillsResult",
    "SkillLoader",
    "SkillManager",
    "SkillService",
    "SkillCatalogCache",
    "SkillCatalogEntry",
    "get_skill_catalog_cache",
    "format_skills_for_prompt",
]
