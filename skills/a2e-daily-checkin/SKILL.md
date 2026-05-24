---
name: a2e-daily-checkin
description: A2E daily reward check-in automation for https://video.a2e.ai/ using the user's local Chrome profiles and visible browser page. Use when the user asks CowAgent/CowWechat to open A2E, run A2E check-in, claim daily rewards, inspect A2E coin/check-in status, or schedule/maintain A2E daily sign-in for the configured Lorna and Rondle Chrome profiles.
metadata:
  requires:
    bins: ["powershell"]
---

# A2E Daily Check-in

## Operating Rules

- Use `state/a2e-checkin.json` in this skill directory as the source of truth for account order, Chrome profile directories, latest known coin count, and next eligible check-in time.
- If `state/a2e-checkin.json` is missing in a new checkout, copy `state/a2e-checkin.example.json` to that path and fill in local account/profile details. The real state file is local runtime data and should not be committed.
- Interpret stored times in `Asia/Shanghai`.
- Check accounts in this order unless the user says otherwise: Lorna first, then Rondle.
- Skip an account when `nextCheckInAfter` exists and the current time is earlier than that value.
- Open `https://video.a2e.ai/` in the mapped local Chrome profile, then work with the visible A2E page.
- Current local profile mappings:
  - Lorna: Chrome `Profile 2`, expected signed-in email `lornamelton@nnu.xintaitong.com`.
  - Rondle: Chrome `Default`, expected signed-in email `while4234@gmail.com`.
- Keep automations account-specific. Prefer one daily automation per account rather than one broad all-account watcher.
- Do not attach to an existing Chrome DevTools endpoint such as `127.0.0.1:9222`; it may belong to a different app or Chrome profile.
- Do not automate or bypass CAPTCHA, Cloudflare Turnstile, Google identity checks, face verification, or any page that explicitly asks to confirm a real human. Stop and report the manual-verification screen instead.
- Never write access tokens, cookies, Chrome storage dumps, or other credentials into chat, handoff files, commits, logs, or state files.

## Native Chrome Helper

Use the bundled PowerShell helper from this skill's base directory. It verifies the Chrome profile email, opens or focuses the correct profile, navigates to A2E, clicks the visible daily reward claim button when requested, verifies success through the A2E API when possible, updates state after verified success, and closes A2E browser windows after a verified claim unless `-KeepOpen` is passed.

Open the due account page without claiming:

```powershell
powershell -ExecutionPolicy Bypass -File "<base_dir>\scripts\a2e_checkin.ps1" -Account all -DueOnly -OpenOnly -Screenshot
```

Run a verified unattended claim for due accounts:

```powershell
powershell -ExecutionPolicy Bypass -File "<base_dir>\scripts\a2e_checkin.ps1" -Account all -DueOnly -ClickClaim -VerifyClaim -AutoUpdateState -Screenshot
```

Use `-KeepOpen` only when you intentionally want to inspect the browser after a verified claim. Use `-CloseAfter` for non-claim flows such as `-OpenOnly` when you want the page opened, captured, and then closed. Failed or unverified claim attempts keep the browser open for manual inspection.

If the helper output includes `ManualActionRequired.Required = true` or `ManualActionRequired.NeedsNotification = true`, immediately tell the current WeCom user/admin that A2E needs manual action. Include the account, reason, screenshot path if present, and say the browser has intentionally been left open for human verification or manual inspection. Do not retry clicks, do not close the browser, and do not claim success until a later API status check verifies the check-in. If the helper output includes `FailureNotification.NeedsNotification = true`, report the failure details to the user instead of staying silent.

Check one account status through the local Chrome profile's A2E session:

```powershell
powershell -ExecutionPolicy Bypass -File "<base_dir>\scripts\a2e_checkin.ps1" -Account lorna -ApiStatus
```

Open a specific account page:

```powershell
powershell -ExecutionPolicy Bypass -File "<base_dir>\scripts\a2e_checkin.ps1" -Account rondle -OpenOnly -Screenshot
```

Manually update state after verified browser success:

```powershell
powershell -ExecutionPolicy Bypass -File "<base_dir>\scripts\a2e_checkin.ps1" -Account lorna -UpdateState -NextCheckInAfter "YYYY-MM-DD HH:mm" -LastKnownCoins 1234 -Result "verified claim"
```

## Check-in Flow

1. Read `state/a2e-checkin.json`.
2. If `DueOnly` is appropriate, skip accounts that are not eligible yet.
3. Open the matching Chrome profile and A2E page with `-OpenOnly` when the user only asks to open the page.
4. Click only the visible site reward claim button when the user asks to sign in or claim, using `-ClickClaim`.
5. Prefer `-VerifyClaim -AutoUpdateState` so state changes only after the API confirms the check-in or the account is already checked in today. After verified success, let the helper close A2E browser windows automatically unless the user asked to keep them open.
6. Treat these as successful check-in evidence:
   - The API reports today's successful check-in.
   - The coin count increases by 60.
   - The page shows a next check-in time after the claim.
7. If face/human verification appears or the helper returns `ManualActionRequired`, stop immediately, leave the browser at that screen, and report manual verification needed to the current WeCom user/admin.

## Scheduling Guidance

For recurring runs, create account-specific automations:

- Lorna: daily shortly after `nextCheckInAfter` for the Lorna account.
- Rondle: daily shortly after `nextCheckInAfter` for the Rondle account.

Use the verified unattended command for scheduled jobs. Delete temporary retry automations after a verified success and replace them with the next daily account-specific schedule.
