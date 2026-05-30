---
name: image-prompt-optimization
description: Hidden prompt optimization resources for non-Grok image generation. Use for GPT/Codex image prompt enhancement with the local Nano Banana Pro prompt reference library.
metadata:
  cowagent:
    always: true
---

# Image Prompt Optimization

This skill owns CowWeCom hidden prompt optimization resources for non-Grok image generation.

Use it as the shared repository for GPT/Codex image prompt enhancement with the existing Nano Banana Pro JSON library.

## Repository Layout

```text
skills/image-prompt-optimization/
  references/nano-banana-pro/      # YouMind/Nano Banana Pro JSON snapshot
```

## Runtime Rules

- GPT/Codex image generation uses the category/filter scoring behavior over `references/nano-banana-pro/`.
- This skill does not contain Grok prompt rewrite templates, Grok random fragment repositories, or Grok image/video runtime rules.
- Grok image prompt polishing belongs to `skills/grok-image-prompt-optimization/`.
- Grok video generation must not depend on this skill.

## Adding Non-Grok Prompt References

Add new non-Grok image prompt reference libraries under `references/` and update the runtime resolver before using them. Do not place Grok-specific repositories or templates in this skill.

After changing project skills, sync the same folder to:

```text
C:\Users\Hi\cow\skills\image-prompt-optimization\
```
