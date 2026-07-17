[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -Force

if ($null -eq ('AkashaBotNativePathV1' -as [type])) {
  Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

public static class AkashaBotNativePathV1 {
    private const uint FILE_READ_ATTRIBUTES = 0x80;
    private const uint FILE_SHARE_READ = 0x1;
    private const uint FILE_SHARE_WRITE = 0x2;
    private const uint FILE_SHARE_DELETE = 0x4;
    private const uint OPEN_EXISTING = 3;
    private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
        uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile
    );

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path, uint pathLength, uint flags);

    public static string GetFinalPath(string path, bool directory) {
        uint flags = directory ? FILE_FLAG_BACKUP_SEMANTICS : 0;
        using (SafeFileHandle handle = CreateFile(
            path, FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            IntPtr.Zero, OPEN_EXISTING, flags, IntPtr.Zero)) {
            if (handle.IsInvalid) return null;
            return GetFinalPathFromHandle(handle.DangerousGetHandle());
        }
    }

    public static string GetFinalPathFromHandle(IntPtr handle) {
        StringBuilder buffer = new StringBuilder(32768);
        uint length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity) return null;
        string value = buffer.ToString();
        if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + value.Substring(8);
        if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) return value.Substring(4);
        return value;
    }
}

public sealed class AkashaBotNativeDirectoryLeaseV1 : IDisposable {
    private const uint FILE_READ_ATTRIBUTES = 0x80;
    private const uint FILE_SHARE_READ = 0x1;
    private const uint FILE_SHARE_WRITE = 0x2;
    private const uint OPEN_EXISTING = 3;
    private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
    private SafeFileHandle handle;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
        uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile
    );

    private AkashaBotNativeDirectoryLeaseV1(SafeFileHandle directoryHandle) {
        handle = directoryHandle;
    }

    public static AkashaBotNativeDirectoryLeaseV1 TryOpen(string path) {
        SafeFileHandle directoryHandle = CreateFile(
            path, FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE,
            IntPtr.Zero, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, IntPtr.Zero
        );
        if (directoryHandle.IsInvalid) {
            directoryHandle.Dispose();
            return null;
        }
        return new AkashaBotNativeDirectoryLeaseV1(directoryHandle);
    }

    public string FinalPath {
        get { return AkashaBotNativePathV1.GetFinalPathFromHandle(handle.DangerousGetHandle()); }
    }

    public void Dispose() {
        if (handle != null) {
            handle.Dispose();
            handle = null;
        }
        GC.SuppressFinalize(this);
    }
}
'@
}

function Close-AkashaLifecycleRootContext {
  param($Context)

  if ($null -eq $Context) { return }
  for ($index = $Context.Leases.Count - 1; $index -ge 0; $index--) {
    try { $Context.Leases[$index].Dispose() } catch { }
  }
}

function Open-AkashaLifecycleRootContext {
  param(
    [Parameter(Mandatory)][string]$Root,
    [switch]$CreateIfMissing
  )

  $leases = New-Object System.Collections.Generic.List[object]
  try {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $volumeRoot = [System.IO.Path]::GetPathRoot($rootPath).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($rootPath) -or $rootPath.Equals($volumeRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'invalid lifecycle root'
    }

    $missing = New-Object System.Collections.Generic.List[string]
    $nearestExisting = $rootPath
    while (-not (Test-Path -LiteralPath $nearestExisting)) {
      $missing.Insert(0, $nearestExisting)
      $parent = [System.IO.Path]::GetDirectoryName($nearestExisting)
      if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $nearestExisting) { throw 'missing lifecycle ancestor' }
      $nearestExisting = [System.IO.Path]::GetFullPath($parent).TrimEnd('\', '/')
    }

    $ancestor = $nearestExisting
    while (-not [string]::IsNullOrWhiteSpace($ancestor)) {
      $item = Get-Item -LiteralPath $ancestor -Force -ErrorAction Stop
      if (-not $item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'reparse lifecycle ancestor'
      }
      $parent = [System.IO.Path]::GetDirectoryName($ancestor)
      if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $ancestor) { break }
      $ancestor = [System.IO.Path]::GetFullPath($parent).TrimEnd('\', '/')
    }

    $nearestLease = [AkashaBotNativeDirectoryLeaseV1]::TryOpen($nearestExisting)
    if ($null -eq $nearestLease) { throw 'unable to pin lifecycle ancestor' }
    $nearestFinal = $nearestLease.FinalPath
    if ([string]::IsNullOrWhiteSpace($nearestFinal) -or
        -not ([System.IO.Path]::GetFullPath($nearestFinal).TrimEnd('\', '/')).Equals($nearestExisting, [System.StringComparison]::OrdinalIgnoreCase)) {
      $nearestLease.Dispose()
      throw 'lifecycle ancestor resolved elsewhere'
    }
    $leases.Add($nearestLease)

    if ($missing.Count -gt 0 -and -not $CreateIfMissing) {
      return [pscustomobject]@{ RootPath = $rootPath; RootExists = $false; Leases = $leases }
    }

    foreach ($component in $missing) {
      [void][System.IO.Directory]::CreateDirectory($component)
      $componentItem = Get-Item -LiteralPath $component -Force -ErrorAction Stop
      if (-not $componentItem.PSIsContainer -or ($componentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'unsafe created lifecycle directory'
      }
      $componentLease = [AkashaBotNativeDirectoryLeaseV1]::TryOpen($component)
      if ($null -eq $componentLease) { throw 'unable to pin created lifecycle directory' }
      $componentFinal = $componentLease.FinalPath
      if ([string]::IsNullOrWhiteSpace($componentFinal) -or
          -not ([System.IO.Path]::GetFullPath($componentFinal).TrimEnd('\', '/')).Equals($component, [System.StringComparison]::OrdinalIgnoreCase)) {
        $componentLease.Dispose()
        throw 'created lifecycle directory resolved elsewhere'
      }
      $leases.Add($componentLease)
    }

    return [pscustomobject]@{ RootPath = $rootPath; RootExists = $true; Leases = $leases }
  } catch {
    Close-AkashaLifecycleRootContext -Context ([pscustomobject]@{ Leases = $leases })
    throw 'E_LIFECYCLE_PATH: Install root and its ancestors must be canonical directories without reparse points.'
  }
}

function Test-AkashaLifecycleInternalPath {
  param(
    [Parameter(Mandatory)][string]$Root,
    [Parameter(Mandatory)][string]$Candidate
  )

  try {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
  } catch {
    return $false
  }
  if (Test-Path -LiteralPath $rootPath) {
    try { $rootItem = Get-Item -LiteralPath $rootPath -Force -ErrorAction Stop } catch { return $false }
    if ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
  }
  if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
  $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
  if (-not $candidatePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }

  $current = $candidatePath
  while (-not $current.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    if (Test-Path -LiteralPath $current) {
      try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop } catch { return $false }
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
    }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $current) { return $false }
    $current = [System.IO.Path]::GetFullPath($parent).TrimEnd('\', '/')
  }
  return $true
}

function Test-AkashaCanonicalExternalExecutable {
  param([Parameter(Mandatory)][string]$Path)

  try { $expected = [System.IO.Path]::GetFullPath($Path) } catch { return $false }
  if ([System.IO.Path]::GetExtension($expected) -ine '.exe' -or -not (Test-Path -LiteralPath $expected -PathType Leaf)) { return $false }
  $current = $expected
  while (-not [string]::IsNullOrWhiteSpace($current)) {
    if (Test-Path -LiteralPath $current) {
      try { $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop } catch { return $false }
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
    }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $current) { break }
    $current = $parent
  }
  $final = [AkashaBotNativePathV1]::GetFinalPath($expected, $false)
  if ([string]::IsNullOrWhiteSpace($final)) { return $false }
  try { $final = [System.IO.Path]::GetFullPath($final) } catch { return $false }
  return $expected.Equals($final, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-AkashaLifecyclePathBoundary {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string[]]$Candidates
  )

  foreach ($candidate in $Candidates) {
    if (-not (Test-AkashaLifecycleInternalPath -Root $Paths.Root -Candidate $candidate)) {
      throw 'E_LIFECYCLE_PATH: Product paths must remain inside the install root without reparse points.'
    }
  }
}

function Get-AkashaLifecyclePreflight {
  param([Parameter(Mandatory)]$Paths)

  $bridgeMain = Join-Path $Paths.Bridge 'main.py'
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @(
      $Paths.State,
      $Paths.Logs,
      $Paths.ProcessState,
      $Paths.WeFlowPathState,
      $Paths.Bridge,
      $Paths.BridgePython,
      $Paths.BridgeConfig,
      $bridgeMain,
      $Paths.AstrBotPython,
      $Paths.AstrBotData
    )

  foreach ($requiredFile in @($Paths.BridgePython, $Paths.AstrBotPython, $Paths.BridgeConfig, $bridgeMain)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
      throw "E_NOT_INSTALLED: Missing required file: $requiredFile"
    }
  }
  if (-not (Test-Path -LiteralPath $Paths.AstrBotData -PathType Container)) {
    throw "E_NOT_INSTALLED: Missing AstrBot data directory: $($Paths.AstrBotData)"
  }
  if (-not (Test-Path -LiteralPath $Paths.WeFlowPathState -PathType Leaf)) {
    throw 'E_WEFLOW_EXE: Recorded WeFlow executable path is missing.'
  }
  try {
    $weFlowPath = (Get-Content -LiteralPath $Paths.WeFlowPathState -Raw -Encoding UTF8 -ErrorAction Stop).Trim()
    $weFlowPath = [System.IO.Path]::GetFullPath($weFlowPath)
  } catch {
    throw 'E_WEFLOW_EXE: Recorded WeFlow executable path is invalid.'
  }
  if ([string]::IsNullOrWhiteSpace($weFlowPath) -or -not (Test-AkashaCanonicalExternalExecutable -Path $weFlowPath)) {
    throw 'E_WEFLOW_EXE: Recorded WeFlow executable is missing.'
  }
  return [pscustomobject]@{ BridgeMain = $bridgeMain; WeFlowExecutable = $weFlowPath }
}

function Assert-AkashaLaunchBoundary {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$ExpectedWeFlowExecutable
  )

  $current = Get-AkashaLifecyclePreflight -Paths $Paths
  if (-not $current.WeFlowExecutable.Equals($ExpectedWeFlowExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'E_LIFECYCLE_PATH: Lifecycle inputs changed during the operation.'
  }
}

function Write-AkashaLifecycleLog {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Message,
    [ValidateSet('debug', 'info', 'warn', 'error')][string]$Level = 'info'
  )

  $logPath = Join-Path $Paths.Logs 'launcher.log'
  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.Logs, [string]$logPath)
  $logStream = $null
  try {
    $line = '{0:o} [{1}] {2}' -f (Get-Date), $Level.ToUpperInvariant(), (Protect-AkashaLogText $Message)
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($line + [Environment]::NewLine)
    $logStream = New-Object System.IO.FileStream(
      $logPath,
      [System.IO.FileMode]::OpenOrCreate,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::Read
    )
    $logFinalPath = [AkashaBotNativePathV1]::GetFinalPathFromHandle($logStream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($logFinalPath) -or
        -not ([System.IO.Path]::GetFullPath($logFinalPath)).Equals([System.IO.Path]::GetFullPath($logPath), [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'log handle resolved outside expected path'
    }
    [void]$logStream.Seek(0, [System.IO.SeekOrigin]::End)
    $logStream.Write($bytes, 0, $bytes.Length)
    $logStream.Flush($true)
    Write-Host $line
  } catch {
    throw 'E_LIFECYCLE_LOG: Unable to write lifecycle log.'
  } finally {
    if ($null -ne $logStream) { $logStream.Dispose() }
  }
}

function Get-AkashaProcessIdentity {
  param([Parameter(Mandatory)][int]$ProcessId)

  $managed = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if ($null -eq $managed) { return $null }
  $native = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
  try {
    $startTime = $managed.StartTime.ToUniversalTime()
  } catch {
    throw 'E_PROCESS_STATE: Unable to verify recorded process start time.'
  }
  return [pscustomobject]@{
    Pid = $ProcessId
    ExecutablePath = $(if ($null -ne $native -and $native.ExecutablePath) { [string]$native.ExecutablePath } else { [string]$managed.Path })
    CommandLine = $(if ($null -ne $native) { [string]$native.CommandLine } else { '' })
    StartTimeUtc = $startTime
  }
}

function Test-AkashaCommandIdentity {
  param(
    [Parameter(Mandatory)][string]$CommandKind,
    [AllowEmptyString()][string]$CommandLine
  )

  switch ($CommandKind) {
    'WeFlowApp' { return $true }
    'AstrBotRun' {
      return $CommandLine -match '(?i)(?:^|\s)-m(?:\s+|\s*["''])astrbot\.cli\.__main__(?:["'']?\s+)run(?:\s|$)'
    }
    'BridgeMain' {
      return $CommandLine -match '(?i)(?:^|[\s"''])main\.py(?:["'']?(?:\s|$))'
    }
    default { return $false }
  }
}

function New-AkashaProcessRecord {
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
    [Parameter(Mandatory)][string]$ExecutablePath,
    [Parameter(Mandatory)][bool]$Owned,
    [Parameter(Mandatory)][string]$CommandKind
  )

  $Process.Refresh()
  return [pscustomobject][ordered]@{
    Name = $Name
    Pid = [int]$Process.Id
    ExecutablePath = [System.IO.Path]::GetFullPath($ExecutablePath)
    StartTimeUtc = $Process.StartTime.ToUniversalTime().ToString('o')
    Owned = $Owned
    CommandKind = $CommandKind
  }
}

function Assert-AkashaRecordSchema {
  param(
    [Parameter(Mandatory)]$Record,
    [Parameter(Mandatory)]$Paths,
    [AllowEmptyString()][string]$WeFlowExecutable = '',
    [Parameter(Mandatory)][hashtable]$SeenNames
  )

  if ($null -eq $Record -or $Record -isnot [psobject]) { throw 'E_PROCESS_STATE: Invalid process state.' }
  $allowedProperties = @('Name', 'Pid', 'ExecutablePath', 'StartTimeUtc', 'Owned', 'CommandKind')
  $actualProperties = @($Record.PSObject.Properties.Name)
  if ($actualProperties.Count -ne $allowedProperties.Count -or @($actualProperties | Where-Object { $allowedProperties -cnotcontains $_ }).Count -gt 0) {
    throw 'E_PROCESS_STATE: Invalid process state.'
  }
  foreach ($propertyName in $allowedProperties) {
    if ($null -eq $Record.PSObject.Properties[$propertyName]) { throw 'E_PROCESS_STATE: Invalid process state.' }
  }
  $name = [string]$Record.Name
  if (@('weflow', 'astrbot', 'bridge') -cnotcontains $name -or $SeenNames.ContainsKey($name)) {
    throw 'E_PROCESS_STATE: Invalid process state.'
  }
  $SeenNames[$name] = $true
  try { $pidValue = [int]$Record.Pid } catch { throw 'E_PROCESS_STATE: Invalid process state.' }
  if ($pidValue -le 0 -or [string]$Record.Pid -notmatch '^\d+$') { throw 'E_PROCESS_STATE: Invalid process state.' }
  if ($Record.Owned -isnot [bool]) { throw 'E_PROCESS_STATE: Invalid process state.' }

  $expected = switch ($name) {
    'weflow' { $(if ([string]::IsNullOrWhiteSpace($WeFlowExecutable)) { [string]$Record.ExecutablePath } else { $WeFlowExecutable }) }
    'astrbot' { $Paths.AstrBotPython }
    'bridge' { $Paths.BridgePython }
  }
  $commandKind = switch ($name) {
    'weflow' { 'WeFlowApp' }
    'astrbot' { 'AstrBotRun' }
    'bridge' { 'BridgeMain' }
  }
  try { $recordPath = [System.IO.Path]::GetFullPath([string]$Record.ExecutablePath) } catch { throw 'E_PROCESS_STATE: Invalid process state.' }
  if (($name -ceq 'weflow' -and [System.IO.Path]::GetExtension($recordPath) -ine '.exe') -or
      -not $recordPath.Equals([System.IO.Path]::GetFullPath($expected), [System.StringComparison]::OrdinalIgnoreCase) -or
      [string]$Record.CommandKind -cne $commandKind -or
      (-not [bool]$Record.Owned -and $name -cne 'weflow')) {
    throw 'E_PROCESS_STATE: Invalid process state.'
  }
  $parsedStart = [datetime]::MinValue
  if (-not [datetime]::TryParse(
      [string]$Record.StartTimeUtc,
      [System.Globalization.CultureInfo]::InvariantCulture,
      [System.Globalization.DateTimeStyles]::RoundtripKind,
      [ref]$parsedStart
    ) -or $parsedStart.Kind -ne [System.DateTimeKind]::Utc) {
    throw 'E_PROCESS_STATE: Invalid process state.'
  }
}

function Read-AkashaProcessState {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$Paths,
    [AllowEmptyString()][string]$WeFlowExecutable = ''
  )

  if (-not (Test-Path -LiteralPath $Path)) { return @() }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'E_PROCESS_STATE: Invalid process state.' }
  try { $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop } catch { throw 'E_PROCESS_STATE: Invalid process state.' }
  $trimmed = $raw.Trim()
  if (-not $trimmed.StartsWith('[') -or -not $trimmed.EndsWith(']')) { throw 'E_PROCESS_STATE: Invalid process state.' }
  try {
    $parsed = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
    if ($parsed -isnot [System.Array]) { throw 'invalid root' }
    $recordList = New-Object System.Collections.Generic.List[object]
    foreach ($item in $parsed) {
      if ($null -eq $item -or $item -is [System.Array]) { throw 'invalid record shape' }
      $recordList.Add($item)
    }
    $records = $recordList.ToArray()
  } catch { throw 'E_PROCESS_STATE: Invalid process state.' }
  $seenNames = @{}
  foreach ($record in $records) {
    Assert-AkashaRecordSchema -Record $record -Paths $Paths -WeFlowExecutable $WeFlowExecutable -SeenNames $seenNames
  }
  return $records
}

function Write-AkashaProcessState {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$Paths,
    [object[]]$Records
  )

  $temporary = $null
  $replacementBackup = $null
  try {
    Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$Path)
    if (@($Records).Count -eq 0) {
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$Path)
      if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force -ErrorAction Stop }
    } else {
      $targetPath = [System.IO.Path]::GetFullPath($Path)
      $directory = [System.IO.Path]::GetDirectoryName($targetPath)
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$directory, [string]$targetPath)
      New-Item -ItemType Directory -Force -Path $directory | Out-Null
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$directory, [string]$targetPath)
      $temporary = Join-Path $directory ('.' + [System.IO.Path]::GetFileName($targetPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
      $replacementBackup = $temporary + '.replace-backup'
      $jsonItems = @($Records | ForEach-Object { $_ | ConvertTo-Json -Depth 16 -Compress })
      $json = '[' + ($jsonItems -join ',') + ']'
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$temporary)
      $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
      $temporaryStream = New-Object System.IO.FileStream(
        $temporary,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
      )
      try {
        $temporaryFinalPath = [AkashaBotNativePathV1]::GetFinalPathFromHandle($temporaryStream.SafeFileHandle.DangerousGetHandle())
        if ([string]::IsNullOrWhiteSpace($temporaryFinalPath) -or
            -not ([System.IO.Path]::GetFullPath($temporaryFinalPath)).Equals([System.IO.Path]::GetFullPath($temporary), [System.StringComparison]::OrdinalIgnoreCase)) {
          throw 'state temp handle resolved outside expected path'
        }
        $temporaryStream.Write($bytes, 0, $bytes.Length)
        $temporaryStream.Flush($true)
      } finally {
        $temporaryStream.Dispose()
      }
      Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$temporary, [string]$targetPath)
      if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
        Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$temporary, [string]$targetPath, [string]$replacementBackup)
        [System.IO.File]::Replace($temporary, $targetPath, $replacementBackup)
      } else {
        Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @([string]$Paths.State, [string]$temporary, [string]$targetPath)
        [System.IO.File]::Move($temporary, $targetPath)
      }
    }
  } catch {
    throw 'E_PROCESS_STATE_WRITE: Unable to persist process state.'
  } finally {
    foreach ($artifact in @($temporary, $replacementBackup)) {
      if (-not [string]::IsNullOrWhiteSpace($artifact) -and
          (Test-AkashaLifecycleInternalPath -Root $Paths.Root -Candidate $artifact) -and
          (Test-Path -LiteralPath $artifact)) {
        Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

function Test-AkashaRecordMatchesLiveProcess {
  param([Parameter(Mandatory)]$Record, [Parameter(Mandatory)]$Identity)

  if ([string]::IsNullOrWhiteSpace([string]$Identity.ExecutablePath)) { return $false }
  try {
    $actualPath = [System.IO.Path]::GetFullPath([string]$Identity.ExecutablePath)
    $recordPath = [System.IO.Path]::GetFullPath([string]$Record.ExecutablePath)
    $recordStart = [datetime]::Parse([string]$Record.StartTimeUtc, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
  } catch {
    return $false
  }
  $commandMatches = if ([string]::IsNullOrWhiteSpace([string]$Identity.CommandLine)) {
    [bool]$Record.Owned -or [string]$Record.CommandKind -ceq 'WeFlowApp'
  } else {
    Test-AkashaCommandIdentity -CommandKind ([string]$Record.CommandKind) -CommandLine ([string]$Identity.CommandLine)
  }
  return $actualPath.Equals($recordPath, [System.StringComparison]::OrdinalIgnoreCase) -and
    $Identity.StartTimeUtc.Ticks -eq $recordStart.Ticks -and $commandMatches
}

function Find-AkashaExternalWeFlow {
  param([Parameter(Mandatory)][string]$ExecutablePath)

  $expected = [System.IO.Path]::GetFullPath($ExecutablePath)
  foreach ($native in @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'WeFlow.exe'" -ErrorAction SilentlyContinue)) {
    if ([string]::IsNullOrWhiteSpace([string]$native.ExecutablePath)) { continue }
    try { $actual = [System.IO.Path]::GetFullPath([string]$native.ExecutablePath) } catch { continue }
    if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
    $process = Get-Process -Id ([int]$native.ProcessId) -ErrorAction SilentlyContinue
    if ($null -ne $process) { return $process }
  }
  $processName = [System.IO.Path]::GetFileNameWithoutExtension($expected)
  foreach ($process in @(Get-Process -Name $processName -ErrorAction SilentlyContinue)) {
    try { $actual = [System.IO.Path]::GetFullPath([string]$process.Path) } catch { continue }
    if ($actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) { return $process }
  }
  return $null
}

function Start-AkashaOwnedProcess {
  param(
    [Parameter(Mandatory)][string]$FilePath,
    [string[]]$ArgumentList = @(),
    [string]$WorkingDirectory,
    [hashtable]$EnvironmentOverrides = @{}
  )

  foreach ($argument in @($ArgumentList)) {
    if ([string]$argument -notmatch '^[A-Za-z0-9_.-]+$') { throw 'E_SERVICE_START: Invalid service argument.' }
  }
  $excluded = @(
    'AKASHABOT_CONFIG_PATH',
    'AKASHABOT_LOG_DIR',
    'AKASHABOT_STATE_DIR',
    'ASTRBOT_DASHBOARD_INITIAL_PASSWORD',
    'PYTHONHOME',
    'PYTHONPATH',
    'PYTHONUSERBASE',
    'PYTHONNOUSERSITE',
    'VIRTUAL_ENV',
    'VIRTUAL_ENV_PROMPT',
    '__PYVENV_LAUNCHER__'
  )
  $childEnvironment = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([System.StringComparer]::OrdinalIgnoreCase)
  $environmentTable = [Environment]::GetEnvironmentVariables()
  foreach ($item in $environmentTable.GetEnumerator()) {
    if ($excluded -contains [string]$item.Key) { continue }
    $childEnvironment[[string]$item.Key] = [string]$item.Value
  }
  foreach ($name in $EnvironmentOverrides.Keys) {
    if (@('AKASHABOT_CONFIG_PATH', 'AKASHABOT_LOG_DIR', 'AKASHABOT_STATE_DIR', 'PYTHONNOUSERSITE') -notcontains [string]$name) {
      throw 'E_SERVICE_START: Invalid service environment override.'
    }
    $childEnvironment[[string]$name] = [string]$EnvironmentOverrides[$name]
  }

  $startInfo = New-Object System.Diagnostics.ProcessStartInfo
  $startInfo.FileName = $FilePath
  $startInfo.Arguments = @($ArgumentList) -join ' '
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $environmentField = [System.Diagnostics.ProcessStartInfo].GetField(
    'environment',
    [System.Reflection.BindingFlags]::Instance -bor [System.Reflection.BindingFlags]::NonPublic
  )
  if ($null -eq $environmentField) {
    throw 'E_SERVICE_START: Unable to construct a safe child environment.'
  }
  $environmentField.SetValue($startInfo, $childEnvironment)
  $legacyEnvironmentField = [System.Diagnostics.ProcessStartInfo].GetField(
    'environmentVariables',
    [System.Reflection.BindingFlags]::Instance -bor [System.Reflection.BindingFlags]::NonPublic
  )
  if ($null -eq $legacyEnvironmentField) {
    throw 'E_SERVICE_START: Unable to construct a safe child environment.'
  }
  $legacyEnvironment = New-Object System.Collections.Specialized.StringDictionary
  foreach ($name in $childEnvironment.Keys) {
    $legacyEnvironment[[string]$name] = [string]$childEnvironment[$name]
  }
  $legacyEnvironmentField.SetValue($startInfo, $legacyEnvironment)
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) { throw 'start returned false' }
    return $process
  } catch {
    $process.Dispose()
    throw 'E_SERVICE_START: Unable to start a product service.'
  }
}

function Assert-AkashaProcessStayedRunning {
  param([Parameter(Mandatory)][System.Diagnostics.Process]$Process, [Parameter(Mandatory)][string]$Name)

  Start-Sleep -Milliseconds 300
  $Process.Refresh()
  if ($Process.HasExited) { throw "E_SERVICE_EXITED: $Name exited immediately after launch." }
}

function Start-AkashaServices {
  param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [scriptblock]$ProcessTerminator = { param($Process) $Process.Kill() },
    [scriptblock]$ProcessWaiter = { param($Process, $TimeoutMilliseconds) $Process.WaitForExit($TimeoutMilliseconds) }
  )

  $rootContext = Open-AkashaLifecycleRootContext -Root $InstallRoot
  try {
  $paths = Get-AkashaBotPaths -Root $InstallRoot
  $preflight = Get-AkashaLifecyclePreflight -Paths $paths
  $lockPath = Join-Path $paths.State 'lifecycle.lock'
  if (-not (Test-AkashaLifecycleInternalPath -Root $paths.Root -Candidate $lockPath)) {
    throw 'E_LIFECYCLE_PATH: Product paths must remain inside the install root without reparse points.'
  }
  try {
    foreach ($directory in @($paths.State, $paths.Logs)) {
      Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$directory)
      New-Item -ItemType Directory -Force -Path $directory | Out-Null
      Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$directory)
    }
  } catch {
    throw 'E_LIFECYCLE_PATH: Unable to create lifecycle directories.'
  }

  $lockStream = $null
  try {
    Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$paths.State, [string]$lockPath)
    try {
      $lockStream = New-Object System.IO.FileStream(
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
    $lockFinalPath = [AkashaBotNativePathV1]::GetFinalPathFromHandle($lockStream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($lockFinalPath) -or
        -not ([System.IO.Path]::GetFullPath($lockFinalPath)).Equals([System.IO.Path]::GetFullPath($lockPath), [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'E_LIFECYCLE_PATH: Lifecycle lock resolved outside its expected path.'
    }

    $preflight = Get-AkashaLifecyclePreflight -Paths $paths
    Write-AkashaLifecycleLog -Paths $paths -Level 'info' -Message 'Lifecycle start requested.'
    $existing = @(Read-AkashaProcessState -Path $paths.ProcessState -Paths $paths -WeFlowExecutable $preflight.WeFlowExecutable)
    $baseline = @()
    foreach ($record in $existing) {
      $identity = Get-AkashaProcessIdentity -ProcessId ([int]$record.Pid)
      if ($null -eq $identity) { continue }
      if (-not (Test-AkashaRecordMatchesLiveProcess -Record $record -Identity $identity)) {
        throw 'E_PROCESS_STATE: Recorded process identity does not match the live process.'
      }
      if ([bool]$record.Owned) {
        throw "E_ALREADY_RUNNING: Recorded service $($record.Name) is already running."
      }
      $baseline += $record
    }
    if ($existing.Count -ne $baseline.Count) {
      Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $baseline
    }

    $started = New-Object System.Collections.Generic.List[object]
    $records = @($baseline)
    $operationError = $null
    $cleanupSucceeded = $true
    try {
      $weFlowRecord = $records | Where-Object Name -ceq 'weflow' | Select-Object -First 1
      if ($null -eq $weFlowRecord) {
        $externalWeFlow = Find-AkashaExternalWeFlow -ExecutablePath $preflight.WeFlowExecutable
        if ($null -ne $externalWeFlow) {
          $weFlowRecord = New-AkashaProcessRecord -Name 'weflow' -Process $externalWeFlow -ExecutablePath $preflight.WeFlowExecutable -Owned $false -CommandKind 'WeFlowApp'
          $records += $weFlowRecord
          Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $records
        } else {
          Assert-AkashaLaunchBoundary -Paths $paths -ExpectedWeFlowExecutable $preflight.WeFlowExecutable
          $weFlowProcess = Start-AkashaOwnedProcess -FilePath $preflight.WeFlowExecutable -WorkingDirectory (Split-Path -Parent $preflight.WeFlowExecutable)
          $weFlowEntry = [pscustomobject]@{ Name = 'weflow'; Process = $weFlowProcess; Record = $null; ExecutablePath = $preflight.WeFlowExecutable; CommandKind = 'WeFlowApp' }
          $started.Add($weFlowEntry)
          $weFlowRecord = New-AkashaProcessRecord -Name 'weflow' -Process $weFlowProcess -ExecutablePath $preflight.WeFlowExecutable -Owned $true -CommandKind 'WeFlowApp'
          $weFlowEntry.Record = $weFlowRecord
          $records += $weFlowRecord
          Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $records
          Assert-AkashaProcessStayedRunning -Process $weFlowProcess -Name 'weflow'
        }
      }

      Assert-AkashaLaunchBoundary -Paths $paths -ExpectedWeFlowExecutable $preflight.WeFlowExecutable
      $astrProcess = Start-AkashaOwnedProcess -FilePath $paths.AstrBotPython -ArgumentList @('-m', 'astrbot.cli.__main__', 'run') -WorkingDirectory $paths.AstrBotData -EnvironmentOverrides @{ PYTHONNOUSERSITE = '1' }
      $astrEntry = [pscustomobject]@{ Name = 'astrbot'; Process = $astrProcess; Record = $null; ExecutablePath = $paths.AstrBotPython; CommandKind = 'AstrBotRun' }
      $started.Add($astrEntry)
      $astrRecord = New-AkashaProcessRecord -Name 'astrbot' -Process $astrProcess -ExecutablePath $paths.AstrBotPython -Owned $true -CommandKind 'AstrBotRun'
      $astrEntry.Record = $astrRecord
      $records += $astrRecord
      Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $records
      Assert-AkashaProcessStayedRunning -Process $astrProcess -Name 'astrbot'

      $bridgeEnvironment = @{
        AKASHABOT_CONFIG_PATH = $paths.BridgeConfig
        AKASHABOT_LOG_DIR = $paths.Logs
        AKASHABOT_STATE_DIR = $paths.State
        PYTHONNOUSERSITE = '1'
      }
      Assert-AkashaLaunchBoundary -Paths $paths -ExpectedWeFlowExecutable $preflight.WeFlowExecutable
      $bridgeProcess = Start-AkashaOwnedProcess -FilePath $paths.BridgePython -ArgumentList @('main.py') -WorkingDirectory $paths.Bridge -EnvironmentOverrides $bridgeEnvironment
      $bridgeEntry = [pscustomobject]@{ Name = 'bridge'; Process = $bridgeProcess; Record = $null; ExecutablePath = $paths.BridgePython; CommandKind = 'BridgeMain' }
      $started.Add($bridgeEntry)
      $bridgeRecord = New-AkashaProcessRecord -Name 'bridge' -Process $bridgeProcess -ExecutablePath $paths.BridgePython -Owned $true -CommandKind 'BridgeMain'
      $bridgeEntry.Record = $bridgeRecord
      $records += $bridgeRecord
      Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $records
      Assert-AkashaProcessStayedRunning -Process $bridgeProcess -Name 'bridge'

      Write-AkashaLifecycleLog -Paths $paths -Level 'info' -Message 'Lifecycle start completed; services=3.'
    } catch {
      $operationError = $_
    }

    if ($null -ne $operationError) {
      $survivors = @()
      for ($index = $started.Count - 1; $index -ge 0; $index--) {
        $entry = $started[$index]
        $confirmedExited = $false
        try {
          $entry.Process.Refresh()
          if (-not $entry.Process.HasExited) {
            & $ProcessTerminator $entry.Process
            if (& $ProcessWaiter $entry.Process 5000) {
              $entry.Process.Refresh()
            }
          }
          $confirmedExited = $entry.Process.HasExited
        } catch {
          $confirmedExited = $false
        }
        if (-not $confirmedExited) {
          $cleanupSucceeded = $false
          $survivorRecord = $entry.Record
          if ($null -eq $survivorRecord) {
            try {
              $survivorRecord = New-AkashaProcessRecord -Name $entry.Name -Process $entry.Process -ExecutablePath $entry.ExecutablePath -Owned $true -CommandKind $entry.CommandKind
            } catch {
              $survivorRecord = $null
            }
          }
          if ($null -ne $survivorRecord) { $survivors += $survivorRecord }
        }
      }
      try { Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records (@($baseline) + @($survivors)) } catch { $cleanupSucceeded = $false }
      if (-not $cleanupSucceeded) { $operationError.Exception.Data['AkashaCleanupFailure'] = 'E_LIFECYCLE_CLEANUP' }
      throw $operationError
    }
    return ,$records
  } finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
  }
  } finally {
    Close-AkashaLifecycleRootContext -Context $rootContext
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  Start-AkashaServices -InstallRoot $InstallRoot
}
