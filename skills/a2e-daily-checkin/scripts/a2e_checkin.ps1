param(
  [ValidateSet("lorna", "rondle", "all")]
  [string]$Account = "all",

  [switch]$OpenOnly,
  [switch]$ClickClaim,
  [switch]$Screenshot,
  [switch]$DueOnly,
  [switch]$UpdateState,
  [switch]$ApiStatus,
  [switch]$VerifyClaim,
  [switch]$AutoUpdateState,
  [Alias("AutoUpdateAutomationSchedule")]
  [switch]$AutoUpdateSchedulerTask,
  [switch]$CloseAfter,
  [switch]$KeepOpen,

  [string]$NextCheckInAfter,
  [int]$LastKnownCoins,
  [int]$ScheduleBufferMinutes = 5,
  [string]$Result,
  [string]$Url
)

$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$StatePath = Join-Path $SkillRoot "state\a2e-checkin.json"

function Read-CheckinState {
  Read-JsonFile $StatePath
}

function Save-CheckinState($State) {
  $State | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}

function Read-JsonFile([string]$Path) {
  $lastError = $null

  for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
      return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    } catch {
      $lastError = $_
      Start-Sleep -Milliseconds (150 * $attempt)
    }
  }

  throw $lastError
}

function Read-TextFile([string]$Path) {
  $lastError = $null

  for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
      return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
    } catch {
      $lastError = $_
      Start-Sleep -Milliseconds (150 * $attempt)
    }
  }

  throw $lastError
}

function Set-JsonProperty($Object, [string]$Name, $Value) {
  $property = $Object.PSObject.Properties[$Name]
  if ($property) {
    $property.Value = $Value
  } else {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
  }
}

function Get-AccountConfig($State, [string]$Name) {
  $property = $State.accounts.PSObject.Properties[$Name]
  if (-not $property) {
    throw "Unknown account '$Name'."
  }
  $property.Value
}

function Get-ChromePath {
  $candidates = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
  )

  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
      return $candidate
    }
  }

  throw "Chrome executable was not found."
}

function Get-ChromeUserDataDir {
  $path = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Chrome user data directory was not found at '$path'."
  }

  $path
}

function Assert-AccountProfile($Config, [string]$Name) {
  $userDataDir = Get-ChromeUserDataDir
  $profileDir = Join-Path $userDataDir $Config.chromeProfileDirectory
  $preferencesPath = Join-Path $profileDir "Preferences"

  if (-not (Test-Path -LiteralPath $profileDir)) {
    throw "Chrome profile '$($Config.chromeProfileDirectory)' for '$Name' was not found at '$profileDir'."
  }

  if (-not (Test-Path -LiteralPath $preferencesPath)) {
    throw "Chrome profile '$($Config.chromeProfileDirectory)' has no Preferences file to verify account ownership."
  }

  $preferencesText = Read-TextFile $preferencesPath
  $emails = @([regex]::Matches($preferencesText, '"email"\s*:\s*"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }) | Where-Object { $_ }
  if ($emails -notcontains $Config.email) {
    throw "Chrome profile '$($Config.chromeProfileDirectory)' is not signed in as '$($Config.email)'. Found: $($emails -join ', ')."
  }
}

function Read-SharedFileBytes([string]$Path, [int64]$MaxBytes = 20971520) {
  $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
  try {
    $length = [Math]::Min($stream.Length, $MaxBytes)
    $buffer = New-Object byte[] $length
    $read = $stream.Read($buffer, 0, $buffer.Length)
    if ($read -lt $buffer.Length) {
      $trimmed = New-Object byte[] $read
      [Array]::Copy($buffer, $trimmed, $read)
      return $trimmed
    }
    return $buffer
  } finally {
    $stream.Dispose()
  }
}

function Get-A2EAccessToken($Config) {
  $levelDbPath = Join-Path (Join-Path (Join-Path (Get-ChromeUserDataDir) $Config.chromeProfileDirectory) "Local Storage") "leveldb"
  if (-not (Test-Path -LiteralPath $levelDbPath)) {
    throw "Chrome profile '$($Config.chromeProfileDirectory)' has no A2E local storage LevelDB directory."
  }

  $files = Get-ChildItem -LiteralPath $levelDbPath -File -ErrorAction Stop |
    Where-Object { $_.Extension -in @(".log", ".ldb") } |
    Sort-Object LastWriteTime -Descending

  $fallbackTokens = @()

  foreach ($file in $files) {
    try {
      $bytes = Read-SharedFileBytes $file.FullName
      $text = [Text.Encoding]::UTF8.GetString($bytes)
      if ($text -notmatch "https://video\.a2e\.ai") {
        continue
      }

      $match = [regex]::Match($text, '"accessToken"\s*:\s*"([^"]+)"')
      if ($match.Success) {
        if ($text -match [regex]::Escape($Config.email)) {
          return $match.Groups[1].Value
        }

        $fallbackTokens += $match.Groups[1].Value
      }
    } catch {
      continue
    }
  }

  if ($fallbackTokens.Count -gt 0) {
    return $fallbackTokens[0]
  }

  throw "A2E access token was not found in Chrome profile '$($Config.chromeProfileDirectory)' for '$($Config.email)'."
}

function Normalize-A2EAccessToken([string]$Token) {
  if (-not $Token) {
    return $Token
  }

  $normalized = $Token -replace '\\u0000', '' -replace '\\0', ''
  $normalized = $normalized -replace '[\x00-\x1F\x7F]', ''
  $normalized.Trim()
}

function Invoke-A2EApi($Config, [string]$Path, [string]$Method = "GET", $Body = $null) {
  $token = Normalize-A2EAccessToken (Get-A2EAccessToken $Config)
  if (-not $token) {
    throw "A2E access token was empty after sanitizing Chrome profile storage for '$($Config.email)'."
  }

  $headers = @{ Authorization = "Bearer $token" }
  $uri = "https://video.a2e.ai$Path"

  if ($Method -eq "POST") {
    $jsonBody = if ($null -eq $Body) { "{}" } else { $Body | ConvertTo-Json -Depth 8 -Compress }
    return Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -Body $jsonBody -ContentType "application/json" -TimeoutSec 30
  }

  Invoke-RestMethod -Uri $uri -Method Get -Headers $headers -TimeoutSec 30
}

function Get-A2EApiStatus([string]$Name) {
  $state = Read-CheckinState
  $config = Get-AccountConfig $state $Name
  Assert-AccountProfile $config $Name

  $currentUser = Invoke-A2EApi $config "/api/v1/currentUser" "POST" @{}
  $remainingCoins = Invoke-A2EApi $config "/api/v1/user/remainingCoins" "GET"
  $user = $currentUser.data

  [pscustomobject]@{
    Account = $Name
    Email = $config.email
    CurrentUserEmail = $user.name
    NickName = $user.nick_name
    Coins = [int]$remainingCoins.data.coins
    Diamonds = [int]$remainingCoins.data.diamonds
    CheckInTime = $user.checkInTime
    NextCheckInTime = $user.nextCheckInTime
    CheckInCoins = $user.checkInCoins
  }
}

function Convert-A2ETimeToLocal([string]$IsoTime) {
  if (-not $IsoTime) {
    return $null
  }

  [datetimeoffset]::Parse($IsoTime).ToLocalTime()
}

function Get-NextCheckInAfterFromStatus($Status) {
  $next = Convert-A2ETimeToLocal $Status.NextCheckInTime
  if ($next) {
    return $next
  }

  $checkIn = Convert-A2ETimeToLocal $Status.CheckInTime
  if ($checkIn) {
    return $checkIn.AddHours(23)
  }

  return $null
}

function Get-CowSchedulerPathCandidates {
  $candidates = @()
  if ($env:USERPROFILE) {
    $candidates += (Join-Path $env:USERPROFILE "cow\scheduler\tasks.json")
  }

  $skillRootParts = $SkillRoot -split '[\\/]'
  $cowIndex = [Array]::IndexOf($skillRootParts, "cow")
  if ($cowIndex -ge 0) {
    $cowRoot = ($skillRootParts[0..$cowIndex] -join [IO.Path]::DirectorySeparatorChar)
    $candidates += (Join-Path $cowRoot "scheduler\tasks.json")
  }

  foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $candidate) {
      $candidate
    }
  }
}

function Find-A2ESchedulerTask($Tasks, [string]$Name) {
  $accountPattern = "(?i)(-Account\s+$Name|Account\s+$Name|$Name)"
  $matches = @()

  foreach ($property in $Tasks.PSObject.Properties) {
    $task = $property.Value
    if (-not $task.enabled -or -not $task.schedule -or $task.schedule.type -ne "cron") {
      continue
    }

    $nameText = [string]$task.name
    $description = [string]$task.action.task_description
    $searchText = "$nameText`n$description"
    if ($searchText -match "(?i)(a2e-daily-checkin|a2e_checkin\.ps1|A2E)" -and $searchText -match $accountPattern) {
      $matches += $task
    }
  }

  if ($matches.Count -eq 0) {
    return $null
  }

  $matches | Sort-Object updated_at -Descending | Select-Object -First 1
}

function Update-CowSchedulerTaskFromNextCheckIn([string]$Name, [datetimeoffset]$NextCheckInAfter) {
  $schedulerPath = Get-CowSchedulerPathCandidates | Select-Object -First 1
  if (-not $schedulerPath) {
    return [pscustomobject]@{
      Updated = $false
      Reason = "cow_scheduler_tasks_json_not_found"
      Account = $Name
    }
  }

  $scheduler = Read-JsonFile $schedulerPath
  $task = Find-A2ESchedulerTask $scheduler.tasks $Name
  if (-not $task) {
    return [pscustomobject]@{
      Updated = $false
      Reason = "a2e_scheduler_task_not_found"
      Account = $Name
      Path = $schedulerPath
    }
  }

  $bufferMinutes = [Math]::Max(0, $ScheduleBufferMinutes)
  $runAt = $NextCheckInAfter.AddMinutes($bufferMinutes)
  $cronExpression = "$($runAt.Minute) $($runAt.Hour) * * *"

  Set-JsonProperty $task "enabled" $true
  Set-JsonProperty $task.schedule "expression" $cronExpression
  Set-JsonProperty $task "next_run_at" $runAt.DateTime.ToString("yyyy-MM-ddTHH:mm:ss")
  Set-JsonProperty $task "updated_at" (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.ffffff")

  $description = [string]$task.action.task_description
  if ($description -and $description -match "-AutoUpdateState" -and $description -notmatch "-AutoUpdateSchedulerTask|-AutoUpdateAutomationSchedule") {
    Set-JsonProperty $task.action "task_description" ($description -replace "-AutoUpdateState", "-AutoUpdateState -AutoUpdateSchedulerTask")
  }

  Set-JsonProperty $scheduler "updated_at" (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.ffffff")
  $scheduler | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $schedulerPath -Encoding UTF8

  [pscustomobject]@{
    Updated = $true
    Account = $Name
    TaskId = $task.id
    Path = $schedulerPath
    CronExpression = $cronExpression
    ScheduledFor = $runAt.ToString("yyyy-MM-dd HH:mm zzz")
  }
}

function Test-CheckedInToday($Status) {
  $checkIn = Convert-A2ETimeToLocal $Status.CheckInTime
  if (-not $checkIn) {
    return $false
  }

  return $checkIn.Date -eq ([datetimeoffset]::Now.Date) -and ([int]$Status.CheckInCoins -ge 60)
}

function Update-AccountStateFromVerifiedClaim([string]$Name, $Status, [string]$ResultText) {
  $state = Read-CheckinState
  $config = Get-AccountConfig $state $Name
  $next = Get-NextCheckInAfterFromStatus $Status
  $schedulerTaskUpdate = $null

  if ($next) {
    Set-JsonProperty $config "nextCheckInAfter" $next.ToString("yyyy-MM-ddTHH:mm:sszzz")
    Set-JsonProperty $config "lastSuccessfulCheckIn" (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    if ($AutoUpdateSchedulerTask) {
      $schedulerTaskUpdate = Update-CowSchedulerTaskFromNextCheckIn $Name $next
    }
  }

  Set-JsonProperty $config "lastKnownCoins" ([int]$Status.Coins)
  Set-JsonProperty $config "lastResult" $ResultText
  Save-CheckinState $state

  $schedulerTaskUpdate
}

function Confirm-ClaimResult([string]$Name, $BeforeStatus) {
  $lastStatus = $null
  $schedulerTaskUpdate = $null

  for ($attempt = 1; $attempt -le 8; $attempt++) {
    Start-Sleep -Seconds 3
    $lastStatus = Get-A2EApiStatus $Name

    $coinIncrease = if ($BeforeStatus) { [int]$lastStatus.Coins - [int]$BeforeStatus.Coins } else { 0 }
    $checkedInToday = Test-CheckedInToday $lastStatus

    if ($coinIncrease -ge 60 -or $checkedInToday) {
      $next = Get-NextCheckInAfterFromStatus $lastStatus
      $resultText = if ($coinIncrease -ge 60) {
        "Success. API verified the claim after clicking: coins increased from $($BeforeStatus.Coins) to $($lastStatus.Coins), CheckInCoins $($lastStatus.CheckInCoins), check-in time $((Convert-A2ETimeToLocal $lastStatus.CheckInTime).ToString("yyyy-MM-dd HH:mm:ss zzz")), next check-in after $(if ($next) { $next.ToString("yyyy-MM-dd HH:mm zzz") } else { "unknown" })."
      } elseif ($BeforeStatus -and (Test-CheckedInToday $BeforeStatus)) {
        "Already claimed today. API already showed today's successful check-in before retry; coins $($lastStatus.Coins), CheckInCoins $($lastStatus.CheckInCoins), check-in time $((Convert-A2ETimeToLocal $lastStatus.CheckInTime).ToString("yyyy-MM-dd HH:mm:ss zzz")), next check-in after $(if ($next) { $next.ToString("yyyy-MM-dd HH:mm zzz") } else { "unknown" })."
      } else {
        "Success. API verified today's check-in after clicking; coins $($lastStatus.Coins), CheckInCoins $($lastStatus.CheckInCoins), check-in time $((Convert-A2ETimeToLocal $lastStatus.CheckInTime).ToString("yyyy-MM-dd HH:mm:ss zzz")), next check-in after $(if ($next) { $next.ToString("yyyy-MM-dd HH:mm zzz") } else { "unknown" })."
      }

      if ($AutoUpdateState) {
        $schedulerTaskUpdate = Update-AccountStateFromVerifiedClaim $Name $lastStatus $resultText
      }

      return [pscustomobject]@{
        Verified = $true
        Result = if ($coinIncrease -ge 60) { "coin_increase" } elseif ($BeforeStatus -and (Test-CheckedInToday $BeforeStatus)) { "already_claimed_today" } else { "checked_in_today" }
        Attempts = $attempt
        CoinIncrease = $coinIncrease
        Status = $lastStatus
        NextCheckInAfter = if ($next) { $next.ToString("yyyy-MM-ddTHH:mm:sszzz") } else { $null }
        StateUpdated = [bool]$AutoUpdateState
        SchedulerTaskUpdate = $schedulerTaskUpdate
        Message = $resultText
      }
    }
  }

  return [pscustomobject]@{
    Verified = $false
    Result = "not_verified"
    Attempts = 8
    CoinIncrease = if ($BeforeStatus -and $lastStatus) { [int]$lastStatus.Coins - [int]$BeforeStatus.Coins } else { $null }
    Status = $lastStatus
    NextCheckInAfter = $null
    StateUpdated = $false
    Message = "Click was sent, but API did not verify a 60 coin increase or today's successful check-in within the wait window."
  }
}

function Get-ChromeLocalState {
  $localStatePath = Join-Path (Get-ChromeUserDataDir) "Local State"
  if (-not (Test-Path -LiteralPath $localStatePath)) {
    throw "Chrome Local State file was not found at '$localStatePath'."
  }

  Read-JsonFile $localStatePath
}

function Ensure-Win32Types {
  if ("A2E.Win32" -as [type]) {
    return
  }

  Add-Type @"
using System;
using System.Runtime.InteropServices;
namespace A2E {
  public class Win32 {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  }
}
"@
}

function Get-DueAccounts($State) {
  $now = Get-Date
  $accounts = @()

  foreach ($property in $State.accounts.PSObject.Properties) {
    $config = $property.Value
    if ($DueOnly -and $config.nextCheckInAfter) {
      $next = [datetimeoffset]::Parse($config.nextCheckInAfter)
      if ([datetimeoffset]::Now -lt $next) {
        continue
      }
    }

    $accounts += [pscustomobject]@{
      Name = $property.Name
      Config = $config
      Order = [int]$config.order
    }
  }

  $accounts | Sort-Object Order
}

function Open-A2EAccount($State, [string]$Name) {
  $config = Get-AccountConfig $State $Name
  Assert-AccountProfile $config $Name
  $chrome = Get-ChromePath
  $userDataDir = Get-ChromeUserDataDir
  $profile = $config.chromeProfileDirectory
  $targetUrl = if ($Url) { $Url } else { $State.siteUrl }
  $arguments = @(
    "--new-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=`"$userDataDir`"",
    "--profile-directory=`"$profile`"",
    $targetUrl
  )

  Start-Process -FilePath $chrome -ArgumentList $arguments | Out-Null
  Start-Sleep -Seconds 5
  Resolve-ProfilePickerIfNeeded $State $config $Name | Out-Null
  Click-ProbableA2ETab
  Navigate-FocusedChromeToUrl $targetUrl
  Focus-A2EChromeWindow
}

function Focus-A2EChromeWindow {
  Ensure-Win32Types

  $window = Get-Process chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and ($_.MainWindowTitle -match "A2E|AI Videos|video\.a2e|RunningHub|runninghub|Google Chrome") } |
    Sort-Object StartTime -Descending |
    Select-Object -First 1

  if (-not $window) {
    throw "No visible Chrome window was found."
  }

  $handle = $window.MainWindowHandle
  [A2E.Win32]::ShowWindow($handle, 9) | Out-Null
  Start-Sleep -Milliseconds 150
  [A2E.Win32]::SetWindowPos($handle, [IntPtr](-1), 100, 60, 1200, 900, 0x0040) | Out-Null
  Start-Sleep -Milliseconds 150
  [A2E.Win32]::SetForegroundWindow($handle) | Out-Null
  [A2E.Win32]::BringWindowToTop($handle) | Out-Null
  Start-Sleep -Milliseconds 150
  [A2E.Win32]::SetWindowPos($handle, [IntPtr](-2), 100, 60, 1200, 900, 0x0040) | Out-Null
  Start-Sleep -Milliseconds 250

  $window
}

function Get-A2EChromeWindows {
  Get-Process chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 -and ($_.MainWindowTitle -match "A2E|AI Videos|video\.a2e|RunningHub|runninghub") } |
    Sort-Object StartTime -Descending
}

function Close-ChromeWindow($Window) {
  if ($Window) {
    Ensure-Win32Types
    $Window.Refresh()
    [A2E.Win32]::ShowWindow($Window.MainWindowHandle, 9) | Out-Null
    Start-Sleep -Milliseconds 150
    [A2E.Win32]::SetForegroundWindow($Window.MainWindowHandle) | Out-Null
    [A2E.Win32]::BringWindowToTop($Window.MainWindowHandle) | Out-Null
    Start-Sleep -Milliseconds 250
  } else {
    $Window = Focus-A2EChromeWindow
  }

  $window = $Window
  $title = $window.MainWindowTitle
  $handle = $window.MainWindowHandle
  $closed = $window.CloseMainWindow()
  Start-Sleep -Seconds 2

  try {
    $window.Refresh()
  } catch {
  }

  [pscustomobject]@{
    Requested = $true
    CloseMainWindow = [bool]$closed
    Exited = [bool]$window.HasExited
    Handle = $handle
    WindowTitle = $title
  }
}

function Close-AllA2EChromeWindows {
  $results = @()
  foreach ($window in @(Get-A2EChromeWindows)) {
    try {
      $results += Close-ChromeWindow $window
    } catch {
      $results += [pscustomobject]@{
        Requested = $true
        CloseMainWindow = $false
        Exited = $false
        Handle = if ($window) { $window.MainWindowHandle } else { $null }
        WindowTitle = if ($window) { $window.MainWindowTitle } else { $null }
        Error = $_.Exception.Message
      }
    }
  }

  $results
}

function Close-FocusedChromeWindow {
  Close-ChromeWindow $null
}

function New-ManualActionRequired([string]$AccountName, [string]$Reason, [string]$Message, [string]$ScreenshotPath = $null) {
  [pscustomobject]@{
    Required = $true
    NeedsNotification = $true
    Account = $AccountName
    Reason = $Reason
    Message = $Message
    Screenshot = $ScreenshotPath
  }
}

function Navigate-FocusedChromeToUrl([string]$TargetUrl) {
  Focus-A2EChromeWindow | Out-Null
  Set-Clipboard -Value $TargetUrl

  Send-KeyChord 0x11 0x4C
  Start-Sleep -Milliseconds 200
  Send-KeyChord 0x11 0x56
  Start-Sleep -Milliseconds 200
  Send-Key 0x0D
  Start-Sleep -Seconds 5
}

function Click-ProbableA2ETab {
  $window = Focus-A2EChromeWindow
  $rect = New-Object A2E.Win32+RECT
  [A2E.Win32]::GetWindowRect($window.MainWindowHandle, [ref]$rect) | Out-Null

  $x = $rect.Left + 500
  $y = $rect.Top + 80

  [A2E.Win32]::SetCursorPos($x, $y) | Out-Null
  Start-Sleep -Milliseconds 120
  [A2E.Win32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  [A2E.Win32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Seconds 2
}

function Send-Key([byte]$KeyCode) {
  Ensure-Win32Types
  [A2E.Win32]::keybd_event($KeyCode, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 60
  [A2E.Win32]::keybd_event($KeyCode, 0, 0x0002, [UIntPtr]::Zero)
}

function Send-KeyChord([byte]$ModifierKeyCode, [byte]$KeyCode) {
  Ensure-Win32Types
  [A2E.Win32]::keybd_event($ModifierKeyCode, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 60
  [A2E.Win32]::keybd_event($KeyCode, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 60
  [A2E.Win32]::keybd_event($KeyCode, 0, 0x0002, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 60
  [A2E.Win32]::keybd_event($ModifierKeyCode, 0, 0x0002, [UIntPtr]::Zero)
}

function Test-ProfilePickerWindow($Window) {
  if (-not $Window) {
    return $false
  }

  $title = $Window.MainWindowTitle
  return $title -match "谁在使用 Chrome|Who's using Chrome"
}

function Get-ProfilePickerIndex($Config) {
  $localState = Get-ChromeLocalState
  $orders = @($localState.profile.profiles_order)

  for ($index = 0; $index -lt $orders.Count; $index++) {
    if ($orders[$index] -eq $Config.chromeProfileDirectory) {
      return $index
    }
  }

  throw "Chrome profile '$($Config.chromeProfileDirectory)' was not found in Local State profile order."
}

function Click-ProfilePickerCard($Window, [int]$ProfileIndex) {
  Ensure-Win32Types

  $rect = New-Object A2E.Win32+RECT
  [A2E.Win32]::GetWindowRect($Window.MainWindowHandle, [ref]$rect) | Out-Null
  $width = $rect.Right - $rect.Left

  # Chrome profile picker lays out profile cards plus the Add card as a centered row.
  $cardWidth = 164
  $cardGap = 15
  $visibleCardCount = 4
  $rowWidth = ($visibleCardCount * $cardWidth) + (($visibleCardCount - 1) * $cardGap)
  $rowLeft = [Math]::Max(0, [Math]::Floor(($width - $rowWidth) / 2))
  $x = $rect.Left + $rowLeft + [Math]::Floor($cardWidth / 2) + ($ProfileIndex * ($cardWidth + $cardGap))
  $y = $rect.Top + 435

  [A2E.Win32]::SetCursorPos($x, $y) | Out-Null
  Start-Sleep -Milliseconds 120
  [A2E.Win32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  [A2E.Win32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Seconds 3

  [pscustomobject]@{
    Clicked = $true
    X = $x
    Y = $y
    ProfileIndex = $ProfileIndex
    WindowTitle = $Window.MainWindowTitle
  }
}

function Resolve-ProfilePickerIfNeeded($State, $Config, [string]$Name) {
  $window = Focus-A2EChromeWindow
  if (-not (Test-ProfilePickerWindow $window)) {
    return $null
  }

  $profileIndex = Get-ProfilePickerIndex $Config
  $clickResult = Click-ProfilePickerCard $window $profileIndex

  $chrome = Get-ChromePath
  $targetUrl = if ($Url) { $Url } else { $State.siteUrl }
  $profile = $Config.chromeProfileDirectory
  $arguments = @(
    "--new-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--profile-directory=`"$profile`"",
    $targetUrl
  )
  Start-Process -FilePath $chrome -ArgumentList $arguments | Out-Null
  Start-Sleep -Seconds 5

  $clickResult
}

function Click-VisibleClaimButton {
  $window = Focus-A2EChromeWindow
  $rect = New-Object A2E.Win32+RECT
  [A2E.Win32]::GetWindowRect($window.MainWindowHandle, [ref]$rect) | Out-Null

  # Tested after positioning Chrome to 1200x900. This is the center of the purple
  # site button labeled "立即领取" in the daily reward modal, not a human check.
  $x = $rect.Left + 690
  $y = $rect.Top + 648

  [A2E.Win32]::SetCursorPos($x, $y) | Out-Null
  Start-Sleep -Milliseconds 120
  [A2E.Win32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Milliseconds 80
  [A2E.Win32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
  Start-Sleep -Seconds 2

  [pscustomobject]@{
    Clicked = $true
    X = $x
    Y = $y
    WindowTitle = $window.MainWindowTitle
  }
}

function Save-ScreenCapture([string]$Name) {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing

  $workspace = "C:\Users\RondleLiu\.openclaw\workspace"
  if (-not (Test-Path -LiteralPath $workspace)) {
    $workspace = $env:TEMP
  }

  $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $path = Join-Path $workspace "a2e-checkin-$Name-$timestamp.png"
  $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
  $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $graphics.Dispose()
  $bitmap.Dispose()

  $path
}

function Update-AccountState([string]$Name) {
  if (-not $NextCheckInAfter -and -not $PSBoundParameters.ContainsKey("LastKnownCoins") -and -not $Result) {
    throw "Pass at least one of -NextCheckInAfter, -LastKnownCoins, or -Result with -UpdateState."
  }

  $state = Read-CheckinState
  $config = Get-AccountConfig $state $Name

  if ($NextCheckInAfter) {
    $parsed = [datetimeoffset]::Parse($NextCheckInAfter)
    Set-JsonProperty $config "nextCheckInAfter" $parsed.ToString("yyyy-MM-ddTHH:mm:sszzz")
    Set-JsonProperty $config "lastSuccessfulCheckIn" (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
  }

  if ($script:PSBoundParameters.ContainsKey("LastKnownCoins")) {
    Set-JsonProperty $config "lastKnownCoins" $LastKnownCoins
  }

  if ($Result) {
    Set-JsonProperty $config "lastResult" $Result
  }

  Save-CheckinState $state
  Get-Content -LiteralPath $StatePath -Raw
}

if ($UpdateState) {
  if ($Account -eq "all") {
    throw "-UpdateState requires -Account lorna or -Account rondle."
  }
  Update-AccountState $Account
  exit 0
}

if ($ApiStatus) {
  if ($Account -eq "all") {
    $state = Read-CheckinState
    $statuses = @()
    foreach ($entry in Get-DueAccounts $state) {
      $statuses += Get-A2EApiStatus $entry.Name
    }
    $statuses | ConvertTo-Json -Depth 8
  } else {
    Get-A2EApiStatus $Account | ConvertTo-Json -Depth 8
  }
  exit 0
}

$state = Read-CheckinState
$selectedAccounts = if ($Account -eq "all") {
  Get-DueAccounts $state
} else {
  $config = Get-AccountConfig $state $Account
  if ($DueOnly -and $config.nextCheckInAfter -and ([datetimeoffset]::Now -lt [datetimeoffset]::Parse($config.nextCheckInAfter))) {
    @()
  } else {
    @([pscustomobject]@{ Name = $Account; Config = $config; Order = [int]$config.order })
  }
}

if (-not $selectedAccounts -or $selectedAccounts.Count -eq 0) {
  Write-Output "No due A2E accounts at this time."
  exit 0
}

$results = @()
foreach ($entry in $selectedAccounts) {
  $clickResult = $null
  $beforeStatus = $null
  $claimVerification = $null
  $screenshotPath = $null
  $openedWindow = $null
  $closeResult = $null
  $manualActionRequired = $null
  $failureNotification = $null
  $apiStatusError = $null
  $accountError = $null

  try {
    $openedWindow = Open-A2EAccount $state $entry.Name
    Focus-A2EChromeWindow | Out-Null

    if ($ClickClaim) {
      if ($VerifyClaim -or $AutoUpdateState) {
        try {
          $beforeStatus = Get-A2EApiStatus $entry.Name
        } catch {
          $apiStatusError = $_.Exception.Message
        }
      }

      $clickResult = Click-VisibleClaimButton

      if ($VerifyClaim -or $AutoUpdateState) {
        try {
          $claimVerification = Confirm-ClaimResult $entry.Name $beforeStatus
        } catch {
          if ($apiStatusError) {
            $apiStatusError = "$apiStatusError; $($_.Exception.Message)"
          } else {
            $apiStatusError = $_.Exception.Message
          }
        }
      }
    }

    if ($Screenshot -or $ClickClaim -or $OpenOnly) {
      $screenshotPath = Save-ScreenCapture $entry.Name
    }

    if ($ClickClaim -and $apiStatusError) {
      $manualActionRequired = New-ManualActionRequired `
        $entry.Name `
        "api_status_unavailable_after_click" `
        "A2E reward claim was clicked for '$($entry.Name)', but the API status could not be read afterward: $apiStatusError. If the page is showing human verification, complete it manually in the open Chrome window, then ask Agent to rerun A2E status and close the page." `
        $screenshotPath
    } elseif ($ClickClaim -and ($VerifyClaim -or $AutoUpdateState) -and $claimVerification -and -not $claimVerification.Verified) {
      $manualActionRequired = New-ManualActionRequired `
        $entry.Name `
        "claim_not_verified_after_click" `
        "A2E reward claim was clicked for '$($entry.Name)', but the API did not verify today's successful check-in within the wait window. If a real-person or human verification prompt is visible, complete it manually in the open Chrome window, then ask Agent to rerun A2E status and close the page." `
        $screenshotPath
    }

    $manualActionNeeded = $manualActionRequired -and $manualActionRequired.Required
    $autoCloseAfterVerifiedClaim = $ClickClaim -and $claimVerification -and $claimVerification.Verified -and -not $KeepOpen
    $shouldCloseBrowser = ($CloseAfter -or $autoCloseAfterVerifiedClaim) -and -not $KeepOpen -and -not $manualActionNeeded
    if ($shouldCloseBrowser) {
      $verifiedClaim = $claimVerification -and $claimVerification.Verified
      $safeToClose = $OpenOnly -or (-not $ClickClaim) -or $verifiedClaim
      if ($safeToClose) {
        $closeResult = Close-ChromeWindow $openedWindow
      } else {
        $closeResult = [pscustomobject]@{
          Requested = $true
          Skipped = $true
          Reason = "Claim was clicked but not API-verified; leaving the browser open for manual inspection."
        }
      }
    } elseif ($manualActionNeeded) {
      $closeResult = [pscustomobject]@{
        Requested = $false
        Skipped = $true
        Reason = "Manual action is required; leaving the A2E browser window open."
      }
    } elseif ($KeepOpen -and ($CloseAfter -or ($ClickClaim -and $claimVerification -and $claimVerification.Verified))) {
      $closeResult = [pscustomobject]@{
        Requested = $false
        Skipped = $true
        Reason = "-KeepOpen was passed; leaving the browser open after the check-in flow."
      }
    }
  } catch {
    $accountError = $_.Exception.Message
    if ($openedWindow) {
      $manualActionRequired = New-ManualActionRequired `
        $entry.Name `
        "automation_error_with_open_browser" `
        "A2E automation failed for '$($entry.Name)' after opening Chrome: $accountError. The browser is left open so you can inspect the page manually, then ask Agent to rerun A2E status and close it." `
        $screenshotPath
      $closeResult = [pscustomobject]@{
        Requested = $false
        Skipped = $true
        Reason = "Automation failed after opening Chrome; leaving the browser open for manual inspection."
      }
    } else {
      $failureNotification = [pscustomobject]@{
        NeedsNotification = $true
        Account = $entry.Name
        Reason = "automation_error"
        Message = "A2E automation failed for '$($entry.Name)' before opening a usable browser window: $accountError."
      }
    }
  }

  $results += [pscustomobject]@{
    Account = $entry.Name
    Email = $entry.Config.email
    Opened = $true
    ClickResult = $clickResult
    BeforeStatus = $beforeStatus
    ClaimVerification = $claimVerification
    Screenshot = $screenshotPath
    CloseResult = $closeResult
    ManualActionRequired = $manualActionRequired
    FailureNotification = $failureNotification
    Error = $accountError
    FinalCloseSweep = $null
  }
}

$manualResults = @($results | Where-Object { $_.ManualActionRequired -and $_.ManualActionRequired.Required })
$closedResults = @($results | Where-Object { $_.CloseResult -and $_.CloseResult.Requested -and -not $_.CloseResult.Skipped })
if (-not $KeepOpen -and $manualResults.Count -eq 0 -and $closedResults.Count -gt 0) {
  $finalCloseSweep = @(Close-AllA2EChromeWindows)
  if ($finalCloseSweep.Count -gt 0 -and $results.Count -gt 0) {
    Set-JsonProperty $results[$results.Count - 1] "FinalCloseSweep" $finalCloseSweep
  }
}

$hardErrors = @($results | Where-Object { $_.Error })
Write-Output ($results | ConvertTo-Json -Depth 8)
if ($hardErrors.Count -gt 0) {
  exit 1
}
