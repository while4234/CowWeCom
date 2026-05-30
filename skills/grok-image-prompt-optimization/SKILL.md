---
name: grok-image-prompt-optimization
description: Grok-only hidden image prompt polishing and random prompt generation. Use for Grok image rewrite, random Grok image prompts, fuzzy repository fragment matching, and nsfw/NSFW/大尺度 Grok image fragment selection.
metadata:
  cowagent:
    always: true
---

# Grok Image Prompt Optimization

This skill owns CowWeCom's Grok image prompt optimization resources only.

Use it for:

- Hidden Grok image prompt rewriting before non-direct Grok image generation.
- Random Grok image prompt generation, such as `随机生成 cyberpunk 提示词`.
- Random Grok image creation prompts, such as `随机生成 cyberpunk 的图片`.
- Fuzzy matching a short user topic against the local Grok fragment repository.
- Selecting Grok NSFW fragments when the user prompt contains `nsfw`, `NSFW`, or `大尺度`.

## Repository Layout

```text
skills/grok-image-prompt-optimization/
  repositories/grok/               # Grok image fragments
  templates/grok_image_system_prompt.txt
  scripts/select_prompt_fragments.py
```

## Runtime Rules

- This skill is Grok image only. Do not use it for GPT/Codex image generation or Grok video generation.
- Grok image prompt polishing must use Grok's own text model through `GrokBot`, not the active GPT/Codex backend.
- Direct/raw Grok image commands can pass `prompt_enhancement=false` to bypass this hidden rewrite path.
- Normal Grok image generation silently rewrites the prompt, stores metadata, and sends only the final English prompt to Grok image generation.
- If the user asks to see the prompt after generation, call `image_generation_prompt_history` with `exact_only=true`; do not regenerate the prompt.

## Random Prompt Rules

- `随机生成 XXX 提示词` / `随机给我 XXX 提示词` means: build one final English Grok image prompt and provide a Chinese translation for display. Do not create an image.
- If the user asks for a random prompt and does not explicitly say `文生图`, default to an image-to-image prompt. `图生图提示词` is still a prompt-text request, not an image generation/editing request.
- `随机生成 XXX 的图片`, `随机生成 XXX 生图`, or equivalent image-generation wording means: build the hidden Grok prompt and submit the image job.
- The short topic `XXX` should be used to fuzzy-match directory names, file names, and fragment text under `repositories/grok/`.
- Random fragment selection is owned by the deterministic repository script/code. Grok only performs final polishing and removes details that violate these rules.
- Random fragments fill missing details only; they must not override the user's subject, intent, requested text, names, numbers, or constraints.
- Do not add generic quality/style booster phrases such as `soft cinematic lighting`, `highly detailed`, `realistic skin texture`, `sensual atmosphere`, `8k`, `4k`, `UHD`, `HDR`, `masterpiece`, or `best quality`.
- For image-to-image prompts, preserve the reference subject's identity and original expression/gaze. Do not introduce new hair, eye, skin, age, ethnicity, body-shape, facial-feature, expression, gaze, mouth-pose, or attractiveness descriptors unless the user explicitly requests them.

## NSFW Control

- Treat `nsfw`, `NSFW`, and `大尺度` as internal repository-selection control keywords.
- Strip those literal control words from the user-visible source prompt and the final prompt.
- For missing details in NSFW-controlled prompts, select about 90% from `repositories/grok/NSFW/` and about 10% from matching non-NSFW Grok categories when available.
- Non-NSFW supplements should come from the fuzzy-matched Grok category first; otherwise use safe context categories such as `Background`, `Styling`, `Colors`, or `Materials`.

## Reference Image Rule

When a reference image is provided, use the reference-image identity lock. Preserve the reference subject's face, facial structure, original expression/gaze, skin tone/texture, hair, distinctive features, and general body proportions unless the user explicitly asks to change them. Do not add random ethnicity, eye color, hair color/style, age, body type, expression, gaze, mouth-pose, or facial traits.

After changing this skill, sync the same folder to:

```text
C:\Users\Hi\cow\skills\grok-image-prompt-optimization\
```
