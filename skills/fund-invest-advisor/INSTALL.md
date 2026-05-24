# Fund Invest Advisor Skill Install Notes

This skill is self-contained and uses only Python standard-library modules.

## Requirements

- Python 3.9+.
- No external API key.
- No network access is required for the bundled calculations.

## Validation

From the CowWechat project root:

```powershell
$env:PYTHONUTF8='1'
.venv\Scripts\python.exe skills\skill-creator\scripts\quick_validate.py skills\fund-invest-advisor
.venv\Scripts\python.exe -m py_compile skills\fund-invest-advisor\scripts\fund_advisor.py
.venv\Scripts\python.exe skills\fund-invest-advisor\scripts\fund_advisor.py invest 1000 8 10
```

The outputs are scenario calculations only and are not investment advice.
