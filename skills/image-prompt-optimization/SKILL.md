---
name: image-prompt-optimization
description: Shared hidden prompt optimization resources for CowWeCom image and Grok video generation. Use when maintaining prompt libraries, Grok prompt rewrite templates, random prompt fragment repositories, or the GPT/Codex and Grok media prompt polishing workflow.
metadata:
  cowagent:
    always: true
---

# Image Prompt Optimization

This skill owns CowWeCom hidden prompt optimization resources.

Use it as the shared repository for:

- GPT/Codex image prompt enhancement with the existing Nano Banana Pro JSON library.
- Grok image and Grok video prompt rewriting with Grok's text model.
- Local random text-fragment repositories used to fill missing prompt details.

## Repository Layout

```text
skills/image-prompt-optimization/
  references/nano-banana-pro/      # existing YouMind/Nano Banana Pro JSON snapshot
  repositories/grok/               # YetAnotherWildcardCollection snapshot for Grok fragments
  repositories/general/            # optional fallback fragments
  templates/grok_image_system_prompt.txt
  templates/grok_video_system_prompt.txt
  scripts/select_prompt_fragments.py
```

## Runtime Rules

- GPT/Codex image generation keeps the existing category/filter scoring behavior
  over `references/nano-banana-pro/`.
- Grok image and Grok video generation use Grok's text model for hidden prompt
  rewriting, not the active GPT/Codex backend.
- Grok rewrite first strips repository trigger keywords from the user-visible
  visual request.
- If the prompt needs Grok rewriting and is not a raw/direct request, random
  missing-detail fragments are selected with this rule: 90% from
  `repositories/grok/`, 10% from other repositories when they contain fragments.
- If the user prompt contains `NSFW`, fragment selection prioritizes
  `repositories/grok/NSFW/` and, when available, includes one small
  non-priority supplement from `grok` non-NSFW files or another repository.
- Direct raw commands can still pass `prompt_enhancement=false` to bypass this
  hidden optimization path.

## Adding A Prompt Category Repository

Create a directory under `repositories/` whose folder name is the trigger
keyword. Put UTF-8 `.txt` files inside it. Each non-empty, non-comment line is
one selectable fragment.

Example:

```text
repositories/grok/
  Styling/Lighting.txt
  Styling/Composition.txt
  NSFW/POV.txt
```

```text
soft diffused window light, natural skin texture, clean background
half-body editorial framing, relaxed pose, clear face, balanced negative space
```

To add another keyword later, create another folder such as
  `repositories/myKeyword/`; when that exact keyword appears in the user's prompt,
the same 90% matched-repository / 10% other-repository rule applies. Without a
keyword, Grok defaults to the `grok` repository.

If the user asks to see the prompt that was just polished, call
`image_generation_prompt_history` with `exact_only=true`. That reads the stored
last enhanced prompt; do not regenerate or rewrite the prompt again.

After changing project skills, sync the same folder to:

```text
C:\Users\RondleLiu\cow\skills\image-prompt-optimization\
```
