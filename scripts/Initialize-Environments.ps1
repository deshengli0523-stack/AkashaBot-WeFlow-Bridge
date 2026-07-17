$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -Force

function Test-AkashaEnvironmentPath {
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
  if ($candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }
  $prefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
  if (-not $candidatePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    return $false
  }

  $currentPath = $candidatePath
  while (-not $currentPath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    if (Test-Path -LiteralPath $currentPath) {
      try {
        $currentItem = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
      } catch {
        return $false
      }
      if ($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        return $false
      }
    }
    $parentPath = [System.IO.Path]::GetDirectoryName($currentPath)
    if ([string]::IsNullOrWhiteSpace($parentPath) -or $parentPath -ceq $currentPath) {
      return $false
    }
    $currentPath = [System.IO.Path]::GetFullPath($parentPath).TrimEnd('\', '/')
  }
  return $true
}

function Assert-AkashaEnvironmentPaths {
  param([Parameter(Mandatory)]$Paths)

  $valid = (Test-AkashaEnvironmentPath -Root $Paths.Root -Candidate $Paths.Runtime) -and
    (Test-AkashaEnvironmentPath -Root $Paths.Runtime -Candidate $Paths.BridgeVenv) -and
    (Test-AkashaEnvironmentPath -Root $Paths.Runtime -Candidate $Paths.AstrBotVenv) -and
    (Test-AkashaEnvironmentPath -Root $Paths.BridgeVenv -Candidate $Paths.BridgePython) -and
    (Test-AkashaEnvironmentPath -Root $Paths.AstrBotVenv -Candidate $Paths.AstrBotPython)
  if (-not $valid) {
    throw 'E_ENVIRONMENT_PATH: Environment paths must remain inside the install root without reparse points.'
  }
}

function Assert-AkashaVenvPython {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$Path
  )

  Assert-AkashaEnvironmentPaths -Paths $Paths
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw 'E_VENV_CREATE: Virtual environment Python was not created.'
  }
}

function Initialize-AkashaEnvironments {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Python,
    [scriptblock]$Runner
  )

  $environmentPaths = [pscustomobject]@{
    Root = [string]$Paths.Root
    Runtime = [string]$Paths.Runtime
    BridgeVenv = [string]$Paths.BridgeVenv
    AstrBotVenv = [string]$Paths.AstrBotVenv
    BridgePython = [string]$Paths.BridgePython
    AstrBotPython = [string]$Paths.AstrBotPython
    Bridge = [string]$Paths.Bridge
    InstallLog = [string]$Paths.InstallLog
  }

  if ($null -eq $Runner) {
    $Runner = {
      param($exe, $arguments, $log)
      Invoke-AkashaNative -FilePath $exe -Arguments $arguments -LogPath $log
    }
  }

  Assert-AkashaEnvironmentPaths -Paths $environmentPaths
  New-Item -ItemType Directory -Force -Path $environmentPaths.Runtime | Out-Null
  Assert-AkashaEnvironmentPaths -Paths $environmentPaths
  $probeCode = "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':platform.architecture()[0]}))"
  foreach ($venv in @($environmentPaths.BridgeVenv, $environmentPaths.AstrBotVenv)) {
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
      Assert-AkashaEnvironmentPaths -Paths $environmentPaths
      $arguments = @($Python.Prefix) + @('-m', 'venv', $venv)
      & $Runner $Python.FilePath $arguments $environmentPaths.InstallLog
      Assert-AkashaVenvPython -Paths $environmentPaths -Path $venvPython
    }
    try {
      Assert-AkashaVenvPython -Paths $environmentPaths -Path $venvPython
      $probeOutput = @(& $Runner $venvPython @('-c', $probeCode) $environmentPaths.InstallLog)
      if ($probeOutput.Count -ne 1) { throw 'invalid probe record count' }
      $probe = $probeOutput[0] | ConvertFrom-Json -ErrorAction Stop
      $version = @($probe.version)
      if ($version.Count -lt 2 -or [int]$version[0] -ne 3 -or [int]$version[1] -ne 12 -or [string]$probe.bits -cne '64bit') {
        throw 'invalid Python descriptor'
      }
    } catch {
      throw 'E_VENV_INVALID: Virtual environment must use Python 3.12 x64.'
    }
  }

  $lockPath = Join-Path $environmentPaths.Bridge 'requirements.lock'
  Assert-AkashaVenvPython -Paths $environmentPaths -Path $environmentPaths.BridgePython
  & $Runner $environmentPaths.BridgePython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $lockPath) $environmentPaths.InstallLog
  Assert-AkashaVenvPython -Paths $environmentPaths -Path $environmentPaths.AstrBotPython
  & $Runner $environmentPaths.AstrBotPython @('-m', 'pip', 'install', '--disable-pip-version-check', 'astrbot==4.26.6') $environmentPaths.InstallLog
  Assert-AkashaVenvPython -Paths $environmentPaths -Path $environmentPaths.BridgePython
  & $Runner $environmentPaths.BridgePython @('-m', 'pip', 'check') $environmentPaths.InstallLog
  Assert-AkashaVenvPython -Paths $environmentPaths -Path $environmentPaths.AstrBotPython
  & $Runner $environmentPaths.AstrBotPython @('-m', 'pip', 'check') $environmentPaths.InstallLog
}

if ($MyInvocation.InvocationName -ne '.') {
  $paths = Get-AkashaBotPaths
  Initialize-AkashaEnvironments -Paths $paths -Python (Resolve-Python312)
}
