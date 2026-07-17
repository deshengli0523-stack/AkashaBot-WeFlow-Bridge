$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-AkashaBotPaths {
  param([string]$Root = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

  $rootPath = [System.IO.Path]::GetFullPath($Root)
  [pscustomobject]@{
    Root = $rootPath
    App = Join-Path $rootPath 'app'
    Bridge = Join-Path $rootPath 'app\bridge'
    Scripts = Join-Path $rootPath 'scripts'
    Runtime = Join-Path $rootPath 'runtime'
    BridgeVenv = Join-Path $rootPath 'runtime\venvs\bridge'
    AstrBotVenv = Join-Path $rootPath 'runtime\venvs\astrbot'
    BridgePython = Join-Path $rootPath 'runtime\venvs\bridge\Scripts\python.exe'
    AstrBotPython = Join-Path $rootPath 'runtime\venvs\astrbot\Scripts\python.exe'
    Data = Join-Path $rootPath 'data'
    BridgeData = Join-Path $rootPath 'data\bridge'
    BridgeConfig = Join-Path $rootPath 'data\bridge\config.json'
    AstrBotData = Join-Path $rootPath 'data\astrbot'
    Logs = Join-Path $rootPath 'data\logs'
    State = Join-Path $rootPath 'data\state'
    Backups = Join-Path $rootPath 'data\backups'
    InstallLog = Join-Path $rootPath 'data\logs\install.log'
    ProcessState = Join-Path $rootPath 'data\state\processes.json'
    InstallState = Join-Path $rootPath 'data\state\install.json'
    WeFlowPathState = Join-Path $rootPath 'data\state\weflow-path.txt'
  }
}
function Protect-AkashaQuotedAssignments {
  param(
    [Parameter(Mandatory)][AllowEmptyString()][string]$Text,
    [Parameter(Mandatory)][string]$KeyPattern,
    [Parameter(Mandatory)]$Options,
    [string[]]$AllowedPlaceholders = @()
  )

  $prefix = '(?<prefix>(?<keyQuote>\\?["'']?)' + $KeyPattern + '\k<keyQuote>\s*[=:]\s*)'
  $patterns = @(
    ($prefix + '(?<open>\\")(?<quoted>.*?)(?<discard>(?<!\\)(?:\\\\\\\\)*)(?<close>\\")'),
    ($prefix + '(?<open>\\'')(?<quoted>.*?)(?<discard>(?<!\\)(?:\\\\\\\\)*)(?<close>\\'')'),
    ($prefix + '(?<open>")(?<quoted>(?:\\.|[^"\\\r\n])*)(?<close>")'),
    ($prefix + '(?<open>'')(?<quoted>(?:\\.|[^''\\\r\n])*)(?<close>'')')
  )
  $safe = $Text
  foreach ($pattern in $patterns) {
    $regex = New-Object System.Text.RegularExpressions.Regex($pattern, $Options)
    $safe = $regex.Replace($safe, [System.Text.RegularExpressions.MatchEvaluator]{
        param($match)

        if ($AllowedPlaceholders -ccontains $match.Groups['quoted'].Value) {
          return $match.Value
        }
        return $match.Groups['prefix'].Value + $match.Groups['open'].Value + '[REDACTED]' + $match.Groups['close'].Value
      })
  }
  return $safe
}
function Protect-AkashaLogText {
  param([AllowEmptyString()][string]$Text)

  if ($null -eq $Text) { return '' }

  $allowedPlaceholders = @('your_weflow_access_token')
  $secretSuffix = '(?:api[-_]?key|access[-_]?token|auth[-_]?token|refresh[-_]?token|client[-_]?secret|jwt[-_]?secret|password|token|jwt)'
  $keyPattern = '(?<![A-Za-z0-9_])(?<key>(?:--)?[A-Za-z0-9_-]*?' + $secretSuffix + ')(?![A-Za-z0-9_-])'
  $options = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor [System.Text.RegularExpressions.RegexOptions]::Multiline

  $safe = Protect-AkashaQuotedAssignments -Text $Text -KeyPattern $keyPattern -Options $options -AllowedPlaceholders $allowedPlaceholders

  $barePattern = '(?<prefix>' + $keyPattern + '\s*[=:](?!\s*\\?["''])\s*)(?<bare>[^\r\n]+)'
  $bareRegex = New-Object System.Text.RegularExpressions.Regex($barePattern, $options)
  $safe = $bareRegex.Replace($safe, [System.Text.RegularExpressions.MatchEvaluator]{
      param($match)

      $rawValue = $match.Groups['bare'].Value
      $value = $rawValue.Trim()
      if ($allowedPlaceholders -ccontains $value) {
        return $match.Value
      }
      $trailingLength = $rawValue.Length - $rawValue.TrimEnd().Length
      $trailing = if ($trailingLength -gt 0) { $rawValue.Substring($rawValue.Length - $trailingLength) } else { '' }
      return $match.Groups['prefix'].Value + '[REDACTED]' + $trailing
    })

  $cliPattern = '(?<prefix>(?<![A-Za-z0-9_])--[A-Za-z0-9_-]*?' + $secretSuffix + '(?![A-Za-z0-9_-])\s+)(?<bare>[^\r\n]+)'
  $cliRegex = New-Object System.Text.RegularExpressions.Regex($cliPattern, $options)
  $safe = $cliRegex.Replace($safe, [System.Text.RegularExpressions.MatchEvaluator]{
      param($match)

      $rawValue = $match.Groups['bare'].Value
      $value = $rawValue.Trim()
      if ($allowedPlaceholders -ccontains $value) {
        return $match.Value
      }
      $trailingLength = $rawValue.Length - $rawValue.TrimEnd().Length
      $trailing = if ($trailingLength -gt 0) { $rawValue.Substring($rawValue.Length - $trailingLength) } else { '' }
      return $match.Groups['prefix'].Value + '[REDACTED]' + $trailing
    })

  $safe = [regex]::Replace($safe, '(?i)sk-[A-Za-z0-9_-]{16,}', '[REDACTED]')
  $safe = [regex]::Replace($safe, '(?i)(\bBearer\s+)\S+', '$1[REDACTED]')
  $safe = [regex]::Replace($safe, '(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])', '[REDACTED]')
  return $safe
}
function Write-AkashaLog {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$Level,
    [Parameter(Mandatory)][AllowEmptyString()][string]$Message
  )

  $directory = Split-Path -Parent $Path
  if (-not [string]::IsNullOrWhiteSpace($directory)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
  }
  $line = '{0:o} [{1}] {2}' -f (Get-Date), $Level.ToUpperInvariant(), (Protect-AkashaLogText $Message)
  Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
  Write-Host $line
}
function Remove-AkashaAtomicArtifacts {
  param(
    [string[]]$Paths = @(),
    [scriptblock]$Remover
  )

  if ($null -eq $Remover) {
    $Remover = {
      param([string]$ArtifactPath)
      Remove-Item -LiteralPath $ArtifactPath -Force -ErrorAction Stop
    }
  }

  $succeeded = $true
  foreach ($artifactPath in $Paths) {
    if ([string]::IsNullOrWhiteSpace($artifactPath)) { continue }
    try {
      if (Test-Path -LiteralPath $artifactPath) {
        & $Remover $artifactPath
      }
    } catch {
      $succeeded = $false
    }
  }
  return $succeeded
}
function Complete-AkashaAtomicOutcome {
  param(
    $OperationError,
    [Parameter(Mandatory)][bool]$CleanupSucceeded
  )

  if ($null -ne $OperationError) {
    if (-not $CleanupSucceeded) {
      $operationException = if ($OperationError -is [System.Management.Automation.ErrorRecord]) {
        $OperationError.Exception
      } elseif ($OperationError -is [System.Exception]) {
        $OperationError
      } else {
        $null
      }
      if ($null -ne $operationException) {
        $operationException.Data['AkashaCleanupFailure'] = 'E_ATOMIC_CLEANUP'
      }
    }
    throw $OperationError
  }
  if (-not $CleanupSucceeded) {
    throw 'E_JSON_ATOMIC_CLEANUP: Unable to remove temporary JSON artifacts.'
  }
}
function Write-JsonAtomic {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)]$Value
  )

  $targetPath = [System.IO.Path]::GetFullPath($Path)
  $directory = [System.IO.Path]::GetDirectoryName($targetPath)
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  $temporary = Join-Path $directory ('.' + [System.IO.Path]::GetFileName($targetPath) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
  $replacementBackup = $temporary + '.replace-backup'
  $encoding = New-Object System.Text.UTF8Encoding($false)
  $operationError = $null
  try {
    $json = $Value | ConvertTo-Json -Depth 64
    [System.IO.File]::WriteAllText($temporary, $json, $encoding)
    if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
      [System.IO.File]::Replace($temporary, $targetPath, $replacementBackup)
    } else {
      [System.IO.File]::Move($temporary, $targetPath)
    }
  } catch {
    $operationError = $_
  }
  $cleanupSucceeded = Remove-AkashaAtomicArtifacts -Paths @($temporary, $replacementBackup)
  Complete-AkashaAtomicOutcome -OperationError $operationError -CleanupSucceeded $cleanupSucceeded
}
function Backup-AkashaFile {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$BackupRoot
  )

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
  $directory = Join-Path $BackupRoot $stamp
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  $destination = Join-Path $directory ([System.IO.Path]::GetFileName($Path))
  Copy-Item -LiteralPath $Path -Destination $destination -Force
  return $destination
}
function ConvertFrom-AkashaPythonProbeOutput {
  param([object[]]$OutputRecords = @())

  $failure = 'E_PYTHON_312_X64: Python 3.12 x64 was not found. Install it and enable the py launcher or PATH entry.'
  $records = @($OutputRecords)
  if ($records.Count -ne 1) { throw $failure }
  $record = [string]$records[0]
  if ([string]::IsNullOrWhiteSpace($record)) { throw $failure }
  try {
    $info = $record | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw $failure
  }
  if ($null -eq $info) { throw $failure }
  $versionProperty = $info.PSObject.Properties['version']
  $bitsProperty = $info.PSObject.Properties['bits']
  if ($null -eq $versionProperty -or $null -eq $bitsProperty) { throw $failure }
  $version = @($versionProperty.Value)
  if ($version.Count -lt 2) { throw $failure }
  try {
    $major = [int]$version[0]
    $minor = [int]$version[1]
  } catch {
    throw $failure
  }
  if ($major -ne 3 -or $minor -ne 12 -or [string]$bitsProperty.Value -cne '64bit') {
    throw $failure
  }
  return $info
}
function Test-AkashaWindowsClientDescriptor {
  param(
    [Parameter(Mandatory)][string]$Platform,
    [Parameter(Mandatory)][int]$VersionMajor,
    [Parameter(Mandatory)][int]$ProductType,
    [Parameter(Mandatory)][bool]$Is64Bit
  )

  return $Platform -ceq 'Win32NT' -and
    $VersionMajor -eq 10 -and
    $ProductType -eq 1 -and
    $Is64Bit
}
function Get-AkashaOperatingSystemProductType {
  param(
    [scriptblock]$OperatingSystemReader,
    [scriptblock]$Sleeper,
    [ValidateRange(1, 3)][int]$MaxAttempts = 3,
    [ValidateRange(0, 250)][int]$DelayMilliseconds = 200
  )

  $failure = 'E_OS_DETECTION: Unable to verify Windows 10/11 x64 client.'
  if ($null -eq $OperatingSystemReader) {
    $OperatingSystemReader = {
      CimCmdlets\Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    }
  }
  if ($null -eq $Sleeper) {
    $Sleeper = { param($Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
  }
  for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    try {
      $records = @(& $OperatingSystemReader)
      if ($records.Count -ne 1) { throw 'invalid operating-system record count' }
      $productTypeProperty = $records[0].PSObject.Properties['ProductType']
      if ($null -eq $productTypeProperty) { throw 'missing ProductType' }
      return [int]$productTypeProperty.Value
    } catch {
      if ($attempt -lt $MaxAttempts) {
        try {
          & $Sleeper $DelayMilliseconds
        } catch {
          throw $failure
        }
      }
    }
  }
  throw $failure
}
function Assert-AkashaWindowsClient {
  param(
    [string]$Platform = [Environment]::OSVersion.Platform.ToString(),
    [int]$VersionMajor = [Environment]::OSVersion.Version.Major,
    [bool]$Is64Bit = [Environment]::Is64BitOperatingSystem,
    [scriptblock]$OperatingSystemReader,
    [scriptblock]$Sleeper
  )

  $productType = Get-AkashaOperatingSystemProductType -OperatingSystemReader $OperatingSystemReader -Sleeper $Sleeper
  if (-not (Test-AkashaWindowsClientDescriptor -Platform $Platform -VersionMajor $VersionMajor -ProductType $productType -Is64Bit $Is64Bit)) {
    throw 'E_OS_WINDOWS_CLIENT_X64: Windows 10/11 x64 client is required.'
  }
  return $true
}
function Invoke-AkashaPrerequisiteValidation {
  param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'),
    [string]$Platform = [Environment]::OSVersion.Platform.ToString(),
    [int]$VersionMajor = [Environment]::OSVersion.Version.Major,
    [bool]$Is64Bit = [Environment]::Is64BitOperatingSystem,
    [scriptblock]$OperatingSystemReader,
    [scriptblock]$Sleeper,
    [scriptblock]$PythonResolver
  )

  Assert-AkashaWindowsClient -Platform $Platform -VersionMajor $VersionMajor -Is64Bit $Is64Bit -OperatingSystemReader $OperatingSystemReader -Sleeper $Sleeper | Out-Null
  $paths = Get-AkashaBotPaths -Root $InstallRoot
  New-Item -ItemType Directory -Force -Path $paths.Logs, $paths.State | Out-Null
  if ($null -eq $PythonResolver) {
    $PythonResolver = { Resolve-Python312 }
  }
  $python = & $PythonResolver
  $probe = Join-Path $paths.State '.write-test'
  try {
    [System.IO.File]::WriteAllText($probe, 'ok')
  } finally {
    if (Test-Path -LiteralPath $probe) {
      Remove-Item -LiteralPath $probe -Force -ErrorAction Stop
    }
  }
  Write-AkashaLog -Path $paths.InstallLog -Level 'info' -Message "Prerequisites passed for $($paths.Root)"
  return [pscustomobject]@{
    Paths = $paths
    Python = $python
    WeFlowExecutable = Get-WeFlowExecutable
  }
}
function Resolve-Python312 {
  $probeCode = "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':platform.architecture()[0]}))"
  $candidates = @()
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) {
    $candidates += [pscustomobject]@{ FilePath = $launcher.Source; Prefix = @('-3.12') }
  }
  $python = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($python) {
    $candidates += [pscustomobject]@{ FilePath = $python.Source; Prefix = @() }
  }

  foreach ($candidate in $candidates) {
    try {
      $outputRecords = @(& $candidate.FilePath @($candidate.Prefix) -c $probeCode 2>$null)
      $exitCode = $LASTEXITCODE
      if ($exitCode -ne 0) { continue }
      ConvertFrom-AkashaPythonProbeOutput -OutputRecords $outputRecords | Out-Null
      return $candidate
    } catch {
      continue
    }
  }

  throw 'E_PYTHON_312_X64: Python 3.12 x64 was not found. Install it and enable the py launcher or PATH entry.'
}
function Protect-AkashaExactValues {
  param(
    [AllowEmptyString()][string]$Text,
    [object[]]$Values = @()
  )

  if ($null -eq $Text) { return '' }
  $safe = $Text
  $orderedValues = @(
    $Values |
      ForEach-Object { [string]$_ } |
      Where-Object { -not [string]::IsNullOrEmpty($_) } |
      Sort-Object { $_.Length } -Descending
  )
  foreach ($value in $orderedValues) {
    $valuePattern = '(?<![A-Za-z0-9_-])' + [regex]::Escape($value) + '(?![A-Za-z0-9_-])'
    $safe = [regex]::Replace($safe, $valuePattern, '[REDACTED]')
    if ($value.Length -ge 16) {
      $safe = $safe.Replace($value, '[REDACTED]')
    }
  }
  return $safe
}
function Invoke-AkashaNative {
  param(
    [Parameter(Mandatory)][string]$FilePath,
    [string[]]$Arguments = @(),
    [Parameter(Mandatory)][string]$LogPath,
    [string[]]$StandardInput = @(),
    [object[]]$SensitiveValues = @()
  )

  $displayName = [System.IO.Path]::GetFileName($FilePath)
  if ([string]::IsNullOrWhiteSpace($displayName)) { $displayName = 'native executable' }
  try {
    $oldErrorActionPreference = $ErrorActionPreference
    try {
      $ErrorActionPreference = 'Continue'
      if (@($StandardInput).Count -gt 0) {
        $output = @($StandardInput | & $FilePath @Arguments 2>&1)
      } else {
        $output = @(& $FilePath @Arguments 2>&1)
      }
      $exitCode = $LASTEXITCODE
    } finally {
      $ErrorActionPreference = $oldErrorActionPreference
    }
  } catch {
    throw "E_NATIVE_START: native command failed: $displayName"
  }

  $knownValues = @($Arguments) + @($StandardInput) + @($SensitiveValues)
  $safeOutput = @()
  $logFailed = $false
  foreach ($line in $output) {
    $safeLine = Protect-AkashaExactValues -Text ([string]$line) -Values $knownValues
    $safeLine = Protect-AkashaLogText $safeLine
    $safeLine = Protect-AkashaExactValues -Text $safeLine -Values $knownValues
    try {
      Write-AkashaLog -Path $LogPath -Level 'native' -Message $safeLine
    } catch {
      $logFailed = $true
    }
    $safeOutput += $safeLine
  }
  if ($exitCode -ne 0) {
    throw "E_NATIVE_$($exitCode): native command failed: $displayName"
  }
  if ($logFailed) {
    throw "E_NATIVE_LOG: native output could not be logged: $displayName"
  }
  return $safeOutput
}
function Resolve-AkashaDisplayIconExecutable {
  param([AllowEmptyString()][string]$DisplayIcon)

  if ([string]::IsNullOrWhiteSpace($DisplayIcon)) { return $null }
  $quoted = [regex]::Match($DisplayIcon, '^\s*"(?<path>[^"]+)"\s*(?:,\s*-?\d+\s*)?$')
  if ($quoted.Success) {
    $candidate = $quoted.Groups['path'].Value
  } else {
    $unquoted = [regex]::Match($DisplayIcon, '^\s*(?<path>.+?)(?:\s*,\s*-?\d+\s*)?$')
    if (-not $unquoted.Success) { return $null }
    $candidate = $unquoted.Groups['path'].Value.Trim()
  }
  try {
    if ([System.IO.Path]::GetExtension($candidate) -ine '.exe') { return $null }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $null }
    return (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
  } catch {
    return $null
  }
}
function Get-WeFlowExecutable {
  $candidates = @()
  $uninstallRoots = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
  )
  foreach ($root in $uninstallRoots) {
    $entries = @(Get-ItemProperty $root -ErrorAction SilentlyContinue |
        Where-Object {
          $displayName = $_.PSObject.Properties['DisplayName']
          $null -ne $displayName -and [string]$displayName.Value -match '(?i)WeFlow'
        })
    foreach ($entry in $entries) {
      $displayIcon = $entry.PSObject.Properties['DisplayIcon']
      if ($null -ne $displayIcon -and $displayIcon.Value) {
        $displayIconExecutable = Resolve-AkashaDisplayIconExecutable -DisplayIcon ([string]$displayIcon.Value)
        if ($displayIconExecutable) { $candidates += $displayIconExecutable }
      }
      $installLocation = $entry.PSObject.Properties['InstallLocation']
      if ($null -ne $installLocation -and $installLocation.Value) {
        $candidates += Join-Path ([string]$installLocation.Value) 'WeFlow.exe'
      }
    }
  }
  $candidates += @(
    (Join-Path $env:LOCALAPPDATA 'Programs\WeFlow\WeFlow.exe'),
    (Join-Path $env:LOCALAPPDATA 'WeFlow\WeFlow.exe'),
    (Join-Path $env:ProgramFiles 'WeFlow\WeFlow.exe')
  )
  return $candidates |
    Where-Object {
      -not [string]::IsNullOrWhiteSpace([string]$_) -and
      [System.IO.Path]::GetExtension([string]$_) -ieq '.exe' -and
      (Test-Path -LiteralPath $_ -PathType Leaf)
    } |
    Select-Object -First 1
}

Export-ModuleMember -Function Get-AkashaBotPaths, Protect-AkashaLogText, Write-AkashaLog, Write-JsonAtomic, Backup-AkashaFile, Resolve-Python312, Invoke-AkashaNative, Get-WeFlowExecutable
