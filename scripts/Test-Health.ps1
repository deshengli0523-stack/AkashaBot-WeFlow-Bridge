[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -Force

function Test-AkashaHealthInternalPath {
  param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$Candidate)

  try {
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidatePath = [System.IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
  } catch { return $false }
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

function Invoke-AkashaDefaultHttpProbe {
  param([Parameter(Mandatory)][string]$Uri)

  try {
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5 -ErrorAction Stop | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Invoke-AkashaDefaultTcpProbe {
  param([Parameter(Mandatory)][string]$HostName, [Parameter(Mandatory)][int]$Port)

  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $operation = $client.BeginConnect($HostName, $Port, $null, $null)
    if (-not $operation.AsyncWaitHandle.WaitOne(5000)) { return $false }
    $client.EndConnect($operation)
    return $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

function Get-AkashaHealthOwnerText {
  param([Parameter(Mandatory)][int]$Port)

  try {
    $owner = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $owner -and [int]$owner.OwningProcess -gt 0) { return " ownerPid=$([int]$owner.OwningProcess)" }
  } catch {}
  return ''
}

function Invoke-AkashaHealthCheck {
  param(
    [Parameter(Mandatory)][string]$InstallRoot,
    [scriptblock]$HttpProbe,
    [scriptblock]$TcpProbe
  )

  Get-AkashaBotPaths -Root $InstallRoot | Out-Null
  if ($null -eq $HttpProbe) { $HttpProbe = { param($Uri) Invoke-AkashaDefaultHttpProbe -Uri $Uri } }
  if ($null -eq $TcpProbe) { $TcpProbe = { param($HostName, $Port) Invoke-AkashaDefaultTcpProbe -HostName $HostName -Port $Port } }

  $checks = @(
    [pscustomobject]@{ Name = 'WeFlow'; Uri = 'http://127.0.0.1:5031/health'; Port = 5031 },
    [pscustomobject]@{ Name = 'AstrBot'; Uri = 'http://127.0.0.1:6185/'; Port = 6185 },
    [pscustomobject]@{ Name = 'Bridge'; Uri = 'http://127.0.0.1:8766/status'; Port = 8766 }
  )
  $failed = 0
  foreach ($check in $checks) {
    $succeeded = $false
    try { $succeeded = [bool](& $HttpProbe $check.Uri) } catch { $succeeded = $false }
    if ($succeeded) {
      Write-Host "[OK] $($check.Name) $($check.Uri)"
    } else {
      $failed++
      Write-Host "[FAIL] $($check.Name) $($check.Uri)$(Get-AkashaHealthOwnerText -Port $check.Port)"
    }
  }

  $oneBotSucceeded = $false
  try { $oneBotSucceeded = [bool](& $TcpProbe '127.0.0.1' 11229) } catch { $oneBotSucceeded = $false }
  if ($oneBotSucceeded) {
    Write-Host '[OK] OneBot tcp://127.0.0.1:11229'
  } else {
    $failed++
    Write-Host "[FAIL] OneBot tcp://127.0.0.1:11229$(Get-AkashaHealthOwnerText -Port 11229)"
  }

  if ($failed -eq 0) { return 0 }
  return 1
}

if ($MyInvocation.InvocationName -ne '.') {
  exit (Invoke-AkashaHealthCheck -InstallRoot $InstallRoot)
}
