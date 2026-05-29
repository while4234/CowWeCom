---
name: grok-video-generation
description: Generate Grok/xAI videos from text prompts, one reference image, or multiple recent WeChat/WeCom image references.
metadata:
  cowagent:
    always: true
---

# Grok Video Generation

Use this skill for video generation requests in CowAgent. Grok/xAI is the
project's configured v1 video provider, so ordinary "生成视频", text-to-video,
image-to-video, and "让图片动起来" requests should use this skill.

Provider selection rule:

- If the user asks to create, generate, animate, or make a video, use
  `grok_video_generation_task`.
- If Grok/xAI is explicitly mentioned, still use `grok_video_generation_task`.
- Do not run `scripts/generate.py` inside the normal chat turn. Long-running
  generation must stay in the background job system.

The tool supports:

- Text-to-video with only `prompt`.
- Image-to-video with `image_url` as a string or list.
- WeChat/WeCom image references embedded in message text:

```text
[图片: C:\path\input.jpg]
[image: C:\path\input.jpg]
```

At most 7 image references are passed to the provider.

Hidden prompt rewriting is automatic for normal Grok video generation. The
runtime uses `skills/image-prompt-optimization/templates/grok_video_system_prompt.txt`,
optional random fragments from `skills/image-prompt-optimization/repositories/`,
and Grok's own text model before submitting the final prompt to xAI video
generation. Missing-detail fragments are selected 90% from `repositories/grok/`
and 10% from other repositories when available. If the prompt contains `NSFW`
or `nsfw`, treat that token as an internal control keyword, not final prompt
text: selection prioritizes `repositories/grok/NSFW/` and can include one small
non-priority supplement from safe context categories such as background,
styling, color, or material. If the user specifies a nationality/ethnicity such
as `Korean`, `Korea`, or `韩国`, preserve it as a mandatory stable constraint and
filter random fragments that would introduce conflicting identity traits. Raw
direct calls may pass `prompt_enhancement=false` to bypass this.

When `image_url` or recent image references are present, preserve the reference
subject's face, identity, hair, skin tone/texture, distinctive features, and
body proportions across frames. Normal rewrite and direct/raw submission both
append a reference-image identity lock; rewrite should not add new ethnicity,
eye color, hair color, age, body type, or facial traits unless the user
explicitly asks to change them.

If the user asks to see the prompt after the video is generated, call
`image_generation_prompt_history` with `exact_only=true`. It reads the stored
last rewritten prompt; direct/raw Grok video commands also write their final
prompt into the same history. Do not regenerate the rewrite.

Example text-to-video task:

```json
{
  "prompt": "Use Grok to generate a cinematic 16:9 video of a futuristic train arriving at a neon station",
  "aspect_ratio": "16:9",
  "duration": "5s"
}
```

Example image-to-video task:

```json
{
  "prompt": "Use Grok to animate this product photo with a slow dolly-in camera move",
  "image_url": "C:\\path\\product.jpg",
  "aspect_ratio": "9:16"
}
```

## Standalone Script Runtime

The background worker calls:

```bash
python <base_dir>/scripts/generate.py '{"prompt": "Use Grok to generate a short cinematic video"}'
```

The script calls `integrations.hermes_xai.video_gen.XAIVideoGenProvider`.
It prints JSON only to stdout and routes logs/errors to stderr.

On success:

```json
{
  "videos": [
    {"url": "C:\\path\\job-output\\result.mp4"}
  ]
}
```

On error:

```json
{"error": "human-readable error"}
```
