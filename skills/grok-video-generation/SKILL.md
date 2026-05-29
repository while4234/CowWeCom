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
generation. If the prompt contains `grokSfw`, missing-detail fragments are
selected 90% from `repositories/grokSfw/` and 10% from other repositories when
available. Raw direct calls may pass `prompt_enhancement=false` to bypass this.

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
