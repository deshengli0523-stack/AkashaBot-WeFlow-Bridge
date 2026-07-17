$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
foreach ($name in @('Start-Services.ps1', 'Stop-Services.ps1', 'Test-Health.ps1')) {
  $path = Join-Path $root (Join-Path 'scripts' $name)
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "$name is missing."
  }
}

function Assert-True {
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) { throw $Message }
}

function Assert-Equal {
  param($Actual, $Expected, [string]$Message)
  if ([string]$Actual -cne [string]$Expected) {
    throw "$Message Expected=[$Expected] Actual=[$Actual]"
  }
}

function Assert-ThrowsLike {
  param([scriptblock]$Action, [string]$Expected, [string]$Message)
  $actual = '[[NO ERROR]]'
  try { & $Action } catch { $actual = $_.Exception.Message }
  if ($actual -notlike $Expected) {
    throw "$Message Expected=[$Expected] Actual=[$actual]"
  }
  return $actual
}

. (Join-Path $root 'scripts\Start-Services.ps1')
. (Join-Path $root 'scripts\Stop-Services.ps1')
. (Join-Path $root 'scripts\Test-Health.ps1')
Import-Module (Join-Path $root 'scripts\AkashaBot.Common.psm1') -Force

$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('akashabot-process-safety-' + [guid]::NewGuid().ToString('N'))
$fixtureBinary = Join-Path $fixtureRoot 'fixture-sleeper.exe'
$startedFixturePids = New-Object System.Collections.Generic.List[int]

function New-ProcessFixtureBinary {
  New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
  $source = @'
using System;
using System.IO;
using System.Text;
using System.Threading;

public static class FixtureSleeper {
    private static string Encode(string value) {
        return Convert.ToBase64String(Encoding.UTF8.GetBytes(value ?? String.Empty));
    }

    public static int Main(string[] args) {
        string recordRoot = Environment.GetEnvironmentVariable("FIXTURE_RECORD_DIR");
        if (!String.IsNullOrEmpty(recordRoot)) {
            Directory.CreateDirectory(recordRoot);
            string[] lines = new string[] {
                "EXE=" + Encode(System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName),
                "ARGS=" + Encode(String.Join("\u001f", args)),
                "CWD=" + Encode(Environment.CurrentDirectory),
                "CONFIG=" + Encode(Environment.GetEnvironmentVariable("AKASHABOT_CONFIG_PATH")),
                "LOG=" + Encode(Environment.GetEnvironmentVariable("AKASHABOT_LOG_DIR")),
                "STATE=" + Encode(Environment.GetEnvironmentVariable("AKASHABOT_STATE_DIR")),
                "PASS" + "WORD=" + Encode(Environment.GetEnvironmentVariable("ASTRBOT_DASHBOARD_INITIAL_PASSWORD")),
                "PYTHONHOME=" + Encode(Environment.GetEnvironmentVariable("PYTHONHOME")),
                "PYTHONPATH=" + Encode(Environment.GetEnvironmentVariable("PYTHONPATH")),
                "PYTHONUSERBASE=" + Encode(Environment.GetEnvironmentVariable("PYTHONUSERBASE")),
                "VIRTUALENV=" + Encode(Environment.GetEnvironmentVariable("VIRTUAL_ENV")),
                "NOUSERSITE=" + Encode(Environment.GetEnvironmentVariable("PYTHONNOUSERSITE"))
            };
            File.WriteAllLines(Path.Combine(recordRoot, System.Diagnostics.Process.GetCurrentProcess().Id + ".txt"), lines, new UTF8Encoding(false));
        }
        if (File.Exists(Path.Combine(Environment.CurrentDirectory, "exit.fixture"))) {
            return 23;
        }
        while (true) { Thread.Sleep(200); }
    }
}
'@
  Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $fixtureBinary -OutputType ConsoleApplication -ErrorAction Stop
  Assert-True (Test-Path -LiteralPath $fixtureBinary -PathType Leaf) 'Add-Type did not create the real process fixture.'
}

function New-InstallFixture {
  param([string]$Name)

  $installRoot = Join-Path $fixtureRoot $Name
  $paths = Get-AkashaBotPaths -Root $installRoot
  foreach ($directory in @(
      $paths.Bridge,
      (Split-Path -Parent $paths.BridgePython),
      (Split-Path -Parent $paths.AstrBotPython),
      $paths.BridgeData,
      $paths.AstrBotData,
      $paths.Logs,
      $paths.State,
      (Join-Path $installRoot 'external'),
      (Join-Path $installRoot 'fixture-records')
    )) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
  }
  Copy-Item -LiteralPath $fixtureBinary -Destination $paths.BridgePython -Force
  Copy-Item -LiteralPath $fixtureBinary -Destination $paths.AstrBotPython -Force
  $weFlow = Join-Path $installRoot 'external\WeFlow.exe'
  Copy-Item -LiteralPath $fixtureBinary -Destination $weFlow -Force
  Set-Content -LiteralPath (Join-Path $paths.Bridge 'main.py') -Value '# fixture' -Encoding ASCII
  Set-Content -LiteralPath $paths.BridgeConfig -Value '{}' -Encoding ASCII
  Set-Content -LiteralPath (Join-Path $paths.AstrBotData 'fixture.txt') -Value 'fixture' -Encoding ASCII
  Set-Content -LiteralPath $paths.WeFlowPathState -Value $weFlow -Encoding ASCII
  return [pscustomobject]@{
    Root = $installRoot
    Paths = $paths
    WeFlow = $weFlow
    RecordRoot = Join-Path $installRoot 'fixture-records'
  }
}

function Stop-TrackedFixtureProcesses {
  foreach ($pidValue in @($startedFixturePids)) {
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    $path = ''
    try { $path = [string]$process.Path } catch { $path = '' }
    if (-not [string]::IsNullOrWhiteSpace($path) -and
        [System.IO.Path]::GetFullPath($path).StartsWith([System.IO.Path]::GetFullPath($fixtureRoot), [System.StringComparison]::OrdinalIgnoreCase)) {
      Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
      Wait-Process -Id $pidValue -Timeout 5 -ErrorAction SilentlyContinue
    }
  }
}

function Register-ProcessStatePids {
  param([string]$Path)

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
  try {
    $parsed = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $records = @($parsed | ForEach-Object { $_ })
  } catch { return }
  foreach ($record in $records) {
    if ($null -ne $record -and $null -ne $record.Pid) {
      $startedFixturePids.Add([int]($record.Pid))
    }
  }
}

function Wait-FixtureRecords {
  param([string]$RecordRoot, [int]$Count)

  for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $files = @(Get-ChildItem -LiteralPath $RecordRoot -Filter '*.txt' -File -ErrorAction SilentlyContinue)
    if ($files.Count -ge $Count) { return $files }
    Start-Sleep -Milliseconds 100
  }
  return @(Get-ChildItem -LiteralPath $RecordRoot -Filter '*.txt' -File -ErrorAction SilentlyContinue)
}

function Read-FixtureRecord {
  param([Parameter(Mandatory)][string]$Path)

  $value = [ordered]@{}
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $separator = $line.IndexOf('=')
    if ($separator -lt 1) { continue }
    $name = $line.Substring(0, $separator)
    $encoded = $line.Substring($separator + 1)
    $value[$name] = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
  }
  $value['Pid'] = [int][System.IO.Path]::GetFileNameWithoutExtension($Path)
  return [pscustomobject]$value
}

function Get-StateRecords {
  param([Parameter(Mandatory)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @() }
  $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
  Assert-True ($raw.TrimStart().StartsWith('[')) 'processes.json is not a JSON array.'
  $parsed = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
  return @($parsed | ForEach-Object { $_ })
}

function Get-FixtureTreeSnapshot {
  param([Parameter(Mandatory)][string]$Root)

  if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return '[[MISSING]]' }
  $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
  $entries = foreach ($item in @(Get-ChildItem -LiteralPath $rootPath -Recurse -Force | Sort-Object FullName)) {
    $relative = $item.FullName.Substring($rootPath.Length).TrimStart('\')
    if ($item.PSIsContainer) {
      "D|$relative"
    } else {
      $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
      "F|$relative|$hash"
    }
  }
  return @($entries) -join "`n"
}

function Register-FixtureRecordPids {
  param([Parameter(Mandatory)][string]$RecordRoot)

  foreach ($file in @(Get-ChildItem -LiteralPath $RecordRoot -Filter '*.txt' -File -ErrorAction SilentlyContinue)) {
    $startedFixturePids.Add([int][System.IO.Path]::GetFileNameWithoutExtension($file.Name))
  }
}

function Assert-FixtureRecordProcessesDead {
  param([Parameter(Mandatory)][string]$RecordRoot, [string]$Message)

  foreach ($file in @(Get-ChildItem -LiteralPath $RecordRoot -Filter '*.txt' -File -ErrorAction SilentlyContinue)) {
    $processId = [int][System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    for ($attempt = 0; $attempt -lt 20 -and $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue); $attempt++) {
      Start-Sleep -Milliseconds 100
    }
    Assert-True ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "$Message PID=$processId"
  }
}

function New-TestProcessRecord {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
    [Parameter(Mandatory)][string]$ExecutablePath,
    [Parameter(Mandatory)][bool]$Owned,
    [Parameter(Mandatory)][string]$CommandKind,
    [datetime]$StartTimeUtc = [datetime]::MinValue
  )

  $Process.Refresh()
  if ($StartTimeUtc -eq [datetime]::MinValue) { $StartTimeUtc = $Process.StartTime.ToUniversalTime() }
  return [pscustomobject][ordered]@{
    Name = $Name
    Pid = [int]$Process.Id
    ExecutablePath = [System.IO.Path]::GetFullPath($ExecutablePath)
    StartTimeUtc = $StartTimeUtc.ToUniversalTime().ToString('o')
    Owned = $Owned
    CommandKind = $CommandKind
  }
}

function Write-TestProcessRecords {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][object[]]$Records)

  $jsonItems = @($Records | ForEach-Object { $_ | ConvertTo-Json -Depth 10 -Compress })
  [System.IO.File]::WriteAllText($Path, ('[' + ($jsonItems -join ',') + ']'), (New-Object System.Text.UTF8Encoding($false)))
}

try {
  New-ProcessFixtureBinary
  $preflight = New-InstallFixture -Name 'preflight'
  Remove-Item -LiteralPath (Join-Path $preflight.Paths.Bridge 'main.py') -Force
  $recordCountBefore = @(Get-ChildItem -LiteralPath $preflight.RecordRoot -File -ErrorAction SilentlyContinue).Count
  Assert-ThrowsLike {
    Start-AkashaServices -InstallRoot $preflight.Root
  } 'E_NOT_INSTALLED:*' 'Start did not fail closed when bridge main.py was missing.' | Out-Null
  $recordCountAfter = @(Get-ChildItem -LiteralPath $preflight.RecordRoot -File -ErrorAction SilentlyContinue).Count
  Assert-Equal $recordCountAfter $recordCountBefore 'Preflight failure launched a process.'

  $normal = New-InstallFixture -Name 'normal-start'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  $oldConfig = $env:AKASHABOT_CONFIG_PATH
  $oldLog = $env:AKASHABOT_LOG_DIR
  $oldState = $env:AKASHABOT_STATE_DIR
  $oldPassword = $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD
  $pythonEnvironmentNames = @('PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE', 'VIRTUAL_ENV', 'PYTHONNOUSERSITE')
  $oldPythonEnvironment = @{}
  foreach ($pythonName in $pythonEnvironmentNames) {
    $oldPythonEnvironment[$pythonName] = [pscustomobject]@{ Present = Test-Path ("Env:" + $pythonName); Value = [Environment]::GetEnvironmentVariable($pythonName) }
  }
  try {
    $env:FIXTURE_RECORD_DIR = $normal.RecordRoot
    $env:AKASHABOT_CONFIG_PATH = 'parent-config-sentinel'
    $env:AKASHABOT_LOG_DIR = 'parent-log-sentinel'
    $env:AKASHABOT_STATE_DIR = 'parent-state-sentinel'
    $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = 'parent-pw'
    $env:PYTHONHOME = 'parent-python-home'
    $env:PYTHONPATH = 'parent-python-path'
    $env:PYTHONUSERBASE = 'parent-python-user'
    $env:VIRTUAL_ENV = 'parent-virtual-env'
    $env:PYTHONNOUSERSITE = 'parent-no-user-site'

    Start-AkashaServices -InstallRoot $normal.Root | Out-Null
    Register-ProcessStatePids -Path $normal.Paths.ProcessState
    $records = @(Get-StateRecords -Path $normal.Paths.ProcessState)
    Assert-Equal $records.Count 3 'Normal start did not record exactly three services.'
    Assert-Equal (@($records.Name | Sort-Object) -join ',') 'astrbot,bridge,weflow' 'Normal start recorded wrong service names.'
    foreach ($record in $records) {
      Assert-True ([int]$record.Pid -gt 0) "Invalid PID for $($record.Name)."
      Assert-True (-not [string]::IsNullOrWhiteSpace([string]$record.ExecutablePath)) "Missing executable path for $($record.Name)."
      Assert-True (-not [string]::IsNullOrWhiteSpace([string]$record.StartTimeUtc)) "Missing start time for $($record.Name)."
      Assert-True (-not [string]::IsNullOrWhiteSpace([string]$record.CommandKind)) "Missing command identity for $($record.Name)."
      Assert-True ([bool]$record.Owned) "Fresh service $($record.Name) was not marked owned."
    }

    $fixtureFiles = @(Wait-FixtureRecords -RecordRoot $normal.RecordRoot -Count 3)
    Assert-Equal $fixtureFiles.Count 3 'Real child fixtures did not all record their arguments and environment.'
    $childRecords = @($fixtureFiles | ForEach-Object { Read-FixtureRecord -Path $_.FullName })
    $bridgeState = $records | Where-Object Name -ceq 'bridge' | Select-Object -First 1
    $astrState = $records | Where-Object Name -ceq 'astrbot' | Select-Object -First 1
    $weFlowState = $records | Where-Object Name -ceq 'weflow' | Select-Object -First 1
    $bridgeChild = $childRecords | Where-Object Pid -eq ([int]$bridgeState.Pid) | Select-Object -First 1
    $astrChild = $childRecords | Where-Object Pid -eq ([int]$astrState.Pid) | Select-Object -First 1
    $weFlowChild = $childRecords | Where-Object Pid -eq ([int]$weFlowState.Pid) | Select-Object -First 1
    Assert-Equal $bridgeChild.ARGS 'main.py' 'Bridge child command identity changed.'
    Assert-Equal $astrChild.ARGS (('-m', 'astrbot.cli.__main__', 'run') -join [char]0x1f) 'AstrBot child command identity changed.'
    Assert-Equal $bridgeChild.CONFIG $normal.Paths.BridgeConfig 'Bridge child did not receive its config path.'
    Assert-Equal $bridgeChild.LOG $normal.Paths.Logs 'Bridge child did not receive its log path.'
    Assert-Equal $bridgeChild.STATE $normal.Paths.State 'Bridge child did not receive its state path.'
    foreach ($nonBridge in @($astrChild, $weFlowChild)) {
      Assert-Equal $nonBridge.CONFIG '' 'A non-bridge child inherited AKASHABOT_CONFIG_PATH.'
      Assert-Equal $nonBridge.LOG '' 'A non-bridge child inherited AKASHABOT_LOG_DIR.'
      Assert-Equal $nonBridge.STATE '' 'A non-bridge child inherited AKASHABOT_STATE_DIR.'
    }
    foreach ($child in $childRecords) {
      Assert-Equal $child.PASSWORD '' 'A normal child inherited ASTRBOT_DASHBOARD_INITIAL_PASSWORD.'
      Assert-Equal $child.PYTHONHOME '' 'A normal child inherited PYTHONHOME.'
      Assert-Equal $child.PYTHONPATH '' 'A normal child inherited PYTHONPATH.'
      Assert-Equal $child.PYTHONUSERBASE '' 'A normal child inherited PYTHONUSERBASE.'
      Assert-Equal $child.VIRTUALENV '' 'A normal child inherited VIRTUAL_ENV.'
    }
    Assert-Equal $weFlowChild.NOUSERSITE '' 'WeFlow received a Python-only environment override.'
    Assert-Equal $astrChild.NOUSERSITE '1' 'AstrBot did not disable user site packages.'
    Assert-Equal $bridgeChild.NOUSERSITE '1' 'Bridge did not disable user site packages.'
    Assert-Equal $env:AKASHABOT_CONFIG_PATH 'parent-config-sentinel' 'Parent config environment was not restored.'
    Assert-Equal $env:AKASHABOT_LOG_DIR 'parent-log-sentinel' 'Parent log environment was not restored.'
    Assert-Equal $env:AKASHABOT_STATE_DIR 'parent-state-sentinel' 'Parent state environment was not restored.'
    Assert-Equal $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD 'parent-pw' 'Parent AstrBot password environment was not restored.'
    Assert-Equal $env:PYTHONHOME 'parent-python-home' 'Parent PYTHONHOME was not preserved.'
    Assert-Equal $env:PYTHONPATH 'parent-python-path' 'Parent PYTHONPATH was not preserved.'
    Assert-Equal $env:PYTHONUSERBASE 'parent-python-user' 'Parent PYTHONUSERBASE was not preserved.'
    Assert-Equal $env:VIRTUAL_ENV 'parent-virtual-env' 'Parent VIRTUAL_ENV was not preserved.'
    Assert-Equal $env:PYTHONNOUSERSITE 'parent-no-user-site' 'Parent PYTHONNOUSERSITE was not preserved.'

    $firstPids = @($records.Pid | ForEach-Object { [int]$_ } | Sort-Object)
    foreach ($processId in $firstPids) {
      Assert-True ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Real fixture PID $processId exited before repeated-start validation."
      Assert-True ($null -ne (Get-AkashaProcessIdentity -ProcessId $processId)) "CIM identity lookup failed for real fixture PID $processId."
    }
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $normal.Root
    } 'E_ALREADY_RUNNING:*' 'Repeated start did not refuse the live owned state.' | Out-Null
    $secondPids = @((Get-StateRecords -Path $normal.Paths.ProcessState).Pid | ForEach-Object { [int]$_ } | Sort-Object)
    Assert-Equal ($secondPids -join ',') ($firstPids -join ',') 'Repeated start overwrote the original process state.'
    $repeatFixtureCount = @(Wait-FixtureRecords -RecordRoot $normal.RecordRoot -Count 4).Count
    Assert-Equal $repeatFixtureCount 3 'Repeated start launched an orphan process.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
    if ($null -eq $oldConfig) { Remove-Item Env:\AKASHABOT_CONFIG_PATH -ErrorAction SilentlyContinue } else { $env:AKASHABOT_CONFIG_PATH = $oldConfig }
    if ($null -eq $oldLog) { Remove-Item Env:\AKASHABOT_LOG_DIR -ErrorAction SilentlyContinue } else { $env:AKASHABOT_LOG_DIR = $oldLog }
    if ($null -eq $oldState) { Remove-Item Env:\AKASHABOT_STATE_DIR -ErrorAction SilentlyContinue } else { $env:AKASHABOT_STATE_DIR = $oldState }
    if ($null -eq $oldPassword) { Remove-Item Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ErrorAction SilentlyContinue } else { $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = $oldPassword }
    foreach ($pythonName in $pythonEnvironmentNames) {
      $snapshot = $oldPythonEnvironment[$pythonName]
      if ([bool]$snapshot.Present) { Set-Item -LiteralPath ("Env:" + $pythonName) -Value ([string]$snapshot.Value) } else { Remove-Item -LiteralPath ("Env:" + $pythonName) -ErrorAction SilentlyContinue }
    }
  }

  $external = New-InstallFixture -Name 'external-weflow'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $external.RecordRoot
    $externalProcess = Start-Process -FilePath $external.WeFlow -WorkingDirectory (Split-Path -Parent $external.WeFlow) -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$externalProcess.Id)
    Assert-Equal (@(Wait-FixtureRecords -RecordRoot $external.RecordRoot -Count 1).Count) 1 'External WeFlow fixture did not start.'
    Start-AkashaServices -InstallRoot $external.Root | Out-Null
    Register-ProcessStatePids -Path $external.Paths.ProcessState
    $externalState = @(Get-StateRecords -Path $external.Paths.ProcessState)
    Assert-Equal $externalState.Count 3 'External WeFlow reuse did not produce three service records.'
    $externalWeFlowRecord = $externalState | Where-Object Name -ceq 'weflow' | Select-Object -First 1
    Assert-Equal ([int]$externalWeFlowRecord.Pid) ([int]$externalProcess.Id) 'Start did not reuse the matching external WeFlow process.'
    Assert-True (-not [bool]$externalWeFlowRecord.Owned) 'External WeFlow was incorrectly marked owned.'
    Assert-True ($null -ne (Get-Process -Id $externalProcess.Id -ErrorAction SilentlyContinue)) 'External WeFlow exited during start.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $busy = New-InstallFixture -Name 'busy-lock'
  $busyLockPath = Join-Path $busy.Paths.State 'lifecycle.lock'
  $busyLock = [System.IO.File]::Open($busyLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  try {
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $busy.Root
    } 'E_LIFECYCLE_BUSY:*' 'A contended real lifecycle lock used the wrong error.' | Out-Null
    Assert-Equal (@(Get-ChildItem -LiteralPath $busy.RecordRoot -Filter '*.txt' -File).Count) 0 'Busy lifecycle lock launched a process.'
  } finally {
    $busyLock.Dispose()
  }

  $earlyExit = New-InstallFixture -Name 'early-exit'
  Set-Content -LiteralPath (Join-Path $earlyExit.Paths.Bridge 'exit.fixture') -Value 'exit' -Encoding ASCII
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $earlyExit.RecordRoot
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $earlyExit.Root
    } 'E_SERVICE_EXITED:*bridge*' 'Immediate bridge exit used the wrong primary error.' | Out-Null
    Wait-FixtureRecords -RecordRoot $earlyExit.RecordRoot -Count 3 | Out-Null
    Register-FixtureRecordPids -RecordRoot $earlyExit.RecordRoot
    Assert-FixtureRecordProcessesDead -RecordRoot $earlyExit.RecordRoot -Message 'Partial-start rollback left a real process running.'
    Assert-Equal (@(Get-StateRecords -Path $earlyExit.Paths.ProcessState).Count) 0 'Partial-start rollback left success state.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $stateFailure = New-InstallFixture -Name 'state-write-failure'
  Set-Content -LiteralPath $stateFailure.Paths.ProcessState -Value '[]' -Encoding ASCII
  $stateLock = [System.IO.File]::Open($stateFailure.Paths.ProcessState, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $stateFailure.RecordRoot
    $stateError = $null
    try { Start-AkashaServices -InstallRoot $stateFailure.Root | Out-Null } catch { $stateError = $_ }
    Assert-True ($null -ne $stateError) 'Locked processes.json did not fail start.'
    Assert-Equal $stateError.Exception.Message 'E_PROCESS_STATE_WRITE: Unable to persist process state.' 'State write failure lost primary error priority.'
    Assert-Equal $stateError.Exception.Data['AkashaCleanupFailure'] 'E_LIFECYCLE_CLEANUP' 'State repair failure did not record its fixed cleanup secondary.'
    Wait-FixtureRecords -RecordRoot $stateFailure.RecordRoot -Count 1 | Out-Null
    Register-FixtureRecordPids -RecordRoot $stateFailure.RecordRoot
    Assert-FixtureRecordProcessesDead -RecordRoot $stateFailure.RecordRoot -Message 'State write failure left its real process running.'
  } finally {
    $stateLock.Dispose()
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }
  Assert-Equal (Get-Content -LiteralPath $stateFailure.Paths.ProcessState -Raw -Encoding UTF8).Trim() '[]' 'State write failure corrupted the previous state.'

  $cleanupSurvivor = New-InstallFixture -Name 'cleanup-survivor'
  Set-Content -LiteralPath (Join-Path $cleanupSurvivor.Paths.Bridge 'exit.fixture') -Value 'exit' -Encoding ASCII
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $cleanupSurvivor.RecordRoot
    $cleanupError = $null
    $refusingTerminator = { param($Process) throw 'fixture termination refusal' }
    $realWaiter = { param($Process, $TimeoutMilliseconds) $Process.WaitForExit($TimeoutMilliseconds) }
    try {
      Start-AkashaServices -InstallRoot $cleanupSurvivor.Root -ProcessTerminator $refusingTerminator -ProcessWaiter $realWaiter | Out-Null
    } catch {
      $cleanupError = $_
    }
    Assert-True ($null -ne $cleanupError) 'Injected cleanup termination refusal did not fail start.'
    Assert-True ($cleanupError.Exception.Message -like 'E_SERVICE_EXITED:*bridge*') 'Cleanup termination refusal replaced the primary start error.'
    Assert-Equal $cleanupError.Exception.Data['AkashaCleanupFailure'] 'E_LIFECYCLE_CLEANUP' 'Cleanup termination refusal did not attach the fixed secondary code.'
    Assert-Equal (@(Wait-FixtureRecords -RecordRoot $cleanupSurvivor.RecordRoot -Count 3).Count) 3 'Cleanup survivor fixture did not launch three real processes.'
    Register-FixtureRecordPids -RecordRoot $cleanupSurvivor.RecordRoot
    $survivorRecords = @(Get-StateRecords -Path $cleanupSurvivor.Paths.ProcessState)
    Assert-Equal $survivorRecords.Count 2 'Cleanup termination refusal did not retain exactly the two live records.'
    Assert-Equal ((@($survivorRecords.Name | Sort-Object)) -join ',') 'astrbot,weflow' 'Cleanup termination refusal retained the wrong services.'
    foreach ($record in $survivorRecords) {
      Assert-Equal ((@($record.PSObject.Properties.Name) | Sort-Object) -join ',') 'CommandKind,ExecutablePath,Name,Owned,Pid,StartTimeUtc' 'Cleanup survivor state did not retain the full strict record.'
      Assert-True ([bool]$record.Owned) 'Cleanup survivor record was not marked owned.'
      $survivorProcess = Get-Process -Id ([int]$record.Pid) -ErrorAction SilentlyContinue
      Assert-True ($null -ne $survivorProcess) "Cleanup survivor process was not actually alive. PID=$($record.Pid)"
      Assert-True ([System.IO.Path]::GetFullPath([string]$survivorProcess.Path).StartsWith([System.IO.Path]::GetFullPath($cleanupSurvivor.Root), [System.StringComparison]::OrdinalIgnoreCase)) 'Cleanup survivor PID did not belong to the real fixture.'
    }
  } finally {
    Register-FixtureRecordPids -RecordRoot $cleanupSurvivor.RecordRoot
    foreach ($recordFile in @(Get-ChildItem -LiteralPath $cleanupSurvivor.RecordRoot -Filter '*.txt' -File -ErrorAction SilentlyContinue)) {
      $survivorPid = [int][System.IO.Path]::GetFileNameWithoutExtension($recordFile.Name)
      $survivorProcess = Get-Process -Id $survivorPid -ErrorAction SilentlyContinue
      if ($null -eq $survivorProcess) { continue }
      $survivorPath = ''
      try { $survivorPath = [System.IO.Path]::GetFullPath([string]$survivorProcess.Path) } catch { $survivorPath = '' }
      if (-not [string]::IsNullOrWhiteSpace($survivorPath) -and
          $survivorPath.StartsWith([System.IO.Path]::GetFullPath($cleanupSurvivor.Root), [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Process -Id $survivorPid -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $survivorPid -Timeout 5 -ErrorAction SilentlyContinue
      }
    }
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }
  Assert-FixtureRecordProcessesDead -RecordRoot $cleanupSurvivor.RecordRoot -Message 'Cleanup survivor test did not clean its exact real fixture PIDs.'

  $logFailure = New-InstallFixture -Name 'log-write-failure'
  $launcherLog = Join-Path $logFailure.Paths.Logs 'launcher.log'
  Set-Content -LiteralPath $launcherLog -Value 'existing metadata' -Encoding ASCII
  $logLock = [System.IO.File]::Open($launcherLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $logFailure.RecordRoot
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $logFailure.Root
    } 'E_LIFECYCLE_LOG:*' 'Launcher log write failure used the wrong primary error.' | Out-Null
    Assert-Equal (@(Get-ChildItem -LiteralPath $logFailure.RecordRoot -Filter '*.txt' -File).Count) 0 'Preflight log failure launched a real process.'
    Assert-True (-not (Test-Path -LiteralPath $logFailure.Paths.ProcessState)) 'Preflight log failure created process state.'
  } finally {
    $logLock.Dispose()
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $normalStop = New-InstallFixture -Name 'normal-stop'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $normalStop.RecordRoot
    Start-AkashaServices -InstallRoot $normalStop.Root | Out-Null
    Register-ProcessStatePids -Path $normalStop.Paths.ProcessState
    $normalStopPids = @((Get-StateRecords -Path $normalStop.Paths.ProcessState).Pid | ForEach-Object { [int]$_ })
    Stop-AkashaServices -InstallRoot $normalStop.Root
    foreach ($processId in $normalStopPids) {
      Assert-True ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Normal stop left PID $processId running."
    }
    Assert-True (-not (Test-Path -LiteralPath $normalStop.Paths.ProcessState)) 'Normal stop left processes.json behind.'
    $launcherText = Get-Content -LiteralPath (Join-Path $normalStop.Paths.Logs 'launcher.log') -Raw -Encoding UTF8
    $bridgeStopIndex = $launcherText.IndexOf('stop name=bridge')
    $astrStopIndex = $launcherText.IndexOf('stop name=astrbot')
    $weFlowStopIndex = $launcherText.IndexOf('stop name=weflow')
    Assert-True ($bridgeStopIndex -ge 0 -and $astrStopIndex -gt $bridgeStopIndex -and $weFlowStopIndex -gt $astrStopIndex) 'Stop order was not bridge, astrbot, weflow.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $degradedStop = New-InstallFixture -Name 'degraded-stop'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  $degradedWeFlowRecord = $null
  try {
    $env:FIXTURE_RECORD_DIR = $degradedStop.RecordRoot
    Start-AkashaServices -InstallRoot $degradedStop.Root | Out-Null
    Register-ProcessStatePids -Path $degradedStop.Paths.ProcessState
    $degradedInitialRecords = @(Get-StateRecords -Path $degradedStop.Paths.ProcessState)
    $degradedInternalPids = @($degradedInitialRecords | Where-Object Name -in @('bridge', 'astrbot') | ForEach-Object { [int]$_.Pid })
    $degradedWeFlowRecord = $degradedInitialRecords | Where-Object Name -ceq 'weflow' | Select-Object -First 1
    Remove-Item -LiteralPath $degradedStop.Paths.BridgeConfig -Force
    Remove-Item -LiteralPath (Join-Path $degradedStop.Paths.Bridge 'main.py') -Force
    Remove-Item -LiteralPath $degradedStop.Paths.WeFlowPathState -Force
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $degradedStop.Root
    } 'E_WEFLOW_EXE:*' 'Degraded stop did not return the fixed fail-closed WeFlow error.' | Out-Null
    foreach ($processId in $degradedInternalPids) {
      Assert-True ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Degraded stop left internal PID $processId running."
    }
    Assert-True ($null -ne (Get-Process -Id ([int]$degradedWeFlowRecord.Pid) -ErrorAction SilentlyContinue)) 'Degraded stop terminated owned WeFlow without its discovery path.'
    $degradedRemaining = @(Get-StateRecords -Path $degradedStop.Paths.ProcessState)
    Assert-Equal $degradedRemaining.Count 1 'Degraded stop did not retain exactly the owned WeFlow record.'
    Assert-Equal $degradedRemaining[0].Name 'weflow' 'Degraded stop retained the wrong service record.'
    Assert-Equal ((@($degradedRemaining[0].PSObject.Properties.Name) | Sort-Object) -join ',') 'CommandKind,ExecutablePath,Name,Owned,Pid,StartTimeUtc' 'Degraded stop did not retain the complete strict WeFlow record.'
  } finally {
    if ($null -ne $degradedWeFlowRecord) {
      $degradedWeFlowProcess = Get-Process -Id ([int]$degradedWeFlowRecord.Pid) -ErrorAction SilentlyContinue
      if ($null -ne $degradedWeFlowProcess) {
        $degradedWeFlowPath = ''
        try { $degradedWeFlowPath = [System.IO.Path]::GetFullPath([string]$degradedWeFlowProcess.Path) } catch { $degradedWeFlowPath = '' }
        if (-not [string]::IsNullOrWhiteSpace($degradedWeFlowPath) -and
            $degradedWeFlowPath.StartsWith([System.IO.Path]::GetFullPath($degradedStop.Root), [System.StringComparison]::OrdinalIgnoreCase)) {
          Stop-Process -Id ([int]$degradedWeFlowRecord.Pid) -Force -ErrorAction SilentlyContinue
          Wait-Process -Id ([int]$degradedWeFlowRecord.Pid) -Timeout 5 -ErrorAction SilentlyContinue
        }
      }
    }
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $externalOwnedPids = @((Get-StateRecords -Path $external.Paths.ProcessState) | Where-Object Owned | ForEach-Object { [int]$_.Pid })
  Stop-AkashaServices -InstallRoot $external.Root
  foreach ($processId in $externalOwnedPids) {
    Assert-True ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Stop left external fixture owned PID $processId running."
  }
  Assert-True ($null -ne (Get-Process -Id $externalProcess.Id -ErrorAction SilentlyContinue)) 'Stop terminated unowned external WeFlow.'
  Assert-True (-not (Test-Path -LiteralPath $external.Paths.ProcessState)) 'Stop retained an already reported unowned WeFlow record.'

  $commandMismatch = New-InstallFixture -Name 'stop-command-mismatch'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $commandMismatch.RecordRoot
    $wrongCommandProcess = Start-Process -FilePath $commandMismatch.Paths.BridgePython -ArgumentList @('wrong.py') -WorkingDirectory $commandMismatch.Paths.Bridge -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$wrongCommandProcess.Id)
    Wait-FixtureRecords -RecordRoot $commandMismatch.RecordRoot -Count 1 | Out-Null
    $wrongCommandRecord = New-TestProcessRecord -Name 'bridge' -Process $wrongCommandProcess -ExecutablePath $commandMismatch.Paths.BridgePython -Owned $true -CommandKind 'BridgeMain'
    Write-TestProcessRecords -Path $commandMismatch.Paths.ProcessState -Records @($wrongCommandRecord)
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $commandMismatch.Root
    } 'E_PROCESS_IDENTITY:*' 'Stop accepted a live bridge process with the wrong command identity.' | Out-Null
    Assert-True ($null -ne (Get-Process -Id $wrongCommandProcess.Id -ErrorAction SilentlyContinue)) 'Stop terminated the wrong-command bridge fixture.'
    Assert-Equal (@(Get-StateRecords -Path $commandMismatch.Paths.ProcessState).Count) 1 'Stop deleted the refused command-mismatch record.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $pathMismatch = New-InstallFixture -Name 'stop-path-mismatch'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $pathMismatch.RecordRoot
    $wrongPathProcess = Start-Process -FilePath $pathMismatch.Paths.AstrBotPython -ArgumentList @('main.py') -WorkingDirectory $pathMismatch.Paths.Bridge -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$wrongPathProcess.Id)
    Wait-FixtureRecords -RecordRoot $pathMismatch.RecordRoot -Count 1 | Out-Null
    $wrongPathRecord = New-TestProcessRecord -Name 'bridge' -Process $wrongPathProcess -ExecutablePath $pathMismatch.Paths.BridgePython -Owned $true -CommandKind 'BridgeMain'
    Write-TestProcessRecords -Path $pathMismatch.Paths.ProcessState -Records @($wrongPathRecord)
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $pathMismatch.Root
    } 'E_PROCESS_IDENTITY:*' 'Stop accepted a live process whose actual executable path differed.' | Out-Null
    Assert-True ($null -ne (Get-Process -Id $wrongPathProcess.Id -ErrorAction SilentlyContinue)) 'Stop terminated the wrong-path fixture.'
    Assert-Equal (@(Get-StateRecords -Path $pathMismatch.Paths.ProcessState).Count) 1 'Stop deleted the refused path-mismatch record.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $timeMismatch = New-InstallFixture -Name 'stop-time-mismatch'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $timeMismatch.RecordRoot
    $wrongTimeProcess = Start-Process -FilePath $timeMismatch.Paths.BridgePython -ArgumentList @('main.py') -WorkingDirectory $timeMismatch.Paths.Bridge -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$wrongTimeProcess.Id)
    Wait-FixtureRecords -RecordRoot $timeMismatch.RecordRoot -Count 1 | Out-Null
    $wrongTimeProcess.Refresh()
    $wrongTimeRecord = New-TestProcessRecord -Name 'bridge' -Process $wrongTimeProcess -ExecutablePath $timeMismatch.Paths.BridgePython -Owned $true -CommandKind 'BridgeMain' -StartTimeUtc ($wrongTimeProcess.StartTime.ToUniversalTime().AddMilliseconds(500))
    Write-TestProcessRecords -Path $timeMismatch.Paths.ProcessState -Records @($wrongTimeRecord)
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $timeMismatch.Root
    } 'E_PROCESS_IDENTITY:*' 'Stop accepted a live process with a reused-PID start time mismatch.' | Out-Null
    Assert-True ($null -ne (Get-Process -Id $wrongTimeProcess.Id -ErrorAction SilentlyContinue)) 'Stop terminated the wrong-start-time fixture.'
    Assert-Equal (@(Get-StateRecords -Path $timeMismatch.Paths.ProcessState).Count) 1 'Stop deleted the refused start-time record.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $invalidState = New-InstallFixture -Name 'invalid-process-state'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $invalidState.RecordRoot
    $sentinelProcess = Start-Process -FilePath $invalidState.Paths.BridgePython -ArgumentList @('main.py') -WorkingDirectory $invalidState.Paths.Bridge -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$sentinelProcess.Id)
    Wait-FixtureRecords -RecordRoot $invalidState.RecordRoot -Count 1 | Out-Null
    $validSentinelRecord = New-TestProcessRecord -Name 'bridge' -Process $sentinelProcess -ExecutablePath $invalidState.Paths.BridgePython -Owned $true -CommandKind 'BridgeMain'
    $validJson = $validSentinelRecord | ConvertTo-Json -Depth 10 -Compress
    $unknownRecord = [pscustomobject][ordered]@{
      Name = 'unknown'; Pid = [int]$sentinelProcess.Id; ExecutablePath = $invalidState.Paths.BridgePython
      StartTimeUtc = $validSentinelRecord.StartTimeUtc; Owned = $true; CommandKind = 'BridgeMain'
    }
    $zeroPidRecord = [pscustomobject][ordered]@{
      Name = 'bridge'; Pid = 0; ExecutablePath = $invalidState.Paths.BridgePython
      StartTimeUtc = $validSentinelRecord.StartTimeUtc; Owned = $true; CommandKind = 'BridgeMain'
    }
    $outsidePathRecord = [pscustomobject][ordered]@{
      Name = 'bridge'; Pid = [int]$sentinelProcess.Id; ExecutablePath = (Join-Path $fixtureRoot 'outside.exe')
      StartTimeUtc = $validSentinelRecord.StartTimeUtc; Owned = $true; CommandKind = 'BridgeMain'
    }
    $extraFieldRecord = [pscustomobject][ordered]@{
      Name = 'bridge'; Pid = [int]$sentinelProcess.Id; ExecutablePath = $invalidState.Paths.BridgePython
      StartTimeUtc = ([datetime]::Parse($validSentinelRecord.StartTimeUtc).AddDays(-1).ToUniversalTime().ToString('o'))
      Owned = $true; CommandKind = 'BridgeMain'; Unexpected = 'field'
    }
    $invalidCases = @(
      @{ Name = 'malformed'; Json = '{' },
      @{ Name = 'scalar'; Json = '42' },
      @{ Name = 'nested array'; Json = '[[]]' },
      @{ Name = 'unknown name'; Json = '[' + ($unknownRecord | ConvertTo-Json -Compress) + ']' },
      @{ Name = 'duplicate name'; Json = '[' + $validJson + ',' + $validJson + ']' },
      @{ Name = 'zero PID'; Json = '[' + ($zeroPidRecord | ConvertTo-Json -Compress) + ']' },
      @{ Name = 'unexpected path'; Json = '[' + ($outsidePathRecord | ConvertTo-Json -Compress) + ']' },
      @{ Name = 'unexpected field'; Json = '[' + ($extraFieldRecord | ConvertTo-Json -Compress) + ']' }
    )
    foreach ($case in $invalidCases) {
      [System.IO.File]::WriteAllText($invalidState.Paths.ProcessState, [string]$case.Json, (New-Object System.Text.UTF8Encoding($false)))
      Assert-ThrowsLike {
        Stop-AkashaServices -InstallRoot $invalidState.Root
      } 'E_PROCESS_STATE:*' "Invalid state case '$($case.Name)' used the wrong error." | Out-Null
      Assert-True ($null -ne (Get-Process -Id $sentinelProcess.Id -ErrorAction SilentlyContinue)) "Invalid state case '$($case.Name)' stopped a real process."
    }
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $stopStateFailure = New-InstallFixture -Name 'stop-state-write-failure'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $stopStateFailure.RecordRoot
    Start-AkashaServices -InstallRoot $stopStateFailure.Root | Out-Null
    Register-ProcessStatePids -Path $stopStateFailure.Paths.ProcessState
    $stopStatePids = @((Get-StateRecords -Path $stopStateFailure.Paths.ProcessState).Pid | ForEach-Object { [int]$_ })
    $stopStateLock = [System.IO.File]::Open($stopStateFailure.Paths.ProcessState, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
      Assert-ThrowsLike {
        Stop-AkashaServices -InstallRoot $stopStateFailure.Root
      } 'E_PROCESS_STATE_WRITE:*' 'Stop state preflight write failure used the wrong error.' | Out-Null
      foreach ($processId in $stopStatePids) {
        Assert-True ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Stop state preflight failure terminated PID $processId."
      }
    } finally {
      $stopStateLock.Dispose()
    }
    Stop-AkashaServices -InstallRoot $stopStateFailure.Root
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $stopLogFailure = New-InstallFixture -Name 'stop-log-write-failure'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $stopLogFailure.RecordRoot
    Start-AkashaServices -InstallRoot $stopLogFailure.Root | Out-Null
    Register-ProcessStatePids -Path $stopLogFailure.Paths.ProcessState
    $stopLogPids = @((Get-StateRecords -Path $stopLogFailure.Paths.ProcessState).Pid | ForEach-Object { [int]$_ })
    $stopLauncherLog = Join-Path $stopLogFailure.Paths.Logs 'launcher.log'
    $stopLogLock = [System.IO.File]::Open($stopLauncherLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
      Assert-ThrowsLike {
        Stop-AkashaServices -InstallRoot $stopLogFailure.Root
      } 'E_LIFECYCLE_LOG:*' 'Stop preflight log failure used the wrong error.' | Out-Null
      foreach ($processId in $stopLogPids) {
        Assert-True ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) "Stop log preflight failure terminated PID $processId."
      }
      Assert-Equal (@(Get-StateRecords -Path $stopLogFailure.Paths.ProcessState).Count) 3 'Stop log preflight failure changed process state.'
    } finally {
      $stopLogLock.Dispose()
    }
    Stop-AkashaServices -InstallRoot $stopLogFailure.Root
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $staleState = New-InstallFixture -Name 'stale-state'
  $staleRecord = [pscustomobject][ordered]@{
    Name = 'bridge'
    Pid = 2147483000
    ExecutablePath = $staleState.Paths.BridgePython
    StartTimeUtc = '2020-01-01T00:00:00.0000000Z'
    Owned = $true
    CommandKind = 'BridgeMain'
  }
  Write-TestProcessRecords -Path $staleState.Paths.ProcessState -Records @($staleRecord)
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $staleState.RecordRoot
    Start-AkashaServices -InstallRoot $staleState.Root | Out-Null
    Register-ProcessStatePids -Path $staleState.Paths.ProcessState
    $replacedStaleState = @(Get-StateRecords -Path $staleState.Paths.ProcessState)
    Assert-Equal $replacedStaleState.Count 3 'Stale state was not replaced by the three live service records.'
    Assert-True ($replacedStaleState.Pid -notcontains 2147483000) 'Stale PID remained in process state.'
    Stop-AkashaServices -InstallRoot $staleState.Root
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $reparse = New-InstallFixture -Name 'reparse-path'
  $reparseTarget = Join-Path $fixtureRoot 'reparse-target'
  New-Item -ItemType Directory -Force -Path $reparseTarget | Out-Null
  Set-Content -LiteralPath (Join-Path $reparseTarget 'outside-sentinel.txt') -Value 'outside' -Encoding ASCII
  Remove-Item -LiteralPath $reparse.Paths.State -Recurse -Force
  New-Item -ItemType Junction -Path $reparse.Paths.State -Target $reparseTarget | Out-Null
  try {
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $reparse.Root
    } 'E_LIFECYCLE_PATH:*' 'Start accepted a real junction in the internal state path.' | Out-Null
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $reparse.Root
    } 'E_LIFECYCLE_PATH:*' 'Stop accepted a real junction in the internal state path.' | Out-Null
    Assert-Equal (@(Get-ChildItem -LiteralPath $reparse.RecordRoot -Filter '*.txt' -File).Count) 0 'Reparse preflight launched a process.'
    Assert-Equal (Get-Content -LiteralPath (Join-Path $reparseTarget 'outside-sentinel.txt') -Raw -Encoding UTF8).Trim() 'outside' 'Reparse preflight changed the outside sentinel.'
  } finally {
    if (Test-Path -LiteralPath $reparse.Paths.State) {
      [System.IO.Directory]::Delete($reparse.Paths.State)
    }
  }

  $rootJunctionTarget = New-InstallFixture -Name 'root-junction-target'
  $rootJunctionAlias = Join-Path $fixtureRoot 'root-junction-alias'
  New-Item -ItemType Junction -Path $rootJunctionAlias -Target $rootJunctionTarget.Root | Out-Null
  try {
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $rootJunctionAlias
    } 'E_LIFECYCLE_PATH:*' 'Start accepted an install root that was itself a junction.' | Out-Null
    Assert-Equal (@(Get-ChildItem -LiteralPath $rootJunctionTarget.RecordRoot -Filter '*.txt' -File).Count) 0 'Junction install root launched a real process.'
  } finally {
    Register-FixtureRecordPids -RecordRoot $rootJunctionTarget.RecordRoot
    if (Test-Path -LiteralPath $rootJunctionAlias) { [System.IO.Directory]::Delete($rootJunctionAlias) }
  }

  $missingAncestorTargetParent = Join-Path $fixtureRoot 'missing-ancestor-target-parent'
  $missingAncestorAliasParent = Join-Path $fixtureRoot 'missing-ancestor-alias-parent'
  New-Item -ItemType Directory -Force -Path $missingAncestorTargetParent | Out-Null
  New-Item -ItemType Junction -Path $missingAncestorAliasParent -Target $missingAncestorTargetParent | Out-Null
  $missingAncestorAliasRoot = Join-Path $missingAncestorAliasParent 'install'
  try {
    $missingAncestorBefore = Get-FixtureTreeSnapshot -Root $missingAncestorTargetParent
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $missingAncestorAliasRoot
    } 'E_LIFECYCLE_PATH:*' 'Start did not reject a missing install root below a junctioned ancestor.' | Out-Null
    Assert-Equal (Get-FixtureTreeSnapshot -Root $missingAncestorTargetParent) $missingAncestorBefore 'Start created target content through a junctioned ancestor for a missing install root.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $missingAncestorTargetParent 'install'))) 'Start created the missing canonical install root through a junction.'
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $missingAncestorAliasRoot
    } 'E_LIFECYCLE_PATH:*' 'Stop did not reject a missing install root below a junctioned ancestor.' | Out-Null
    Assert-Equal (Get-FixtureTreeSnapshot -Root $missingAncestorTargetParent) $missingAncestorBefore 'Stop created target content through a junctioned ancestor for a missing install root.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $missingAncestorTargetParent 'install'))) 'Stop created the missing canonical install root through a junction.'
  } finally {
    if (Test-Path -LiteralPath $missingAncestorAliasParent) { [System.IO.Directory]::Delete($missingAncestorAliasParent) }
  }

  $ancestorStartTarget = New-InstallFixture -Name 'ancestor-start-target-parent\install'
  $ancestorStartAliasParent = Join-Path $fixtureRoot 'ancestor-start-alias-parent'
  New-Item -ItemType Junction -Path $ancestorStartAliasParent -Target (Split-Path -Parent $ancestorStartTarget.Root) | Out-Null
  $ancestorStartAliasRoot = Join-Path $ancestorStartAliasParent 'install'
  Remove-Item -LiteralPath $ancestorStartTarget.Paths.Logs -Recurse -Force
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $ancestorStartTarget.RecordRoot
    $ancestorStartBefore = Get-FixtureTreeSnapshot -Root $ancestorStartTarget.Root
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $ancestorStartAliasRoot
    } 'E_LIFECYCLE_PATH:*' 'Start accepted an install root below a junctioned ancestor.' | Out-Null
    $ancestorStartAfter = Get-FixtureTreeSnapshot -Root $ancestorStartTarget.Root
    Assert-Equal $ancestorStartAfter $ancestorStartBefore 'Start mutated the canonical target before rejecting a junctioned install-root ancestor.'
    Assert-Equal (@(Get-ChildItem -LiteralPath $ancestorStartTarget.RecordRoot -Filter '*.txt' -File).Count) 0 'Start launched a process before rejecting a junctioned install-root ancestor.'
  } finally {
    Register-FixtureRecordPids -RecordRoot $ancestorStartTarget.RecordRoot
    if (Test-Path -LiteralPath $ancestorStartAliasParent) { [System.IO.Directory]::Delete($ancestorStartAliasParent) }
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $ancestorStopTarget = New-InstallFixture -Name 'ancestor-stop-target-parent\install'
  $ancestorStopAliasParent = Join-Path $fixtureRoot 'ancestor-stop-alias-parent'
  New-Item -ItemType Junction -Path $ancestorStopAliasParent -Target (Split-Path -Parent $ancestorStopTarget.Root) | Out-Null
  $ancestorStopAliasRoot = Join-Path $ancestorStopAliasParent 'install'
  Remove-Item -LiteralPath $ancestorStopTarget.Paths.Logs -Recurse -Force
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  $ancestorStopProcess = $null
  try {
    $env:FIXTURE_RECORD_DIR = $ancestorStopTarget.RecordRoot
    $ancestorStopProcess = Start-Process -FilePath $ancestorStopTarget.Paths.BridgePython -ArgumentList @('main.py') -WorkingDirectory $ancestorStopTarget.Paths.Bridge -PassThru -WindowStyle Hidden
    $startedFixturePids.Add([int]$ancestorStopProcess.Id)
    Assert-Equal (@(Wait-FixtureRecords -RecordRoot $ancestorStopTarget.RecordRoot -Count 1).Count) 1 'Ancestor Stop fixture process did not start.'
    $ancestorStopRecord = New-TestProcessRecord -Name 'bridge' -Process $ancestorStopProcess -ExecutablePath $ancestorStopTarget.Paths.BridgePython -Owned $true -CommandKind 'BridgeMain'
    Write-TestProcessRecords -Path $ancestorStopTarget.Paths.ProcessState -Records @($ancestorStopRecord)
    $ancestorStopBefore = Get-FixtureTreeSnapshot -Root $ancestorStopTarget.Root
    Assert-ThrowsLike {
      Stop-AkashaServices -InstallRoot $ancestorStopAliasRoot
    } 'E_LIFECYCLE_PATH:*' 'Stop accepted an install root below a junctioned ancestor.' | Out-Null
    $ancestorStopAfter = Get-FixtureTreeSnapshot -Root $ancestorStopTarget.Root
    Assert-Equal $ancestorStopAfter $ancestorStopBefore 'Stop mutated the canonical target before rejecting a junctioned install-root ancestor.'
    Assert-True ($null -ne (Get-Process -Id $ancestorStopProcess.Id -ErrorAction SilentlyContinue)) 'Stop killed a recorded process before rejecting a junctioned install-root ancestor.'
  } finally {
    if (Test-Path -LiteralPath $ancestorStopAliasParent) { [System.IO.Directory]::Delete($ancestorStopAliasParent) }
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $weFlowParentJunction = New-InstallFixture -Name 'weflow-parent-junction'
  $weFlowOutsideParent = Join-Path $fixtureRoot 'weflow-outside-parent'
  New-Item -ItemType Directory -Force -Path $weFlowOutsideParent | Out-Null
  Copy-Item -LiteralPath $fixtureBinary -Destination (Join-Path $weFlowOutsideParent 'WeFlow.exe') -Force
  Remove-Item -LiteralPath (Split-Path -Parent $weFlowParentJunction.WeFlow) -Recurse -Force
  New-Item -ItemType Junction -Path (Split-Path -Parent $weFlowParentJunction.WeFlow) -Target $weFlowOutsideParent | Out-Null
  try {
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $weFlowParentJunction.Root
    } 'E_WEFLOW_EXE:*' 'Start accepted a WeFlow executable through a junctioned parent chain.' | Out-Null
    Assert-Equal (@(Get-ChildItem -LiteralPath $weFlowParentJunction.RecordRoot -Filter '*.txt' -File).Count) 0 'Junctioned WeFlow parent launched a real process.'
  } finally {
    Register-FixtureRecordPids -RecordRoot $weFlowParentJunction.RecordRoot
    $weFlowLink = Split-Path -Parent $weFlowParentJunction.WeFlow
    if (Test-Path -LiteralPath $weFlowLink) { [System.IO.Directory]::Delete($weFlowLink) }
  }

  $lateReparse = New-InstallFixture -Name 'late-reparse-path'
  $lateReparseTarget = Join-Path $fixtureRoot 'late-reparse-target'
  New-Item -ItemType Directory -Force -Path $lateReparseTarget | Out-Null
  Set-Content -LiteralPath (Join-Path $lateReparseTarget 'outside-sentinel.txt') -Value 'outside' -Encoding ASCII
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  $lateWatcher = $null
  try {
    $env:FIXTURE_RECORD_DIR = $lateReparse.RecordRoot
    $lateWatcher = Start-Job -ArgumentList $lateReparse.Paths.Logs, $lateReparseTarget -ScriptBlock {
      param($LogsPath, $TargetPath)
      $launcherPath = Join-Path $LogsPath 'launcher.log'
      for ($attempt = 0; $attempt -lt 2000 -and -not (Test-Path -LiteralPath $launcherPath); $attempt++) { Start-Sleep -Milliseconds 5 }
      if (Test-Path -LiteralPath $launcherPath) {
        Remove-Item -LiteralPath $LogsPath -Recurse -Force
        New-Item -ItemType Junction -Path $LogsPath -Target $TargetPath | Out-Null
      }
    }
    Assert-ThrowsLike {
      Start-AkashaServices -InstallRoot $lateReparse.Root
    } 'E_LIFECYCLE_PATH:*' 'Start did not revalidate a path changed to a junction after lock acquisition.' | Out-Null
    Wait-Job -Job $lateWatcher -Timeout 15 | Out-Null
    Receive-Job -Job $lateWatcher -ErrorAction Stop | Out-Null
    Register-FixtureRecordPids -RecordRoot $lateReparse.RecordRoot
    Assert-FixtureRecordProcessesDead -RecordRoot $lateReparse.RecordRoot -Message 'Late reparse failure left a real process running.'
    Assert-Equal (Get-Content -LiteralPath (Join-Path $lateReparseTarget 'outside-sentinel.txt') -Raw -Encoding UTF8).Trim() 'outside' 'Late reparse failure changed the outside sentinel.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $lateReparseTarget 'launcher.log'))) 'Late reparse failure wrote its launcher log outside the install root.'
  } finally {
    if ($null -ne $lateWatcher) { Remove-Job -Job $lateWatcher -Force -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $lateReparse.Paths.Logs) {
      $lateLogsItem = Get-Item -LiteralPath $lateReparse.Paths.Logs -Force
      if ($lateLogsItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { [System.IO.Directory]::Delete($lateReparse.Paths.Logs) }
    }
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $direct = New-InstallFixture -Name 'direct-entry'
  $oldFixtureRecordDirectory = $env:FIXTURE_RECORD_DIR
  try {
    $env:FIXTURE_RECORD_DIR = $direct.RecordRoot
    $directStartHost = $null
    try {
      $directStartScript = Join-Path $root 'scripts\Start-Services.ps1'
      Assert-True (-not $directStartScript.Contains('"') -and -not $direct.Root.Contains('"')) 'Direct Start fixture paths contain an unsupported quote.'
      $directStartHost = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $directStartScript + '"'), '-InstallRoot', ('"' + $direct.Root + '"')) -PassThru -WindowStyle Hidden
      Assert-True $directStartHost.WaitForExit(15000) 'Direct Start-Services.ps1 entry timed out.'
      $directStartCode = $directStartHost.ExitCode
    } finally {
      if ($null -ne $directStartHost -and -not $directStartHost.HasExited) { $directStartHost.Kill() }
    }
    Assert-Equal $directStartCode 0 'Direct Start-Services.ps1 entry failed.'
    Register-ProcessStatePids -Path $direct.Paths.ProcessState
    Assert-Equal (@(Get-StateRecords -Path $direct.Paths.ProcessState).Count) 3 'Direct Start-Services.ps1 entry did not persist three records.'
    $directStopInfo = New-Object System.Diagnostics.ProcessStartInfo
    $directStopInfo.FileName = 'powershell.exe'
    $directStopScript = Join-Path $root 'scripts\Stop-Services.ps1'
    Assert-True (-not $directStopScript.Contains('"') -and -not $direct.Root.Contains('"')) 'Direct Stop fixture paths contain an unsupported quote.'
    $directStopInfo.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $directStopScript + '" -InstallRoot "' + $direct.Root + '"'
    $directStopInfo.UseShellExecute = $false
    $directStopInfo.CreateNoWindow = $true
    $directStopInfo.RedirectStandardOutput = $true
    $directStopInfo.RedirectStandardError = $true
    $directStopHost = New-Object System.Diagnostics.Process
    $directStopHost.StartInfo = $directStopInfo
    try {
      Assert-True $directStopHost.Start() 'Direct Stop-Services.ps1 process did not start.'
      $directStopOutput = $directStopHost.StandardOutput.ReadToEnd() + $directStopHost.StandardError.ReadToEnd()
      Assert-True $directStopHost.WaitForExit(15000) 'Direct Stop-Services.ps1 entry timed out.'
      $directStopCode = $directStopHost.ExitCode
    } finally {
      if (-not $directStopHost.HasExited) { $directStopHost.Kill() }
    }
    if ($directStopCode -ne 0) { throw "Direct Stop-Services.ps1 entry failed: $directStopOutput" }
    Assert-True (-not (Test-Path -LiteralPath $direct.Paths.ProcessState)) 'Direct Stop-Services.ps1 entry left process state.'
  } finally {
    if ($null -eq $oldFixtureRecordDirectory) { Remove-Item Env:\FIXTURE_RECORD_DIR -ErrorAction SilentlyContinue } else { $env:FIXTURE_RECORD_DIR = $oldFixtureRecordDirectory }
  }

  $health = New-InstallFixture -Name 'health-aggregation'
  Remove-Item -LiteralPath $health.Paths.Logs -Recurse -Force
  Remove-Item -LiteralPath $health.Paths.State -Recurse -Force
  $healthCalls = New-Object System.Collections.Generic.List[string]
  $healthyHttp = {
    param([string]$Uri)
    $healthCalls.Add('HTTP ' + $Uri)
    return $true
  }
  $healthyTcp = {
    param([string]$HostName, [int]$Port)
    $healthCalls.Add("TCP $HostName`:$Port")
    return $true
  }
  $healthyCode = Invoke-AkashaHealthCheck -InstallRoot $health.Root -HttpProbe $healthyHttp -TcpProbe $healthyTcp
  Assert-Equal $healthyCode 0 'All-success health aggregation did not return exit code 0.'
  Assert-Equal ($healthCalls -join '|') 'HTTP http://127.0.0.1:5031/health|HTTP http://127.0.0.1:6185/|HTTP http://127.0.0.1:8766/status|TCP 127.0.0.1:11229' 'Health probes changed fixed endpoints or order.'

  $healthCalls.Clear()
  $failingHttp = {
    param([string]$Uri)
    $healthCalls.Add('HTTP ' + $Uri)
    if ($Uri -like '*:6185/*') { return $false }
    if ($Uri -like '*:8766/*') { throw 'probe failure with response-body-secret' }
    return $true
  }
  $failingTcp = {
    param([string]$HostName, [int]$Port)
    $healthCalls.Add("TCP $HostName`:$Port")
    return $false
  }
  $failedCode = Invoke-AkashaHealthCheck -InstallRoot $health.Root -HttpProbe $failingHttp -TcpProbe $failingTcp
  Assert-Equal $failedCode 1 'Partial health failure did not return exit code 1.'
  Assert-Equal $healthCalls.Count 4 'Health aggregation stopped before running all four probes.'
  Assert-True (-not (Test-Path -LiteralPath $health.Paths.Logs)) 'Health check created the logs directory.'
  Assert-True (-not (Test-Path -LiteralPath $health.Paths.State)) 'Health check created the state directory.'
  $healthSource = Get-Content -LiteralPath (Join-Path $root 'scripts\Test-Health.ps1') -Raw -Encoding UTF8
  Assert-True ($healthSource -match 'System\.Net\.Sockets\.TcpClient') 'OneBot default health probe does not use TcpClient.'
  Assert-True ($healthSource -notmatch 'Get-NetTCPConnection[\s\S]{0,300}\[OK\].*OneBot') 'OneBot success is inferred from a listening port owner.'

  $startSource = Get-Content -LiteralPath (Join-Path $root 'scripts\Start-Services.ps1') -Raw -Encoding UTF8
  $stopSource = Get-Content -LiteralPath (Join-Path $root 'scripts\Stop-Services.ps1') -Raw -Encoding UTF8
  Assert-True ($stopSource -match 'OpenProcess' -and $stopSource -match 'QueryFullProcessImageName' -and $stopSource -match 'GetProcessTimes' -and $stopSource -match 'NtQueryInformationProcess' -and $stopSource -match 'TerminateProcess' -and $stopSource -match 'WaitForSingleObject') 'Stop does not verify and terminate through a retained native process handle.'
  Assert-True ($stopSource -notmatch '\.Process\.Kill\(') 'Stop still terminates through a separately resolved managed Process object.'
  Assert-True ($startSource -match 'FileOptions\]::DeleteOnClose') 'Lifecycle lock is not retained with DeleteOnClose semantics.'
  Assert-True ($startSource -match 'FileMode\]::CreateNew' -and $startSource -match 'FileShare\]::None') 'Atomic process-state temporary files are not exclusively created.'
  Assert-True ($startSource -match 'GetFinalPathFromHandle\(\$temporaryStream\.SafeFileHandle\.DangerousGetHandle\(\)\)') 'Process-state temp handle is not final-path verified before writing.'
  Assert-True ($startSource -match 'GetFinalPathFromHandle\(\$logStream\.SafeFileHandle\.DangerousGetHandle\(\)\)') 'Lifecycle log handle is not final-path verified before writing.'

  Write-Host 'Process safety tests: PASS' -ForegroundColor Green
} finally {
  Stop-TrackedFixtureProcesses
  if (Test-Path -LiteralPath $fixtureRoot) {
    for ($removeAttempt = 0; $removeAttempt -lt 10 -and (Test-Path -LiteralPath $fixtureRoot); $removeAttempt++) {
      try { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction Stop } catch { Start-Sleep -Milliseconds 100 }
    }
  }
}
