[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'Start-Services.ps1') -InstallRoot $InstallRoot

function Get-AkashaCalibrationPreflight {
  param([Parameter(Mandatory)]$Paths)

  $calibrationScript = Join-Path $Paths.Bridge 'calibrate_uia_fixed.py'
  $lockPath = Join-Path $Paths.State 'lifecycle.lock'
  $bridgePidPath = Join-Path $Paths.State 'bridge.pid'
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @(
      $Paths.State,
      $Paths.ProcessState,
      $Paths.Backups,
      $Paths.Bridge,
      $Paths.BridgePython,
      $Paths.BridgeConfig,
      $calibrationScript,
      $lockPath,
      $bridgePidPath
    )
  foreach ($requiredFile in @($Paths.BridgePython, $Paths.BridgeConfig, $calibrationScript)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "E_NOT_INSTALLED: Missing required file: $requiredFile"
    }
  }
  if (-not (Test-Path -LiteralPath $Paths.State -PathType Container)) {
    throw 'E_NOT_INSTALLED: Missing lifecycle state directory.'
  }
  return [pscustomobject]@{
    CalibrationScript = $calibrationScript
    LockPath = $lockPath
    BridgePidPath = $bridgePidPath
  }
}

function Read-AkashaBridgePid {
  param([Parameter(Mandatory)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw 'E_PROCESS_STATE: Invalid bridge pid state.'
  }
  try {
    $value = (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop).Trim()
    $processId = 0
    if ($value -notmatch '^[1-9][0-9]*$' -or -not [int]::TryParse($value, [ref]$processId)) {
      throw 'invalid bridge pid'
    }
    return $processId
  } catch {
    throw 'E_PROCESS_STATE: Invalid bridge pid state.'
  }
}

function Read-AkashaCalibrationProcessIdentity {
  param(
    [Parameter(Mandatory)][int]$ProcessId,
    [Parameter(Mandatory)][scriptblock]$ProcessReader
  )

  try { $identities = @(& $ProcessReader $ProcessId) } catch { throw 'E_PROCESS_STATE: Unable to inspect a recorded process.' }
  if ($identities.Count -eq 0 -or $null -eq $identities[0]) { return $null }
  if ($identities.Count -ne 1) { throw 'E_PROCESS_STATE: Unable to inspect a recorded process.' }
  return $identities[0]
}

function Test-AkashaCalibrationBridgeIdentity {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Identity
  )

  if ([string]::IsNullOrWhiteSpace([string]$Identity.ExecutablePath)) { return $false }
  try {
    $actual = [System.IO.Path]::GetFullPath([string]$Identity.ExecutablePath)
    $expected = [System.IO.Path]::GetFullPath([string]$Paths.BridgePython)
  } catch {
    return $false
  }
  return $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase) -and
    (Test-AkashaCommandIdentity -CommandKind 'BridgeMain' -CommandLine ([string]$Identity.CommandLine))
}

function Test-AkashaCalibrationBridgeExecutable {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Identity
  )

  if ([string]::IsNullOrWhiteSpace([string]$Identity.ExecutablePath)) { return $false }
  try {
    $actual = [System.IO.Path]::GetFullPath([string]$Identity.ExecutablePath)
    $expected = [System.IO.Path]::GetFullPath([string]$Paths.BridgePython)
    return $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)
  } catch {
    return $false
  }
}

function Test-AkashaCalibrationExitCodeType {
  param($Value)

  return $Value -is [sbyte] -or
    $Value -is [byte] -or
    $Value -is [int16] -or
    $Value -is [uint16] -or
    $Value -is [int32] -or
    $Value -is [uint32] -or
    $Value -is [int64] -or
    $Value -is [uint64]
}

function Invoke-AkashaUiaCalibration {
  param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [scriptblock]$ProcessReader,
    [scriptblock]$Runner
  )

  if ($null -eq $ProcessReader) {
    $ProcessReader = { param([int]$ProcessId) Get-AkashaProcessIdentity -ProcessId $ProcessId }
  }

  $rootContext = Open-AkashaLifecycleRootContext -Root $InstallRoot
  try {
    $paths = Get-AkashaBotPaths -Root $InstallRoot
    $preflight = Get-AkashaCalibrationPreflight -Paths $paths
    $lockStream = $null
    try {
      try {
        $lockStream = New-Object System.IO.FileStream(
          $preflight.LockPath,
          [System.IO.FileMode]::OpenOrCreate,
          [System.IO.FileAccess]::ReadWrite,
          [System.IO.FileShare]::None,
          1,
          [System.IO.FileOptions]::DeleteOnClose
        )
      } catch {
        throw 'E_UIA_CALIBRATION_BUSY'
      }
      $lockFinalPath = [AkashaBotNativePathV1]::GetFinalPathFromHandle($lockStream.SafeFileHandle.DangerousGetHandle())
      if ([string]::IsNullOrWhiteSpace($lockFinalPath) -or
          -not ([System.IO.Path]::GetFullPath($lockFinalPath)).Equals([System.IO.Path]::GetFullPath($preflight.LockPath), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'E_LIFECYCLE_PATH: Lifecycle lock resolved outside its expected path.'
      }

      $preflight = Get-AkashaCalibrationPreflight -Paths $paths
      foreach ($record in @(Read-AkashaProcessState -Path $paths.ProcessState -Paths $paths)) {
        $identity = Read-AkashaCalibrationProcessIdentity -ProcessId ([int]$record.Pid) -ProcessReader $ProcessReader
        if ($null -ne $identity -and (Test-AkashaRecordMatchesLiveProcess -Record $record -Identity $identity)) {
          throw 'E_UIA_CALIBRATION_BUSY'
        }
      }

      $bridgePid = Read-AkashaBridgePid -Path $preflight.BridgePidPath
      if ($null -ne $bridgePid) {
        $bridgeIdentity = Read-AkashaCalibrationProcessIdentity -ProcessId ([int]$bridgePid) -ProcessReader $ProcessReader
        if ($null -ne $bridgeIdentity -and (Test-AkashaCalibrationBridgeIdentity -Paths $paths -Identity $bridgeIdentity)) {
          throw 'E_UIA_CALIBRATION_BUSY'
        }
        if ($null -ne $bridgeIdentity -and
            (Test-AkashaCalibrationBridgeExecutable -Paths $paths -Identity $bridgeIdentity) -and
            [string]::IsNullOrWhiteSpace([string]$bridgeIdentity.CommandLine)) {
          throw 'E_PROCESS_STATE: Unable to verify bridge pid identity.'
        }
      }

      $preflight = Get-AkashaCalibrationPreflight -Paths $paths
      $arguments = @($preflight.CalibrationScript, '--config', $paths.BridgeConfig, '--backup-dir', $paths.Backups)
      try {
        if ($null -eq $Runner) {
          & $paths.BridgePython @arguments | Out-Host
          $exitCode = [int]$LASTEXITCODE
        } else {
          $runnerRecords = @(& $Runner $paths.BridgePython $arguments)
          if ($runnerRecords.Count -ne 1 -or -not (Test-AkashaCalibrationExitCodeType -Value $runnerRecords[0])) {
            throw 'invalid runner exit code'
          }
          $exitCode = [int]$runnerRecords[0]
        }
      } catch {
        throw 'E_UIA_CALIBRATION_INVALID'
      }

      switch ($exitCode) {
        0 { return 0 }
        2 { return 2 }
        20 { throw 'E_UIA_CALIBRATION_INVALID' }
        21 { throw 'E_UIA_CALIBRATION_WINDOW' }
        22 { throw 'E_UIA_CALIBRATION_REQUIRED' }
        23 { throw 'E_UIA_CALIBRATION_BUSY' }
        24 { throw 'E_UIA_RECALIBRATION_REQUIRED' }
        default { throw 'E_UIA_CALIBRATION_INVALID' }
      }
    } finally {
      if ($null -ne $lockStream) { $lockStream.Dispose() }
    }
  } finally {
    Close-AkashaLifecycleRootContext -Context $rootContext
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  try {
    $exitCode = Invoke-AkashaUiaCalibration -InstallRoot $InstallRoot
    exit ([int]$exitCode)
  } catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
  }
}
