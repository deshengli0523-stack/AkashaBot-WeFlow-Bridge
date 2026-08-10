[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'),
  [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-NormalizedPath {
  param([Parameter(Mandatory)][string]$Path)

  return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-PathOverlap {
  param(
    [Parameter(Mandatory)][string]$First,
    [Parameter(Mandatory)][string]$Second
  )

  $firstPath = Get-NormalizedPath -Path $First
  $secondPath = Get-NormalizedPath -Path $Second
  $separator = [System.IO.Path]::DirectorySeparatorChar
  return $firstPath.Equals($secondPath, [System.StringComparison]::OrdinalIgnoreCase) -or
    $firstPath.StartsWith($secondPath + $separator, [System.StringComparison]::OrdinalIgnoreCase) -or
    $secondPath.StartsWith($firstPath + $separator, [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-BundledPowerShell {
  param(
    [Parameter(Mandatory)][string]$ScriptPath,
    [Parameter(Mandatory)][string[]]$Arguments
  )

  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments | Out-Host
  $exitCode = $LASTEXITCODE
  return [int]$exitCode
}

try {
  $sourceRoot = Get-NormalizedPath -Path (Join-Path $PSScriptRoot '..')
  $targetRoot = Get-NormalizedPath -Path $InstallRoot
  $sourceInstaller = Join-Path $sourceRoot 'scripts\Install.ps1'
  $sourceStopper = Join-Path $sourceRoot 'scripts\Stop-Services.ps1'
  $targetState = Join-Path $targetRoot 'data\state\install.json'

  if (Test-PathOverlap -First $sourceRoot -Second $targetRoot) {
    throw 'E_UPDATE_LOCATION: Extract the update package outside the installed application directory.'
  }
  foreach ($required in @($sourceInstaller, $sourceStopper, (Join-Path $sourceRoot 'VERSION'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
      throw 'E_UPDATE_PACKAGE: The update package is incomplete; extract the whole ZIP and retry.'
    }
  }
  if (-not (Test-Path -LiteralPath $targetState -PathType Leaf)) {
    throw 'E_UPDATE_NOT_INSTALLED: No existing installation was found. Use the bundled installer for first-time setup.'
  }

  $oldVersionPath = Join-Path $targetRoot 'VERSION'
  $oldVersion = if (Test-Path -LiteralPath $oldVersionPath -PathType Leaf) {
    (Get-Content -LiteralPath $oldVersionPath -Raw -Encoding UTF8).Trim()
  } else {
    'unknown'
  }
  $newVersion = (Get-Content -LiteralPath (Join-Path $sourceRoot 'VERSION') -Raw -Encoding UTF8).Trim()

  Write-Host ('Target: ' + $targetRoot)
  Write-Host ('Version: ' + $oldVersion + ' -> ' + $newVersion)
  Write-Host '[1/3] Stopping AkashaBot, AstrBot, Bridge, and WeFlow...'
  $stopCode = Invoke-BundledPowerShell -ScriptPath $sourceStopper -Arguments @('-InstallRoot', $targetRoot)
  if ($stopCode -ne 0) {
    throw 'E_UPDATE_STOP: Services could not be stopped safely. Review the error above and retry.'
  }

  Write-Host '[2/3] Installing the verified payload while preserving data and configuration...'
  $installArguments = @('-SourceRoot', $sourceRoot, '-InstallRoot', $targetRoot)
  if ($SkipStart) { $installArguments += '-SkipStart' }
  $installCode = Invoke-BundledPowerShell -ScriptPath $sourceInstaller -Arguments $installArguments
  if ($installCode -ne 0) {
    throw 'E_UPDATE_INSTALL: Installation failed. Existing data was preserved; inspect data\logs\install.log.'
  }

  if ($SkipStart) {
    Write-Host '[3/3] Startup was skipped by request.'
  } else {
    Write-Host '[3/3] Startup and aggregate health verification completed when calibration is valid.'
    Write-Host 'If calibration was reported as required, run the installed calibration launcher and then start the service.'
  }
  Write-Host ('Updated installation: ' + $targetRoot)
  exit 0
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
