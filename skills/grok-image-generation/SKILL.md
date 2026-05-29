---
name: grok-image-generation
description: Use when the active model backend is Grok or when the user explicitly asks to generate an image with Grok, xAI, X.ai, a Grok account, or the Grok web image generator. Do not use this skill for ordinary image requests from GPT-backend users that only mention quality or speed preferences.
metadata:
  cowagent:
    always: true
---

# Grok Image Generation

Use this skill for image requests while the active model backend is Grok, or for
explicit Grok/xAI image requests.

Provider selection rule:

- If the user asks to generate, draw, create, design, or make an image without
  mentioning a provider, leave `runtime` omitted. The runtime uses Grok when the
  user's active model backend is Grok; GPT-backend users stay on the default
  Codex/GPT image runtime.
- If the user only says high quality, quality mode, speed mode, fast, quick,
  draft, or similar preference words, keep the active backend default. Do not
  switch a GPT-backend user to Grok, and do not switch a Grok-backend user to
  GPT.
- If a Grok-backend user explicitly asks for GPT, OpenAI, Codex, or ChatGPT
  image generation, use `image_generation_task` with `"runtime": "codex_auth"`.
- If Grok/xAI is explicitly mentioned, use `image_generation_task` with
  `"runtime": "grok"`.

Grok PR3 supports text-to-image only. For image editing, image fusion, or video,
tell the user Grok mode is not supported for that operation yet and use the
default Codex/GPT image runtime when the user accepts that path.

When using Grok:

- Pass the user's visual request in `prompt`.
- Pass `aspect_ratio` when the user asks for landscape, portrait, square,
  `16:9`, `9:16`, `4:3`, `3:4`, `3:2`, or `2:3`.
- Pass `size` only for clear resolution requests such as `1K`, `2K`, `1024`,
  or `2048`.
- Pass `quality` for explicit preference words: `quality` or `high` for quality
  mode, `speed` or `fast` for speed mode.
- If quality/speed is not explicit, omit `quality`; Grok defaults to the fast
  model. Do not infer quality mode from the image type alone.
- Hidden prompt enhancement is automatic after Grok model selection. The runtime
  uses the full YouMind Nano Banana Pro library and adapts the final prompt for
  high-aesthetic people/portrait photography by default, unless the user
  explicitly asks for a non-portrait image such as a poster, product visual, or
  diagram.
- Do not display the enhanced prompt during generation. If the user explicitly
  asks to see the prompt after the image is generated, use
  `image_generation_prompt_history`.

Example explicit Grok request:

```json
{
  "prompt": "A detailed product poster for a matte black smart speaker on a white studio table",
  "runtime": "grok",
  "quality": "high",
  "aspect_ratio": "3:4",
  "size": "2K"
}
```

Example ordinary request on a Grok backend; omit runtime so it stays Grok:

```json
{
  "prompt": "Draw a quick cute sticker of a smiling coffee cup",
  "quality": "speed"
}
```

Example explicit GPT image request from a Grok-backend user:

```json
{
  "prompt": "Use GPT to draw a quick cute sticker of a smiling coffee cup",
  "runtime": "codex_auth",
  "quality": "speed"
}
```
