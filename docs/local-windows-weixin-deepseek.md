# Local Windows Weixin + DeepSeek Deployment

This branch is for a local Windows deployment that logs in through a personal Weixin account and uses the built-in DeepSeek provider.

## Git workflow

- Keep `origin` pointed at `https://github.com/zhayujie/CowAgent.git`.
- Keep upstream history clean and make local deployment changes on `deploy/windows-weixin-deepseek`.
- Do not commit `config.json`, virtual environments, logs, local credentials, `.env` files, or key files.

## Local runtime

Use a Python virtual environment in `.venv` and install the project in editable mode:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

Start and inspect the service with:

```powershell
.\.venv\Scripts\cow.exe start
.\.venv\Scripts\cow.exe status
.\.venv\Scripts\cow.exe logs
```

## Config shape

Create `config.json` locally from `config-template.json` and set at least:

```json
{
  "channel_type": "weixin",
  "web_console": true,
  "web_host": "127.0.0.1",
  "web_port": 9899,
  "web_password": "<local-console-password>",
  "model": "deepseek-v4-flash",
  "deepseek_api_key": "<deepseek-api-key>",
  "deepseek_api_base": "https://api.deepseek.com",
  "agent": true,
  "enable_thinking": true,
  "reasoning_effort": "high"
}
```

`web_host` is explicitly set to `127.0.0.1` so enabling `web_password` does not make the console listen on all network interfaces.

## Validation

- `git status --short --branch` should show this branch and no tracked secrets.
- `.\.venv\Scripts\cow.exe status` should report the CowAgent service state.
- Open `http://127.0.0.1:9899` locally for the Web console.
- Use the Web console or startup QR flow to connect Weixin, then send a short message from the logged-in account to verify DeepSeek replies.
