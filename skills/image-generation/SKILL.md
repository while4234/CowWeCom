---
name: image-generation
description: Generate or edit images from text prompts or reference images. Use when the user asks to create, draw, design, or edit an image, illustration, photo, icon, poster, or other visual content.
metadata:
  cowagent:
    always: true
---

# Image Generation

## CowAgent Runtime

When this skill is used inside CowAgent, use the `image_generation_task` tool by
default. The tool creates a controlled background job and returns immediately;
the background worker sends the final image back to the original chat after
generation completes.

Provider routing is strict:

- Leave `runtime` omitted when the user asks for image generation without naming
  a provider. The background worker follows the active model backend: Grok
  backend users generate with Grok, while GPT backend users use the default
  Codex/GPT image runtime.
- Do not switch providers because the user says quality, high quality, speed,
  fast, draft, or similar preference words.
- Pass `"runtime": "codex_auth"` only when a Grok-backend user explicitly asks
  for GPT, OpenAI, Codex, or ChatGPT image generation.
- Pass `"runtime": "grok"` only when the user explicitly asks to use Grok, xAI,
  X.ai, a Grok account, or the Grok web image generator.
- Inside Grok runtime, quality mode is also explicit-only: use it only when the
  user says high quality, quality mode, HD, 高清, 高质量, 精细, or similar. If the
  user does not say that, Grok stays on the fast model even for photos, products,
  posters, or portraits.

Prompt enhancement is automatic and hidden:

- GPT/Codex image generation uses the full YouMind Nano Banana Pro prompt
  reference library under `../image-prompt-optimization/references/nano-banana-pro/`
  as a local retrieval source.
- Grok image generation does not use the YouMind library. It has a separate
  rewrite branch in `../grok-image-prompt-optimization/`: the runtime sends the
  user's prompt plus `templates/grok_image_system_prompt.txt` and optional random
  repository fragments to Grok's text model, then sends only the model-returned
  final prompt to Grok image generation.
- Do not manually paste the enhanced prompt into chat before generation. The
  runtime silently prepares the final prompt and keeps it out of ordinary status
  messages.
- GPT/Codex image generation is all-purpose: portraits, posters, product
  visuals, infographics, flowcharts, UI mockups, comics, icons, and edits all use
  the library category that best matches the request.
- If the user explicitly asks to see the prompt after an image is generated,
  call `image_generation_prompt_history` with `exact_only=true` and show the
  stored hidden prompt directly. Do not regenerate or rewrite the prompt again,
  and do not reveal it otherwise.

Do not run `scripts/generate.py` inside the normal chat turn. Long-running image
generation must stay out of the ordinary agent loop so the user can keep
chatting while the image is produced.

For this project, CowAgent uses direct Codex-auth runtime:

```json
"skill": {
  "image-generation": {
    "runtime": "codex_auth",
    "codex_auth_file": "",
    "prompt_enhancement_enabled": true,
    "prompt_library_dir": ""
  }
}
```

At startup, this config is flattened to:

- `SKILL_IMAGE_GENERATION_RUNTIME=codex_auth`
- `SKILL_IMAGE_GENERATION_CODEX_AUTH_FILE=<path>` when configured

In `codex_auth` runtime, `scripts/generate.py` reads the local Codex auth JSON,
uses the access token and account id to call the Codex image backend directly,
and writes the returned image bytes into the job `output_dir`. It does not call
the Codex CLI, a broker process, `/images/generations`, `/images/edits`, or any
third-party intermediary API.

The default auth location is `$CODEX_HOME/auth.json`, falling back to
`~/.codex/auth.json`. To override it for a controlled deployment, set
`skill.image-generation.codex_auth_file` or `CODEX_AUTH_FILE` to an auth JSON
path. Never paste tokens into chat or config, and never log token values.

The background worker is the only CowAgent runtime component that should call:

```text
<cow-home>\skills\image-generation\scripts\generate.py
```

Per-user isolation is handled by the runtime:

- Each actor has an independent FIFO queue.
- The same actor's image jobs run one at a time.
- Different actors can generate concurrently up to the configured global worker limit.
- Outputs are written under `~/cow/users/<memory_user_id>/files/image-generation/<job_id>/`.
- Provider fallback remains disabled unless explicitly configured outside Codex-auth runtime.

## WeChat Image-To-Image Flow

The `image_generation_task` tool accepts `image_url` as either a string or a
list of strings.

When the user asks to edit an image and the model does not explicitly pass
`image_url`, the tool extracts local image references from the current WeChat
message text:

```text
[图片: C:\path\input.jpg]
[image: C:\path\input.jpg]
```

Supported user flows:

- Single image edit: user sends an image, then sends an edit instruction within
  the channel cache window. CowWechat appends `[图片: path]`, and the tool passes
  that path to the direct Codex-auth provider.
- Quoted image edit: user replies to or quotes an image with an edit instruction.
  The WeChat channel downloads the quoted image and includes `[图片: path]`.
- Multi-image fusion: user sends several images, then asks to combine them. The
  tool passes `image_url` as a list.

If a request looks like image-to-image editing but no image reference is found,
return a direct prompt asking the user to send, reply to, or quote an image
first.

## Codex Auth Mode

The direct provider sends one streaming request to:

```text
https://chatgpt.com/backend-api/codex/responses
```

It uses the logged-in Codex access token as `Authorization: Bearer ...` and the
stored account id as `ChatGPT-Account-Id`. These values are read at runtime only
and are never printed in normal success or failure responses.

The script receives job JSON:

```json
{
  "prompt": "turn the outfit red, keep everything else unchanged",
  "image_url": "C:\\path\\input.jpg",
  "quality": "medium",
  "size": "1K",
  "aspect_ratio": "1:1",
  "output_dir": "C:\\path\\job-output"
}
```

For multi-image fusion, `image_url` is an array of paths. Local input images are
compressed if needed and embedded as `input_image` data URLs for the Codex
backend.

The script prints stdout JSON:

```json
{
  "images": [
    {"url": "C:\\path\\job-output\\result.png"}
  ]
}
```

It may also return:

```json
{"error": "human-readable error"}
```

## Standalone Script Runtime

Run `scripts/generate.py` with a JSON argument:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "A corgi astronaut floating in space"}'
```

Image-to-image:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Add a Santa hat to the dog", "runtime": "codex_auth", "image_url": "C:\\path\\dog.png"}'
```

Multi-image fusion:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Combine these characters into a group photo", "runtime": "codex_auth", "image_url": ["C:\\path\\a.png", "C:\\path\\b.png"]}'
```

Parameters:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | yes | - | Image description or edit instruction |
| `image_url` | string / list | no | null | Input image path(s) or URL(s) for image editing or fusion |
| `quality` | string | no | auto | `low` / `medium` / `high` |
| `size` | string | no | auto | `1K`, `2K`, `4K`, or pixel value such as `1024x1024` |
| `aspect_ratio` | string | no | null | `1:1`, `3:2`, `2:3`, `16:9`, `9:16` |
| `runtime` | string | no | active backend/env | Omit by default; use `codex_auth` only for explicit GPT/OpenAI/Codex image requests on Grok backend, and `grok` only for explicit Grok/xAI requests |

On success:

```json
{
  "model": "gpt-5.5",
  "images": [
    {"url": "C:\\path\\output.png"}
  ]
}
```

On error:

```json
{"error": "error message"}
```

Do not retry a failed Codex-auth job with the same parameters unless the auth
state or backend-side error has been fixed.

## Codex Prompt Shape

Use this structure inside the `prompt` string when it improves output quality:

```text
Use case: <photorealistic-natural | product-mockup | ui-mockup | infographic-diagram | illustration-story | stylized-concept | precise-object-edit | background-extraction>
Asset type: <where the image will be used>
Primary request: <user's exact goal>
Input images: <Image 1 role; Image 2 role> (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo, illustration, 3D, etc.>
Composition/framing: <camera, crop, placement>
Lighting/mood: <lighting and mood>
Text (verbatim): "<exact text, if any>"
Constraints: <must keep / must avoid>
Avoid: no watermark, no unintended logos, no extra text
```

## Verified Codex Demo Asset

This project includes `assets/codex-imagegen-demo.png`, created through Codex
built-in image generation and copied into this skill as a smoke-test artifact
proving that Codex-mode generation can work without an OpenAI API key in project
configuration.
