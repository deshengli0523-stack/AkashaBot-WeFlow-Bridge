$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('Akasha installer tests with spaces ' + [guid]::NewGuid().ToString('N'))

function Join-Chars {
  param([int[]]$Values)
  return -join @($Values | ForEach-Object { [char]$_ })
}

function Get-TestLauncherNames {
  [pscustomobject]@{
    Install = (Join-Chars @(0x5B89, 0x88C5)) + '.bat'
    Calibrate = (Join-Chars @(0x6821, 0x51C6)) + '.bat'
    Start = (Join-Chars @(0x542F, 0x52A8)) + '.bat'
    Stop = (Join-Chars @(0x505C, 0x6B62)) + '.bat'
    Health = (Join-Chars @(0x5065, 0x5EB7, 0x68C0, 0x67E5)) + '.bat'
    Update = (Join-Chars @(0x66F4, 0x65B0)) + '.bat'
  }
}

function Assert-True {
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) { throw $Message }
}

function Assert-Equal {
  param($Actual, $Expected, [string]$Message)
  if ([string]$Actual -cne [string]$Expected) { throw "$Message Expected=<$Expected> Actual=<$Actual>" }
}

function Assert-SequenceEqual {
  param([object[]]$Actual, [object[]]$Expected, [string]$Message)
  $actualText = @($Actual | ForEach-Object { [string]$_ }) -join '|'
  $expectedText = @($Expected | ForEach-Object { [string]$_ }) -join '|'
  Assert-Equal $actualText $expectedText $Message
}

function Stop-TestProcessTree {
  param([System.Diagnostics.Process]$Process)
  if ($null -eq $Process) { return }
  try {
    if (-not $Process.HasExited) {
      $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
      & $taskkill /PID $Process.Id /T /F 2>$null | Out-Null
      [void]$Process.WaitForExit(5000)
    }
  } catch {
    try {
      if (-not $Process.HasExited) {
        $Process.Kill()
        [void]$Process.WaitForExit(5000)
      }
    } catch {
    }
  }
}

function Assert-ThrowsLike {
  param([scriptblock]$Action, [string]$Pattern, [string]$Message)
  try { & $Action } catch {
    if ($_.Exception.Message -like $Pattern) { return $_ }
    throw "$Message Wrong error: $($_.Exception.Message)"
  }
  throw "$Message No error was thrown."
}

function Get-RelativeFiles {
  param([string]$Base)
  if (-not (Test-Path -LiteralPath $Base -PathType Container)) { return @() }
  return @(
    Get-ChildItem -LiteralPath $Base -Recurse -Force -File |
      ForEach-Object { $_.FullName.Substring($Base.Length + 1).Replace('\', '/') } |
      Sort-Object
  )
}

function Get-FileFingerprint {
  param([string]$Path)

  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    return [Convert]::ToBase64String($sha.ComputeHash([System.IO.File]::ReadAllBytes($Path)))
  } finally {
    $sha.Dispose()
  }
}

function New-TestBoundaries {
  param(
    [object[]]$Discoveries,
    [int]$PackageExitCode = 0,
    [int]$HealthExitCode = 0,
    [int[]]$HealthExitCodes = @(),
    [AllowNull()][string]$SelectedInstaller = $null,
    [AllowNull()][object]$CalibrationStatus = 'required'
  )

  $state = [pscustomobject]@{
    Calls = New-Object System.Collections.Generic.List[string]
    Discoveries = New-Object 'System.Collections.Generic.Queue[object]'
    HealthExitCodes = New-Object 'System.Collections.Generic.Queue[int]'
    MonotonicMilliseconds = [long]0
    ShortcutEntries = @()
    PackageExitCode = $PackageExitCode
    HealthExitCode = $HealthExitCode
    SelectedInstaller = $SelectedInstaller
    CalibrationStatus = $CalibrationStatus
    LockSeenByStarter = $true
  }
  foreach ($value in @($Discoveries)) { $state.Discoveries.Enqueue($value) }
  foreach ($value in @($HealthExitCodes)) { $state.HealthExitCodes.Enqueue([int]$value) }

  $prerequisite = {
    param($paths)
    $state.Calls.Add('prerequisite')
    return [pscustomobject]@{ FilePath = 'fixture-python.exe'; Prefix = @() }
  }.GetNewClosure()
  $discovery = {
    $state.Calls.Add('discovery')
    if ($state.Discoveries.Count -eq 0) { return $null }
    return $state.Discoveries.Dequeue()
  }.GetNewClosure()
  $selector = {
    $state.Calls.Add('selector')
    return $state.SelectedInstaller
  }.GetNewClosure()
  $runner = {
    param($kind, $path)
    $state.Calls.Add('package:' + $kind + ':' + [System.IO.Path]::GetFileName([string]$path))
    return $state.PackageExitCode
  }.GetNewClosure()
  $environment = {
    param($paths, $python)
    $state.Calls.Add('environment')
  }.GetNewClosure()
  $configuration = {
    param($paths, $configPath)
    $state.Calls.Add('configuration')
  }.GetNewClosure()
  $shortcuts = {
    param($entries)
    $state.Calls.Add('shortcuts')
    $state.ShortcutEntries = @($entries)
  }.GetNewClosure()
  $starter = {
    param($installRoot)
    $state.Calls.Add('start')
    $lockPath = Join-Path $installRoot 'data\state\lifecycle.lock'
    $state.LockSeenByStarter = Test-Path -LiteralPath $lockPath
  }.GetNewClosure()
  $health = {
    param($installRoot)
    $state.Calls.Add('health')
    if ($state.HealthExitCodes.Count -gt 0) { return $state.HealthExitCodes.Dequeue() }
    return $state.HealthExitCode
  }.GetNewClosure()
  $healthSleeper = {
    param($milliseconds)
    $state.Calls.Add('health-delay:' + [int]$milliseconds)
    $state.MonotonicMilliseconds += [long]$milliseconds
  }.GetNewClosure()
  $monotonicMillisecondsReader = {
    return [long]$state.MonotonicMilliseconds
  }.GetNewClosure()
  $calibrationStatusReader = {
    param($configPath)
    $state.Calls.Add('calibration')
    return $state.CalibrationStatus
  }.GetNewClosure()
  return [pscustomobject]@{
    State = $state
    Prerequisite = $prerequisite
    Discovery = $discovery
    Selector = $selector
    Runner = $runner
    Environment = $environment
    Configuration = $configuration
    Shortcuts = $shortcuts
    Starter = $starter
    Health = $health
    HealthSleeper = $healthSleeper
    MonotonicMillisecondsReader = $monotonicMillisecondsReader
    CalibrationStatusReader = $calibrationStatusReader
  }
}

function Invoke-TestInstall {
  param(
    [string]$InstallRoot,
    [string]$WeFlowConfigPath,
    $Boundaries,
    [string]$InstallerPath = '',
    [switch]$SkipStart,
    [scriptblock]$ReplacementHook,
    [string]$SourceRoot = $root,
    [scriptblock]$CalibrationStatusReader,
    [int]$HealthReadyTimeoutMilliseconds = 14,
    [int]$HealthRetryDelayMilliseconds = 7
  )
  $arguments = @{
    InstallRoot = $InstallRoot
    SourceRoot = $SourceRoot
    WeFlowInstallerPath = $InstallerPath
    WeFlowConfigPath = $WeFlowConfigPath
    PrerequisiteValidator = $Boundaries.Prerequisite
    WeFlowDiscovery = $Boundaries.Discovery
    InstallerSelector = $Boundaries.Selector
    PackageRunner = $Boundaries.Runner
    EnvironmentInitializer = $Boundaries.Environment
    ConfigurationInitializer = $Boundaries.Configuration
    ShortcutCreator = $Boundaries.Shortcuts
    ServiceStarter = $Boundaries.Starter
    HealthChecker = $Boundaries.Health
    HealthReadyTimeoutMilliseconds = $HealthReadyTimeoutMilliseconds
    HealthRetryDelayMilliseconds = $HealthRetryDelayMilliseconds
    HealthRetrySleeper = $Boundaries.HealthSleeper
    HealthMonotonicMillisecondsReader = $Boundaries.MonotonicMillisecondsReader
    CalibrationStatusReader = if ($null -ne $CalibrationStatusReader) { $CalibrationStatusReader } else { $Boundaries.CalibrationStatusReader }
    SkipStart = $SkipStart
  }
  if ($null -ne $ReplacementHook) { $arguments.ReplacementHook = $ReplacementHook }
  return Invoke-AkashaInstall @arguments
}

function Assert-NoTransactionResidue {
  param([string]$InstallRoot)
  if (-not (Test-Path -LiteralPath $InstallRoot)) { return }
  $residue = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -Force | Where-Object { $_.Name -like '.install-stage-*' -or $_.Name -like '.install-rollback-*' })
  Assert-Equal $residue.Count 0 'Installer left transaction directories.'
}

function New-TestSourceFixture {
  param([string]$Path)
  New-Item -ItemType Directory -Force -Path $Path | Out-Null
  foreach ($entry in @(Get-AkashaInstallPayload)) {
    $source = Join-Path $root ([string]$entry.Source)
    $destination = Join-Path $Path ([string]$entry.Source)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
  }
  return $Path
}

$launchers = Get-TestLauncherNames
$entrypoints = @('scripts\Install.ps1', $launchers.Install, $launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health)
foreach ($relative in $entrypoints) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf)) { throw "Installer entrypoint missing: $relative" }
}
Assert-True (-not (Test-Path -LiteralPath (Join-Path $root $launchers.Update))) 'Phase 1 shipped an update entrypoint.'

$installSource = Get-Content -LiteralPath (Join-Path $root 'scripts\Install.ps1') -Raw -Encoding UTF8
Assert-True ($installSource -match 'OpenFileDialog') 'Installer does not offer a local WeFlow file picker.'
Assert-True ($installSource -notmatch '(?i)Invoke-WebRequest[\s\S]{0,200}WeFlow|curl(?:\.exe)?[\s\S]{0,200}WeFlow') 'Installer contains WeFlow download code.'
Assert-True ($installSource -match 'Start-Process' -and $installSource -match '-Wait' -and $installSource -match '-PassThru') 'Default package runner is not synchronous and exit-code aware.'
Assert-True ($installSource -match "Start-Process -FilePath 'msiexec\.exe'" -and $installSource -match 'ArgumentList @\(''/i'', \$quotedPath\)' -and $installSource -match '\[int\]\$process\.ExitCode') 'Default MSI runner does not quote a spaced path or inspect ExitCode.'
Assert-True ($installSource -notmatch '[^\x00-\x7F]') 'Install.ps1 must remain ASCII-only for Windows PowerShell 5.1.'
Assert-True ($installSource -match '\$installResult\s*=\s*Invoke-AkashaInstall' -and $installSource -match '\$installResult\.CalibrationRequired') 'Direct installer entrypoint does not retain the install result for calibration guidance.'
Assert-True ($installSource -match '\$launchers\.Calibrate') 'Direct installer entrypoint does not construct the calibration launcher from codepoints.'

foreach ($name in @($launchers.Install, $launchers.Start, $launchers.Stop, $launchers.Health)) {
  $path = Join-Path $root $name
  $bytes = [System.IO.File]::ReadAllBytes($path)
  Assert-True (@($bytes | Where-Object { $_ -gt 127 }).Count -eq 0) "$name is not ASCII-only."
  $text = [System.IO.File]::ReadAllText($path)
  Assert-True ($text -match '%~dp0') "$name does not use an absolute source-relative root."
  Assert-True ($text -match 'powershell\.exe -NoProfile -ExecutionPolicy Bypass -File "') "$name changed the stable PowerShell invocation."
  Assert-True ($text -match 'set "CODE=%ERRORLEVEL%"') "$name does not capture ERRORLEVEL immediately."
  Assert-True ($text -match 'exit /b %CODE%') "$name does not preserve ERRORLEVEL."
}
$calibrateBat = [System.IO.File]::ReadAllText((Join-Path $root $launchers.Calibrate))
Assert-True (@([System.IO.File]::ReadAllBytes((Join-Path $root $launchers.Calibrate)) | Where-Object { $_ -gt 127 }).Count -eq 0) "$($launchers.Calibrate) is not ASCII-only."
Assert-True ($calibrateBat -match '%~dp0scripts\\Calibrate-Uia\.ps1') "$($launchers.Calibrate) does not use the source-relative calibration script."
Assert-True ($calibrateBat -match 'set "code=%ERRORLEVEL%"') "$($launchers.Calibrate) does not capture ERRORLEVEL immediately."
Assert-True ($calibrateBat -match 'exit /b %code%') "$($launchers.Calibrate) does not preserve ERRORLEVEL."
Assert-True ($calibrateBat -match '(?m)^pause\s*$') "$($launchers.Calibrate) must pause for interactive calibration."
$healthBat = [System.IO.File]::ReadAllText((Join-Path $root $launchers.Health))
Assert-True ($healthBat -match '(?m)^pause\s*$') 'Health launcher must always pause.'
foreach ($name in @($launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health)) {
  $launcherText = [System.IO.File]::ReadAllText((Join-Path $root $name))
  Assert-True ($launcherText.Contains('-InstallRoot "%~dp0."')) "$name must terminate the source-relative install root with a dot before native argument parsing."
  Assert-True (-not $launcherText.Contains('-InstallRoot "%~dp0"')) "$name passes a trailing backslash before the closing quote and can append a literal quote to InstallRoot."
}
foreach ($name in @($launchers.Install, $launchers.Start, $launchers.Stop)) {
  Assert-True ([System.IO.File]::ReadAllText((Join-Path $root $name)) -match 'if not "%CODE%"=="0" pause') "$name must pause only on failure."
}

. (Join-Path $root 'scripts\Install.ps1')
Assert-True ($null -ne (Get-Command Invoke-AkashaInstall -ErrorAction SilentlyContinue)) 'Dot-sourcing did not expose Invoke-AkashaInstall.'
Assert-True ($null -ne (Get-Command Wait-AkashaInstallHealth -ErrorAction SilentlyContinue)) 'Installer does not expose bounded service-readiness polling.'

$transientHealth = New-TestBoundaries -Discoveries @() -HealthExitCodes @(1, 1, 0)
$transientHealthResult = Wait-AkashaInstallHealth -InstallRoot $root -HealthChecker $transientHealth.Health -TimeoutMilliseconds 28 -RetryDelayMilliseconds 7 -Sleeper $transientHealth.HealthSleeper -MonotonicMillisecondsReader $transientHealth.MonotonicMillisecondsReader
Assert-Equal $transientHealthResult.ExitCode 0 'Readiness polling rejected services that became healthy before the deadline.'
Assert-Equal $transientHealthResult.Attempts 3 'Readiness polling reported the wrong successful attempt.'
Assert-SequenceEqual @($transientHealth.State.Calls) @('health', 'health-delay:7', 'health', 'health-delay:7', 'health') 'Readiness polling changed retry order or delay placement.'

$persistentHealth = New-TestBoundaries -Discoveries @() -HealthExitCode 1
$persistentHealthResult = Wait-AkashaInstallHealth -InstallRoot $root -HealthChecker $persistentHealth.Health -TimeoutMilliseconds 22 -RetryDelayMilliseconds 11 -Sleeper $persistentHealth.HealthSleeper -MonotonicMillisecondsReader $persistentHealth.MonotonicMillisecondsReader
Assert-Equal $persistentHealthResult.ExitCode 1 'Readiness polling accepted services that never became healthy.'
Assert-Equal $persistentHealthResult.Attempts 3 'Readiness polling did not stop at the deadline.'
Assert-SequenceEqual @($persistentHealth.State.Calls) @('health', 'health-delay:11', 'health', 'health-delay:11', 'health') 'Readiness timeout slept after the final failed attempt or skipped a bounded retry.'

$lateHealth = New-TestBoundaries -Discoveries @()
$lateHealthChecker = {
  param($installRoot)
  $lateHealth.State.Calls.Add('health')
  $lateHealth.State.MonotonicMilliseconds = 29
  return 0
}.GetNewClosure()
$lateHealthResult = Wait-AkashaInstallHealth -InstallRoot $root -HealthChecker $lateHealthChecker -TimeoutMilliseconds 28 -RetryDelayMilliseconds 7 -Sleeper $lateHealth.HealthSleeper -MonotonicMillisecondsReader $lateHealth.MonotonicMillisecondsReader
Assert-Equal $lateHealthResult.ExitCode 1 'Readiness polling accepted a health probe that completed after the deadline.'
Assert-Equal $lateHealthResult.Attempts 1 'Late health success triggered an unexpected retry.'
Assert-SequenceEqual @($lateHealth.State.Calls) @('health') 'Late health success slept or ran more than one probe.'

$stalledClockHealth = New-TestBoundaries -Discoveries @() -HealthExitCode 1
$stalledClockReader = { return [long]0 }
$stalledClockResult = Wait-AkashaInstallHealth -InstallRoot $root -HealthChecker $stalledClockHealth.Health -TimeoutMilliseconds 22 -RetryDelayMilliseconds 11 -Sleeper $stalledClockHealth.HealthSleeper -MonotonicMillisecondsReader $stalledClockReader
Assert-Equal $stalledClockResult.ExitCode 1 'Readiness polling accepted unhealthy services while the monotonic clock was stalled.'
Assert-Equal $stalledClockResult.Attempts 3 'Readiness polling did not apply its defensive attempt bound while the monotonic clock was stalled.'
Assert-SequenceEqual @($stalledClockHealth.State.Calls) @('health', 'health-delay:11', 'health', 'health-delay:11', 'health') 'Stalled-clock readiness polling did not stop at its defensive attempt bound.'

$payload = @(Get-AkashaInstallPayload)
Assert-Equal $payload.Count 30 'Installed payload count changed.'
Assert-Equal @($payload | Where-Object { $_.Source -like 'bridge\*' }).Count 14 'Bridge payload count changed.'
Assert-Equal @($payload | Where-Object { $_.Source -like 'scripts\*' }).Count 9 'Script payload count changed.'
Assert-Equal @($payload | Where-Object { $_.Source -notlike 'bridge\*' -and $_.Source -notlike 'scripts\*' }).Count 7 'Root payload count changed.'
Assert-True (@($payload | Where-Object { $_.Source -ceq $launchers.Install }).Count -eq 0) 'Install launcher must not be installed.'
$expectedPayloadSources = @(
  'bridge\bridge_core.py', 'bridge\config.py', 'bridge\main.py', 'bridge\ob_client.py',
  'bridge\ob_protocol.py', 'bridge\privacy.py', 'bridge\state.py',
  'bridge\uia_fixed_sender.py', 'bridge\uia_support.py', 'bridge\calibrate_uia_fixed.py', 'bridge\web_panel.py',
  'bridge\config.example.json', 'bridge\requirements.txt', 'bridge\requirements.lock',
  'scripts\AkashaBot.Common.psm1', 'scripts\Test-Prerequisites.ps1',
  'scripts\Initialize-Environments.ps1', 'scripts\Initialize-Configuration.ps1',
  'scripts\Start-Services.ps1', 'scripts\Stop-Services.ps1', 'scripts\Test-Health.ps1',
  'scripts\Install.ps1', 'scripts\Calibrate-Uia.ps1', $launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health,
  'VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md'
)
Assert-SequenceEqual @($payload.Source | Sort-Object) @($expectedPayloadSources | Sort-Object) 'Installed payload manifest is not the frozen exact allowlist.'

try {
  New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
  $launcherProbeRoot = Join-Path $fixtureRoot 'launcher transport with spaces'
  $launcherProbeScripts = Join-Path $launcherProbeRoot 'scripts'
  New-Item -ItemType Directory -Force -Path $launcherProbeScripts | Out-Null
  $launcherProbeSource = @'
param([string]$InstallRoot)
$markerName = [System.IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Name) + '.txt'
$markerPath = Join-Path (Split-Path -Parent $PSScriptRoot) $markerName
[System.IO.File]::WriteAllText($markerPath, $InstallRoot, (New-Object System.Text.UTF8Encoding($false)))
exit 0
'@
  $launcherProbeCases = @(
    [pscustomobject]@{ Launcher = $launchers.Calibrate; Script = 'Calibrate-Uia.ps1' },
    [pscustomobject]@{ Launcher = $launchers.Start; Script = 'Start-Services.ps1' },
    [pscustomobject]@{ Launcher = $launchers.Stop; Script = 'Stop-Services.ps1' },
    [pscustomobject]@{ Launcher = $launchers.Health; Script = 'Test-Health.ps1' }
  )
  foreach ($case in $launcherProbeCases) {
    Copy-Item -LiteralPath (Join-Path $root $case.Launcher) -Destination (Join-Path $launcherProbeRoot $case.Launcher) -Force
    [System.IO.File]::WriteAllText((Join-Path $launcherProbeScripts $case.Script), $launcherProbeSource, (New-Object System.Text.UTF8Encoding($false)))
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = 'cmd.exe'
    $processInfo.Arguments = '/d /c call "' + $case.Launcher + '"'
    $processInfo.WorkingDirectory = $launcherProbeRoot
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardInput = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $process = $null
    try {
      $process = New-Object System.Diagnostics.Process
      $process.StartInfo = $processInfo
      Assert-True $process.Start() "$($case.Launcher) transport probe did not start."
      $process.StandardInput.WriteLine()
      $process.StandardInput.Close()
      if (-not $process.WaitForExit(15000)) {
        Stop-TestProcessTree -Process $process
        throw "$($case.Launcher) transport probe timed out."
      }
      $probeOutput = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
      Assert-Equal $process.ExitCode 0 "$($case.Launcher) transport probe failed: $probeOutput"
    } finally {
      if ($null -ne $process) {
        Stop-TestProcessTree -Process $process
        $process.Dispose()
      }
    }
    $markerPath = Join-Path $launcherProbeRoot ([System.IO.Path]::GetFileNameWithoutExtension($case.Script) + '.txt')
    Assert-True (Test-Path -LiteralPath $markerPath -PathType Leaf) "$($case.Launcher) transport probe did not capture InstallRoot."
    $receivedRoot = [System.IO.File]::ReadAllText($markerPath)
    Assert-True (-not $receivedRoot.Contains('"')) "$($case.Launcher) appended a literal quote to InstallRoot."
    Assert-Equal ([System.IO.Path]::GetFullPath($receivedRoot).TrimEnd('\', '/')) ([System.IO.Path]::GetFullPath($launcherProbeRoot).TrimEnd('\', '/')) "$($case.Launcher) changed InstallRoot during BAT to PowerShell transport."
  }
  $externalRoot = Join-Path $fixtureRoot 'external packages and config'
  New-Item -ItemType Directory -Force -Path $externalRoot | Out-Null
  $weFlowExe = Join-Path $externalRoot 'WeFlow.exe'
  Copy-Item -LiteralPath (Get-Command powershell.exe).Source -Destination $weFlowExe -Force
  $weFlowConfig = Join-Path $externalRoot 'WeFlow-config.json'
  [System.IO.File]::WriteAllText($weFlowConfig, '{}', (New-Object System.Text.UTF8Encoding($false)))

  $successRoot = Join-Path $fixtureRoot 'successful install root with spaces'
  $success = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'ready'
  $result = Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $success -SkipStart
  Assert-Equal $result.Status 'installed' 'SkipStart install did not succeed.'
  Assert-Equal $result.Started $false 'SkipStart install unexpectedly started services.'
  Assert-Equal $result.CalibrationRequired $false 'Ready SkipStart install reported calibration required.'
  $expectedBridge = @($payload | Where-Object { $_.Destination -like 'app\bridge\*' } | ForEach-Object { [System.IO.Path]::GetFileName([string]$_.Destination) } | Sort-Object)
  $actualBridge = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'app\bridge') -File | Select-Object -ExpandProperty Name | Sort-Object)
  Assert-SequenceEqual $actualBridge $expectedBridge 'Installed bridge payload is not exact.'
  $expectedScripts = @($payload | Where-Object { $_.Destination -like 'scripts\*' } | ForEach-Object { [System.IO.Path]::GetFileName([string]$_.Destination) } | Sort-Object)
  $actualScripts = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'scripts') -File | Select-Object -ExpandProperty Name | Sort-Object)
  Assert-SequenceEqual $actualScripts $expectedScripts 'Installed script payload is not exact.'
  foreach ($name in @($launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health, 'VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    Assert-True (Test-Path -LiteralPath (Join-Path $successRoot $name) -PathType Leaf) "Installed root payload is missing $name."
  }
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $successRoot $launchers.Install))) 'Install launcher was copied into the product.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $successRoot 'README.md'))) 'An extra repository file was copied.'
  $installStatePath = Join-Path $successRoot 'data\state\install.json'
  $installState = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-SequenceEqual @($installState.PSObject.Properties.Name | Sort-Object) @('status', 'updated_at', 'version') 'Installed state metadata changed.'
  Assert-Equal $installState.status 'installed' 'Installed state has wrong status.'
  Assert-Equal $installState.version ((Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw -Encoding UTF8).Trim()) 'Installed version changed.'
  $pathBytes = [System.IO.File]::ReadAllBytes((Join-Path $successRoot 'data\state\weflow-path.txt'))
  Assert-True (-not ($pathBytes.Length -ge 3 -and $pathBytes[0] -eq 0xEF -and $pathBytes[1] -eq 0xBB -and $pathBytes[2] -eq 0xBF)) 'weflow-path.txt contains a UTF-8 BOM.'
  Assert-Equal ([System.Text.Encoding]::UTF8.GetString($pathBytes)) ([System.IO.Path]::GetFullPath($weFlowExe)) 'weflow-path.txt changed the canonical path.'
  Assert-SequenceEqual $success.State.Calls @('prerequisite', 'discovery', 'environment', 'configuration', 'shortcuts', 'calibration') 'SkipStart call order changed.'
  Assert-True ($success.State.Calls -cnotcontains 'start' -and $success.State.Calls -cnotcontains 'health') 'SkipStart crossed start/health boundaries.'
  Assert-Equal $success.State.ShortcutEntries.Count 3 'Shortcut count changed.'
  Assert-True (@($success.State.ShortcutEntries | Where-Object { [string]$_.Target -ceq (Join-Path $successRoot $launchers.Calibrate) }).Count -eq 0) 'Calibration launcher unexpectedly received a desktop shortcut.'
  foreach ($entry in $success.State.ShortcutEntries) {
    Assert-Equal $entry.WorkingDirectory $successRoot 'Shortcut working directory is not the install root.'
    Assert-True ([System.IO.Path]::GetFullPath([string]$entry.Target).StartsWith($successRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) 'Shortcut target escaped the install root.'
  }
  Assert-NoTransactionResidue -InstallRoot $successRoot

  foreach ($case in @(
      [pscustomobject]@{ Kind = 'exe'; Extension = '.exe' },
      [pscustomobject]@{ Kind = 'msi'; Extension = '.msi' }
    )) {
    $packagePath = Join-Path $externalRoot ('local installer with spaces ' + $case.Kind + $case.Extension)
    Copy-Item -LiteralPath (Get-Command powershell.exe).Source -Destination $packagePath -Force
    $packageRoot = Join-Path $fixtureRoot ('package install ' + $case.Kind)
    $package = New-TestBoundaries -Discoveries @('', $weFlowExe)
    Invoke-TestInstall -InstallRoot $packageRoot -WeFlowConfigPath $weFlowConfig -Boundaries $package -InstallerPath $packagePath -SkipStart | Out-Null
    Assert-True (@($package.State.Calls | Where-Object { $_ -like ('package:' + $case.Kind + ':*') }).Count -eq 1) "$($case.Kind) package boundary was not invoked exactly once."
    Assert-NoTransactionResidue -InstallRoot $packageRoot
  }

  $invalidPackage = Join-Path $externalRoot 'not-an-installer.txt'
  Set-Content -LiteralPath $invalidPackage -Value 'fixture' -Encoding ASCII
  $invalid = New-TestBoundaries -Discoveries @('')
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot (Join-Path $fixtureRoot 'invalid extension') -WeFlowConfigPath $weFlowConfig -Boundaries $invalid -InstallerPath $invalidPackage -SkipStart } 'E_WEFLOW_INSTALLER:*' 'Invalid package extension was accepted.' | Out-Null
  Assert-True (@($invalid.State.Calls | Where-Object { $_ -like 'package:*' }).Count -eq 0) 'Invalid package reached the runner.'

  $cancelled = New-TestBoundaries -Discoveries @('') -SelectedInstaller $null
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot (Join-Path $fixtureRoot 'cancelled picker') -WeFlowConfigPath $weFlowConfig -Boundaries $cancelled -SkipStart } 'E_WEFLOW_CANCELLED:*' 'Cancelled picker was accepted.' | Out-Null
  Assert-SequenceEqual $cancelled.State.Calls @('prerequisite', 'discovery', 'selector') 'Cancel path crossed a forbidden boundary.'

  $nonzeroPackagePath = Join-Path $externalRoot 'failing installer.exe'
  Copy-Item -LiteralPath (Get-Command powershell.exe).Source -Destination $nonzeroPackagePath -Force
  $nonzeroRoot = Join-Path $fixtureRoot 'nonzero package'
  $nonzero = New-TestBoundaries -Discoveries @('') -PackageExitCode 17
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $nonzeroRoot -WeFlowConfigPath $weFlowConfig -Boundaries $nonzero -InstallerPath $nonzeroPackagePath -SkipStart } 'E_WEFLOW_INSTALL_FAILED:*' 'Non-zero package exit was accepted.' | Out-Null
  Assert-True (@($nonzero.State.Calls | Where-Object { $_ -like 'environment' -or $_ -like 'configuration' -or $_ -like 'shortcuts' }).Count -eq 0) 'Non-zero package crossed a post-package boundary.'
  $nonzeroLogPath = Join-Path $nonzeroRoot 'data\logs\install.log'
  Assert-True (Test-Path -LiteralPath $nonzeroLogPath -PathType Leaf) 'Non-zero package failure did not create an installer log.'
  $nonzeroLog = Get-Content -LiteralPath $nonzeroLogPath -Raw -Encoding UTF8
  Assert-True ($nonzeroLog -match 'E_WEFLOW_INSTALL_FAILED' -and -not $nonzeroLog.Contains($nonzeroPackagePath)) 'Non-zero package log is missing the fixed code or leaked its path.'

  $prerequisiteFailureRoot = Join-Path $fixtureRoot 'prerequisite failure'
  $prerequisiteFailure = New-TestBoundaries -Discoveries @($weFlowExe)
  $prerequisiteFailure.Prerequisite = { param($paths) throw 'E_PYTHON_312_X64: fixture failure' }
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $prerequisiteFailureRoot -WeFlowConfigPath $weFlowConfig -Boundaries $prerequisiteFailure -SkipStart } 'E_PYTHON_312_X64:*' 'Prerequisite failure was not preserved.' | Out-Null
  $prerequisiteLogPath = Join-Path $prerequisiteFailureRoot 'data\logs\install.log'
  Assert-True (Test-Path -LiteralPath $prerequisiteLogPath -PathType Leaf) 'Prerequisite failure did not create an installer log.'
  Assert-True ((Get-Content -LiteralPath $prerequisiteLogPath -Raw -Encoding UTF8) -match 'E_PYTHON_312_X64') 'Prerequisite failure log omitted its fixed code.'

  $pendingRoot = Join-Path $fixtureRoot 'weflow rediscovery pending'
  $pendingPackagePath = Join-Path $externalRoot 'private-selected-installer-secret.exe'
  Copy-Item -LiteralPath (Get-Command powershell.exe).Source -Destination $pendingPackagePath -Force
  $pending = New-TestBoundaries -Discoveries @('', '')
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $pendingRoot -WeFlowConfigPath $weFlowConfig -Boundaries $pending -InstallerPath $pendingPackagePath -SkipStart } 'E_WEFLOW_NOT_DETECTED:*' 'Missing post-install discovery did not become pending.' | Out-Null
  $pendingState = Get-Content -LiteralPath (Join-Path $pendingRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-SequenceEqual @($pendingState.PSObject.Properties.Name | Sort-Object) @('error_code', 'status', 'updated_at', 'version') 'Pending state metadata changed.'
  Assert-Equal $pendingState.status 'weflow_pending' 'Missing discovery has wrong state.'
  Assert-Equal $pendingState.error_code 'E_WEFLOW_NOT_DETECTED' 'Missing discovery has wrong code.'
  Assert-True ($pending.State.Calls -contains 'environment') 'Pending path did not initialize environments after real payload installation.'
  Assert-True ($pending.State.Calls -notcontains 'configuration' -and $pending.State.Calls -notcontains 'shortcuts' -and $pending.State.Calls -notcontains 'start') 'Pending path crossed configuration/shortcut/start boundaries.'
  $pendingArtifacts = (Get-Content -LiteralPath (Join-Path $pendingRoot 'data\state\install.json') -Raw -Encoding UTF8) + (Get-Content -LiteralPath (Join-Path $pendingRoot 'data\logs\install.log') -Raw -Encoding UTF8)
  Assert-True (-not $pendingArtifacts.Contains($pendingPackagePath) -and -not $pendingArtifacts.Contains('private-selected-installer-secret')) 'Selected installer path leaked into state or log.'
  Assert-NoTransactionResidue -InstallRoot $pendingRoot

  $configPendingRoot = Join-Path $fixtureRoot 'config pending'
  $configPending = New-TestBoundaries -Discoveries @($weFlowExe)
  $missingConfig = Join-Path $externalRoot 'missing-WeFlow-config.json'
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $configPendingRoot -WeFlowConfigPath $missingConfig -Boundaries $configPending -SkipStart } 'E_WEFLOW_CONFIG_MISSING:*' 'Missing WeFlow config did not become pending.' | Out-Null
  $configPendingState = Get-Content -LiteralPath (Join-Path $configPendingRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal $configPendingState.status 'weflow_config_pending' 'Missing WeFlow config has wrong state.'
  Assert-Equal $configPendingState.error_code 'E_WEFLOW_CONFIG_MISSING' 'Missing WeFlow config has wrong code.'
  Assert-True ($configPending.State.Calls -notcontains 'configuration' -and $configPending.State.Calls -notcontains 'shortcuts' -and $configPending.State.Calls -notcontains 'start') 'Config-pending path crossed forbidden boundaries.'

  $configRaceRoot = Join-Path $fixtureRoot 'config disappears at boundary'
  $configRace = New-TestBoundaries -Discoveries @($weFlowExe)
  $configRace.Configuration = { param($paths, $configPath) throw 'E_WEFLOW_CONFIG_MISSING: fixture disappeared' }
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $configRaceRoot -WeFlowConfigPath $weFlowConfig -Boundaries $configRace -SkipStart } 'E_WEFLOW_CONFIG_MISSING:*' 'Configuration-boundary missing error was not preserved.' | Out-Null
  $configRaceState = Get-Content -LiteralPath (Join-Path $configRaceRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal $configRaceState.status 'weflow_config_pending' 'Configuration-boundary missing error did not write pending state.'
  Assert-Equal $configRaceState.error_code 'E_WEFLOW_CONFIG_MISSING' 'Configuration-boundary missing state has wrong code.'

  $oldBridge = 'old bridge payload sentinel'
  [System.IO.File]::WriteAllText((Join-Path $successRoot 'app\bridge\main.py'), $oldBridge, (New-Object System.Text.UTF8Encoding($false)))
  New-Item -ItemType Directory -Force -Path (Join-Path $successRoot 'data\bridge') | Out-Null
  $seededSecret = 'seeded-secret-value-1234567890'
  $readyConfig = '{"access_token":"' + $seededSecret + '","uia_fixed_calibration":{"schema_version":1,"completed":true,"coordinate_space":"client_area_ratio","points":{"search_box":{"x":0.1,"y":0.1},"first_result":{"x":0.2,"y":0.2},"message_input":{"x":0.6,"y":0.8},"send_button":{"x":0.9,"y":0.9}},"reference":{"client_width":1200,"client_height":800,"aspect_ratio":1.5,"dpi":96}}}'
  $installedBridgeConfig = Join-Path $successRoot 'data\bridge\config.json'
  [System.IO.File]::WriteAllText($installedBridgeConfig, $readyConfig, (New-Object System.Text.UTF8Encoding($false)))
  $readyConfigFingerprint = Get-FileFingerprint $installedBridgeConfig
  [System.IO.File]::WriteAllText((Join-Path $successRoot 'runtime\runtime-sentinel.txt'), 'runtime-preserved', (New-Object System.Text.UTF8Encoding($false)))
  $rerun = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'ready'
  $rerunResult = Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $rerun
  Assert-Equal $rerunResult.Started $true 'Ready schema 1 update did not start services by default.'
  Assert-Equal $rerunResult.CalibrationRequired $false 'Ready schema 1 update requested calibration.'
  Assert-SequenceEqual @($rerun.State.Calls | Select-Object -Last 3) @('calibration', 'start', 'health') 'Ready schema 1 update changed calibration/start/health order.'
  Assert-Equal (Get-Content -LiteralPath (Join-Path $successRoot 'runtime\runtime-sentinel.txt') -Raw -Encoding UTF8) 'runtime-preserved' 'Rerun changed runtime data.'
  Assert-Equal (Get-FileFingerprint $installedBridgeConfig) $readyConfigFingerprint 'Ready schema 1 update changed bridge config bytes.'
  $bridgeBackups = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'data\backups') -Directory -Filter 'bridge-*')
  Assert-True (@($bridgeBackups | Where-Object { (Get-Content -LiteralPath (Join-Path $_.FullName 'main.py') -Raw -Encoding UTF8) -ceq $oldBridge }).Count -ge 1) 'Rerun did not back up the previous bridge.'
  $safeArtifacts = (Get-Content -LiteralPath (Join-Path $successRoot 'data\state\install.json') -Raw -Encoding UTF8) + (Get-Content -LiteralPath (Join-Path $successRoot 'data\logs\install.log') -Raw -Encoding UTF8)
  Assert-True (-not $safeArtifacts.Contains($seededSecret) -and -not $safeArtifacts.Contains($weFlowExe)) 'A seeded secret or WeFlow executable path leaked into installer metadata.'

  [System.IO.File]::WriteAllText($installedBridgeConfig, ('{"access_token":"' + $seededSecret + '"}'), (New-Object System.Text.UTF8Encoding($false)))
  $legacyConfigFingerprint = Get-FileFingerprint $installedBridgeConfig
  $legacyUpdate = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'required'
  $legacyUpdateResult = Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $legacyUpdate
  Assert-Equal $legacyUpdateResult.Status 'installed' 'v0.1 update without nested calibration did not succeed.'
  Assert-Equal $legacyUpdateResult.Started $false 'v0.1 update without nested calibration unexpectedly started services.'
  Assert-Equal $legacyUpdateResult.CalibrationRequired $true 'v0.1 update without nested calibration omitted the calibration flag.'
  Assert-True ($legacyUpdate.State.Calls -cnotcontains 'start' -and $legacyUpdate.State.Calls -cnotcontains 'health') 'v0.1 update without nested calibration crossed start/health boundaries.'
  Assert-Equal (Get-FileFingerprint $installedBridgeConfig) $legacyConfigFingerprint 'v0.1 update changed bridge config bytes.'

  $rollbackOld = 'rollback old bridge sentinel'
  [System.IO.File]::WriteAllText((Join-Path $successRoot 'app\bridge\main.py'), $rollbackOld, (New-Object System.Text.UTF8Encoding($false)))
  $rollback = New-TestBoundaries -Discoveries @($weFlowExe)
  $failAfterMove = { param($phase) if ($phase -ceq 'AfterExistingMoved') { throw 'E_FIXTURE_REPLACEMENT_FAILURE' } }
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $rollback -SkipStart -ReplacementHook $failAfterMove } 'E_FIXTURE_REPLACEMENT_FAILURE*' 'Injected replacement failure was not preserved.' | Out-Null
  Assert-Equal (Get-Content -LiteralPath (Join-Path $successRoot 'app\bridge\main.py') -Raw -Encoding UTF8) $rollbackOld 'Failed replacement did not restore previous bridge.'
  Assert-Equal (Get-FileFingerprint $installedBridgeConfig) $legacyConfigFingerprint 'Failed replacement changed user config bytes.'
  Assert-NoTransactionResidue -InstallRoot $successRoot

  $partialOldBridge = 'partial rollback bridge sentinel'
  $partialOldScript = 'partial rollback script sentinel'
  [System.IO.File]::WriteAllText((Join-Path $successRoot 'app\bridge\main.py'), $partialOldBridge, (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText((Join-Path $successRoot 'scripts\Install.ps1'), $partialOldScript, (New-Object System.Text.UTF8Encoding($false)))
  $partialRollback = New-TestBoundaries -Discoveries @($weFlowExe)
  $failAfterFirstCommit = { param($phase) if ($phase -ceq 'AfterCommit:app\bridge') { throw 'E_FIXTURE_PARTIAL_COMMIT' } }
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $partialRollback -SkipStart -ReplacementHook $failAfterFirstCommit } 'E_FIXTURE_PARTIAL_COMMIT*' 'Partial commit failure was not preserved.' | Out-Null
  Assert-Equal (Get-Content -LiteralPath (Join-Path $successRoot 'app\bridge\main.py') -Raw -Encoding UTF8) $partialOldBridge 'Partial commit did not restore bridge.'
  Assert-Equal (Get-Content -LiteralPath (Join-Path $successRoot 'scripts\Install.ps1') -Raw -Encoding UTF8) $partialOldScript 'Partial commit did not restore scripts.'
  Assert-Equal (Get-FileFingerprint $installedBridgeConfig) $legacyConfigFingerprint 'Partial commit rollback changed user config bytes.'
  Assert-NoTransactionResidue -InstallRoot $successRoot

  $outsidePrivateBridge = Join-Path $fixtureRoot 'outside private bridge data'
  New-Item -ItemType Directory -Force -Path $outsidePrivateBridge | Out-Null
  Set-Content -LiteralPath (Join-Path $outsidePrivateBridge 'private.txt') -Value 'private-outside-content' -Encoding ASCII
  $nestedLink = Join-Path $successRoot 'app\bridge\nested-private'
  New-Item -ItemType Junction -Path $nestedLink -Target $outsidePrivateBridge | Out-Null
  $backupsBeforeUnsafeRerun = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'data\backups') -Directory -Filter 'bridge-*').Count
  $unsafeRerun = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $successRoot -WeFlowConfigPath $weFlowConfig -Boundaries $unsafeRerun -SkipStart } 'E_INSTALL_PATH:*' 'Nested bridge junction was accepted.' | Out-Null
  $backupsAfterUnsafeRerun = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'data\backups') -Directory -Filter 'bridge-*').Count
  Assert-Equal $backupsAfterUnsafeRerun $backupsBeforeUnsafeRerun 'Unsafe bridge tree created a backup before rejection.'
  $privateCopies = @(Get-ChildItem -LiteralPath (Join-Path $successRoot 'data\backups') -Recurse -File -Filter 'private.txt' -ErrorAction SilentlyContinue)
  Assert-Equal $privateCopies.Count 0 'Unsafe bridge backup copied external private data.'
  [System.IO.Directory]::Delete($nestedLink)

  $missingSource = New-TestSourceFixture -Path (Join-Path $fixtureRoot 'source missing one required file')
  Remove-Item -LiteralPath (Join-Path $missingSource 'bridge\main.py') -Force
  $missingSourceRoot = Join-Path $fixtureRoot 'source failure install root'
  $missingSourceBoundaries = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $missingSourceRoot -WeFlowConfigPath $weFlowConfig -Boundaries $missingSourceBoundaries -SourceRoot $missingSource -SkipStart } 'E_SOURCE_PAYLOAD:*' 'Missing source payload was accepted.' | Out-Null
  Assert-True (-not (Test-Path -LiteralPath $missingSourceRoot)) 'Missing source payload mutated InstallRoot.'
  Assert-Equal $missingSourceBoundaries.State.Calls.Count 0 'Missing source payload crossed an external/heavy boundary.'

  $unsafeSource = New-TestSourceFixture -Path (Join-Path $fixtureRoot 'source with junction')
  $outsideBridgeSource = Join-Path $fixtureRoot 'outside bridge source'
  Move-Item -LiteralPath (Join-Path $unsafeSource 'bridge') -Destination $outsideBridgeSource
  New-Item -ItemType Junction -Path (Join-Path $unsafeSource 'bridge') -Target $outsideBridgeSource | Out-Null
  $unsafeSourceRoot = Join-Path $fixtureRoot 'unsafe source install root'
  $unsafeSourceBoundaries = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $unsafeSourceRoot -WeFlowConfigPath $weFlowConfig -Boundaries $unsafeSourceBoundaries -SourceRoot $unsafeSource -SkipStart } 'E_SOURCE_PAYLOAD:*' 'Junctioned source payload was accepted.' | Out-Null
  Assert-True (-not (Test-Path -LiteralPath $unsafeSourceRoot)) 'Junctioned source payload mutated InstallRoot.'
  [System.IO.Directory]::Delete((Join-Path $unsafeSource 'bridge'))

  $busyRoot = Join-Path $fixtureRoot 'busy lock root'
  New-Item -ItemType Directory -Force -Path (Join-Path $busyRoot 'data\state') | Out-Null
  $busyStream = New-Object System.IO.FileStream((Join-Path $busyRoot 'data\state\lifecycle.lock'), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  try {
    $busy = New-TestBoundaries -Discoveries @($weFlowExe)
    Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $busyRoot -WeFlowConfigPath $weFlowConfig -Boundaries $busy -SkipStart } 'E_LIFECYCLE_BUSY:*' 'Busy lifecycle lock was ignored.' | Out-Null
    Assert-Equal $busy.State.Calls.Count 0 'Busy lock crossed an external/heavy boundary.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $busyRoot 'app'))) 'Busy lock copied payload.'
  } finally { $busyStream.Dispose() }

  $malformedRoot = Join-Path $fixtureRoot 'malformed process state'
  New-Item -ItemType Directory -Force -Path (Join-Path $malformedRoot 'data\state') | Out-Null
  Set-Content -LiteralPath (Join-Path $malformedRoot 'data\state\processes.json') -Value '{}' -Encoding ASCII
  $malformed = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $malformedRoot -WeFlowConfigPath $weFlowConfig -Boundaries $malformed -SkipStart } 'E_PROCESS_STATE:*' 'Malformed process state was accepted.' | Out-Null
  Assert-Equal $malformed.State.Calls.Count 0 'Malformed process state crossed an external/heavy boundary.'

  $runningRoot = Join-Path $fixtureRoot 'nonempty process state'
  New-Item -ItemType Directory -Force -Path (Join-Path $runningRoot 'data\state') | Out-Null
  $record = [ordered]@{ Name = 'weflow'; Pid = 1; ExecutablePath = $weFlowExe; StartTimeUtc = (Get-Date).ToUniversalTime().ToString('o'); Owned = $false; CommandKind = 'WeFlowApp' }
  [System.IO.File]::WriteAllText((Join-Path $runningRoot 'data\state\processes.json'), ('[' + ($record | ConvertTo-Json -Compress) + ']'), (New-Object System.Text.UTF8Encoding($false)))
  $running = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $runningRoot -WeFlowConfigPath $weFlowConfig -Boundaries $running -SkipStart } 'E_INSTALL_RUNNING:*' 'Non-empty process state was accepted.' | Out-Null
  Assert-Equal $running.State.Calls.Count 0 'Non-empty process state crossed an external/heavy boundary.'

  $outsideState = Join-Path $fixtureRoot 'outside state target'
  New-Item -ItemType Directory -Force -Path $outsideState | Out-Null
  Set-Content -LiteralPath (Join-Path $outsideState 'sentinel.txt') -Value 'outside-state' -Encoding ASCII
  $stateJunctionRoot = Join-Path $fixtureRoot 'state junction root'
  New-Item -ItemType Directory -Force -Path (Join-Path $stateJunctionRoot 'data') | Out-Null
  New-Item -ItemType Junction -Path (Join-Path $stateJunctionRoot 'data\state') -Target $outsideState | Out-Null
  $stateJunction = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $stateJunctionRoot -WeFlowConfigPath $weFlowConfig -Boundaries $stateJunction -SkipStart } 'E_LIFECYCLE_PATH:*' 'Junctioned state directory was accepted.' | Out-Null
  Assert-SequenceEqual (Get-RelativeFiles -Base $outsideState) @('sentinel.txt') 'State junction target was mutated.'
  [System.IO.Directory]::Delete((Join-Path $stateJunctionRoot 'data\state'))

  $outsideApp = Join-Path $fixtureRoot 'outside app target'
  New-Item -ItemType Directory -Force -Path $outsideApp | Out-Null
  Set-Content -LiteralPath (Join-Path $outsideApp 'sentinel.txt') -Value 'outside' -Encoding ASCII
  $internalRoot = Join-Path $fixtureRoot 'internal junction root'
  New-Item -ItemType Directory -Force -Path $internalRoot | Out-Null
  New-Item -ItemType Junction -Path (Join-Path $internalRoot 'app') -Target $outsideApp | Out-Null
  $internal = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $internalRoot -WeFlowConfigPath $weFlowConfig -Boundaries $internal -SkipStart } 'E_LIFECYCLE_PATH:*' 'Internal junction was accepted.' | Out-Null
  Assert-SequenceEqual (Get-RelativeFiles -Base $outsideApp) @('sentinel.txt') 'Internal junction target was mutated.'
  [System.IO.Directory]::Delete((Join-Path $internalRoot 'app'))

  $ancestorTargetParent = Join-Path $fixtureRoot 'ancestor real parent'
  $ancestorAliasParent = Join-Path $fixtureRoot 'ancestor alias parent'
  New-Item -ItemType Directory -Force -Path $ancestorTargetParent | Out-Null
  New-Item -ItemType Junction -Path $ancestorAliasParent -Target $ancestorTargetParent | Out-Null
  $ancestorRoot = Join-Path $ancestorAliasParent 'install child'
  $ancestor = New-TestBoundaries -Discoveries @($weFlowExe)
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $ancestorRoot -WeFlowConfigPath $weFlowConfig -Boundaries $ancestor -SkipStart } 'E_LIFECYCLE_PATH:*' 'Junctioned install-root ancestor was accepted.' | Out-Null
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $ancestorTargetParent 'install child'))) 'Junctioned ancestor target was mutated.'
  [System.IO.Directory]::Delete($ancestorAliasParent)

  $shortcutRoot = Join-Path $fixtureRoot 'shortcut transaction desktop'
  New-Item -ItemType Directory -Force -Path $shortcutRoot | Out-Null
  $shortcutFinals = @(1..3 | ForEach-Object { Join-Path $shortcutRoot ("shortcut-$_.lnk") })
  for ($shortcutIndex = 0; $shortcutIndex -lt 3; $shortcutIndex++) {
    Set-Content -LiteralPath $shortcutFinals[$shortcutIndex] -Value ('old-shortcut-' + ($shortcutIndex + 1)) -Encoding ASCII
  }
  $shortcutEntries = @(
    [pscustomobject]@{ Path = $shortcutFinals[0]; Target = Join-Path $successRoot $launchers.Start; WorkingDirectory = $successRoot },
    [pscustomobject]@{ Path = $shortcutFinals[1]; Target = Join-Path $successRoot $launchers.Stop; WorkingDirectory = $successRoot },
    [pscustomobject]@{ Path = $shortcutFinals[2]; Target = Join-Path $successRoot $launchers.Health; WorkingDirectory = $successRoot }
  )
  $shortcutWriteCount = 0
  $failingShortcutWriter = {
    param($path, $target, $workingDirectory)
    $script:shortcutWriteCount++
    if ($script:shortcutWriteCount -eq 2) { throw 'fixture shortcut writer failure' }
    [System.IO.File]::WriteAllText($path, ('new:' + $target), (New-Object System.Text.UTF8Encoding($false)))
  }
  Assert-ThrowsLike { New-AkashaShortcuts -Entries $shortcutEntries -Writer $failingShortcutWriter } 'E_SHORTCUT_CREATE:*' 'Mid-shortcut failure was not normalized.' | Out-Null
  for ($shortcutIndex = 0; $shortcutIndex -lt 3; $shortcutIndex++) {
    Assert-Equal (Get-Content -LiteralPath $shortcutFinals[$shortcutIndex] -Raw -Encoding UTF8).Trim() ('old-shortcut-' + ($shortcutIndex + 1)) 'Shortcut failure did not preserve all three previous shortcuts.'
  }
  Assert-Equal @(Get-ChildItem -LiteralPath $shortcutRoot -Force | Where-Object { $_.Name -like '.akasha-shortcut-*' }).Count 0 'Shortcut failure left transaction residue.'

  $shortcutFailureRoot = Join-Path $fixtureRoot 'shortcut integration failure'
  $shortcutFailure = New-TestBoundaries -Discoveries @($weFlowExe)
  $shortcutFailure.Shortcuts = { param($entries) throw 'E_SHORTCUT_CREATE: fixture COM failure' }
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $shortcutFailureRoot -WeFlowConfigPath $weFlowConfig -Boundaries $shortcutFailure } 'E_SHORTCUT_CREATE:*' 'Shortcut integration failure was not preserved.' | Out-Null
  $shortcutFailureState = Get-Content -LiteralPath (Join-Path $shortcutFailureRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal $shortcutFailureState.status 'failed' 'Shortcut failure left a false installed state.'
  Assert-Equal $shortcutFailureState.error_code 'E_SHORTCUT_CREATE' 'Shortcut failure state has the wrong fixed code.'
  Assert-True ($shortcutFailure.State.Calls -notcontains 'start' -and $shortcutFailure.State.Calls -notcontains 'health') 'Shortcut failure crossed start/health boundaries.'

  $uncalibratedRoot = Join-Path $fixtureRoot 'uncalibrated default install'
  $uncalibrated = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'required'
  $uncalibratedResult = Invoke-TestInstall -InstallRoot $uncalibratedRoot -WeFlowConfigPath $weFlowConfig -Boundaries $uncalibrated
  Assert-Equal $uncalibratedResult.Status 'installed' 'Uncalibrated install did not succeed.'
  Assert-Equal $uncalibratedResult.Started $false 'Uncalibrated install unexpectedly started services.'
  Assert-Equal $uncalibratedResult.CalibrationRequired $true 'Uncalibrated install omitted the calibration flag.'
  Assert-True ($uncalibrated.State.Calls -cnotcontains 'start') 'Uncalibrated install called start.'
  Assert-True ($uncalibrated.State.Calls -cnotcontains 'health') 'Uncalibrated install called health.'
  $uncalibratedState = Get-Content -LiteralPath (Join-Path $uncalibratedRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-SequenceEqual @($uncalibratedState.PSObject.Properties.Name | Sort-Object) @('status', 'updated_at', 'version') 'Uncalibrated install wrote failure metadata.'
  Assert-Equal $uncalibratedState.status 'installed' 'Uncalibrated install state is not installed.'
  $uncalibratedLog = Get-Content -LiteralPath (Join-Path $uncalibratedRoot 'data\logs\install.log') -Raw -Encoding UTF8
  Assert-True ($uncalibratedLog -match 'calibration_required=true') 'Uncalibrated install log omitted the boolean calibration state.'
  Assert-True (-not $uncalibratedLog.Contains('data\bridge\config.json')) 'Uncalibrated install log leaked the calibration config path.'

  $invalidCalibrationRoot = Join-Path $fixtureRoot 'invalid calibration install'
  $invalidCalibration = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'invalid'
  $invalidCalibrationResult = Invoke-TestInstall -InstallRoot $invalidCalibrationRoot -WeFlowConfigPath $weFlowConfig -Boundaries $invalidCalibration
  Assert-Equal $invalidCalibrationResult.Status 'installed' 'Invalid calibration install did not succeed.'
  Assert-Equal $invalidCalibrationResult.Started $false 'Invalid calibration install unexpectedly started services.'
  Assert-Equal $invalidCalibrationResult.CalibrationRequired $true 'Invalid calibration install omitted the calibration flag.'
  Assert-True ($invalidCalibration.State.Calls -cnotcontains 'start' -and $invalidCalibration.State.Calls -cnotcontains 'health') 'Invalid calibration install crossed start/health boundaries.'

  $defaultRoot = Join-Path $fixtureRoot 'ready default start success'
  $default = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus 'ready'
  $defaultResult = Invoke-TestInstall -InstallRoot $defaultRoot -WeFlowConfigPath $weFlowConfig -Boundaries $default
  Assert-True $defaultResult.Started 'Ready default install did not report service start.'
  Assert-Equal $defaultResult.CalibrationRequired $false 'Ready default install reported calibration required.'
  Assert-SequenceEqual @($default.State.Calls | Select-Object -Last 3) @('calibration', 'start', 'health') 'Ready default calibration/start/health order changed.'
  Assert-True (-not $default.State.LockSeenByStarter) 'Service start ran before lifecycle lock release.'
  $readyLog = Get-Content -LiteralPath (Join-Path $defaultRoot 'data\logs\install.log') -Raw -Encoding UTF8
  Assert-True ($readyLog -match 'calibration_required=false') 'Ready install log omitted the boolean calibration state.'

  $transientReadyRoot = Join-Path $fixtureRoot 'transient readiness success'
  $transientReady = New-TestBoundaries -Discoveries @($weFlowExe) -HealthExitCodes @(1, 0) -CalibrationStatus 'ready'
  $transientReadyResult = Invoke-TestInstall -InstallRoot $transientReadyRoot -WeFlowConfigPath $weFlowConfig -Boundaries $transientReady
  Assert-True $transientReadyResult.Started 'Transient readiness install did not report service start.'
  Assert-SequenceEqual @($transientReady.State.Calls | Select-Object -Last 5) @('calibration', 'start', 'health', 'health-delay:7', 'health') 'Installer did not retry a transient aggregate health failure after service start.'
  $transientReadyLog = Get-Content -LiteralPath (Join-Path $transientReadyRoot 'data\logs\install.log') -Raw -Encoding UTF8
  Assert-True ($transientReadyLog -match 'phase=health status=completed attempts=2') 'Transient readiness success did not record the successful attempt count.'

  foreach ($unsupportedCase in @(
      [pscustomobject]@{ Name = 'unknown'; Value = 'unsupported' },
      [pscustomobject]@{ Name = 'wrong-case'; Value = 'READY' },
      [pscustomobject]@{ Name = 'non-string'; Value = 42 },
      [pscustomobject]@{ Name = 'multiple'; Value = [object[]]@('ready', 'required') },
      [pscustomobject]@{ Name = 'null'; Value = $null }
    )) {
    $unsupportedRoot = Join-Path $fixtureRoot ('unsupported calibration status ' + $unsupportedCase.Name)
    $unsupported = New-TestBoundaries -Discoveries @($weFlowExe) -CalibrationStatus $unsupportedCase.Value
    Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $unsupportedRoot -WeFlowConfigPath $weFlowConfig -Boundaries $unsupported } 'E_INSTALL_FAILED:*' "Unsupported calibration reader output was accepted: $($unsupportedCase.Name)." | Out-Null
    $unsupportedState = Get-Content -LiteralPath (Join-Path $unsupportedRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Equal $unsupportedState.status 'failed' "Unsupported calibration reader output left installed state: $($unsupportedCase.Name)."
    Assert-Equal $unsupportedState.error_code 'E_INSTALL_FAILED' "Unsupported calibration reader output used the wrong fixed error code: $($unsupportedCase.Name)."
    Assert-True ($unsupported.State.Calls -cnotcontains 'start' -and $unsupported.State.Calls -cnotcontains 'health') "Unsupported calibration reader output crossed start/health boundaries: $($unsupportedCase.Name)."
  }

  $healthFailRoot = Join-Path $fixtureRoot 'aggregate health failure'
  $healthFail = New-TestBoundaries -Discoveries @($weFlowExe) -HealthExitCode 1 -CalibrationStatus 'ready'
  Assert-ThrowsLike { Invoke-TestInstall -InstallRoot $healthFailRoot -WeFlowConfigPath $weFlowConfig -Boundaries $healthFail } 'E_HEALTH_FAILED:*' 'Aggregate health failure was accepted.' | Out-Null
  Assert-SequenceEqual @($healthFail.State.Calls | Select-Object -Last 5) @('health', 'health-delay:7', 'health', 'health-delay:7', 'health') 'Persistent health failure did not exhaust the bounded readiness attempts.'
  Assert-Equal (Get-Content -LiteralPath (Join-Path $healthFailRoot 'data\state\install.json') -Raw -Encoding UTF8 | ConvertFrom-Json).status 'installed' 'Health failure falsified completed installation state.'
  Assert-True ((Get-Content -LiteralPath (Join-Path $healthFailRoot 'data\logs\install.log') -Raw -Encoding UTF8) -match 'phase=health status=failed attempts=3 code=E_HEALTH_FAILED') 'Persistent readiness failure did not record its bounded timeout.'

  $directSourceMissing = Join-Path $fixtureRoot 'missing direct source'
  $directRoot = Join-Path $fixtureRoot 'direct install root with spaces'
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'powershell.exe'
  $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $root 'scripts\Install.ps1') + '" -SourceRoot "' + $directSourceMissing + '" -InstallRoot "' + $directRoot + '" -SkipStart'
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $directProcess = New-Object System.Diagnostics.Process
  $directProcess.StartInfo = $psi
  Assert-True $directProcess.Start() 'Direct installer process did not start.'
  $directOutput = $directProcess.StandardOutput.ReadToEnd() + $directProcess.StandardError.ReadToEnd()
  Assert-True $directProcess.WaitForExit(15000) 'Direct installer process timed out.'
  Assert-Equal $directProcess.ExitCode 1 'Direct failure did not map to stable exit code 1.'
  Assert-True ($directOutput -match 'E_SOURCE_PAYLOAD|E_LIFECYCLE_PATH') 'Direct failure did not emit a fixed error code.'
  Assert-True (-not (Test-Path -LiteralPath $directRoot)) 'Direct source failure mutated the install root.'

  Write-Host 'Installer layout tests: PASS' -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}
