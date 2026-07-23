[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'),
  [string]$SourceRoot = '',
  [string]$WeFlowInstallerPath = '',
  [string]$WeFlowConfigPath = '',
  [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:AkashaInstallScriptRoot = $PSScriptRoot
$script:AkashaInstallDefaultSourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script:AkashaInstallCommonModule = Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -Force -PassThru
. (Join-Path $PSScriptRoot 'Start-Services.ps1')
. (Join-Path $PSScriptRoot 'Initialize-Environments.ps1')
. (Join-Path $PSScriptRoot 'Initialize-Configuration.ps1')
. (Join-Path $PSScriptRoot 'Test-Health.ps1')

function ConvertFrom-AkashaCodePoints {
  param([Parameter(Mandatory)][int[]]$CodePoints)

  return -join @($CodePoints | ForEach-Object { [char]$_ })
}

function Get-AkashaLauncherNames {
  $install = (ConvertFrom-AkashaCodePoints -CodePoints @(0x5B89, 0x88C5)) + '.bat'
  $calibrate = (ConvertFrom-AkashaCodePoints -CodePoints @(0x6821, 0x51C6)) + '.bat'
  $start = (ConvertFrom-AkashaCodePoints -CodePoints @(0x542F, 0x52A8)) + '.bat'
  $stop = (ConvertFrom-AkashaCodePoints -CodePoints @(0x505C, 0x6B62)) + '.bat'
  $health = (ConvertFrom-AkashaCodePoints -CodePoints @(0x5065, 0x5EB7, 0x68C0, 0x67E5)) + '.bat'
  return [pscustomobject]@{
    Install = $install
    Calibrate = $calibrate
    Start = $start
    Stop = $stop
    Health = $health
  }
}

function Get-AkashaInstallPayload {
  $launchers = Get-AkashaLauncherNames
  $entries = New-Object System.Collections.Generic.List[object]
  foreach ($name in @(
      'bridge_core.py', 'config.py', 'main.py', 'ob_client.py', 'ob_protocol.py', 'privacy.py',
      'state.py', 'uia_fixed_sender.py', 'uia_support.py', 'calibrate_uia_fixed.py', 'web_panel.py',
      'config.example.json', 'requirements.txt', 'requirements.lock'
    )) {
    $entries.Add([pscustomobject]@{ Source = Join-Path 'bridge' $name; Destination = Join-Path 'app\bridge' $name })
  }
  foreach ($name in @(
      'AkashaBot.Common.psm1', 'Test-Prerequisites.ps1', 'Initialize-Environments.ps1',
      'Initialize-Configuration.ps1', 'Start-Services.ps1', 'Stop-Services.ps1',
      'Test-Health.ps1', 'Calibrate-Uia.ps1', 'Install.ps1'
    )) {
    $entries.Add([pscustomobject]@{ Source = Join-Path 'scripts' $name; Destination = Join-Path 'scripts' $name })
  }
  foreach ($name in @($launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health, 'VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    $entries.Add([pscustomobject]@{ Source = $name; Destination = $name })
  }
  return $entries.ToArray()
}

function Test-AkashaCanonicalFile {
  param(
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$Path
  )

  if (-not (Test-AkashaLifecycleInternalPath -Root $Root -Candidate $Path)) { return $false }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
  $current = [System.IO.Path]::GetFullPath($Path)
  $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
  while (-not $current.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop } catch { return $false }
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
    $current = [System.IO.Path]::GetDirectoryName($current).TrimEnd('\', '/')
  }
  $expected = [System.IO.Path]::GetFullPath($Path)
  $final = [AkashaBotNativePathV1]::GetFinalPath($expected, $false)
  if ([string]::IsNullOrWhiteSpace($final)) { return $false }
  try { $final = [System.IO.Path]::GetFullPath($final) } catch { return $false }
  return $expected.Equals($final, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-AkashaInstallSource {
  param(
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][object[]]$Payload
  )

  foreach ($entry in $Payload) {
    $sourcePath = Join-Path $Root ([string]$entry.Source)
    if (-not (Test-AkashaCanonicalFile -Root $Root -Path $sourcePath)) {
      throw 'E_SOURCE_PAYLOAD: A required source payload file is missing or unsafe.'
    }
  }
  $versionPath = Join-Path $Root 'VERSION'
  try { $version = (Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8 -ErrorAction Stop).Trim() } catch { throw 'E_SOURCE_VERSION: VERSION is invalid.' }
  if ([string]::IsNullOrWhiteSpace($version) -or $version.Length -gt 64 -or $version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
    throw 'E_SOURCE_VERSION: VERSION is invalid.'
  }
  return $version
}

function Assert-AkashaInstallPaths {
  param([Parameter(Mandatory)]$Paths)

  $candidates = @(
    $Paths.App, $Paths.Bridge, $Paths.Scripts, $Paths.Runtime, $Paths.BridgeVenv, $Paths.AstrBotVenv,
    $Paths.Data, $Paths.BridgeData, $Paths.AstrBotData, $Paths.Logs, $Paths.State, $Paths.Backups,
    $Paths.InstallLog, $Paths.ProcessState, $Paths.InstallState, $Paths.WeFlowPathState
  )
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates $candidates
}

function New-AkashaInstallDirectory {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Path
  )

  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Path)
  New-Item -ItemType Directory -Force -Path $Path | Out-Null
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Path)
}

function Open-AkashaInstallLifecycleLock {
  param([Parameter(Mandatory)]$Paths)

  $lockPath = Join-Path $Paths.State 'lifecycle.lock'
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.State, $lockPath)
  try {
    $stream = New-Object System.IO.FileStream(
      $lockPath,
      [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::ReadWrite,
      [System.IO.FileShare]::None,
      1,
      [System.IO.FileOptions]::DeleteOnClose
    )
  } catch {
    throw 'E_LIFECYCLE_BUSY: Another lifecycle operation is already running.'
  }
  try {
    $final = [AkashaBotNativePathV1]::GetFinalPathFromHandle($stream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($final) -or
        -not ([System.IO.Path]::GetFullPath($final)).Equals([System.IO.Path]::GetFullPath($lockPath), [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'E_LIFECYCLE_PATH: Lifecycle lock resolved outside its expected path.'
    }
    return $stream
  } catch {
    $stream.Dispose()
    throw
  }
}

function Assert-AkashaInstallProcessState {
  param([Parameter(Mandatory)]$Paths)

  $records = @(Read-AkashaProcessState -Path $Paths.ProcessState -Paths $Paths)
  if ($records.Count -gt 0) {
    throw 'E_INSTALL_RUNNING: Stop all recorded services before installing.'
  }
}

function Invoke-AkashaInstallPrerequisites {
  param([Parameter(Mandatory)]$Paths)

  $python = & $script:AkashaInstallCommonModule {
    Assert-AkashaWindowsClient | Out-Null
    Resolve-Python312
  }
  $probe = Join-Path $Paths.State ('.install-write-' + [guid]::NewGuid().ToString('N'))
  try {
    [System.IO.File]::WriteAllText($probe, 'ok', (New-Object System.Text.UTF8Encoding($false)))
  } finally {
    if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force -ErrorAction Stop }
  }
  return $python
}

function Select-AkashaWeFlowInstaller {
  Add-Type -AssemblyName System.Windows.Forms
  $dialog = New-Object System.Windows.Forms.OpenFileDialog
  $dialog.Title = 'Select a local WeFlow Windows installer'
  $dialog.Filter = 'Windows installer (*.exe;*.msi)|*.exe;*.msi'
  $dialog.Multiselect = $false
  try {
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
    return [string]$dialog.FileName
  } finally {
    $dialog.Dispose()
  }
}

function Assert-AkashaWeFlowInstaller {
  param([Parameter(Mandatory)][string]$Path)

  try { $candidate = [System.IO.Path]::GetFullPath($Path) } catch { throw 'E_WEFLOW_INSTALLER: Selected installer is invalid.' }
  $extension = [System.IO.Path]::GetExtension($candidate)
  if (@('.exe', '.msi') -inotcontains $extension -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw 'E_WEFLOW_INSTALLER: Selected installer must be an existing .exe or .msi file.'
  }
  $current = $candidate
  while (-not [string]::IsNullOrWhiteSpace($current)) {
    try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop } catch { throw 'E_WEFLOW_INSTALLER: Selected installer is invalid.' }
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'E_WEFLOW_INSTALLER: Selected installer is invalid.' }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $current) { break }
    $current = $parent
  }
  $final = [AkashaBotNativePathV1]::GetFinalPath($candidate, $false)
  if ([string]::IsNullOrWhiteSpace($final) -or
      -not ([System.IO.Path]::GetFullPath($final)).Equals($candidate, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'E_WEFLOW_INSTALLER: Selected installer is invalid.'
  }
  return $candidate
}

function Invoke-AkashaWeFlowPackage {
  param(
    [Parameter(Mandatory)][ValidateSet('exe', 'msi')][string]$Kind,
    [Parameter(Mandatory)][string]$Path
  )

  if ($Kind -ceq 'msi') {
    $quotedPath = '"' + $Path.Replace('"', '""') + '"'
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', $quotedPath) -Wait -PassThru
  } else {
    $process = Start-Process -FilePath $Path -Wait -PassThru
  }
  return [int]$process.ExitCode
}

function Write-AkashaInstallLog {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Message,
    [ValidateSet('info', 'warn', 'error')][string]$Level = 'info'
  )

  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.Logs, $Paths.InstallLog)
  $stream = $null
  try {
    $line = '{0:o} [{1}] {2}' -f (Get-Date), $Level.ToUpperInvariant(), (Protect-AkashaLogText $Message)
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($line + [Environment]::NewLine)
    $stream = New-Object System.IO.FileStream($Paths.InstallLog, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    $final = [AkashaBotNativePathV1]::GetFinalPathFromHandle($stream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($final) -or
        -not ([System.IO.Path]::GetFullPath($final)).Equals([System.IO.Path]::GetFullPath($Paths.InstallLog), [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'invalid install log handle'
    }
    [void]$stream.Seek(0, [System.IO.SeekOrigin]::End)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
    Write-Host $line
  } catch {
    throw 'E_INSTALL_LOG: Unable to write installer log.'
  } finally {
    if ($null -ne $stream) { $stream.Dispose() }
  }
}

function Write-AkashaInstallState {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Status,
    [Parameter(Mandatory)][string]$Version,
    [string]$ErrorCode = ''
  )

  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.State, $Paths.InstallState)
  $value = [ordered]@{ status = $Status; version = $Version; updated_at = (Get-Date).ToUniversalTime().ToString('o') }
  if (-not [string]::IsNullOrWhiteSpace($ErrorCode)) { $value.error_code = $ErrorCode }
  $json = $value | ConvertTo-Json -Depth 8
  Write-AkashaInstallBytesAtomic -Paths $Paths -Path $Paths.InstallState -Bytes ((New-Object System.Text.UTF8Encoding($false)).GetBytes($json))
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.State, $Paths.InstallState)
}

function Write-AkashaInstallBytesAtomic {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][byte[]]$Bytes
  )

  $target = [System.IO.Path]::GetFullPath($Path)
  $directory = [System.IO.Path]::GetDirectoryName($target)
  $temporary = Join-Path $directory ('.' + [System.IO.Path]::GetFileName($target) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
  $replacementBackup = $temporary + '.replace-backup'
  $stream = $null
  $verificationStream = $null
  try {
    Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($directory, $target, $temporary, $replacementBackup)
    $stream = New-Object System.IO.FileStream($temporary, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $temporaryFinal = [AkashaBotNativePathV1]::GetFinalPathFromHandle($stream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($temporaryFinal) -or
        -not ([System.IO.Path]::GetFullPath($temporaryFinal)).Equals($temporary, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'temporary handle resolved elsewhere'
    }
    $stream.Write($Bytes, 0, $Bytes.Length)
    $stream.Flush($true)
    $stream.Dispose()
    $stream = $null
    Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($directory, $target, $temporary, $replacementBackup)
    if (Test-Path -LiteralPath $target -PathType Leaf) {
      [System.IO.File]::Replace($temporary, $target, $replacementBackup)
    } else {
      [System.IO.File]::Move($temporary, $target)
    }
    Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($directory, $target)
    $verificationStream = New-Object System.IO.FileStream($target, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $targetFinal = [AkashaBotNativePathV1]::GetFinalPathFromHandle($verificationStream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($targetFinal) -or
        -not ([System.IO.Path]::GetFullPath($targetFinal)).Equals($target, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'target handle resolved elsewhere'
    }
  } catch {
    throw 'E_INSTALL_STATE_WRITE: Unable to write installer state safely.'
  } finally {
    if ($null -ne $verificationStream) { $verificationStream.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
    foreach ($artifact in @($temporary, $replacementBackup)) {
      if ((Test-AkashaLifecycleInternalPath -Root $Paths.Root -Candidate $artifact) -and (Test-Path -LiteralPath $artifact)) {
        Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

function Assert-AkashaInstallTreeSafe {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Path
  )

  if (-not (Test-Path -LiteralPath $Path)) { return }
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Path)
  foreach ($item in @(Get-Item -LiteralPath $Path -Force -ErrorAction Stop) + @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop)) {
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      throw 'E_INSTALL_PATH: Product payload trees must not contain reparse points.'
    }
  }
}

function Remove-AkashaInstallTree {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Path
  )

  if (-not (Test-Path -LiteralPath $Path)) { return }
  Assert-AkashaInstallTreeSafe -Paths $Paths -Path $Path
  Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
}

function Install-AkashaPayload {
  param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][object[]]$Payload,
    [scriptblock]$ReplacementHook
  )

  $transactionId = [guid]::NewGuid().ToString('N')
  $stageRoot = Join-Path $Paths.State ('.install-stage-' + $transactionId)
  $rollbackRoot = Join-Path $Paths.State ('.install-rollback-' + $transactionId)
  $launchers = Get-AkashaLauncherNames
  $topTargets = @('app\bridge', 'scripts') + @($launchers.Calibrate, $launchers.Start, $launchers.Stop, $launchers.Health, 'VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md')
  $movedOriginals = New-Object System.Collections.Generic.List[string]
  $committedTargets = New-Object System.Collections.Generic.List[string]
  $originalLocations = @{}
  $operationError = $null
  try {
    New-AkashaInstallDirectory -Paths $Paths -Path $stageRoot
    foreach ($entry in $Payload) {
      $source = Join-Path $SourceRoot ([string]$entry.Source)
      if (-not (Test-AkashaCanonicalFile -Root $SourceRoot -Path $source)) { throw 'E_SOURCE_PAYLOAD: Source payload changed during installation.' }
      $destination = Join-Path $stageRoot ([string]$entry.Destination)
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($stageRoot, $destination)
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
      Copy-Item -LiteralPath $source -Destination $destination -Force -ErrorAction Stop
    }

    if (Test-Path -LiteralPath $Paths.Bridge -PathType Container) {
      Assert-AkashaInstallTreeSafe -Paths $Paths -Path $Paths.Bridge
      New-AkashaInstallDirectory -Paths $Paths -Path $Paths.Backups
      $backup = Join-Path $Paths.Backups ('bridge-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $transactionId.Substring(0, 8))
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.Bridge, $Paths.Backups, $backup)
      Move-Item -LiteralPath $Paths.Bridge -Destination $backup -Force -ErrorAction Stop
      $movedOriginals.Add('app\bridge')
      $originalLocations['app\bridge'] = $backup
    }

    New-AkashaInstallDirectory -Paths $Paths -Path $rollbackRoot
    foreach ($relative in $topTargets) {
      $target = Join-Path $Paths.Root $relative
      $saved = Join-Path $rollbackRoot $relative
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($target, $saved)
      if (Test-Path -LiteralPath $target) {
        Assert-AkashaInstallTreeSafe -Paths $Paths -Path $target
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
        Move-Item -LiteralPath $target -Destination $saved -Force -ErrorAction Stop
        $movedOriginals.Add($relative)
        $originalLocations[$relative] = $saved
      }
    }
    if ($null -ne $ReplacementHook) { & $ReplacementHook 'AfterExistingMoved' }

    foreach ($relative in $topTargets) {
      $staged = Join-Path $stageRoot $relative
      $target = Join-Path $Paths.Root $relative
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($staged, $target)
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
      Move-Item -LiteralPath $staged -Destination $target -Force -ErrorAction Stop
      $committedTargets.Add($relative)
      if ($null -ne $ReplacementHook) { & $ReplacementHook ('AfterCommit:' + $relative) }
    }
  } catch {
    $operationError = $_
  }

  if ($null -ne $operationError -and ($movedOriginals.Count -gt 0 -or $committedTargets.Count -gt 0)) {
    $rollbackSucceeded = $true
    foreach ($relative in $topTargets) {
      $target = Join-Path $Paths.Root $relative
      $saved = if ($originalLocations.ContainsKey($relative)) { [string]$originalLocations[$relative] } else { Join-Path $rollbackRoot $relative }
      try {
        if ($committedTargets.Contains($relative) -and (Test-Path -LiteralPath $target)) {
          Remove-AkashaInstallTree -Paths $Paths -Path $target
        }
        if ($movedOriginals.Contains($relative) -and (Test-Path -LiteralPath $saved)) {
          if (Test-Path -LiteralPath $target) { Remove-AkashaInstallTree -Paths $Paths -Path $target }
          New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
          Move-Item -LiteralPath $saved -Destination $target -Force -ErrorAction Stop
        }
      } catch { $rollbackSucceeded = $false }
    }
    if (-not $rollbackSucceeded) { $operationError.Exception.Data['AkashaRollbackFailure'] = 'E_INSTALL_ROLLBACK' }
  }

  $cleanupSucceeded = $true
  foreach ($artifact in @($stageRoot, $rollbackRoot)) {
    try { Remove-AkashaInstallTree -Paths $Paths -Path $artifact } catch { $cleanupSucceeded = $false }
  }
  if ($null -ne $operationError) {
    if (-not $cleanupSucceeded) { $operationError.Exception.Data['AkashaCleanupFailure'] = 'E_INSTALL_CLEANUP' }
    throw $operationError
  }
  if (-not $cleanupSucceeded) { throw 'E_INSTALL_CLEANUP: Unable to remove installer transaction directories.' }
}

function New-AkashaShortcuts {
  param(
    [Parameter(Mandatory)][object[]]$Entries,
    [scriptblock]$Writer
  )

  if (@($Entries).Count -ne 3) { throw 'E_SHORTCUT_CREATE: Exactly three shortcuts are required.' }
  if ($null -eq $Writer) {
    $Writer = {
      param($path, $target, $workingDirectory)
      $shell = $null
      $shortcut = $null
      try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($path)
        $shortcut.TargetPath = $target
        $shortcut.WorkingDirectory = $workingDirectory
        $shortcut.Save()
      } finally {
        if ($null -ne $shortcut -and [System.Runtime.InteropServices.Marshal]::IsComObject($shortcut)) {
          [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        if ($null -ne $shell -and [System.Runtime.InteropServices.Marshal]::IsComObject($shell)) {
          [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
      }
    }
  }

  $records = New-Object System.Collections.Generic.List[object]
  $transactionId = [guid]::NewGuid().ToString('N')
  $directoryContext = $null
  $operationError = $null
  $rollbackSucceeded = $true
  try {
    $firstDirectory = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath([string]$Entries[0].Path))
    $directoryContext = Open-AkashaLifecycleRootContext -Root $firstDirectory
    $seen = @{}
    for ($index = 0; $index -lt $Entries.Count; $index++) {
      $entry = $Entries[$index]
      $final = [System.IO.Path]::GetFullPath([string]$entry.Path)
      $directory = [System.IO.Path]::GetDirectoryName($final)
      if (-not $directory.Equals($firstDirectory, [System.StringComparison]::OrdinalIgnoreCase) -or
          [System.IO.Path]::GetExtension($final) -ine '.lnk' -or $seen.ContainsKey($final)) {
        throw 'invalid shortcut destination'
      }
      $seen[$final] = $true
      if (Test-Path -LiteralPath $final) {
        $finalItem = Get-Item -LiteralPath $final -Force -ErrorAction Stop
        if ($finalItem.PSIsContainer -or ($finalItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
          throw 'unsafe existing shortcut'
        }
      }
      $stage = Join-Path $directory ('.akasha-shortcut-' + $transactionId + '-' + $index + '.tmp.lnk')
      $backup = Join-Path $directory ('.akasha-shortcut-' + $transactionId + '-' + $index + '.backup.lnk')
      $records.Add([pscustomobject]@{ Entry = $entry; Final = $final; Stage = $stage; Backup = $backup; OriginalMoved = $false; Committed = $false })
    }

    foreach ($record in $records) {
      & $Writer $record.Stage ([string]$record.Entry.Target) ([string]$record.Entry.WorkingDirectory)
      if (-not (Test-AkashaCanonicalFile -Root $firstDirectory -Path $record.Stage)) { throw 'shortcut staging failed' }
    }
    foreach ($record in $records) {
      if (Test-Path -LiteralPath $record.Final) {
        Move-Item -LiteralPath $record.Final -Destination $record.Backup -Force -ErrorAction Stop
        $record.OriginalMoved = $true
      }
    }
    foreach ($record in $records) {
      Move-Item -LiteralPath $record.Stage -Destination $record.Final -Force -ErrorAction Stop
      $record.Committed = $true
      if (-not (Test-AkashaCanonicalFile -Root $firstDirectory -Path $record.Final)) { throw 'shortcut commit failed' }
    }
  } catch {
    $operationError = $_
  }

  if ($null -ne $operationError) {
    for ($index = $records.Count - 1; $index -ge 0; $index--) {
      $record = $records[$index]
      try {
        if ($record.Committed -and (Test-Path -LiteralPath $record.Final)) { Remove-Item -LiteralPath $record.Final -Force -ErrorAction Stop }
        if ($record.OriginalMoved -and (Test-Path -LiteralPath $record.Backup)) { Move-Item -LiteralPath $record.Backup -Destination $record.Final -Force -ErrorAction Stop }
      } catch { $rollbackSucceeded = $false }
    }
    if (-not $rollbackSucceeded) { $operationError.Exception.Data['AkashaRollbackFailure'] = 'E_SHORTCUT_ROLLBACK' }
  }

  $cleanupSucceeded = $true
  foreach ($record in $records) {
    foreach ($artifact in @($record.Stage, $record.Backup)) {
      if ($artifact -ceq $record.Backup -and -not $rollbackSucceeded -and $record.OriginalMoved) { continue }
      try { if (Test-Path -LiteralPath $artifact) { Remove-Item -LiteralPath $artifact -Force -ErrorAction Stop } } catch { $cleanupSucceeded = $false }
    }
  }
  Close-AkashaLifecycleRootContext -Context $directoryContext
  if ($null -ne $operationError) {
    $shortcutError = New-Object System.Exception('E_SHORTCUT_CREATE: Desktop shortcuts could not be created.')
    if (-not $rollbackSucceeded) { $shortcutError.Data['AkashaRollbackFailure'] = 'E_SHORTCUT_ROLLBACK' }
    if (-not $cleanupSucceeded) { $shortcutError.Data['AkashaCleanupFailure'] = 'E_SHORTCUT_CLEANUP' }
    throw $shortcutError
  }
  if (-not $cleanupSucceeded) { throw 'E_SHORTCUT_CREATE: Desktop shortcut cleanup failed.' }
}

function Get-AkashaInstallErrorCode {
  param($ErrorRecord)

  $message = if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) { [string]$ErrorRecord.Exception.Message } else { [string]$ErrorRecord }
  $match = [regex]::Match($message, '\bE_[A-Z0-9_]+\b')
  if ($match.Success) { return $match.Value }
  return 'E_INSTALL_FAILED'
}

function Wait-AkashaInstallHealth {
  param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory)][scriptblock]$HealthChecker,
    [ValidateRange(1, 3600000)][int]$TimeoutMilliseconds = 90000,
    [ValidateRange(1, 60000)][int]$RetryDelayMilliseconds = 2000,
    [scriptblock]$Sleeper,
    [scriptblock]$MonotonicMillisecondsReader
  )

  if ($null -eq $Sleeper) {
    $Sleeper = { param($milliseconds) Start-Sleep -Milliseconds $milliseconds }
  }
  if ($null -eq $MonotonicMillisecondsReader) {
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $MonotonicMillisecondsReader = { return [long]$stopwatch.ElapsedMilliseconds }.GetNewClosure()
  }

  $attempts = 0
  try {
    $startedAt = [long](& $MonotonicMillisecondsReader)
  } catch {
    return [pscustomobject]@{ ExitCode = 1; Attempts = $attempts }
  }
  $deadline = $startedAt + [long]$TimeoutMilliseconds
  $maximumAttempts = [int][System.Math]::Ceiling([double]$TimeoutMilliseconds / [double]$RetryDelayMilliseconds) + 1

  while ($attempts -lt $maximumAttempts) {
    $attempts++
    try {
      $healthCode = [int](& $HealthChecker $InstallRoot)
    } catch {
      $healthCode = 1
    }
    try {
      $now = [long](& $MonotonicMillisecondsReader)
    } catch {
      break
    }
    if ($healthCode -eq 0 -and $now -le $deadline) {
      return [pscustomobject]@{ ExitCode = 0; Attempts = $attempts }
    }
    if ($now -ge $deadline -or $attempts -ge $maximumAttempts) { break }
    $delay = [int][System.Math]::Min([long]$RetryDelayMilliseconds, $deadline - $now)
    if ($delay -le 0) { break }
    try {
      & $Sleeper $delay
    } catch {
      break
    }
  }

  return [pscustomobject]@{ ExitCode = 1; Attempts = $attempts }
}

function Invoke-AkashaInstall {
  param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'),
    [string]$SourceRoot = $script:AkashaInstallDefaultSourceRoot,
    [string]$WeFlowInstallerPath = '',
    [string]$WeFlowConfigPath = '',
    [switch]$SkipStart,
    [scriptblock]$PrerequisiteValidator,
    [scriptblock]$WeFlowDiscovery,
    [scriptblock]$InstallerSelector,
    [scriptblock]$PackageRunner,
    [scriptblock]$EnvironmentInitializer,
    [scriptblock]$ConfigurationInitializer,
    [scriptblock]$ShortcutCreator,
    [scriptblock]$ServiceStarter,
    [scriptblock]$HealthChecker,
    [ValidateRange(1, 3600000)][int]$HealthReadyTimeoutMilliseconds = 90000,
    [ValidateRange(1, 60000)][int]$HealthRetryDelayMilliseconds = 2000,
    [scriptblock]$HealthRetrySleeper,
    [scriptblock]$HealthMonotonicMillisecondsReader,
    [scriptblock]$CalibrationStatusReader,
    [scriptblock]$ReplacementHook
  )

  if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = $script:AkashaInstallDefaultSourceRoot }
  if ([string]::IsNullOrWhiteSpace($WeFlowConfigPath)) { $WeFlowConfigPath = Join-Path $env:APPDATA 'weflow\WeFlow-config.json' }
  if ($null -eq $PrerequisiteValidator) { $PrerequisiteValidator = { param($p) Invoke-AkashaInstallPrerequisites -Paths $p } }
  if ($null -eq $WeFlowDiscovery) { $WeFlowDiscovery = { Get-WeFlowExecutable } }
  if ($null -eq $InstallerSelector) { $InstallerSelector = { Select-AkashaWeFlowInstaller } }
  if ($null -eq $PackageRunner) { $PackageRunner = { param($kind, $path) Invoke-AkashaWeFlowPackage -Kind $kind -Path $path } }
  if ($null -eq $EnvironmentInitializer) { $EnvironmentInitializer = { param($p, $python) Initialize-AkashaEnvironments -Paths $p -Python $python } }
  if ($null -eq $ConfigurationInitializer) { $ConfigurationInitializer = { param($p, $config) Initialize-AkashaConfiguration -Paths $p -WeFlowConfigPath $config } }
  if ($null -eq $ShortcutCreator) { $ShortcutCreator = { param($entries) New-AkashaShortcuts -Entries $entries } }
  if ($null -eq $ServiceStarter) { $ServiceStarter = { param($root) Start-AkashaServices -InstallRoot $root | Out-Null } }
  if ($null -eq $HealthChecker) { $HealthChecker = { param($root) Invoke-AkashaHealthCheck -InstallRoot $root } }
  if ($null -eq $CalibrationStatusReader) { $CalibrationStatusReader = { param($configPath) Get-AkashaUiaCalibrationStatus -ConfigPath $configPath } }

  $payload = @(Get-AkashaInstallPayload)
  $sourceContext = Open-AkashaLifecycleRootContext -Root $SourceRoot
  $rootContext = $null
  $stateContext = $null
  $logContext = $null
  $lockStream = $null
  $paths = $null
  $version = ''
  $startAfterUnlock = $false
  $calibrationRequired = $false
  $installStateActive = $false
  try {
    $version = Assert-AkashaInstallSource -Root $sourceContext.RootPath -Payload $payload
    $sourcePath = [System.IO.Path]::GetFullPath($sourceContext.RootPath).TrimEnd('\', '/')
    $installPath = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\', '/')
    $sourcePrefix = $sourcePath + [System.IO.Path]::DirectorySeparatorChar
    $installPrefix = $installPath + [System.IO.Path]::DirectorySeparatorChar
    if ($sourcePath.Equals($installPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $sourcePath.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        $installPath.StartsWith($sourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'E_INSTALL_ROOT: Source and install roots must be separate.'
    }

    $rootContext = Open-AkashaLifecycleRootContext -Root $installPath -CreateIfMissing
    $paths = Get-AkashaBotPaths -Root $rootContext.RootPath
    Assert-AkashaInstallPaths -Paths $paths
    New-AkashaInstallDirectory -Paths $paths -Path $paths.State
    $stateContext = Open-AkashaLifecycleRootContext -Root $paths.State
    $lockStream = Open-AkashaInstallLifecycleLock -Paths $paths
    Assert-AkashaInstallPaths -Paths $paths
    Assert-AkashaInstallProcessState -Paths $paths

    New-AkashaInstallDirectory -Paths $paths -Path $paths.Logs
    $logContext = Open-AkashaLifecycleRootContext -Root $paths.Logs
    Write-AkashaInstallLog -Paths $paths -Message 'phase=install status=started'
    Write-AkashaInstallState -Paths $paths -Status 'installing' -Version $version
    $installStateActive = $true

    $python = & $PrerequisiteValidator $paths
    $weFlowExecutable = @(& $WeFlowDiscovery) | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace([string]$weFlowExecutable)) {
      $selected = $WeFlowInstallerPath
      if ([string]::IsNullOrWhiteSpace($selected)) { $selected = & $InstallerSelector }
      if ([string]::IsNullOrWhiteSpace([string]$selected)) { throw 'E_WEFLOW_CANCELLED: WeFlow installer selection was cancelled.' }
      $selected = Assert-AkashaWeFlowInstaller -Path ([string]$selected)
      $kind = if ([System.IO.Path]::GetExtension($selected) -ieq '.msi') { 'msi' } else { 'exe' }
      $exitCode = [int](& $PackageRunner $kind $selected)
      if ($exitCode -ne 0) { throw 'E_WEFLOW_INSTALL_FAILED: WeFlow installer returned a non-zero exit code.' }
      $weFlowExecutable = @(& $WeFlowDiscovery) | Select-Object -First 1
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$weFlowExecutable) -and
        -not (Test-AkashaCanonicalExternalExecutable -Path ([string]$weFlowExecutable))) {
      throw 'E_WEFLOW_EXE: Discovered WeFlow executable is unsafe or missing.'
    }

    foreach ($directory in @($paths.App, $paths.Runtime, $paths.Data, $paths.Backups)) {
      New-AkashaInstallDirectory -Paths $paths -Path $directory
    }
    Write-AkashaInstallLog -Paths $paths -Message 'phase=payload status=started'
    Install-AkashaPayload -SourceRoot $sourceContext.RootPath -Paths $paths -Payload $payload -ReplacementHook $ReplacementHook
    Write-AkashaInstallLog -Paths $paths -Message 'phase=payload status=completed'
    & $EnvironmentInitializer $paths $python
    Write-AkashaInstallLog -Paths $paths -Message 'phase=environments status=completed'

    if ([string]::IsNullOrWhiteSpace([string]$weFlowExecutable)) {
      Write-AkashaInstallState -Paths $paths -Status 'weflow_pending' -Version $version -ErrorCode 'E_WEFLOW_NOT_DETECTED'
      Write-AkashaInstallLog -Paths $paths -Level 'warn' -Message 'phase=weflow status=pending code=E_WEFLOW_NOT_DETECTED'
      throw 'E_WEFLOW_NOT_DETECTED: Finish WeFlow installation and run the installer again.'
    }
    Assert-AkashaInstallPaths -Paths $paths
    $weFlowPathBytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes([System.IO.Path]::GetFullPath([string]$weFlowExecutable))
    Write-AkashaInstallBytesAtomic -Paths $paths -Path $paths.WeFlowPathState -Bytes $weFlowPathBytes

    if (-not (Test-Path -LiteralPath $WeFlowConfigPath -PathType Leaf)) {
      Write-AkashaInstallState -Paths $paths -Status 'weflow_config_pending' -Version $version -ErrorCode 'E_WEFLOW_CONFIG_MISSING'
      Write-AkashaInstallLog -Paths $paths -Level 'warn' -Message 'phase=configuration status=pending code=E_WEFLOW_CONFIG_MISSING'
      throw 'E_WEFLOW_CONFIG_MISSING: Complete WeFlow first-run configuration and run the installer again.'
    }
    try {
      & $ConfigurationInitializer $paths $WeFlowConfigPath
    } catch {
      if ($_.Exception.Message -like 'E_WEFLOW_CONFIG_MISSING:*') {
        Write-AkashaInstallState -Paths $paths -Status 'weflow_config_pending' -Version $version -ErrorCode 'E_WEFLOW_CONFIG_MISSING'
        Write-AkashaInstallLog -Paths $paths -Level 'warn' -Message 'phase=configuration status=pending code=E_WEFLOW_CONFIG_MISSING'
      }
      throw
    }
    Write-AkashaInstallLog -Paths $paths -Message 'phase=configuration status=completed'

    $launchers = Get-AkashaLauncherNames
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutEntries = @(
      [pscustomobject]@{ Path = Join-Path $desktop ((ConvertFrom-AkashaCodePoints @(0x542F, 0x52A8)) + ' AkashaBot.lnk'); Target = Join-Path $paths.Root $launchers.Start; WorkingDirectory = $paths.Root },
      [pscustomobject]@{ Path = Join-Path $desktop ((ConvertFrom-AkashaCodePoints @(0x505C, 0x6B62)) + ' AkashaBot.lnk'); Target = Join-Path $paths.Root $launchers.Stop; WorkingDirectory = $paths.Root },
      [pscustomobject]@{ Path = Join-Path $desktop ((ConvertFrom-AkashaCodePoints @(0x68C0, 0x67E5)) + ' AkashaBot.lnk'); Target = Join-Path $paths.Root $launchers.Health; WorkingDirectory = $paths.Root }
    )
    & $ShortcutCreator $shortcutEntries
    $calibrationStatusValues = @(& $CalibrationStatusReader $paths.BridgeConfig)
    if ($calibrationStatusValues.Count -ne 1 -or $calibrationStatusValues[0] -isnot [string] -or
        @('required', 'invalid', 'ready') -cnotcontains [string]$calibrationStatusValues[0]) {
      throw 'E_INSTALL_FAILED: Calibration status reader returned an unsupported value.'
    }
    $calibrationStatus = [string]$calibrationStatusValues[0]
    $calibrationRequired = $calibrationStatus -cne 'ready'
    $calibrationRequiredText = if ($calibrationRequired) { 'true' } else { 'false' }
    Write-AkashaInstallLog -Paths $paths -Message ('phase=calibration calibration_required=' + $calibrationRequiredText)
    Write-AkashaInstallState -Paths $paths -Status 'installed' -Version $version
    Write-AkashaInstallLog -Paths $paths -Message ('phase=install status=completed version=' + $version)
    $startAfterUnlock = (-not $SkipStart) -and (-not $calibrationRequired)
  } catch {
    $code = Get-AkashaInstallErrorCode -ErrorRecord $_
    if ($installStateActive -and @('E_WEFLOW_NOT_DETECTED', 'E_WEFLOW_CONFIG_MISSING') -cnotcontains $code) {
      try { Write-AkashaInstallState -Paths $paths -Status 'failed' -Version $version -ErrorCode $code } catch { }
    }
    if ($null -ne $paths -and (Test-Path -LiteralPath $paths.Logs -PathType Container)) {
      try { Write-AkashaInstallLog -Paths $paths -Level 'error' -Message ('phase=install status=failed code=' + $code) } catch { }
    }
    throw
  } finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
    Close-AkashaLifecycleRootContext -Context $logContext
    Close-AkashaLifecycleRootContext -Context $stateContext
    Close-AkashaLifecycleRootContext -Context $rootContext
    Close-AkashaLifecycleRootContext -Context $sourceContext
  }

  if ($startAfterUnlock) {
    & $ServiceStarter $paths.Root
    Write-AkashaInstallLog -Paths $paths -Message ('phase=health status=started timeout_ms=' + $HealthReadyTimeoutMilliseconds + ' interval_ms=' + $HealthRetryDelayMilliseconds)
    $healthResult = Wait-AkashaInstallHealth -InstallRoot $paths.Root -HealthChecker $HealthChecker -TimeoutMilliseconds $HealthReadyTimeoutMilliseconds -RetryDelayMilliseconds $HealthRetryDelayMilliseconds -Sleeper $HealthRetrySleeper -MonotonicMillisecondsReader $HealthMonotonicMillisecondsReader
    if ([int]$healthResult.ExitCode -ne 0) {
      Write-AkashaInstallLog -Paths $paths -Level 'error' -Message ('phase=health status=failed attempts=' + [int]$healthResult.Attempts + ' code=E_HEALTH_FAILED')
      throw 'E_HEALTH_FAILED: One or more services failed the aggregate health check.'
    }
    Write-AkashaInstallLog -Paths $paths -Message ('phase=health status=completed attempts=' + [int]$healthResult.Attempts)
  }
  return [pscustomobject]@{
    Status = 'installed'
    Version = $version
    InstallRoot = $paths.Root
    Started = $startAfterUnlock
    CalibrationRequired = $calibrationRequired
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  try {
    $installResult = Invoke-AkashaInstall -InstallRoot $InstallRoot -SourceRoot $SourceRoot -WeFlowInstallerPath $WeFlowInstallerPath -WeFlowConfigPath $WeFlowConfigPath -SkipStart:$SkipStart
    if ($installResult.CalibrationRequired) {
      $launchers = Get-AkashaLauncherNames
      Write-Host ('Calibration is required before startup. Run "' + $launchers.Calibrate + '" from the install directory.')
    }
    exit 0
  } catch {
    [Console]::Error.WriteLine((Get-AkashaInstallErrorCode -ErrorRecord $_))
    exit 1
  }
}
