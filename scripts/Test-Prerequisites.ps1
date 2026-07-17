[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$commonModule = Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -PassThru

& $commonModule {
  param($Root)
  Invoke-AkashaPrerequisiteValidation -InstallRoot $Root
} $InstallRoot
