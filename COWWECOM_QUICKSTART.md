# CowWeCom Quickstart

This repository is prepared as a clean CowWeCom baseline. It is safe to clone on
another machine, install dependencies, copy the config template, and fill only
local credentials in `config.json`.

## 1. Create Local Config

```powershell
Copy-Item .\config-template.json .\config.json
```

`config.json` is ignored by Git. Keep real API keys, passwords, Bot IDs, and
Secrets there only.

## 2. Fill Required Values

For the default DeepSeek-compatible template:

```json
{
  "channel_type": "wecom_bot",
  "model": "deepseek-v4-flash",
  "deepseek_api_key": "YOUR_LLM_API_KEY",
  "wecom_bot_id": "YOUR_WECOM_BOT_ID",
  "wecom_bot_secret": "YOUR_WECOM_BOT_SECRET",
  "wecom_bot_auth_source": "cowagent"
}
```

You can also use the Web console to scan or enter the WeCom Bot ID and Secret
manually. The scan flow uses `wecom_bot_auth_source`; if WeCom reports invalid
parameters, confirm the source value or create the bot in the WeCom desktop
client and copy the credentials into the Web console or `config.json`.

## 3. Install And Run

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

The WeCom Bot connection is ready when the log includes:

```text
[WecomBot] Subscribe success
```

## Safety Notes

- Do not commit `config.json`, `.env`, logs, runtime databases, or local
  workspace files.
- Personal knowledge, conversation summaries, and knowledge-wiki outputs are
  local runtime data and are ignored by Git.
- Only administrator Web-uploaded public protocol/specification knowledge goes
  under `public_protocol_knowledge/` and is committed after validation.
- Push only to this project's `origin` remote after it is changed to the new
  CowWeCom repository.
