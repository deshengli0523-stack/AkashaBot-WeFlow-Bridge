$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Import-Module (Join-Path $PSScriptRoot 'AkashaBot.Common.psm1') -Force

function Set-JsonProperty {
  param(
    [Parameter(Mandatory)][psobject]$Object,
    [Parameter(Mandatory)][string]$Name,
    $Value
  )

  if ($Object.PSObject.Properties.Name -ccontains $Name) {
    $Object.$Name = $Value
  } else {
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
  }
}

function New-HexToken {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function New-DashboardPassword {
  return 'Ak!' + (New-HexToken).Substring(0, 21)
}

function Test-AkashaConfigurationPath {
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

function Assert-AkashaConfigurationPaths {
  param([Parameter(Mandatory)]$Paths)

  foreach ($candidate in @(
      $Paths.State,
      $Paths.Backups,
      $Paths.Bridge,
      $Paths.BridgeData,
      $Paths.BridgeConfig,
      $Paths.AstrBotData,
      $Paths.AstrBotPython
    )) {
    if (-not (Test-AkashaConfigurationPath -Root $Paths.Root -Candidate ([string]$candidate))) {
      throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
    }
  }
}

function New-AkashaConfigurationPathSnapshot {
  param([Parameter(Mandatory)]$Paths)

  $snapshot = [ordered]@{}
  foreach ($name in @('Root', 'State', 'Backups', 'Bridge', 'BridgeData', 'BridgeConfig', 'AstrBotData', 'AstrBotPython')) {
    $snapshot[$name] = [System.IO.Path]::GetFullPath([string]$Paths.$name).TrimEnd('\', '/')
  }
  return [pscustomobject]$snapshot
}

function Assert-AkashaConfigurationPathSnapshot {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot
  )

  foreach ($name in @('Root', 'State', 'Backups', 'Bridge', 'BridgeData', 'BridgeConfig', 'AstrBotData', 'AstrBotPython')) {
    try {
      $currentPath = [System.IO.Path]::GetFullPath([string]$Paths.$name).TrimEnd('\', '/')
    } catch {
      throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
    }
    if (-not $currentPath.Equals([string]$Snapshot.$name, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
    }
  }
}

function Assert-AkashaConfigurationTargetPath {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot,
    [Parameter(Mandatory)][string]$Candidate
  )

  Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $Snapshot
  Assert-AkashaConfigurationPaths -Paths $Paths
  if (-not (Test-AkashaConfigurationPath -Root $Snapshot.Root -Candidate $Candidate)) {
    throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
  }
}

function Assert-AkashaConfigurationTransactionPaths {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot
  )

  Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $Snapshot
  Assert-AkashaConfigurationPaths -Paths $Paths
  foreach ($derivedPath in @(
      (Join-Path $Snapshot.AstrBotData 'data\cmd_config.json'),
      (Join-Path $Snapshot.AstrBotData 'FIRST_LOGIN.txt')
    )) {
    if (-not (Test-AkashaConfigurationPath -Root $Snapshot.Root -Candidate $derivedPath)) {
      throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
    }
  }
}

function Assert-AkashaConfigurationPreflight {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$WeFlowConfigPath
  )

  Assert-AkashaConfigurationPaths -Paths $Paths
  foreach ($derivedPath in @(
      (Join-Path $Paths.AstrBotData 'data\cmd_config.json'),
      (Join-Path $Paths.AstrBotData 'FIRST_LOGIN.txt')
    )) {
    if (-not (Test-AkashaConfigurationPath -Root $Paths.Root -Candidate $derivedPath)) {
      throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
    }
  }
  if (-not (Test-Path -LiteralPath $WeFlowConfigPath -PathType Leaf)) {
    throw 'E_WEFLOW_CONFIG_MISSING: Complete the WeFlow first-run wizard, then run the installer again.'
  }
  if (@(Get-Process -Name 'WeFlow' -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.'
  }

  $astrConfigPath = Join-Path $Paths.AstrBotData 'data\cmd_config.json'
  if (-not (Test-Path -LiteralPath $astrConfigPath -PathType Leaf) -and
      (Test-Path -LiteralPath $Paths.AstrBotData)) {
    throw 'E_ASTRBOT_PARTIAL: AstrBot data exists without data\cmd_config.json; move it aside and retry.'
  }
  if ((Test-Path -LiteralPath $Paths.BridgeConfig) -and
      -not (Test-Path -LiteralPath $Paths.BridgeConfig -PathType Leaf)) {
    throw 'E_BRIDGE_PARTIAL: Bridge config target exists but is not a regular file.'
  }
}

function Read-AkashaConfigurationJson {
  param([Parameter(Mandatory)][string]$Path)

  try {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop
    $value = $raw | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $value) {
      if ($raw.Trim() -match '^\[\s*\]$') {
        $value = New-Object object[] 0
      } else {
        throw 'empty JSON value'
      }
    }
    Write-Output -NoEnumerate $value
  } catch {
    throw 'E_CONFIGURATION_JSON: Required configuration JSON is invalid.'
  }
}

function New-AkashaAstrBotOwnership {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot
  )

  Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $Paths.AstrBotData
  $markerPath = Join-Path $Paths.AstrBotData ('.akasha-ownership-' + [guid]::NewGuid().ToString('N') + '.tmp')
  Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $markerPath
  $stream = [System.IO.File]::Open(
    $markerPath,
    [System.IO.FileMode]::CreateNew,
    [System.IO.FileAccess]::ReadWrite,
    [System.IO.FileShare]::None
  )
  return [pscustomobject]@{
    RootPath = [System.IO.Path]::GetFullPath([string]$Paths.AstrBotData).TrimEnd('\', '/')
    MarkerPath = [System.IO.Path]::GetFullPath($markerPath)
    Stream = $stream
  }
}

function Test-AkashaOwnedAstrBotItem {
  param(
    [Parameter(Mandatory)][string]$OwnedRoot,
    [Parameter(Mandatory)][string]$Path,
    [switch]$AllowRoot
  )

  try {
    $ownedRootPath = [System.IO.Path]::GetFullPath($OwnedRoot).TrimEnd('\', '/')
    $itemPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
  } catch {
    return $false
  }
  if ($itemPath.Equals($ownedRootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    return [bool]$AllowRoot
  }
  return Test-AkashaConfigurationPath -Root $ownedRootPath -Candidate $itemPath
}

function Complete-AkashaAstrBotOwnership {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot,
    [Parameter(Mandatory)]$Ownership
  )

  try {
    Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $Snapshot
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $Ownership.RootPath
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $Ownership.MarkerPath
    if ($null -eq $Ownership.Stream -or -not $Ownership.Stream.CanWrite -or
        -not (Test-Path -LiteralPath $Ownership.MarkerPath -PathType Leaf)) {
      return $false
    }
    $markerItem = Get-Item -LiteralPath $Ownership.MarkerPath -Force -ErrorAction Stop
    if ($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      return $false
    }
    $Ownership.Stream.Dispose()
    $Ownership.Stream = $null
    [System.IO.File]::Delete($Ownership.MarkerPath)
    return -not (Test-Path -LiteralPath $Ownership.MarkerPath)
  } catch {
    return $false
  }
}

function Remove-FreshAstrBotData {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)]$Snapshot,
    $Ownership,
    [Parameter(Mandatory)][bool]$CleanupRequired
  )

  if (-not $CleanupRequired) { return $true }
  if ($null -eq $Ownership -or $null -eq $Ownership.Stream) { return $false }
  try {
    Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $Snapshot
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $Ownership.RootPath
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $Snapshot -Candidate $Ownership.MarkerPath
    if (-not ([System.IO.Path]::GetFullPath($Ownership.RootPath).TrimEnd('\', '/')).Equals(
        [System.IO.Path]::GetFullPath($Snapshot.AstrBotData).TrimEnd('\', '/'),
        [System.StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }

    $ownedRootItem = Get-Item -LiteralPath $Ownership.RootPath -Force -ErrorAction Stop
    if (($ownedRootItem.Attributes -band [System.IO.FileAttributes]::Directory) -eq 0 -or
        ($ownedRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      return $false
    }
    $markerItem = Get-Item -LiteralPath $Ownership.MarkerPath -Force -ErrorAction Stop
    if (($markerItem.Attributes -band [System.IO.FileAttributes]::Directory) -or
        ($markerItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
        -not $Ownership.Stream.CanWrite) {
      return $false
    }

    $files = New-Object System.Collections.ArrayList
    $directories = New-Object System.Collections.ArrayList
    $pending = New-Object System.Collections.Stack
    $pending.Push($Ownership.RootPath)
    $markerFound = $false
    while ($pending.Count -gt 0) {
      $directoryPath = [string]$pending.Pop()
      if (-not (Test-AkashaOwnedAstrBotItem -OwnedRoot $Ownership.RootPath -Path $directoryPath -AllowRoot)) {
        return $false
      }
      $directoryItem = Get-Item -LiteralPath $directoryPath -Force -ErrorAction Stop
      if (($directoryItem.Attributes -band [System.IO.FileAttributes]::Directory) -eq 0 -or
          ($directoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        return $false
      }
      foreach ($child in @(Get-ChildItem -LiteralPath $directoryPath -Force -ErrorAction Stop)) {
        $childPath = [System.IO.Path]::GetFullPath($child.FullName)
        if ($childPath.Equals($Ownership.MarkerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
          if (($child.Attributes -band [System.IO.FileAttributes]::Directory) -or
              ($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            return $false
          }
          $markerFound = $true
          continue
        }
        if (-not (Test-AkashaOwnedAstrBotItem -OwnedRoot $Ownership.RootPath -Path $childPath) -or
            ($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
          return $false
        }
        if ($child.Attributes -band [System.IO.FileAttributes]::Directory) {
          [void]$directories.Add($childPath)
          $pending.Push($childPath)
        } else {
          [void]$files.Add($childPath)
        }
      }
    }
    if (-not $markerFound) { return $false }

    foreach ($filePath in @($files)) {
      if (-not (Test-AkashaOwnedAstrBotItem -OwnedRoot $Ownership.RootPath -Path $filePath)) { return $false }
      $fileItem = Get-Item -LiteralPath $filePath -Force -ErrorAction Stop
      if (($fileItem.Attributes -band [System.IO.FileAttributes]::Directory) -or
          ($fileItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        return $false
      }
      Remove-Item -LiteralPath $filePath -Force -ErrorAction Stop
    }
    foreach ($directoryPath in @($directories | Sort-Object { $_.Length } -Descending)) {
      if (-not (Test-AkashaOwnedAstrBotItem -OwnedRoot $Ownership.RootPath -Path $directoryPath)) { return $false }
      $directoryItem = Get-Item -LiteralPath $directoryPath -Force -ErrorAction Stop
      if (($directoryItem.Attributes -band [System.IO.FileAttributes]::Directory) -eq 0 -or
          ($directoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        return $false
      }
      [System.IO.Directory]::Delete($directoryPath, $false)
    }

    $remaining = @(Get-ChildItem -LiteralPath $Ownership.RootPath -Force -ErrorAction Stop)
    if ($remaining.Count -ne 1 -or
        -not ([System.IO.Path]::GetFullPath($remaining[0].FullName)).Equals($Ownership.MarkerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      return $false
    }
    if (-not (Complete-AkashaAstrBotOwnership -Paths $Paths -Snapshot $Snapshot -Ownership $Ownership)) {
      return $false
    }
    $rootAfterMarker = Get-Item -LiteralPath $Ownership.RootPath -Force -ErrorAction Stop
    if (($rootAfterMarker.Attributes -band [System.IO.FileAttributes]::Directory) -eq 0 -or
        ($rootAfterMarker.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or
        @(Get-ChildItem -LiteralPath $Ownership.RootPath -Force -ErrorAction Stop).Count -ne 0) {
      return $false
    }
    [System.IO.Directory]::Delete($Ownership.RootPath, $false)
    return -not (Test-Path -LiteralPath $Ownership.RootPath)
  } catch {
    return $false
  }
}

function Invoke-AkashaRollbackStep {
  param([Parameter(Mandatory)][scriptblock]$Action)

  try {
    & $Action
    return $true
  } catch {
    return $false
  }
}

function Initialize-AkashaConfiguration {
  param(
    [Parameter(Mandatory)]$Paths,
    [Parameter(Mandatory)][string]$WeFlowConfigPath,
    [scriptblock]$AstrBotInitializer
  )

  Assert-AkashaConfigurationPreflight -Paths $Paths -WeFlowConfigPath $WeFlowConfigPath
  $pathSnapshot = New-AkashaConfigurationPathSnapshot -Paths $Paths

  $lockStream = $null
  $lockOwned = $false
  $astrBotOwnership = $null
  $lockPath = Join-Path $Paths.State 'configuration.lock'
  try {
    Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $pathSnapshot
    Assert-AkashaConfigurationPaths -Paths $Paths
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.State
    try {
      New-Item -ItemType Directory -Force -Path $Paths.State | Out-Null
    } catch {
      throw 'E_CONFIG_BUSY: Configuration initialization is already running.'
    }
    Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $lockPath
    try {
      $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
      )
      $lockOwned = $true
    } catch {
      throw 'E_CONFIG_BUSY: Configuration initialization is already running.'
    }

    Assert-AkashaConfigurationPreflight -Paths $Paths -WeFlowConfigPath $WeFlowConfigPath
    Assert-AkashaConfigurationPathSnapshot -Paths $Paths -Snapshot $pathSnapshot

    $astrConfigPath = Join-Path $Paths.AstrBotData 'data\cmd_config.json'
    $freshAstrBot = -not (Test-Path -LiteralPath $astrConfigPath -PathType Leaf)

    $freshAstrBotCreated = $false
    $bridgeTargetExists = Test-Path -LiteralPath $Paths.BridgeConfig
    $freshBridge = -not $bridgeTargetExists
    $freshBridgeCreated = $false
    $password = $null
    $astrBackup = $null
    $weFlowBackup = $null
    $weFlowWritten = $false
    $operationError = $null
    try {
      if ($freshAstrBot) {
        $password = New-DashboardPassword
        $passwordWasPresent = Test-Path Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD
        $oldPassword = if ($passwordWasPresent) { [string]$env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD } else { $null }
        try {
          $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = $password
          try {
            Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.AstrBotData
            New-Item -ItemType Directory -Path $Paths.AstrBotData -ErrorAction Stop | Out-Null
            $freshAstrBotCreated = $true
            if (-not (Test-AkashaConfigurationPath -Root $Paths.Root -Candidate $Paths.AstrBotData)) {
              throw 'E_CONFIG_PATH: Configuration paths must remain inside the install root.'
            }
            $astrBotOwnership = New-AkashaAstrBotOwnership -Paths $Paths -Snapshot $pathSnapshot
            Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $astrConfigPath
            Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate (Join-Path $Paths.AstrBotData 'FIRST_LOGIN.txt')
            Push-Location $Paths.AstrBotData
            try {
              if ($null -ne $AstrBotInitializer) {
                & $AstrBotInitializer $Paths.AstrBotPython $Paths.AstrBotData
              } else {
                Invoke-AkashaNative -FilePath $Paths.AstrBotPython -Arguments @('-m', 'astrbot.cli.__main__', 'init') -LogPath $Paths.InstallLog -StandardInput @('y') -SensitiveValues @($password) | Out-Null
              }
            } finally {
              Pop-Location
            }
          } catch {
            if ($_.Exception.Message -ceq 'E_CONFIG_PATH: Configuration paths must remain inside the install root.') {
              throw $_
            }
            throw 'E_ASTRBOT_INIT: AstrBot initialization failed.'
          }
        } finally {
          if ($passwordWasPresent) {
            $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = $oldPassword
          } else {
            Remove-Item Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ErrorAction SilentlyContinue
          }
        }
        Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
        if (-not (Test-Path -LiteralPath $astrConfigPath -PathType Leaf)) {
          throw 'E_ASTRBOT_INIT: AstrBot did not create data\cmd_config.json.'
        }
      }

      Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
      $astr = Read-AkashaConfigurationJson -Path $astrConfigPath
      if ($astr.GetType() -ne [System.Management.Automation.PSCustomObject]) {
        throw 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.'
      }
      $dashboardProperty = $astr.PSObject.Properties['dashboard']
      $platformProperty = $astr.PSObject.Properties['platform']
      if ($null -eq $dashboardProperty -or $null -eq $dashboardProperty.Value -or
          $dashboardProperty.Value.GetType() -ne [System.Management.Automation.PSCustomObject] -or
          $null -eq $platformProperty -or $null -eq $platformProperty.Value -or
          $platformProperty.Value -isnot [System.Collections.IList]) {
        throw 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.'
      }
      Set-JsonProperty -Object $astr.dashboard -Name 'enable' -Value $true
      Set-JsonProperty -Object $astr.dashboard -Name 'host' -Value '127.0.0.1'
      Set-JsonProperty -Object $astr.dashboard -Name 'port' -Value 6185

      $akashaPlatform = [pscustomobject][ordered]@{
        id = 'akasha_ob11'
        type = 'aiocqhttp'
        enable = $true
        ws_reverse_host = '127.0.0.1'
        ws_reverse_port = 11229
        ws_reverse_token = ''
      }
      $platforms = New-Object System.Collections.ArrayList
      $akashaPlatformAdded = $false
      foreach ($platform in @($astr.platform)) {
        if ($null -eq $platform -or $platform.GetType() -ne [System.Management.Automation.PSCustomObject]) {
          throw 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.'
        }
        $platformId = if ($null -ne $platform -and $null -ne $platform.PSObject.Properties['id']) {
          [string]$platform.PSObject.Properties['id'].Value
        } else {
          ''
        }
        if ($platformId -ceq 'akasha_ob11') {
          if (-not $akashaPlatformAdded) {
            Set-JsonProperty -Object $platform -Name 'id' -Value 'akasha_ob11'
            Set-JsonProperty -Object $platform -Name 'type' -Value 'aiocqhttp'
            Set-JsonProperty -Object $platform -Name 'enable' -Value $true
            Set-JsonProperty -Object $platform -Name 'ws_reverse_host' -Value '127.0.0.1'
            Set-JsonProperty -Object $platform -Name 'ws_reverse_port' -Value 11229
            Set-JsonProperty -Object $platform -Name 'ws_reverse_token' -Value ''
            [void]$platforms.Add($platform)
            $akashaPlatformAdded = $true
          }
        } else {
          [void]$platforms.Add($platform)
        }
      }
      if (-not $akashaPlatformAdded) {
        [void]$platforms.Add($akashaPlatform)
      }
      Set-JsonProperty -Object $astr -Name 'platform' -Value @($platforms)

      if ($freshBridge) {
        $token = New-HexToken
        $bridgeTemplatePath = Join-Path $Paths.Bridge 'config.example.json'
        Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $bridgeTemplatePath
        $bridge = Read-AkashaConfigurationJson -Path $bridgeTemplatePath
        if ($bridge.GetType() -ne [System.Management.Automation.PSCustomObject]) {
          throw 'E_CONFIGURATION_SCHEMA: Bridge configuration must be a JSON object.'
        }
        Set-JsonProperty -Object $bridge -Name 'access_token' -Value $token
        Set-JsonProperty -Object $bridge -Name 'astrbot_attachments' -Value (Join-Path $Paths.AstrBotData 'data\attachments')
        Set-JsonProperty -Object $bridge -Name 'bot_nicknames' -Value @()
        Set-JsonProperty -Object $bridge -Name 'bot_wxid' -Value ''
        Set-JsonProperty -Object $bridge -Name 'image_caption_api_key' -Value ''
        Set-JsonProperty -Object $bridge -Name 'weflow_base_url' -Value 'http://127.0.0.1:5031'
        Set-JsonProperty -Object $bridge -Name 'weflow_send_api' -Value 'http://127.0.0.1:5031/api/v1/message'
        Set-JsonProperty -Object $bridge -Name 'astrbot_ob_url' -Value 'ws://127.0.0.1:11229/ws'
      } else {
        Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.BridgeConfig
        $bridge = Read-AkashaConfigurationJson -Path $Paths.BridgeConfig
        if ($bridge.GetType() -ne [System.Management.Automation.PSCustomObject]) {
          throw 'E_CONFIGURATION_SCHEMA: Bridge configuration must be a JSON object.'
        }
        $tokenProperty = $bridge.PSObject.Properties['access_token']
        $token = if ($null -eq $tokenProperty) { '' } else { [string]$tokenProperty.Value }
        if ($token -cnotmatch '^[0-9a-f]{64}$') {
          throw 'E_BRIDGE_TOKEN: Existing bridge token is missing or invalid.'
        }
      }

      $weFlow = Read-AkashaConfigurationJson -Path $WeFlowConfigPath
      if ($weFlow.GetType() -ne [System.Management.Automation.PSCustomObject]) {
        throw 'E_CONFIGURATION_SCHEMA: WeFlow configuration must be a JSON object.'
      }
      Set-JsonProperty -Object $weFlow -Name 'httpApiEnabled' -Value $true
      Set-JsonProperty -Object $weFlow -Name 'httpApiHost' -Value '127.0.0.1'
      Set-JsonProperty -Object $weFlow -Name 'httpApiPort' -Value 5031
      Set-JsonProperty -Object $weFlow -Name 'httpApiToken' -Value $token
      Set-JsonProperty -Object $weFlow -Name 'messagePushEnabled' -Value $true
      Set-JsonProperty -Object $weFlow -Name 'messagePushFilterMode' -Value 'all'

      Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
      try {
        if (-not $freshAstrBot) {
          Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
          Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.Backups
          $astrBackup = Backup-AkashaFile -Path $astrConfigPath -BackupRoot $Paths.Backups
        }
        Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
        Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.Backups
        $weFlowBackup = Backup-AkashaFile -Path $WeFlowConfigPath -BackupRoot $Paths.Backups
      } catch {
        if ($_.Exception.Message -ceq 'E_CONFIG_PATH: Configuration paths must remain inside the install root.') {
          throw $_
        }
        throw 'E_CONFIGURATION_BACKUP: Configuration backups could not be created.'
      }

      try {
        Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
        Write-JsonAtomic -Path $astrConfigPath -Value $astr
        Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
        if (@(Get-Process -Name 'WeFlow' -ErrorAction SilentlyContinue).Count -gt 0) {
          throw 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.'
        }
        Write-JsonAtomic -Path $WeFlowConfigPath -Value $weFlow
        $weFlowWritten = $true
        if ($freshBridge) {
          Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
          Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.BridgeData
          New-Item -ItemType Directory -Force -Path $Paths.BridgeData | Out-Null
          Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
          Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.BridgeConfig
          Write-JsonAtomic -Path $Paths.BridgeConfig -Value $bridge
          $freshBridgeCreated = $true
        }
        if ($freshAstrBot) {
          $firstLoginPath = Join-Path $Paths.AstrBotData 'FIRST_LOGIN.txt'
          Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
          $credentialLabel = 'Pass' + 'word: '
          $firstLoginLines = @(
            'AstrBot first login',
            'URL: http://127.0.0.1:6185',
            'Username: astrbot',
            ($credentialLabel + $password),
            '',
            'Change this password immediately after signing in.'
          )
          [System.IO.File]::WriteAllLines($firstLoginPath, $firstLoginLines, (New-Object System.Text.UTF8Encoding($false)))
        }
      } catch {
        if ($_.Exception.Message -ceq 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' -or
            $_.Exception.Message -ceq 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.') {
          throw $_
        }
        throw 'E_CONFIGURATION_WRITE: Configuration files could not be written.'
      }
      if ($freshAstrBot -and
          -not (Complete-AkashaAstrBotOwnership -Paths $Paths -Snapshot $pathSnapshot -Ownership $astrBotOwnership)) {
        throw 'E_CONFIGURATION_WRITE: Configuration files could not be written.'
      }
    } catch {
      $operationError = $_
    }

    if ($null -ne $operationError) {
      $rollbackSucceeded = $true
      if ($weFlowWritten -and $null -ne $weFlowBackup) {
        if (-not (Invoke-AkashaRollbackStep {
              Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.Backups
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $weFlowBackup
              if (@(Get-Process -Name 'WeFlow' -ErrorAction SilentlyContinue).Count -gt 0) {
                throw 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.'
              }
              Copy-Item -LiteralPath $weFlowBackup -Destination $WeFlowConfigPath -Force -ErrorAction Stop
            })) {
          $rollbackSucceeded = $false
        }
      }
      if ($null -ne $astrBackup) {
        if (-not (Invoke-AkashaRollbackStep {
              Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.Backups
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $astrBackup
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $astrConfigPath
              Copy-Item -LiteralPath $astrBackup -Destination $astrConfigPath -Force -ErrorAction Stop
            })) {
          $rollbackSucceeded = $false
        }
      }
      if ($freshBridgeCreated -and (Test-Path -LiteralPath $Paths.BridgeConfig -PathType Leaf)) {
        if (-not (Invoke-AkashaRollbackStep {
              Assert-AkashaConfigurationTransactionPaths -Paths $Paths -Snapshot $pathSnapshot
              Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $Paths.BridgeConfig
              Remove-Item -LiteralPath $Paths.BridgeConfig -Force -ErrorAction Stop
            })) {
          $rollbackSucceeded = $false
        }
      }
      if (-not (Remove-FreshAstrBotData -Paths $Paths -Snapshot $pathSnapshot -Ownership $astrBotOwnership -CleanupRequired $freshAstrBotCreated)) {
        $rollbackSucceeded = $false
      }
      if (-not $rollbackSucceeded) {
        $operationError.Exception.Data['AkashaRollbackFailure'] = 'E_CONFIG_ROLLBACK'
      }
      throw $operationError
    }
  } finally {
    if ($null -ne $astrBotOwnership -and $null -ne $astrBotOwnership.Stream) {
      $astrBotOwnership.Stream.Dispose()
      $astrBotOwnership.Stream = $null
    }
    $removeOwnedLock = $false
    if ($lockOwned) {
      try {
        Assert-AkashaConfigurationTargetPath -Paths $Paths -Snapshot $pathSnapshot -Candidate $lockPath
        $removeOwnedLock = $true
      } catch {
      }
    }
    if ($null -ne $lockStream) {
      $lockStream.Dispose()
    }
    if ($removeOwnedLock) {
      Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  $paths = Get-AkashaBotPaths
  Initialize-AkashaConfiguration -Paths $paths -WeFlowConfigPath (Join-Path $env:APPDATA 'weflow\WeFlow-config.json')
}
