# encoding:utf-8

import copy
import json
import logging
import os
import pickle

from common.log import logger
from common.llm_backend_router import DEFAULT_LLM_BACKEND_CONFIG


_DEFAULT_KNOWLEDGE_BACKEND_CONFIG = {
    "enabled": False,
    "mode": "backend_subsystem",
    "fail_open": True,
    "admin_api_enabled": True,
    "provider_api_enabled": False,
    "data_dir": "./public_protocol_knowledge",
    "sqlite_path": "./public_protocol_knowledge/indexes/kb.sqlite",
    "workspace_root": ".",
    "default_kb_id": "kb_default",
    "ingest": {
        "auto_build_after_upload": True,
        "allowed_extensions": [".pdf", ".docx", ".txt", ".md"],
        "allowed_import_roots": [],
        "max_file_size_mb": 500,
        "document_library_root": "~/cow",
        "preserve_original": True,
        "ocr_enabled": False,
    },
    "llm_builder": {
        "enabled": True,
        "use_current_model": True,
        "fallback_model": "",
        "batch_chunks": 8,
        "max_chunks": 80,
        "max_output_tokens": 6000,
        "auto_generate_study_doc": False,
        "index_generated_document": True,
        "require_source_spans": True,
        "min_relation_confidence": 0.70,
    },
    "retrieval": {
        "auto_inject": True,
        "hybrid": True,
        "keyword_top_k": 80,
        "vector_top_k": 80,
        "rerank_top_k": 30,
        "final_top_k": 10,
        "deep_query_enabled": True,
        "context_window_chunks": 1,
        "deep_top_k": 5,
        "max_evidence_chars": 12000,
        "enable_federated": True,
        "max_federated_hops": 2,
        "source_verification": "auto",
    },
    "vector_store": {
        "provider": "sqlite",
        "url": "http://127.0.0.1:6333",
        "collection": "cowagent_knowledge",
        "required": False,
    },
    "security": {
        "require_web_auth": True,
        "provider_api_token_env": "KNOWLEDGE_PROVIDER_TOKEN",
        "disable_admin_api_when_web_password_empty": True,
        "log_full_text": False,
    },
}


_DEFAULT_MEMORY_DEEP_DREAM_CONFIG = {
    "enabled": True,
    "check_time": "00:00",
    "catch_up_on_startup": True,
    "catch_up_days": 1,
    "flush_active_agents": True,
    "include_user_memories": True,
    "state_path": "",
}


_KNOWLEDGE_BACKEND_ENV_MAP = {
    "KNOWLEDGE_BACKEND_ENABLED": ("enabled", "bool"),
    "KNOWLEDGE_BACKEND_FAIL_OPEN": ("fail_open", "bool"),
    "KNOWLEDGE_BACKEND_ADMIN_API_ENABLED": ("admin_api_enabled", "bool"),
    "KNOWLEDGE_BACKEND_PROVIDER_API_ENABLED": ("provider_api_enabled", "bool"),
    "KNOWLEDGE_BACKEND_DATA_DIR": ("data_dir", "str"),
    "KNOWLEDGE_BACKEND_SQLITE_PATH": ("sqlite_path", "str"),
    "KNOWLEDGE_BACKEND_WORKSPACE_ROOT": ("workspace_root", "str"),
    "KNOWLEDGE_BACKEND_DEFAULT_KB_ID": ("default_kb_id", "str"),
    "KNOWLEDGE_BACKEND_AUTO_BUILD": ("ingest.auto_build_after_upload", "bool"),
    "KNOWLEDGE_BACKEND_ALLOWED_EXTENSIONS": ("ingest.allowed_extensions", "csv"),
    "KNOWLEDGE_BACKEND_ALLOWED_IMPORT_ROOTS": ("ingest.allowed_import_roots", "csv"),
    "KNOWLEDGE_BACKEND_MAX_FILE_SIZE_MB": ("ingest.max_file_size_mb", "int"),
    "KNOWLEDGE_BACKEND_DOCUMENT_LIBRARY_ROOT": ("ingest.document_library_root", "str"),
    "KNOWLEDGE_BACKEND_LLM_BUILDER_ENABLED": ("llm_builder.enabled", "bool"),
    "KNOWLEDGE_BACKEND_LLM_AUTO_GENERATE_STUDY_DOC": ("llm_builder.auto_generate_study_doc", "bool"),
    "KNOWLEDGE_BACKEND_LLM_INDEX_GENERATED_DOCUMENT": ("llm_builder.index_generated_document", "bool"),
    "KNOWLEDGE_BACKEND_LLM_MAX_CHUNKS": ("llm_builder.max_chunks", "int"),
    "KNOWLEDGE_BACKEND_LLM_MAX_OUTPUT_TOKENS": ("llm_builder.max_output_tokens", "int"),
    "KNOWLEDGE_BACKEND_AUTO_INJECT": ("retrieval.auto_inject", "bool"),
    "KNOWLEDGE_BACKEND_DEEP_QUERY_ENABLED": ("retrieval.deep_query_enabled", "bool"),
    "KNOWLEDGE_BACKEND_DEEP_CONTEXT_WINDOW_CHUNKS": ("retrieval.context_window_chunks", "int"),
    "KNOWLEDGE_BACKEND_DEEP_TOP_K": ("retrieval.deep_top_k", "int"),
    "KNOWLEDGE_BACKEND_MAX_EVIDENCE_CHARS": ("retrieval.max_evidence_chars", "int"),
    "KNOWLEDGE_BACKEND_VECTOR_PROVIDER": ("vector_store.provider", "str"),
    "KNOWLEDGE_BACKEND_QDRANT_URL": ("vector_store.url", "str"),
    "KNOWLEDGE_BACKEND_QDRANT_COLLECTION": ("vector_store.collection", "str"),
    "KNOWLEDGE_BACKEND_VECTOR_REQUIRED": ("vector_store.required", "bool"),
    "KNOWLEDGE_BACKEND_PROVIDER_TOKEN_ENV": ("security.provider_api_token_env", "str"),
    "KNOWLEDGE_BACKEND_DISABLE_ADMIN_WHEN_NO_PASSWORD": ("security.disable_admin_api_when_web_password_empty", "bool"),
}

# 将所有可用的配置项写在字典里, 请使用小写字母
# 此处的配置值无实际意义，程序不会读取此处的配置，仅用于提示格式，请将配置加入到config.json中
available_setting = {
    # openai api配置
    "open_ai_api_key": "",  # openai api key
    # openai apibase，当use_azure_chatgpt为true时，需要设置对应的api base
    "open_ai_api_base": "https://api.openai.com/v1",
    "open_ai_wire_api": "chat_completions",  # OpenAI wire API: chat_completions or responses
    "openai_wire_api": "",  # Alias for open_ai_wire_api
    "wire_api": "",  # Alias for Codex-style config; when set to "responses" uses /v1/responses
    "disable_response_storage": False,  # When using Responses API, set store=false
    "model_reasoning_effort": "",  # Responses reasoning effort: none/low/medium/high/xhigh
    "model_context_window": 0,  # Optional explicit model context window
    "model_auto_compact_token_limit": 0,  # Optional compact limit hint for Agent mode
    "enable_prompt_cache_key": True,  # Send prompt_cache_key for OpenAI Responses requests
    "prompt_cache_key_prefix": "cowwechat",  # Stable namespace for prompt cache routing
    "prompt_cache_key_granularity": "channel",  # global/channel/session
    "prompt_cache_retention": "",  # Optional: in_memory or 24h when supported upstream
    "llm_usage_tracking": True,  # Persist token/cache usage counters for the web dashboard
    "llm_usage_history_limit": 2000,  # Max local usage records to keep
    "llm_usage_user_labels": {},  # Optional usage-dashboard display labels keyed by actor id, raw id, or telemetry hash
    "llm_usage_user_aliases": {},  # Optional usage-dashboard merge aliases: alias id/hash/label -> canonical id/hash/label
    "project_optimizer_evidence_enabled": True,  # Local-only evidence for CowWeCom project optimization
    "project_optimizer_raw_capture_enabled": True,  # Store raw model/user inputs locally until optimizer consumes them
    "project_optimizer_preserve_temp_scripts": True,  # Snapshot tmp/workspace scripts into ignored optimizer archive
    "project_optimizer_delete_raw_after_run": True,  # Optimizer skill deletes consumed raw input cache after report
    "project_optimizer_data_dir": "",  # Default: <agent_workspace>/data/project-optimizer
    "project_optimizer_raw_max_string_chars": 20000,
    "project_optimizer_raw_max_payload_chars": 250000,
    "project_optimizer_temp_script_max_bytes": 1000000,
    "reasoning_effort_policy_runtime_auto_optimize_enabled": False,  # Keep legacy in-Agent optimizer disabled unless explicitly enabled
    "cowagent_self_evolution_post_task_enabled": True,  # Run background lesson mining after completed Agent tasks
    "cowagent_self_evolution_post_task_max_texts": 12,  # Max assistant process statements per reflection
    "cowagent_self_evolution_post_task_max_chars": 6000,  # Max process-text chars per reflection
    "cowagent_self_evolution_post_task_queue_size": 10,  # Bounded background reflection queue
    "cowagent_self_evolution_skip_medium_context": True,  # Skip dynamic self-evolution guidance for simple medium-effort requests
    "prompt_cache_stable_runtime_info": True,  # Keep volatile runtime time out of the reusable prompt prefix
    "runtime_time_in_user_message": True,  # Add exact current time only to the active user request
    "knowledge_index_in_system_prompt": False,  # Keep changing knowledge index out of the stable prompt prefix
    "knowledge_auto_retrieval": False,  # Auto-inject local Markdown knowledge search results into the current request
    "knowledge_auto_retrieval_max_results": 5,
    "knowledge_auto_retrieval_min_score": 0.1,
    "knowledge_auto_retrieval_max_chars": 4000,
    "claude_api_base": "https://api.anthropic.com/v1",  # claude api base
    "gemini_api_base": "https://generativelanguage.googleapis.com",  # gemini api base
    "custom_api_key": "",  # custom OpenAI-compatible provider api key (used when bot_type is "custom")
    "custom_api_base": "",  # custom OpenAI-compatible provider api base (used when bot_type is "custom")
    "grok_model": "grok-4.3",  # xAI Grok model used when bot_type is "grok" or "xai"
    "grok_api_base": "https://api.x.ai/v1",  # xAI API base; OAuth bearer is only sent to xAI origins
    "grok_auth_file": "",  # Grok OAuth auth store; defaults to data/auth/grok_auth.json
    "grok_auth_prefer_oauth": True,  # Prefer Web OAuth tokens before API key fallback
    "grok_oauth_accept_bare_code": False,  # Optional legacy manual paste: accept bare authorization code only with active PKCE login
    "grok_gray_enabled": False,  # Show Grok in normal Web model provider selection only for gray testing
    "grok_import_hermes_auth": True,  # Read-only import from ~/.hermes/auth.json when CowWeCom auth store is absent
    "grok_import_hermes_auth_overwrite": False,  # Do not overwrite CowWeCom xai-oauth unless explicitly enabled
    "grok_wire_api": "responses",  # Grok uses Responses API
    "grok_api_key": "",  # Fallback xAI API key when OAuth is unavailable
    "codex_auth_file": "",  # Optional Codex auth.json path; defaults to CODEX_AUTH_FILE or ~/.codex/auth.json
    "codex_base_url": "https://chatgpt.com/backend-api/codex",
    "codex_endpoint_path": "/responses",
    "codex_timeout_seconds": 60,
    "codex_max_response_bytes": 5000000,
    "codex_max_error_response_bytes": 200000,
    "codex_user_agent": "codex_cli/0.126.0-alpha.8",
    "codex_originator": "codex_vscode",
    "codex_reasoning_effort": "",
    "proxy": "",  # openai使用的代理
    # chatgpt模型， 当use_azure_chatgpt为true时，其名称为Azure上model deployment名称
    "model": "gpt-3.5-turbo",  # 可选择: gpt-4o, pt-4o-mini, gpt-4-turbo, claude-3-sonnet, wenxin, moonshot, qwen-turbo, xunfei, glm-4, minimax, gemini等模型，全部可选模型详见common/const.py文件
    "bot_type": "",  # 可选配置；启用 Grok 时填 "grok" 或 "xai" 并用 grok_model 控制 xAI 模型；兼容 OpenAI 三方服务可填 "openai" 或 "custom"。如不填根据 model 名称判断
    "use_azure_chatgpt": False,  # 是否使用azure的chatgpt
    "azure_deployment_id": "",  # azure 模型部署名称
    "azure_api_version": "",  # azure api版本
    # Bot触发配置
    "single_chat_prefix": ["bot", "@bot"],  # 私聊时文本需要包含该前缀才能触发机器人回复
    "single_chat_image_recognition": True,  # 私聊收到单张图片时是否自动识图并缓存上下文
    "single_chat_image_recognition_auto_reply": False,  # 非账单私聊图片识别完成后是否主动回复；账单自动记账不受影响
    "single_chat_image_recognition_prompt": "请先识别这张图片，再结合当前短期对话上下文和可用的长期记忆来回答用户。不要只给图片说明；如果图片与已知偏好、任务、人物或正在讨论的事情相关，请把这些上下文一起用于回复。图中文字请提取关键内容；看不清时说明不确定之处。",
    "background_image_recognition_enabled": True,
    "image_recognition_result_ttl_seconds": 86400,
    "image_recognition_image_ttl_seconds": 604800,
    "image_recognition_related_followup_window_seconds": 900,
    "image_recognition_followup_wait_seconds": 6,
    "image_recognition_workers": 2,
    "image_recognition_max_tokens": 700,
    "image_recognition_prompt": "Identify this image for a later chat follow-up. Keep it natural and short. Mention the main subject, visible action or scene, important text/OCR, and uncertainty if needed. Do not use report headings or formal sections unless the image itself is a document where key text matters. If it is a bill, payment receipt, or order detail, extract the visible date, exact amount, platform, merchant, item, and payment method. Preserve the exact amount and decimal places shown in the screenshot; do not estimate or round it to an integer. If unclear, say it is uncertain instead of guessing.",
    "single_chat_reply_prefix": "[bot] ",  # 私聊时自动回复的前缀，用于区分真人
    "single_chat_reply_suffix": "",  # 私聊时自动回复的后缀，\n 可以换行
    "group_chat_prefix": ["@bot"],  # 群聊时包含该前缀则会触发机器人回复
    "no_need_at": False,  # 群聊回复时是否不需要艾特
    "group_chat_reply_prefix": "",  # 群聊时自动回复的前缀
    "group_chat_reply_suffix": "",  # 群聊时自动回复的后缀，\n 可以换行
    "group_chat_keyword": [],  # 群聊时包含该关键词则会触发机器人回复
    "group_at_off": False,  # 是否关闭群聊时@bot的触发
    "group_name_white_list": ["ChatGPT测试群", "ChatGPT测试群2"],  # 开启自动回复的群名称列表
    "group_name_keyword_white_list": [],  # 开启自动回复的群名称关键词列表
    "group_chat_in_one_session": ["ChatGPT测试群"],  # 支持会话上下文共享的群名称
    "group_shared_session": False,  # 群聊是否共享会话上下文（所有成员共享）。False时每个用户在群内有独立会话
    "nick_name_black_list": [],  # 用户昵称黑名单
    "group_welcome_msg": "",  # 配置新人进群固定欢迎语，不配置则使用随机风格欢迎
    "trigger_by_self": False,  # 是否允许机器人触发
    "text_to_image": "dall-e-2",  # 图片生成模型，可选 dall-e-2, dall-e-3
    "grok_image_model": "grok-imagine-image",
    "grok_image_resolution": "1k",
    "grok_image_aspect_ratio": "square",
    "grok_image_timeout_seconds": 120,
    "grok_image_download_timeout_seconds": 60,
    "video_generation_provider": "xai",
    "video_create_prefix": ["生成视频", "视频生成", "画个视频"],
    "grok_video_model": "grok-imagine-video",
    "grok_video_duration": 8,
    "grok_video_aspect_ratio": "16:9",
    "grok_video_resolution": "720p",
    "grok_video_timeout_seconds": 240,
    "grok_video_poll_interval_seconds": 5,
    "grok_video_download_timeout_seconds": 120,
    # Azure OpenAI dall-e-3 配置
    "dalle3_image_style": "vivid", # 图片生成dalle3的风格，可选有 vivid, natural
    "dalle3_image_quality": "hd", # 图片生成dalle3的质量，可选有 standard, hd
    # Azure OpenAI DALL-E API 配置, 当use_azure_chatgpt为true时,用于将文字回复的资源和Dall-E的资源分开.
    "azure_openai_dalle_api_base": "", # [可选] azure openai 用于回复图片的资源 endpoint，默认使用 open_ai_api_base
    "azure_openai_dalle_api_key": "", # [可选] azure openai 用于回复图片的资源 key，默认使用 open_ai_api_key
    "azure_openai_dalle_deployment_id":"", # [可选] azure openai 用于回复图片的资源 deployment id，默认使用 text_to_image
    "image_proxy": True,  # 是否需要图片代理，国内访问LinkAI时需要
    "image_create_prefix": ["画图", "生图", "生成图片", "生成图", "绘图", "出图"],  # 开启图片生成的显式前缀
    "concurrency_in_session": 1,  # 同一会话最多有多少条消息在处理中，大于1可能乱序
    "image_prompt_enhancement_enabled": True,  # Hidden YouMind prompt-library enhancement for GPT/Grok image generation
    "image_prompt_library_dir": "",  # Optional full YouMind prompt library override; defaults to skills/image-generation/references/nano-banana-pro
    "image_create_size": "256x256",  # 图片大小,可选有 256x256, 512x512, 1024x1024 (dall-e-3默认为1024x1024)
    "image_create_format": "png",  # OpenAI GPT Image output format: png, jpeg, or webp
    "image_create_quality": "",  # OpenAI GPT Image quality: auto, low, medium, or high
    "image_send_max_width": 2048,  # 微信/企业微信发送图片前的最大宽度
    "image_send_max_height": 2048,  # 微信/企业微信发送图片前的最大高度
    "weixin_image_send_max_bytes": 10485759,  # 微信发送图片前的最大字节数
    "wecom_image_send_max_bytes": 2097152,  # 企业微信智能机器人图片上传前的最大字节数
    "wechatcom_image_send_max_bytes": 10485759,  # 企业微信自建应用图片上传前的最大字节数
    "group_chat_exit_group": False,
    # chatgpt会话参数
    "expires_in_seconds": 3600,  # 无操作会话的过期时间
    # 人格描述
    "character_desc": "你是ChatGPT, 一个由OpenAI训练的大型语言模型, 你旨在回答并解决人们的任何问题，并且可以使用多种语言与人交流。",
    "conversation_max_tokens": 1000,  # 支持上下文记忆的最多字符数
    # chatgpt限流配置
    "rate_limit_chatgpt": 20,  # chatgpt的调用频率限制
    "rate_limit_dalle": 50,  # openai dalle的调用频率限制
    # chatgpt api参数 参考https://platform.openai.com/docs/api-reference/chat/create
    "temperature": 0.9,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
    "request_timeout": 180,  # chatgpt请求超时时间，openai接口默认设置为600，对于难问题一般需要较长时间
    "timeout": 120,  # chatgpt重试超时时间，在这个时间内，将会自动重试
    # Baidu 文心一言参数
    "baidu_wenxin_model": "eb-instant",  # 默认使用ERNIE-Bot-turbo模型
    "baidu_wenxin_api_key": "",  # Baidu api key
    "baidu_wenxin_secret_key": "",  # Baidu secret key
    "baidu_wenxin_prompt_enabled": False,  # Enable prompt if you are using ernie character model
    # Baidu Qianfan / ERNIE OpenAI-compatible API
    "qianfan_api_key": "",  # Baidu Qianfan API key in bce-v3 format
    "qianfan_api_base": "https://qianfan.baidubce.com/v2",  # Qianfan OpenAI-compatible API base
    # 讯飞星火API
    "xunfei_app_id": "",  # 讯飞应用ID
    "xunfei_api_key": "",  # 讯飞 API key
    "xunfei_api_secret": "",  # 讯飞 API secret
    "xunfei_domain": "",  # 讯飞模型对应的domain参数，Spark4.0 Ultra为 4.0Ultra，其他模型详见: https://www.xfyun.cn/doc/spark/Web.html
    "xunfei_spark_url": "",  # 讯飞模型对应的请求地址，Spark4.0 Ultra为 wss://spark-api.xf-yun.com/v4.0/chat，其他模型参考详见: https://www.xfyun.cn/doc/spark/Web.html
    # claude 配置
    "claude_api_cookie": "",
    "claude_uuid": "",
    # claude api key
    "claude_api_key": "",
    # 通义千问API, 获取方式查看文档 https://help.aliyun.com/document_detail/2587494.html
    "qwen_access_key_id": "",
    "qwen_access_key_secret": "",
    "qwen_agent_key": "",
    "qwen_app_id": "",
    "qwen_node_id": "",  # 流程编排模型用到的id，如果没有用到qwen_node_id，请务必保持为空字符串
    # 阿里灵积(通义新版sdk)模型api key
    "dashscope_api_key": "",
    # Google Gemini Api Key
    "gemini_api_key": "",
    # Embedding 模型设置
    "embedding_provider": "",  # 显式指定厂商：openai / linkai / dashscope / doubao / zhipu (与 bot_type 命名一致)
    "embedding_model": "",     # 留空使用厂商默认 model
    "embedding_dimensions": 0, # 留空/0 使用厂商默认维度（推荐统一 1024）
    # 语音设置
    "speech_recognition": True,  # 是否开启语音识别
    "group_speech_recognition": False,  # 是否开启群组语音识别
    "voice_reply_voice": False,  # 是否使用语音回复语音，需要设置对应语音合成引擎的api key
    "always_reply_voice": False,  # 是否一直使用语音回复
    "voice_to_text": "openai",  # 语音识别引擎，支持openai,baidu,google,azure,xunfei,ali
    "voice_to_text_model": "whisper-1",
    "text_to_voice": "openai",  # 语音合成引擎，支持openai,baidu,google,azure,xunfei,ali,pytts(offline),elevenlabs,edge(online)
    "text_to_voice_model": "tts-1",
    "tts_voice_id": "alloy",
    "grok_tts_voice_id": "eve",
    "grok_tts_language": "zh",
    "grok_tts_sample_rate": 24000,
    "grok_tts_bit_rate": 128000,
    "grok_tts_codec": "mp3",
    "grok_tts_auto_speech_tags": False,
    "grok_voice_reply_enabled": True,
    "grok_voice_mode_enabled": True,
    "grok_voice_conversation_mode_enabled": True,
    "grok_voice_reply_channels": ["wechatcom_app", "wecom_bot"],
    "grok_voice_streaming_enabled": True,
    "grok_voice_require_low_reasoning": True,
    "grok_voice_require_low_reasoning_when_not_conversation_mode": True,
    "grok_voice_force_voice_for_voice_input_in_conversation_mode": True,
    "grok_voice_force_reasoning_effort": "low",
    "grok_voice_low_latency_backend": "",
    "grok_voice_low_latency_model": "",
    "grok_voice_max_output_tokens": 220,
    "grok_voice_short_answer_prompt_enabled": True,
    "grok_voice_max_segment_chars": 180,
    "grok_voice_min_segment_chars": 18,
    "grok_voice_flush_idle_ms": 1500,
    "grok_voice_tts_queue_size": 4,
    "wecom_voice_max_seconds": 55,
    "wecom_voice_max_bytes": 1900000,
    "wecom_voice_normalize_enabled": True,
    "wecom_voice_normalize_target_dbfs": -18.0,
    "wecom_voice_normalize_headroom_db": 1.0,
    "wecom_voice_amr_bitrate": "12.2k",
    "reasoning_effort_policy_low_effort": "low",
    # baidu 语音api配置， 使用百度语音识别和语音合成时需要
    "baidu_app_id": "",
    "baidu_api_key": "",
    "baidu_secret_key": "",
    # 1536普通话(支持简单的英文识别) 1737英语 1637粤语 1837四川话 1936普通话远场
    "baidu_dev_pid": 1536,
    # azure 语音api配置， 使用azure语音识别和语音合成时需要
    "azure_voice_api_key": "",
    "azure_voice_region": "japaneast",
    # elevenlabs 语音api配置
    "xi_api_key": "",  # 获取ap的方法可以参考https://docs.elevenlabs.io/api-reference/quick-start/authentication
    "xi_voice_id": "",  # ElevenLabs提供了9种英式、美式等英语发音id，分别是“Adam/Antoni/Arnold/Bella/Domi/Elli/Josh/Rachel/Sam”
    # 服务时间限制
    "chat_time_module": False,  # 是否开启服务时间限制
    "chat_start_time": "00:00",  # 服务开始时间
    "chat_stop_time": "24:00",  # 服务结束时间
    # 翻译api
    "translate": "baidu",  # 翻译api，支持baidu, youdao
    # baidu翻译api的配置
    "baidu_translate_app_id": "",  # 百度翻译api的appid
    "baidu_translate_app_key": "",  # 百度翻译api的秘钥
    # youdao翻译api的配置
    "youdao_translate_app_key": "",  # 有道翻译api的应用ID
    "youdao_translate_app_secret": "",  # 有道翻译api的应用密钥
    # wechatmp的配置
    "wechatmp_token": "",  # 微信公众平台的Token
    "wechatmp_port": 8080,  # 微信公众平台的端口,需要端口转发到80或443
    "wechatmp_app_id": "",  # 微信公众平台的appID
    "wechatmp_app_secret": "",  # 微信公众平台的appsecret
    "wechatmp_aes_key": "",  # 微信公众平台的EncodingAESKey，加密模式需要
    # wechatcom的通用配置
    "wechatcom_corp_id": "",  # 企业微信公司的corpID
    # wechatcomapp的配置
    "wechatcomapp_token": "",  # 企业微信app的token
    "wechatcomapp_port": 9898,  # 企业微信app的服务端口,不需要端口转发
    "wechatcomapp_secret": "",  # 企业微信app的secret
    "wechatcomapp_agent_id": "",  # 企业微信app的agent_id
    "wechatcomapp_aes_key": "",  # 企业微信app的aes_key
    # 飞书配置
    "feishu_port": 80,  # 飞书bot监听端口，仅webhook模式需要
    "feishu_app_id": "",  # 飞书机器人应用APP Id
    "feishu_app_secret": "",  # 飞书机器人APP secret
    "feishu_token": "",  # 飞书 verification token，仅webhook模式需要
    "feishu_event_mode": "websocket",  # 飞书事件接收模式: webhook(HTTP服务器) 或 websocket(长连接)
    # 飞书流式回复（基于官方 cardkit 流式卡片 API，需要机器人开通 cardkit:card:write 权限，且飞书客户端 7.20+）
    "feishu_stream_reply": True,  # 是否开启流式回复（打字机效果）。失败/老客户端自动降级为非流式或升级提示
    # 钉钉配置
    "dingtalk_client_id": "",  # 钉钉机器人Client ID 
    "dingtalk_client_secret": "",  # 钉钉机器人Client Secret
    "dingtalk_card_enabled": False,
    # 企微智能机器人配置(长连接模式)
    "wecom_bot_id": "",  # 企微智能机器人BotID
    "wecom_bot_secret": "",  # 企微智能机器人长连接Secret
    "wecom_bot_auth_source": "cowagent",  # WeCom QR creation auth source
    "wecom_bot_member_aliases": {},
    "wecom_bot_group_member_aliases": {},
    # 微信配置
    "weixin_token": "",  # 微信登录后获取的bot_token，留空则启动时自动扫码登录
    "weixin_base_url": "https://ilinkai.weixin.qq.com",  # Weixin ilink API base URL
    "weixin_cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",  # CDN base URL
    "weixin_credentials_path": "~/.weixin_cow_credentials.json",  # credentials file path
    "weixin_instances": {},  # Named personal Weixin instances, e.g. {"weixin_user": {"credentials_path": "~/.weixin_cow_credentials_user.json"}}
    # chatgpt指令自定义触发词
    "clear_memory_commands": ["#清除记忆"],  # 重置会话指令，必须以#开头
    # channel配置
    "channel_type": "",  # 通道类型，支持多渠道同时运行。单个: "feishu"，多个: "feishu, dingtalk" 或 ["feishu", "dingtalk"]。可选值: web,feishu,dingtalk,wecom_bot,weixin,wechatmp,wechatmp_service,wechatcom_app
    "web_console": True,  # 是否自动启动Web控制台（默认启动）。设为False可禁用
    "subscribe_msg": "",  # 订阅消息, 支持: wechatmp, wechatmp_service, wechatcom_app
    "debug": False,  # 是否开启debug模式，开启后会打印更多日志
    "appdata_dir": "",  # 数据目录
    # 插件配置
    "plugin_trigger_prefix": "$",  # 规范插件提供聊天相关指令的前缀，建议不要和管理员指令前缀"#"冲突
    # 是否使用全局插件配置
    "use_global_plugin_config": False,
    "max_media_send_count": 3,  # 单次最大发送媒体资源的个数
    "media_send_interval": 1,  # 发送图片的事件间隔，单位秒
    # 智谱AI 平台配置
    "zhipu_ai_api_key": "",
    "zhipu_ai_api_base": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot_api_key": "",
    "moonshot_base_url": "https://api.moonshot.cn/v1",
    # 豆包(火山方舟) 平台配置
    "ark_api_key": "",
    "ark_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    # 魔搭社区 平台配置
    "modelscope_api_key": "",
    "modelscope_base_url": "https://api-inference.modelscope.cn/v1/chat/completions",
    # LinkAI平台配置
    "use_linkai": False,
    "linkai_api_key": "",
    "linkai_app_code": "",
    "linkai_api_base": "https://api.link-ai.tech",
    "cloud_host": "client.link-ai.tech",
    "cloud_port": None,
    "cloud_deployment_id": "",
    "minimax_api_key": "",
    "Minimax_group_id": "",
    "Minimax_base_url": "",
    "deepseek_api_key": "",
    "deepseek_api_base": "https://api.deepseek.com/v1",
    "web_host": "",  # Web console bind address; empty means auto
    "web_port": 9899,
    "web_password": "",  # Web console password; empty means no authentication required
    "web_session_expire_days": 30,  # Auth session expiry in days
    "agent": True,  # 是否开启Agent模式
    "agent_workspace": "~/cow",  # agent工作空间路径，用于存储skills、memory等
    "agent_default_role": "user",  # Multi-user agent default role: user/admin
    "agent_admin_users": [],  # Actor ids or raw chat user ids with admin privileges
    "agent_user_profiles": {},  # Per-actor overrides keyed by actor_id (e.g. weixin:<wxid>) or raw user id
    "agent_user_workspace_root": "",  # Normal-user sandbox root; default is <agent_workspace>/users
    "agent_sensitive_roots": [],  # Extra filesystem roots normal users cannot access
    "agent_sensitive_files": [],  # Extra sensitive files normal users cannot access
    "agent_normal_user_enable_common_read_roots": True,  # Allow normal users to read low-risk attachment/download roots
    "agent_normal_user_read_roots": [],  # Extra normal-user readable roots, e.g. ["D:/SharedDownloads"]
    "agent_normal_user_write_roots": [],  # Extra normal-user writable roots, keep narrow
    "agent_normal_user_can_write_knowledge": True,  # Let trusted normal users add reusable shared knowledge
    "agent_normal_user_allow_delete_files": False,  # Keep file deletion blocked for normal users by default
    "agent_browser_lock_timeout_seconds": 900,  # Browser tool lease timeout for cross-user contention
    "social_bridge_enabled": True,  # Enable controlled cross-user relationship bridge tools
    "social_bridge_auto_send": True,  # Proactively send authorized bridge messages when reachable
    "social_bridge_max_users": 100,  # Maximum bridge directory entries returned to a user
    "social_bridge_pending_retention_days": 30,  # Retain unsent bridge messages for later retry
    "agent_max_context_tokens": 50000,  # Agent模式下最大上下文tokens
    "agent_max_context_turns": 20,  # Agent模式下最大上下文记忆轮次
    "agent_max_steps": 20,  # Agent模式下单次运行最大决策步数
    "agent_development_max_steps": 40,  # 代码开发/调试/测试任务的单次运行最大决策步数
    "agent_knowledge_max_steps": 40,  # 知识库/协议/规范问答任务的单次运行最大决策步数
    "agent_complex_planning_max_steps": 40,  # 复杂旅行/多工具规划任务的单次运行最大决策步数
    "long_task_completion_notice_enabled": True,  # 长任务成功完成后是否发送短完成提示
    "long_task_completion_notice_min_turns": 10,  # 发送长任务成功提示的最小决策轮数
    "long_task_completion_notice_min_silence_notices": 2,  # 兜底进度提醒出现几次后成功完成也发送短提示
    "memory_deep_dream": copy.deepcopy(_DEFAULT_MEMORY_DEEP_DREAM_CONFIG),
    "enable_thinking": False,  # Enable deep-thinking mode for thinking-capable models
    "reasoning_effort": "high",  # Reasoning depth under thinking mode: "high" or "max"
    "knowledge": True,  # 是否开启知识库功能
    "llm_backend": copy.deepcopy(DEFAULT_LLM_BACKEND_CONFIG),
    "knowledge_backend": copy.deepcopy(_DEFAULT_KNOWLEDGE_BACKEND_CONFIG),
    "skill": {
        "image-generation": {
            "runtime": "codex_auth",
            "codex_auth_file": "",
            "prompt_enhancement_enabled": True,
            "prompt_library_dir": "",
        }
    },  # Per-skill runtime config; nested keys flatten to SKILL_<NAME>_<KEY> env vars at startup
    "mcp_servers": [],  # MCP server list; each entry supports type "stdio" (local process) or "sse" (remote URL)
}


class Config(dict):
    def __init__(self, d=None):
        super().__init__()
        if d is None:
            d = {}
        for k, v in d.items():
            self[k] = v
        # user_datas: 用户数据，key为用户名，value为用户数据，也是dict
        self.user_datas = {}

    def __getitem__(self, key):
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        return super().__setitem__(key, value)

    def get(self, key, default=None):
        # 跳过以下划线开头的注释字段
        if key.startswith("_"):
            return super().get(key, default)
        
        # 如果key不在available_setting中，直接走dict的get，返回config.json中实际加载的值（如不存在则返回default）
        if key not in available_setting:
            return super().get(key, default)
        
        try:
            return self[key]
        except KeyError as e:
            return default
        except Exception as e:
            raise e

    # Make sure to return a dictionary to ensure atomic
    def get_user_data(self, user) -> dict:
        if self.user_datas.get(user) is None:
            self.user_datas[user] = {}
        return self.user_datas[user]

    def load_user_datas(self):
        try:
            with open(os.path.join(get_appdata_dir(), "user_datas.pkl"), "rb") as f:
                self.user_datas = pickle.load(f)
                logger.debug("[Config] User datas loaded.")
        except FileNotFoundError as e:
            logger.debug("[Config] User datas file not found, ignore.")
        except Exception as e:
            logger.warning("[Config] User datas error: {}".format(e))
            self.user_datas = {}

    def save_user_datas(self):
        try:
            with open(os.path.join(get_appdata_dir(), "user_datas.pkl"), "wb") as f:
                pickle.dump(self.user_datas, f)
                logger.info("[Config] User datas saved.")
        except Exception as e:
            logger.info("[Config] User datas error: {}".format(e))


config = Config()


def _is_sensitive_config_key(key):
    key = str(key).lower()
    sensitive_markers = ("key", "secret", "password", "token", "cookie", "credential", "auth", "bearer")
    return any(marker in key for marker in sensitive_markers)


def _mask_sensitive_value(key, value):
    if not _is_sensitive_config_key(key) or not isinstance(value, str):
        return value
    if not value:
        return value
    if len(value) <= 6:
        return "*" * len(value)
    return value[0:3] + "*" * 5 + value[-3:]


def drag_sensitive(config):
    try:
        if isinstance(config, str):
            conf_dict: dict = json.loads(config)
            conf_dict_copy = copy.deepcopy(conf_dict)
            conf_dict_copy = _mask_sensitive_tree(conf_dict_copy)
            return json.dumps(conf_dict_copy, indent=4)

        elif isinstance(config, dict):
            config_copy = copy.deepcopy(config)
            return _mask_sensitive_tree(config_copy)
    except Exception as e:
        logger.exception(e)
        return config
    return config


def _mask_sensitive_tree(value, parent_key=""):
    if isinstance(value, dict):
        return {key: _mask_sensitive_tree(child, key) for key, child in value.items()}
    if isinstance(value, list):
        return [_mask_sensitive_tree(item, parent_key) for item in value]
    return _mask_sensitive_value(parent_key, value)


def _deep_merge_dict(defaults, overrides):
    merged = copy.deepcopy(defaults)
    if not isinstance(overrides, dict):
        return merged
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool_env(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_knowledge_backend_env(value, value_type):
    if value_type == "bool":
        return _parse_bool_env(value)
    if value_type == "int":
        return int(value)
    if value_type == "csv":
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return value


def _set_nested_config_value(target, dotted_path, value):
    current = target
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _normalize_knowledge_backend_config():
    raw = config.get("knowledge_backend", {})
    normalized = _deep_merge_dict(_DEFAULT_KNOWLEDGE_BACKEND_CONFIG, raw)

    for env_key, (path, value_type) in _KNOWLEDGE_BACKEND_ENV_MAP.items():
        if env_key not in os.environ:
            continue
        try:
            value = _parse_knowledge_backend_env(os.environ[env_key], value_type)
            _set_nested_config_value(normalized, path, value)
            logger.info("[INIT] override knowledge_backend by environ args: {}".format(env_key))
        except Exception as e:
            logger.warning("[INIT] invalid {} ignored: {}".format(env_key, e))

    config["knowledge_backend"] = normalized


def _normalize_llm_backend_config():
    raw = config.get("llm_backend", {})
    config["llm_backend"] = _deep_merge_dict(DEFAULT_LLM_BACKEND_CONFIG, raw)


def _normalize_memory_deep_dream_config():
    raw = config.get("memory_deep_dream", {})
    config["memory_deep_dream"] = _deep_merge_dict(_DEFAULT_MEMORY_DEEP_DREAM_CONFIG, raw)


def load_config():
    global config

    # 打印 ASCII Logo
    logger.info("  ____                _                    _   ")
    logger.info(" / ___|_____      __ / \\   __ _  ___ _ __ | |_ ")
    logger.info("| |   / _ \\ \\ /\\ / // _ \\ / _` |/ _ \\ '_ \\| __|")
    logger.info("| |__| (_) \\ V  V // ___ \\ (_| |  __/ | | | |_ ")
    logger.info(" \\____\\___/ \\_/\\_//_/   \\_\\__, |\\___|_| |_|\\__|")
    logger.info("                          |___/                 ")
    logger.info("")
    config_path = "./config.json"
    if not os.path.exists(config_path):
        logger.info("配置文件不存在，将使用config-template.json模板")
        config_path = "./config-template.json"

    config_str = read_file(config_path)
    logger.debug("[INIT] config str: {}".format(drag_sensitive(config_str)))

    # 将json字符串反序列化为dict类型
    config = Config(json.loads(config_str))

    # override config with environment variables.
    # Some online deployment platforms (e.g. Railway) deploy project from github directly. So you shouldn't put your secrets like api key in a config file, instead use environment variables to override the default config.
    for name, value in os.environ.items():
        name = name.lower()
        # 跳过以下划线开头的注释字段
        if name.startswith("_"):
            continue
        if name in available_setting:
            masked_value = _mask_sensitive_value(name, value)
            logger.info("[INIT] override config by environ args: {}={}".format(name, masked_value))
            try:
                config[name] = eval(value)
            except Exception:
                if value == "false":
                    config[name] = False
                elif value == "true":
                    config[name] = True
                else:
                    config[name] = value

    _normalize_knowledge_backend_config()
    _normalize_llm_backend_config()
    _normalize_memory_deep_dream_config()

    if config.get("debug", False):
        logger.setLevel(logging.DEBUG)
        logger.debug("[INIT] set log level to DEBUG")

    logger.info("[INIT] load config: {}".format(drag_sensitive(config)))

    # 打印系统初始化信息
    logger.info("[INIT] ========================================")
    logger.info("[INIT] System Initialization")
    logger.info("[INIT] ========================================")
    logger.info("[INIT] Channel: {}".format(config.get("channel_type", "unknown")))
    logger.info("[INIT] Model: {}".format(config.get("model", "unknown")))

    # Agent模式信息
    if config.get("agent", True):
        workspace = config.get("agent_workspace", "~/cow")
        logger.info("[INIT] Mode: Agent (workspace: {})".format(workspace))
    else:
        logger.info("[INIT] Mode: Chat (在config.json中设置 \"agent\":true 可启用Agent模式)")

    logger.info("[INIT] Debug: {}".format(config.get("debug", False)))
    logger.info("[INIT] ========================================")

    # Sync selected config values to environment variables so that
    # subprocesses (e.g. shell skill scripts) can access them directly.
    # Existing env vars are NOT overwritten (env takes precedence).
    _CONFIG_TO_ENV = {
        "open_ai_api_key": "OPENAI_API_KEY",
        "open_ai_api_base": "OPENAI_API_BASE",
        "open_ai_wire_api": "OPENAI_WIRE_API",
        "openai_wire_api": "OPENAI_WIRE_API",
        "wire_api": "OPENAI_WIRE_API",
        "model": "OPENAI_MODEL",
        "disable_response_storage": "OPENAI_DISABLE_RESPONSE_STORAGE",
        "model_reasoning_effort": "OPENAI_REASONING_EFFORT",
        "reasoning_effort": "OPENAI_REASONING_EFFORT",
        "enable_prompt_cache_key": "OPENAI_ENABLE_PROMPT_CACHE_KEY",
        "prompt_cache_key_prefix": "OPENAI_PROMPT_CACHE_KEY_PREFIX",
        "prompt_cache_key_granularity": "OPENAI_PROMPT_CACHE_KEY_GRANULARITY",
        "prompt_cache_retention": "OPENAI_PROMPT_CACHE_RETENTION",
        "llm_usage_tracking": "LLM_USAGE_TRACKING",
        "llm_usage_history_limit": "LLM_USAGE_HISTORY_LIMIT",
        "prompt_cache_stable_runtime_info": "PROMPT_CACHE_STABLE_RUNTIME_INFO",
        "runtime_time_in_user_message": "RUNTIME_TIME_IN_USER_MESSAGE",
        "knowledge_index_in_system_prompt": "KNOWLEDGE_INDEX_IN_SYSTEM_PROMPT",
        "knowledge_auto_retrieval": "KNOWLEDGE_AUTO_RETRIEVAL",
        "knowledge_auto_retrieval_max_results": "KNOWLEDGE_AUTO_RETRIEVAL_MAX_RESULTS",
        "knowledge_auto_retrieval_min_score": "KNOWLEDGE_AUTO_RETRIEVAL_MIN_SCORE",
        "knowledge_auto_retrieval_max_chars": "KNOWLEDGE_AUTO_RETRIEVAL_MAX_CHARS",
        "linkai_api_key": "LINKAI_API_KEY",
        "linkai_api_base": "LINKAI_API_BASE",
        "claude_api_key": "CLAUDE_API_KEY",
        "claude_api_base": "CLAUDE_API_BASE",
        "gemini_api_key": "GEMINI_API_KEY",
        "gemini_api_base": "GEMINI_API_BASE",
        "grok_api_key": "XAI_API_KEY",
        "grok_api_base": "XAI_BASE_URL",
        "minimax_api_key": "MINIMAX_API_KEY",
        "minimax_api_base": "MINIMAX_API_BASE",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "deepseek_api_base": "DEEPSEEK_API_BASE",
        "qianfan_api_key": "QIANFAN_API_KEY",
        "qianfan_api_base": "QIANFAN_API_BASE",
        "zhipu_ai_api_key": "ZHIPU_AI_API_KEY",
        "zhipu_ai_api_base": "ZHIPU_AI_API_BASE",
        "moonshot_api_key": "MOONSHOT_API_KEY",
        "moonshot_api_base": "MOONSHOT_API_BASE",
        "ark_api_key": "ARK_API_KEY",
        "ark_api_base": "ARK_API_BASE",
        "dashscope_api_key": "DASHSCOPE_API_KEY",
        "dashscope_api_base": "DASHSCOPE_API_BASE",
        # Channel credentials (used by skills that check env vars)
        "feishu_app_id": "FEISHU_APP_ID",
        "feishu_app_secret": "FEISHU_APP_SECRET",
        "dingtalk_client_id": "DINGTALK_CLIENT_ID",
        "dingtalk_client_secret": "DINGTALK_CLIENT_SECRET",
        "wechatmp_app_id": "WECHATMP_APP_ID",
        "wechatmp_app_secret": "WECHATMP_APP_SECRET",
        "wechatcomapp_agent_id": "WECHATCOMAPP_AGENT_ID",
        "wechatcomapp_secret": "WECHATCOMAPP_SECRET",
        "qq_app_id": "QQ_APP_ID",
        "qq_app_secret": "QQ_APP_SECRET",
        "weixin_token": "WEIXIN_TOKEN",
    }
    injected = 0
    for conf_key, env_key in _CONFIG_TO_ENV.items():
        if env_key not in os.environ:
            val = config.get(conf_key, "")
            if val:
                os.environ[env_key] = str(val)
                injected += 1

    injected += _sync_skill_config_to_env(config.get("skill", {}))
    injected += _sync_llm_backend_config_to_env(config.get("llm_backend", {}))
    if "SKILL_IMAGE_GENERATION_MODEL" not in os.environ:
        image_model = config.get("text_to_image", "")
        if image_model:
            os.environ["SKILL_IMAGE_GENERATION_MODEL"] = str(image_model)
            injected += 1

    if injected:
        logger.info("[INIT] Synced {} config values to environment variables".format(injected))

    config.load_user_datas()


def _sync_skill_config_to_env(skill_section) -> int:
    """Flatten skill-namespaced config into environment variables.

    Mapping rule: ``config["skill"][<name>][<key>]`` -> ``SKILL_<NAME>_<KEY>``
    (e.g. ``skill["image-generation"].model`` -> ``SKILL_IMAGE_GENERATION_MODEL``).

    This lets subprocess-based skill scripts read their own settings without
    importing project code. Existing env vars are NOT overwritten so the
    real environment always wins.

    Returns the number of variables actually injected.
    """
    if not isinstance(skill_section, dict):
        return 0
    injected = 0
    for skill_name, skill_conf in skill_section.items():
        if not isinstance(skill_conf, dict):
            continue
        name_part = str(skill_name).replace("-", "_").upper()
        for key, val in skill_conf.items():
            if val is None or val == "":
                continue
            env_key = "SKILL_{}_{}".format(name_part, str(key).upper())
            if env_key in os.environ:
                continue
            os.environ[env_key] = str(val)
            injected += 1
    return injected


def _sync_llm_backend_config_to_env(llm_backend_section) -> int:
    if not isinstance(llm_backend_section, dict):
        return 0
    providers = llm_backend_section.get("providers")
    if not isinstance(providers, dict):
        return 0
    codex = providers.get("codex")
    if not isinstance(codex, dict):
        return 0
    injected = 0
    auth_file = codex.get("auth_file")
    if auth_file and "CODEX_AUTH_FILE" not in os.environ:
        os.environ["CODEX_AUTH_FILE"] = str(auth_file)
        injected += 1
    model = codex.get("model")
    if model and "CODEX_MODEL" not in os.environ:
        os.environ["CODEX_MODEL"] = str(model)
        injected += 1
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        api_key = provider.get("api_key")
        api_key_env = str(provider.get("api_key_env") or "").strip()
        if api_key and api_key_env and api_key_env not in os.environ:
            os.environ[api_key_env] = str(api_key)
            injected += 1
    return injected


def get_root():
    return os.path.dirname(os.path.abspath(__file__))


def read_file(path):
    with open(path, mode="r", encoding="utf-8-sig") as f:
        return f.read()


def conf():
    return config


def get_appdata_dir():
    data_path = os.path.join(get_root(), conf().get("appdata_dir", ""))
    if not os.path.exists(data_path):
        logger.info("[INIT] data path not exists, create it: {}".format(data_path))
        os.makedirs(data_path)
    return data_path


def subscribe_msg():
    trigger_prefix = conf().get("single_chat_prefix", [""])[0]
    msg = conf().get("subscribe_msg", "")
    return msg.format(trigger_prefix=trigger_prefix)


# global plugin config
plugin_config = {}


def write_plugin_config(pconf: dict):
    """
    写入插件全局配置
    :param pconf: 全量插件配置
    """
    global plugin_config
    for k in pconf:
        plugin_config[k.lower()] = pconf[k]

def remove_plugin_config(name: str):
    """
    移除待重新加载的插件全局配置
    :param name: 待重载的插件名
    """
    global plugin_config
    plugin_config.pop(name.lower(), None)


def pconf(plugin_name: str) -> dict:
    """
    根据插件名称获取配置
    :param plugin_name: 插件名称
    :return: 该插件的配置项
    """
    return plugin_config.get(plugin_name.lower())


# 全局配置，用于存放全局生效的状态
global_config = {"admin_users": []}
