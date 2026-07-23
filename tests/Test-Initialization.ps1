$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$environmentScript = Join-Path $PSScriptRoot '..\scripts\Initialize-Environments.ps1'
$configurationScript = Join-Path $PSScriptRoot '..\scripts\Initialize-Configuration.ps1'

function Assert-True {
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) { throw $Message }
}

function Assert-Equal {
  param($Actual, $Expected, [string]$Message)
  if ($Actual -cne $Expected) {
    throw "$Message Expected=[$Expected] Actual=[$Actual]"
  }
}

function Assert-ThrowsExact {
  param([scriptblock]$Action, [string]$Expected, [string]$Message)
  $actual = '[NO ERROR]'
  $stack = ''
  try {
    & $Action | Out-Null
  } catch {
    $actual = $_.Exception.Message
    $stack = $_.ScriptStackTrace
  }
  if ($actual -cne $Expected) {
    throw "$Message Expected=[$Expected] Actual=[$actual] Stack=[$stack]"
  }
}

if (-not (Test-Path -LiteralPath $environmentScript -PathType Leaf)) {
  throw 'Initialize-Environments.ps1 is missing.'
}
. $environmentScript

$environmentRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('akasha environment root ' + [guid]::NewGuid().ToString('N'))
try {
  Assert-True $environmentRoot.Contains(' ') 'Environment fixture root must exercise space-bearing native arguments.'
  $paths = Get-AkashaBotPaths -Root $environmentRoot
  New-Item -ItemType Directory -Force -Path $paths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $paths.Bridge 'requirements.lock')

  $runnerState = [pscustomobject]@{
    Calls = New-Object System.Collections.ArrayList
  }
  $runner = {
    param($exe, $arguments, $log)
    [void]$runnerState.Calls.Add([pscustomobject]@{
        Exe = [string]$exe
        Arguments = @($arguments)
        Log = [string]$log
      })
    $argumentList = @($arguments)
    $venvIndex = [array]::IndexOf($argumentList, 'venv')
    if ($venvIndex -ge 1 -and $argumentList[$venvIndex - 1] -ceq '-m') {
      $venvRoot = [string]$argumentList[$venvIndex + 1]
      New-Item -ItemType Directory -Force -Path (Join-Path $venvRoot 'Scripts') | Out-Null
      Set-Content -LiteralPath (Join-Path $venvRoot 'Scripts\python.exe') -Value 'fixture' -Encoding ASCII
    }
    if ($argumentList.Count -eq 2 -and $argumentList[0] -ceq '-c') {
      return '{"version":[3,12,13],"bits":"64bit"}'
    }
  }
  $python = [pscustomobject]@{ FilePath = 'C:\fixture\py.exe'; Prefix = @('-3.12') }
  Initialize-AkashaEnvironments -Paths $paths -Python $python -Runner $runner

  Assert-True (Test-Path -LiteralPath $paths.BridgePython -PathType Leaf) 'Bridge venv marker is missing.'
  Assert-True (Test-Path -LiteralPath $paths.AstrBotPython -PathType Leaf) 'AstrBot venv marker is missing.'
  Assert-Equal $runnerState.Calls.Count 8 'Fresh environment initialization used the wrong command count.'
  $lockPath = Join-Path $paths.Bridge 'requirements.lock'
  $probeCode = "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':platform.architecture()[0]}))"
  $expectedCalls = @(
    @{ Exe=$python.FilePath; Args=@('-3.12','-m','venv',$paths.BridgeVenv) },
    @{ Exe=$paths.BridgePython; Args=@('-c',$probeCode) },
    @{ Exe=$python.FilePath; Args=@('-3.12','-m','venv',$paths.AstrBotVenv) },
    @{ Exe=$paths.AstrBotPython; Args=@('-c',$probeCode) },
    @{ Exe=$paths.BridgePython; Args=@('-m','pip','install','--disable-pip-version-check','-r',$lockPath) },
    @{ Exe=$paths.AstrBotPython; Args=@('-m','pip','install','--disable-pip-version-check','astrbot==4.26.6') },
    @{ Exe=$paths.BridgePython; Args=@('-m','pip','check') },
    @{ Exe=$paths.AstrBotPython; Args=@('-m','pip','check') }
  )
  for ($index = 0; $index -lt $expectedCalls.Count; $index++) {
    $actualCall = $runnerState.Calls[$index]
    $expectedCall = $expectedCalls[$index]
    Assert-Equal $actualCall.Exe $expectedCall.Exe "Environment command $index used the wrong executable."
    Assert-Equal (@($actualCall.Arguments).Count) (@($expectedCall.Args).Count) "Environment command $index changed an argument boundary."
    Assert-Equal ($actualCall.Arguments -join '|') (@($expectedCall.Args) -join '|') "Environment command $index used the wrong arguments."
    Assert-Equal $actualCall.Log $paths.InstallLog "Environment command $index used the wrong log path."
  }

  $runnerState.Calls.Clear()
  Initialize-AkashaEnvironments -Paths $paths -Python $python -Runner $runner
  Assert-Equal $runnerState.Calls.Count 6 'Resumed environment initialization recreated an existing venv.'
  $expectedResumeCalls = @($expectedCalls[1], $expectedCalls[3], $expectedCalls[4], $expectedCalls[5], $expectedCalls[6], $expectedCalls[7])
  for ($index = 0; $index -lt 6; $index++) {
    $actualCall = $runnerState.Calls[$index]
    $expectedCall = $expectedResumeCalls[$index]
    Assert-Equal $actualCall.Exe $expectedCall.Exe "Resumed environment command $index used the wrong executable."
    Assert-Equal (@($actualCall.Arguments).Count) (@($expectedCall.Args).Count) "Resumed environment command $index changed an argument boundary."
    Assert-Equal ($actualCall.Arguments -join '|') (@($expectedCall.Args) -join '|') "Resumed environment command $index used the wrong arguments."
    Assert-Equal $actualCall.Log $paths.InstallLog "Resumed environment command $index used the wrong log path."
  }

  $missingMarkerRoot = Join-Path $environmentRoot 'missing-marker'
  $missingMarkerPaths = Get-AkashaBotPaths -Root $missingMarkerRoot
  New-Item -ItemType Directory -Force -Path $missingMarkerPaths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $missingMarkerPaths.Bridge 'requirements.lock')
  $missingMarkerState = [pscustomobject]@{ Count = 0 }
  $missingMarkerRunner = {
    param($exe, $arguments, $log)
    $missingMarkerState.Count++
  }
  Assert-ThrowsExact {
    Initialize-AkashaEnvironments -Paths $missingMarkerPaths -Python $python -Runner $missingMarkerRunner
  } 'E_VENV_CREATE: Virtual environment Python was not created.' 'A runner that omitted the venv marker reported success.'
  Assert-Equal $missingMarkerState.Count 1 'Environment initialization continued after a missing venv marker.'

  $invalidMarkerRoot = Join-Path $environmentRoot 'invalid-marker'
  $invalidMarkerPaths = Get-AkashaBotPaths -Root $invalidMarkerRoot
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $invalidMarkerPaths.BridgePython), $invalidMarkerPaths.Bridge | Out-Null
  Set-Content -LiteralPath $invalidMarkerPaths.BridgePython -Value 'fixture' -Encoding ASCII
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $invalidMarkerPaths.Bridge 'requirements.lock')
  $invalidMarkerState = [pscustomobject]@{ Count = 0 }
  $invalidMarkerRunner = {
    param($exe, $arguments, $log)
    $invalidMarkerState.Count++
    if (@($arguments).Count -eq 2 -and $arguments[0] -ceq '-c') {
      return '{"version":[3,11,9],"bits":"64bit"}'
    }
  }
  Assert-ThrowsExact {
    Initialize-AkashaEnvironments -Paths $invalidMarkerPaths -Python $python -Runner $invalidMarkerRunner
  } 'E_VENV_INVALID: Virtual environment must use Python 3.12 x64.' 'An invalid venv Python marker was accepted.'
  Assert-Equal $invalidMarkerState.Count 1 'Environment initialization continued after an invalid venv probe.'

  $invalidArchitectureRoot = Join-Path $environmentRoot 'invalid-architecture'
  $invalidArchitecturePaths = Get-AkashaBotPaths -Root $invalidArchitectureRoot
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $invalidArchitecturePaths.BridgePython), $invalidArchitecturePaths.Bridge | Out-Null
  Set-Content -LiteralPath $invalidArchitecturePaths.BridgePython -Value 'fixture' -Encoding ASCII
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $invalidArchitecturePaths.Bridge 'requirements.lock')
  $invalidArchitectureSideEffect = Join-Path $invalidArchitectureRoot 'unexpected-runner-side-effect.txt'
  $invalidArchitectureState = [pscustomobject]@{ Calls = New-Object System.Collections.ArrayList }
  $invalidArchitectureRunner = {
    param($exe, $arguments, $log)
    [void]$invalidArchitectureState.Calls.Add([pscustomobject]@{
        Exe = [string]$exe
        Arguments = @($arguments)
        Log = [string]$log
      })
    $argumentList = @($arguments)
    $venvIndex = [array]::IndexOf($argumentList, 'venv')
    if ($venvIndex -ge 1 -and $argumentList[$venvIndex - 1] -ceq '-m') {
      $venvRoot = [string]$argumentList[$venvIndex + 1]
      New-Item -ItemType Directory -Force -Path (Join-Path $venvRoot 'Scripts') | Out-Null
      Set-Content -LiteralPath (Join-Path $venvRoot 'Scripts\python.exe') -Value 'fixture' -Encoding ASCII
      return
    }
    if ($argumentList.Count -eq 2 -and $argumentList[0] -ceq '-c') {
      return '{"version":[3,12,13],"bits":"32bit"}'
    }
    Set-Content -LiteralPath $invalidArchitectureSideEffect -Value 'unexpected' -Encoding ASCII
  }.GetNewClosure()
  Assert-ThrowsExact {
    Initialize-AkashaEnvironments -Paths $invalidArchitecturePaths -Python $python -Runner $invalidArchitectureRunner
  } 'E_VENV_INVALID: Virtual environment must use Python 3.12 x64.' 'A Python 3.12 32-bit venv descriptor was accepted.'
  Assert-Equal $invalidArchitectureState.Calls.Count 1 'Environment initialization continued after a 32-bit venv probe.'
  Assert-Equal $invalidArchitectureState.Calls[0].Exe $invalidArchitecturePaths.BridgePython '32-bit venv test did not fail at the first venv probe.'
  Assert-Equal (@($invalidArchitectureState.Calls[0].Arguments) -join '|') (@('-c', $probeCode) -join '|') '32-bit venv test recorded the wrong probe arguments.'
  Assert-Equal $invalidArchitectureState.Calls[0].Log $invalidArchitecturePaths.InstallLog '32-bit venv test used the wrong log path.'
  Assert-True (-not (Test-Path -LiteralPath $invalidArchitecturePaths.AstrBotVenv)) '32-bit venv probe created the later AstrBot venv.'
  Assert-True (-not (Test-Path -LiteralPath $invalidArchitectureSideEffect)) '32-bit venv probe reached a later pip side effect.'

  $existingRuntimeJunctionRoot = Join-Path $environmentRoot 'existing-runtime-junction'
  $existingRuntimeJunctionPaths = Get-AkashaBotPaths -Root $existingRuntimeJunctionRoot
  New-Item -ItemType Directory -Force -Path $existingRuntimeJunctionPaths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $existingRuntimeJunctionPaths.Bridge 'requirements.lock')
  $existingRuntimeTarget = Join-Path $environmentRoot 'existing-runtime-target'
  New-Item -ItemType Directory -Force -Path $existingRuntimeTarget | Out-Null
  $existingRuntimeSentinel = Join-Path $existingRuntimeTarget 'keep.txt'
  Set-Content -LiteralPath $existingRuntimeSentinel -Value 'keep' -Encoding ASCII
  New-Item -ItemType Junction -Path $existingRuntimeJunctionPaths.Runtime -Target $existingRuntimeTarget | Out-Null
  $existingRuntimeRunnerState = [pscustomobject]@{ Count = 0 }
  $existingRuntimeRunner = {
    param($exe, $arguments, $log)
    $existingRuntimeRunnerState.Count++
  }.GetNewClosure()
  try {
    Assert-ThrowsExact {
      Initialize-AkashaEnvironments -Paths $existingRuntimeJunctionPaths -Python $python -Runner $existingRuntimeRunner
    } 'E_ENVIRONMENT_PATH: Environment paths must remain inside the install root without reparse points.' 'Environment initialization accepted a pre-existing runtime reparse point.'
    Assert-Equal $existingRuntimeRunnerState.Count 0 'Environment initialization invoked the runner before validating runtime paths.'
    Assert-True (Test-Path -LiteralPath $existingRuntimeSentinel -PathType Leaf) 'Environment preflight changed an existing outside file.'
  } finally {
    if ((Test-Path -LiteralPath $existingRuntimeJunctionPaths.Runtime) -and
        ((Get-Item -LiteralPath $existingRuntimeJunctionPaths.Runtime -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($existingRuntimeJunctionPaths.Runtime)
    }
  }

  $junctionVenvRoot = Join-Path $environmentRoot 'junction-venv'
  $junctionVenvPaths = Get-AkashaBotPaths -Root $junctionVenvRoot
  New-Item -ItemType Directory -Force -Path $junctionVenvPaths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $junctionVenvPaths.Bridge 'requirements.lock')
  $junctionVenvTarget = Join-Path $environmentRoot 'junction-venv-target'
  $junctionVenvExecutionMarker = Join-Path $junctionVenvTarget 'runner-executed.txt'
  $junctionVenvState = [pscustomobject]@{ Count = 0 }
  $junctionVenvRunner = {
    param($exe, $arguments, $log)
    $junctionVenvState.Count++
    $argumentList = @($arguments)
    $venvIndex = [array]::IndexOf($argumentList, 'venv')
    if ($venvIndex -ge 1 -and $argumentList[$venvIndex - 1] -ceq '-m') {
      New-Item -ItemType Directory -Force -Path (Join-Path $junctionVenvTarget 'Scripts') | Out-Null
      Set-Content -LiteralPath (Join-Path $junctionVenvTarget 'Scripts\python.exe') -Value 'fixture' -Encoding ASCII
      New-Item -ItemType Junction -Path ([string]$argumentList[$venvIndex + 1]) -Target $junctionVenvTarget | Out-Null
      return
    }
    Set-Content -LiteralPath $junctionVenvExecutionMarker -Value 'unexpected' -Encoding ASCII
    if ($argumentList.Count -eq 2 -and $argumentList[0] -ceq '-c') {
      return '{"version":[3,12,13],"bits":"64bit"}'
    }
  }.GetNewClosure()
  try {
    Assert-ThrowsExact {
      Initialize-AkashaEnvironments -Paths $junctionVenvPaths -Python $python -Runner $junctionVenvRunner
    } 'E_ENVIRONMENT_PATH: Environment paths must remain inside the install root without reparse points.' 'Environment initialization accepted a venv reparse point created by the runner.'
    Assert-Equal $junctionVenvState.Count 1 'Environment initialization invoked a venv Python after the runner changed the local path.'
    Assert-True (-not (Test-Path -LiteralPath $junctionVenvExecutionMarker)) 'Environment initialization executed through a changed venv path.'
  } finally {
    if ((Test-Path -LiteralPath $junctionVenvPaths.BridgeVenv) -and
        ((Get-Item -LiteralPath $junctionVenvPaths.BridgeVenv -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($junctionVenvPaths.BridgeVenv)
    }
  }

  $lateVenvRoot = Join-Path $environmentRoot 'late-junction-venv'
  $lateVenvPaths = Get-AkashaBotPaths -Root $lateVenvRoot
  New-Item -ItemType Directory -Force -Path $lateVenvPaths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $lateVenvPaths.Bridge 'requirements.lock')
  $lateVenvTarget = Join-Path $environmentRoot 'late-junction-venv-target'
  $lateVenvExecutionMarker = Join-Path $lateVenvTarget 'runner-executed.txt'
  $lateVenvState = [pscustomobject]@{ Count = 0 }
  $lateVenvRunner = {
    param($exe, $arguments, $log)
    $lateVenvState.Count++
    $argumentList = @($arguments)
    $venvIndex = [array]::IndexOf($argumentList, 'venv')
    if ($venvIndex -ge 1 -and $argumentList[$venvIndex - 1] -ceq '-m') {
      $venvRoot = [string]$argumentList[$venvIndex + 1]
      New-Item -ItemType Directory -Force -Path (Join-Path $venvRoot 'Scripts') | Out-Null
      Set-Content -LiteralPath (Join-Path $venvRoot 'Scripts\python.exe') -Value 'fixture' -Encoding ASCII
      return
    }
    if ($argumentList.Count -eq 2 -and $argumentList[0] -ceq '-c') {
      if ([string]$exe -ceq [string]$lateVenvPaths.AstrBotPython) {
        Remove-Item -LiteralPath $lateVenvPaths.BridgeVenv -Recurse -Force
        New-Item -ItemType Directory -Force -Path (Join-Path $lateVenvTarget 'Scripts') | Out-Null
        Set-Content -LiteralPath (Join-Path $lateVenvTarget 'Scripts\python.exe') -Value 'fixture' -Encoding ASCII
        New-Item -ItemType Junction -Path $lateVenvPaths.BridgeVenv -Target $lateVenvTarget | Out-Null
      }
      return '{"version":[3,12,13],"bits":"64bit"}'
    }
    Set-Content -LiteralPath $lateVenvExecutionMarker -Value 'unexpected' -Encoding ASCII
  }.GetNewClosure()
  try {
    Assert-ThrowsExact {
      Initialize-AkashaEnvironments -Paths $lateVenvPaths -Python $python -Runner $lateVenvRunner
    } 'E_ENVIRONMENT_PATH: Environment paths must remain inside the install root without reparse points.' 'Environment initialization did not recheck a venv path before pip.'
    Assert-Equal $lateVenvState.Count 4 'Environment initialization invoked pip after a late venv path change.'
    Assert-True (-not (Test-Path -LiteralPath $lateVenvExecutionMarker)) 'Environment initialization wrote through a late changed venv path.'
  } finally {
    if ((Test-Path -LiteralPath $lateVenvPaths.BridgeVenv) -and
        ((Get-Item -LiteralPath $lateVenvPaths.BridgeVenv -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($lateVenvPaths.BridgeVenv)
    }
  }
} finally {
  if (Test-Path -LiteralPath $environmentRoot) {
    Remove-Item -LiteralPath $environmentRoot -Recurse -Force
  }
}

if (-not (Test-Path -LiteralPath $configurationScript -PathType Leaf)) {
  throw 'Initialize-Configuration.ps1 is missing.'
}
. $configurationScript

function Get-FileFingerprint {
  param([string]$Path)
  return [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($Path))
}

function New-AstrBotFixtureValue {
  param([switch]$IncludePlatform)

  $platforms = @()
  if ($IncludePlatform) {
    $platforms = @(
      [ordered]@{
        id = 'fixture-platform'
        type = 'fixture'
        enable = $false
        ws_reverse_host = '0.0.0.0'
        ws_reverse_port = 1
        ws_reverse_token = 'fixture'
        preserve_fixture = 'platform-keep'
      },
      [ordered]@{
        id = 'akasha_ob11'
        type = 'old-type'
        enable = $false
        ws_reverse_host = '0.0.0.0'
        ws_reverse_port = 2
        ws_reverse_token = 'old'
        preserve_fixture = 'akasha-keep'
      }
    )
  }
  return [ordered]@{
    config_version = 2
    preserve_fixture = 'astr-keep'
    dashboard = [ordered]@{
      enable = $false
      username = 'astrbot'
      password = 'hash'
      pbkdf2_password = 'pbkdf2'
      host = '0.0.0.0'
      port = 6000
      preserve_fixture = 'dashboard-keep'
    }
    platform_settings = [ordered]@{
      forward_threshold = 123
      preserve_fixture = 'settings-keep'
      segmented_reply = [ordered]@{
        enable = $false
        preserve_fixture = 'segment-keep'
      }
    }
    platform = $platforms
  }
}

function New-AstrBotInitializerState {
  return [pscustomobject]@{
    Calls = 0
    Python = ''
    Root = ''
    WorkingDirectory = ''
    Password = ''
    OwnershipMarkers = 0
    AstrBotMarkerBeforeCall = $false
    ThrowAfterCreate = $false
    SkipConfig = $false
    InvalidSchema = $false
    MakeFirstLoginDirectory = $false
    LockRollbackFile = $false
    LockStream = $null
  }
}

function New-AstrBotInitializer {
  param([Parameter(Mandatory)]$State)

  return {
    param($pythonExe, $astrBotRoot)

    $State.Calls++
    $State.Python = [string]$pythonExe
    $State.Root = [string]$astrBotRoot
    $State.WorkingDirectory = (Get-Location).Path
    $State.Password = [string]$env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD
    $State.OwnershipMarkers = @(Get-ChildItem -LiteralPath $astrBotRoot -Force -Filter '.akasha-ownership-*.tmp').Count
    $State.AstrBotMarkerBeforeCall = Test-Path -LiteralPath (Join-Path $astrBotRoot '.astrbot') -PathType Leaf
    New-Item -ItemType Directory -Force -Path (Join-Path $astrBotRoot 'data') | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $astrBotRoot '.astrbot') | Out-Null
    if ($State.ThrowAfterCreate) {
      throw 'fixture initializer failure'
    }
    if (-not $State.SkipConfig) {
      $value = if ($State.InvalidSchema) {
        [ordered]@{ config_version = 2; dashboard = $null; platform = 'invalid' }
      } else {
        New-AstrBotFixtureValue
      }
      Write-JsonAtomic -Path (Join-Path $astrBotRoot 'data\cmd_config.json') -Value $value
    }
    if ($State.MakeFirstLoginDirectory) {
      New-Item -ItemType Directory -Force -Path (Join-Path $astrBotRoot 'FIRST_LOGIN.txt') | Out-Null
    }
    if ($State.LockRollbackFile) {
      $lockedPath = Join-Path $astrBotRoot 'rollback.lock'
      [System.IO.File]::WriteAllText($lockedPath, 'fixture')
      $State.LockStream = [System.IO.File]::Open($lockedPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
    }
  }.GetNewClosure()
}

function New-ConfigurationFixture {
  param(
    [Parameter(Mandatory)][string]$BaseRoot,
    [Parameter(Mandatory)][string]$Name
  )

  $caseRoot = Join-Path $BaseRoot $Name
  $paths = Get-AkashaBotPaths -Root (Join-Path $caseRoot 'install root')
  New-Item -ItemType Directory -Force -Path $paths.Bridge | Out-Null
  Copy-Item -LiteralPath (Join-Path $root 'bridge\config.example.json') -Destination (Join-Path $paths.Bridge 'config.example.json')
  Copy-Item -LiteralPath (Join-Path $root 'bridge\requirements.lock') -Destination (Join-Path $paths.Bridge 'requirements.lock')
  $weFlowConfigPath = Join-Path $caseRoot 'external weflow\WeFlow-config.json'
  Write-JsonAtomic -Path $weFlowConfigPath -Value ([ordered]@{
      onboardingDone = $true
      httpApiEnabled = $false
      httpApiHost = '0.0.0.0'
      httpApiPort = 1
      httpApiToken = ''
      messagePushEnabled = $false
      messagePushFilterMode = 'none'
      preserve_fixture = 'keep'
    })
  return [pscustomobject]@{
    Root = $caseRoot
    Paths = $paths
    WeFlowConfigPath = $weFlowConfigPath
  }
}

function Write-ExistingAstrBotFixture {
  param($Paths)

  Write-JsonAtomic -Path (Join-Path $Paths.AstrBotData 'data\cmd_config.json') -Value (New-AstrBotFixtureValue -IncludePlatform)
}

function Write-ExistingBridgeFixture {
  param($Paths, [string]$Token)

  $bridge = Get-Content -LiteralPath (Join-Path $Paths.Bridge 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $bridge.access_token = $Token
  Write-JsonAtomic -Path $Paths.BridgeConfig -Value $bridge
}

function New-UncompletedCalibrationFixture {
  return [pscustomobject][ordered]@{
    schema_version = 1
    completed = $false
    coordinate_space = 'client_area_ratio'
    points = [pscustomobject][ordered]@{
      search_box = $null
      first_result = $null
      message_input = $null
      send_button = $null
    }
    reference = $null
  }
}

function New-CompletedCalibrationFixture {
  return [pscustomobject][ordered]@{
    schema_version = 1
    completed = $true
    coordinate_space = 'client_area_ratio'
    points = [pscustomobject][ordered]@{
      search_box = [pscustomobject][ordered]@{ x = 0.1; y = 0.1 }
      first_result = [pscustomobject][ordered]@{ x = 0.2; y = 0.2 }
      message_input = [pscustomobject][ordered]@{ x = 0.6; y = 0.8 }
      send_button = [pscustomobject][ordered]@{ x = 0.9; y = 0.9 }
    }
    reference = [pscustomobject][ordered]@{
      client_width = 1200
      client_height = 800
      aspect_ratio = 1.5
      dpi = 96
    }
  }
}

$legacyBridgeKeys = @(
  'send_method',
  'weflow_send_api',
  'uia_fixed_search_x',
  'uia_fixed_search_y',
  'uia_fixed_first_result_x',
  'uia_fixed_first_result_y',
  'uia_fixed_input_x',
  'uia_fixed_input_y',
  'uia_fixed_send_x',
  'uia_fixed_send_y',
  'uia_fixed_search_delay',
  'uia_fixed_switch_delay',
  'uia_fixed_paste_delay',
  'uia_fixed_clear_input',
  'uia_fixed_use_enter_to_send'
)

$configurationSource = Get-Content -LiteralPath $configurationScript -Raw -Encoding UTF8
$legacyAllowlistMatch = [regex]::Match(
  $configurationSource,
  '(?s)\$legacyBridgeKeys\s*=\s*@\((?<body>.*?)\)\s*foreach\s*\(\$legacyBridgeKey'
)
Assert-True $legacyAllowlistMatch.Success 'Legacy bridge keys are not isolated in the initializer deletion allowlist.'
$legacyAllowlistBody = $legacyAllowlistMatch.Groups['body'].Value
$configurationOutsideLegacyAllowlist = $configurationSource.Remove($legacyAllowlistMatch.Index, $legacyAllowlistMatch.Length)
foreach ($legacyKey in $legacyBridgeKeys) {
  Assert-True $legacyAllowlistBody.Contains("'$legacyKey'") "Initializer deletion allowlist is missing legacy key: $legacyKey"
  Assert-True (-not $configurationOutsideLegacyAllowlist.Contains($legacyKey)) "Initializer uses a legacy bridge key outside the deletion allowlist: $legacyKey"
}

$configurationRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('akasha-configuration-' + [guid]::NewGuid().ToString('N'))
$dashboardPasswordWasPresent = Test-Path Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD
$originalDashboardPassword = if ($dashboardPasswordWasPresent) { [string]$env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD } else { $null }
$rollbackState = $null
$weFlowFixtureProcess = $null
try {
  Remove-Item Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ErrorAction SilentlyContinue

  $missingWeFlow = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'missing-weflow'
  Remove-Item -LiteralPath $missingWeFlow.WeFlowConfigPath -Force
  $missingWeFlowState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $missingWeFlow.Paths -WeFlowConfigPath $missingWeFlow.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $missingWeFlowState)
  } 'E_WEFLOW_CONFIG_MISSING: Complete the WeFlow first-run wizard, then run the installer again.' 'Missing WeFlow config used the wrong error.'
  Assert-Equal $missingWeFlowState.Calls 0 'Missing WeFlow config invoked AstrBot.'
  Assert-True (-not (Test-Path -LiteralPath $missingWeFlow.Paths.AstrBotData)) 'Missing WeFlow config created AstrBot data.'
  Assert-True (-not (Test-Path -LiteralPath $missingWeFlow.Paths.State)) 'Missing WeFlow config created state before side-effect-free preflight completed.'

  $busy = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'configuration-busy'
  New-Item -ItemType Directory -Force -Path $busy.Paths.State | Out-Null
  $busyLockPath = Join-Path $busy.Paths.State 'configuration.lock'
  $busyLockStream = [System.IO.File]::Open($busyLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  try {
    $busyState = New-AstrBotInitializerState
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $busy.Paths -WeFlowConfigPath $busy.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $busyState)
    } 'E_CONFIG_BUSY: Configuration initialization is already running.' 'A real configuration lock contention used the wrong error.'
    Assert-Equal $busyState.Calls 0 'Busy configuration lock invoked AstrBot.'
    Assert-True (-not (Test-Path -LiteralPath $busy.Paths.AstrBotData)) 'Busy configuration lock created AstrBot data.'
  } finally {
    $busyLockStream.Dispose()
  }

  $runningWeFlow = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'weflow-running'
  $weFlowExecutable = Join-Path $runningWeFlow.Root 'WeFlow.exe'
  Copy-Item -LiteralPath $env:ComSpec -Destination $weFlowExecutable
  $weFlowFixtureProcess = Start-Process -FilePath $weFlowExecutable -ArgumentList @('/c', 'ping', '127.0.0.1', '-n', '30') -WindowStyle Hidden -PassThru
  try {
    $weFlowFixtureProcess.WaitForInputIdle(2000) | Out-Null
  } catch {
  }
  $runningProcess = Get-Process -Id $weFlowFixtureProcess.Id -ErrorAction Stop
  Assert-Equal $runningProcess.ProcessName 'WeFlow' 'Controlled WeFlow process fixture has the wrong process name.'
  $runningWeFlowState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $runningWeFlow.Paths -WeFlowConfigPath $runningWeFlow.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $runningWeFlowState)
  } 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.' 'Running WeFlow did not fail closed.'
  Assert-Equal $runningWeFlowState.Calls 0 'Running WeFlow invoked AstrBot.'
  Assert-True (-not (Test-Path -LiteralPath $runningWeFlow.Paths.State)) 'Running WeFlow created state before side-effect-free preflight completed.'
  Stop-Process -Id $weFlowFixtureProcess.Id -Force -ErrorAction Stop
  $weFlowFixtureProcess.WaitForExit()
  $weFlowFixtureProcess = $null

  $lateWeFlow = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'weflow-started-during-initializer'
  $lateWeFlowExecutable = Join-Path $lateWeFlow.Root 'WeFlow.exe'
  Copy-Item -LiteralPath (Join-Path $PSHOME 'powershell.exe') -Destination $lateWeFlowExecutable
  $lateWeFlowFingerprint = Get-FileFingerprint $lateWeFlow.WeFlowConfigPath
  $lateWeFlowInitializerState = New-AstrBotInitializerState
  $lateWeFlowBaseInitializer = New-AstrBotInitializer $lateWeFlowInitializerState
  $lateWeFlowProcessState = [pscustomobject]@{ Process = $null }
  $lateWeFlowInitializer = {
    param($pythonExe, $astrBotRoot)
    & $lateWeFlowBaseInitializer $pythonExe $astrBotRoot
    $lateWeFlowProcessState.Process = Start-Process -FilePath $lateWeFlowExecutable -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 30') -WorkingDirectory $lateWeFlow.Root -WindowStyle Hidden -PassThru
    try {
      $lateWeFlowProcessState.Process.WaitForInputIdle(2000) | Out-Null
    } catch {
    }
    $lateProcess = Get-Process -Id $lateWeFlowProcessState.Process.Id -ErrorAction Stop
    if ($lateProcess.ProcessName -cne 'WeFlow') {
      throw 'controlled late WeFlow process has the wrong name'
    }
  }.GetNewClosure()
  try {
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $lateWeFlow.Paths -WeFlowConfigPath $lateWeFlow.WeFlowConfigPath -AstrBotInitializer $lateWeFlowInitializer
    } 'E_WEFLOW_RUNNING: Close WeFlow before updating its configuration.' 'WeFlow starting during initialization was not rechecked immediately before write.'
    Assert-Equal (Get-FileFingerprint $lateWeFlow.WeFlowConfigPath) $lateWeFlowFingerprint 'Late WeFlow process changed WeFlow configuration.'
    Assert-True (-not (Test-Path -LiteralPath $lateWeFlow.Paths.BridgeConfig)) 'Late WeFlow process left a fresh bridge config.'
    Assert-True (-not (Test-Path -LiteralPath $lateWeFlow.Paths.AstrBotData)) 'Late WeFlow process left fresh AstrBot data.'
  } finally {
    if ($null -ne $lateWeFlowProcessState.Process -and -not $lateWeFlowProcessState.Process.HasExited) {
      Stop-Process -Id $lateWeFlowProcessState.Process.Id -Force -ErrorAction SilentlyContinue
      $lateWeFlowProcessState.Process.WaitForExit()
    }
  }

  $partialAstrBot = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'partial-astrbot'
  New-Item -ItemType Directory -Force -Path $partialAstrBot.Paths.AstrBotData | Out-Null
  $partialSentinel = Join-Path $partialAstrBot.Paths.AstrBotData 'keep.txt'
  Set-Content -LiteralPath $partialSentinel -Value 'keep' -Encoding ASCII
  $partialState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $partialAstrBot.Paths -WeFlowConfigPath $partialAstrBot.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $partialState)
  } 'E_ASTRBOT_PARTIAL: AstrBot data exists without data\cmd_config.json; move it aside and retry.' 'Partial AstrBot data used the wrong error.'
  Assert-Equal $partialState.Calls 0 'Partial AstrBot data invoked the initializer.'
  Assert-True (Test-Path -LiteralPath $partialSentinel -PathType Leaf) 'Partial pre-existing AstrBot data was deleted.'
  Assert-True (-not (Test-Path -LiteralPath $partialAstrBot.Paths.State)) 'Partial AstrBot data created state before side-effect-free preflight completed.'

  $astrPreflightJunction = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'astr-child-junction-preflight'
  New-Item -ItemType Directory -Force -Path $astrPreflightJunction.Paths.AstrBotData | Out-Null
  $astrPreflightTarget = Join-Path $astrPreflightJunction.Root 'astr-existing-target'
  Write-JsonAtomic -Path (Join-Path $astrPreflightTarget 'cmd_config.json') -Value (New-AstrBotFixtureValue -IncludePlatform)
  New-Item -ItemType Junction -Path (Join-Path $astrPreflightJunction.Paths.AstrBotData 'data') -Target $astrPreflightTarget | Out-Null
  Write-ExistingBridgeFixture -Paths $astrPreflightJunction.Paths -Token ('a' * 64)
  try {
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $astrPreflightJunction.Paths -WeFlowConfigPath $astrPreflightJunction.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
    } 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Configuration preflight accepted an existing changed AstrBot configuration ancestor.'
    Assert-True (-not (Test-Path -LiteralPath $astrPreflightJunction.Paths.State)) 'Existing changed AstrBot ancestor created state before side-effect-free preflight completed.'
  } finally {
    $astrPreflightLink = Join-Path $astrPreflightJunction.Paths.AstrBotData 'data'
    if ((Test-Path -LiteralPath $astrPreflightLink) -and
        ((Get-Item -LiteralPath $astrPreflightLink -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($astrPreflightLink)
    }
  }

  $junctionInitialize = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'junction-initialize'
  $junctionInitializeTarget = Join-Path $junctionInitialize.Root 'junction-target'
  New-Item -ItemType Directory -Force -Path $junctionInitializeTarget | Out-Null
  New-Item -ItemType Junction -Path $junctionInitialize.Paths.Data -Target $junctionInitializeTarget | Out-Null
  try {
    $junctionInitializeState = New-AstrBotInitializerState
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $junctionInitialize.Paths -WeFlowConfigPath $junctionInitialize.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $junctionInitializeState)
    } 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Configuration initialization accepted a reparse-point ancestor.'
    Assert-Equal $junctionInitializeState.Calls 0 'Reparse-point ancestor invoked AstrBot initializer.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $junctionInitializeTarget 'astrbot'))) 'Configuration initialization created AstrBot data through a reparse point.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $junctionInitializeTarget 'state'))) 'Configuration initialization created its lock through a reparse point.'
  } finally {
    [System.IO.Directory]::Delete($junctionInitialize.Paths.Data)
  }

  $junctionCleanup = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'junction-cleanup'
  $junctionTarget = Join-Path $junctionCleanup.Root 'junction-target'
  $junctionAstrBot = Join-Path $junctionTarget 'astrbot'
  New-Item -ItemType Directory -Force -Path $junctionAstrBot | Out-Null
  $junctionSentinel = Join-Path $junctionAstrBot 'keep.txt'
  Set-Content -LiteralPath $junctionSentinel -Value 'keep' -Encoding ASCII
  New-Item -ItemType Junction -Path $junctionCleanup.Paths.Data -Target $junctionTarget | Out-Null
  $junctionCleanupSnapshot = New-AkashaConfigurationPathSnapshot -Paths $junctionCleanup.Paths
  $junctionCleanupResult = Remove-FreshAstrBotData -Paths $junctionCleanup.Paths -Snapshot $junctionCleanupSnapshot -Ownership $null -CleanupRequired $true
  Assert-True (-not $junctionCleanupResult) 'Fresh AstrBot cleanup accepted a reparse-point ancestor.'
  Assert-True (Test-Path -LiteralPath $junctionSentinel -PathType Leaf) 'Fresh AstrBot cleanup traversed a reparse-point ancestor.'
  [System.IO.Directory]::Delete($junctionCleanup.Paths.Data)

  $rootJunction = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'root-junction'
  $rootJunctionTarget = Join-Path $rootJunction.Root 'root-target'
  [System.IO.Directory]::Move($rootJunction.Paths.Root, $rootJunctionTarget)
  New-Item -ItemType Junction -Path $rootJunction.Paths.Root -Target $rootJunctionTarget | Out-Null
  try {
    $rootJunctionState = New-AstrBotInitializerState
    $rootJunctionState.ThrowAfterCreate = $true
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $rootJunction.Paths -WeFlowConfigPath $rootJunction.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $rootJunctionState)
    } 'E_ASTRBOT_INIT: AstrBot initialization failed.' 'Root junction changed the primary initializer failure.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $rootJunctionTarget 'data\astrbot'))) 'Root junction prevented cleanup of fresh AstrBot data within the canonical install root.'
  } finally {
    [System.IO.Directory]::Delete($rootJunction.Paths.Root)
  }

  $backupJunction = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'backup-junction-after-initializer'
  $backupJunctionTarget = Join-Path $backupJunction.Root 'backup-target'
  New-Item -ItemType Directory -Force -Path $backupJunctionTarget | Out-Null
  $backupJunctionSentinel = Join-Path $backupJunctionTarget 'keep.txt'
  Set-Content -LiteralPath $backupJunctionSentinel -Value 'keep' -Encoding ASCII
  $backupJunctionState = New-AstrBotInitializerState
  $backupJunctionBaseInitializer = New-AstrBotInitializer $backupJunctionState
  $backupJunctionInitializer = {
    param($pythonExe, $astrBotRoot)
    & $backupJunctionBaseInitializer $pythonExe $astrBotRoot
    New-Item -ItemType Junction -Path $backupJunction.Paths.Backups -Target $backupJunctionTarget | Out-Null
  }.GetNewClosure()
  try {
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $backupJunction.Paths -WeFlowConfigPath $backupJunction.WeFlowConfigPath -AstrBotInitializer $backupJunctionInitializer
    } 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Configuration initialization accepted a backup reparse point created by the initializer.'
    Assert-True (Test-Path -LiteralPath $backupJunctionSentinel -PathType Leaf) 'Backup path validation changed an existing outside file.'
    Assert-Equal @(Get-ChildItem -LiteralPath $backupJunctionTarget -Force).Count 1 'Configuration initialization wrote through a changed backup path.'
  } finally {
    if ((Test-Path -LiteralPath $backupJunction.Paths.Backups) -and
        ((Get-Item -LiteralPath $backupJunction.Paths.Backups -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($backupJunction.Paths.Backups)
    }
  }

  $mutatedPaths = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'mutated-paths-after-initializer'
  $mutatedPathsState = New-AstrBotInitializerState
  $mutatedPathsBaseInitializer = New-AstrBotInitializer $mutatedPathsState
  $mutatedBridgeData = Join-Path $mutatedPaths.Paths.Root 'data\unrelated'
  $mutatedBridgeConfig = Join-Path $mutatedBridgeData 'config.json'
  $mutatedPathsInitializer = {
    param($pythonExe, $astrBotRoot)
    & $mutatedPathsBaseInitializer $pythonExe $astrBotRoot
    $mutatedPaths.Paths.BridgeData = $mutatedBridgeData
    $mutatedPaths.Paths.BridgeConfig = $mutatedBridgeConfig
  }.GetNewClosure()
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $mutatedPaths.Paths -WeFlowConfigPath $mutatedPaths.WeFlowConfigPath -AstrBotInitializer $mutatedPathsInitializer
  } 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Configuration initialization accepted changed destination paths from the initializer.'
  Assert-True (-not (Test-Path -LiteralPath $mutatedBridgeConfig)) 'Configuration initialization wrote to a changed in-root destination.'

  $astrSubtreeJunction = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'astr-subtree-junction-after-initializer'
  $astrSubtreeTarget = Join-Path $astrSubtreeJunction.Root 'astr-data-target'
  New-Item -ItemType Directory -Force -Path $astrSubtreeTarget | Out-Null
  $astrSubtreeConfig = Join-Path $astrSubtreeTarget 'cmd_config.json'
  Write-JsonAtomic -Path $astrSubtreeConfig -Value (New-AstrBotFixtureValue)
  $astrSubtreeFingerprint = Get-FileFingerprint $astrSubtreeConfig
  $astrSubtreeSentinel = Join-Path $astrSubtreeTarget 'keep.txt'
  Set-Content -LiteralPath $astrSubtreeSentinel -Value 'keep' -Encoding ASCII
  $astrSubtreeInitializer = {
    param($pythonExe, $astrBotRoot)
    New-Item -ItemType Junction -Path (Join-Path $astrBotRoot 'data') -Target $astrSubtreeTarget | Out-Null
  }.GetNewClosure()
  try {
    $astrSubtreeError = $null
    try {
      Initialize-AkashaConfiguration -Paths $astrSubtreeJunction.Paths -WeFlowConfigPath $astrSubtreeJunction.WeFlowConfigPath -AstrBotInitializer $astrSubtreeInitializer
    } catch {
      $astrSubtreeError = $_
    }
    Assert-True ($null -ne $astrSubtreeError) 'Configuration initialization accepted a changed AstrBot configuration ancestor.'
    Assert-Equal $astrSubtreeError.Exception.Message 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Changed AstrBot configuration ancestor changed the primary error.'
    Assert-Equal ([string]$astrSubtreeError.Exception.Data['AkashaRollbackFailure']) 'E_CONFIG_ROLLBACK' 'Changed AstrBot subtree omitted the rollback trust signal.'
    Assert-Equal (Get-FileFingerprint $astrSubtreeConfig) $astrSubtreeFingerprint 'Configuration initialization changed configuration through an untrusted AstrBot ancestor.'
    Assert-True (Test-Path -LiteralPath $astrSubtreeSentinel -PathType Leaf) 'AstrBot path validation changed an existing outside file.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $astrSubtreeJunction.Paths.AstrBotData 'FIRST_LOGIN.txt'))) 'AstrBot path validation wrote FIRST_LOGIN after losing path trust.'
    Assert-True (Test-Path -LiteralPath $astrSubtreeJunction.Paths.AstrBotData -PathType Container) 'Rollback recursively deleted an AstrBot tree after losing subtree trust.'
  } finally {
    $astrSubtreeLink = Join-Path $astrSubtreeJunction.Paths.AstrBotData 'data'
    if ((Test-Path -LiteralPath $astrSubtreeLink) -and
        ((Get-Item -LiteralPath $astrSubtreeLink -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($astrSubtreeLink)
    }
  }

  $directoryReplacement = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'astr-directory-replacement'
  $directoryReplacementMoved = Join-Path $directoryReplacement.Root 'moved-owned-astrbot'
  $directoryReplacementSentinel = Join-Path $directoryReplacement.Root 'preexisting-keep.txt'
  Set-Content -LiteralPath $directoryReplacementSentinel -Value 'keep' -Encoding ASCII
  $directoryReplacementState = [pscustomobject]@{ MoveBlocked = $false }
  $directoryReplacementInitializer = {
    param($pythonExe, $astrBotRoot)
    try {
      [System.IO.Directory]::Move($astrBotRoot, $directoryReplacementMoved)
    } catch {
      $directoryReplacementState.MoveBlocked = $true
      throw
    }
    throw 'owned directory replacement unexpectedly succeeded'
  }.GetNewClosure()
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $directoryReplacement.Paths -WeFlowConfigPath $directoryReplacement.WeFlowConfigPath -AstrBotInitializer $directoryReplacementInitializer
  } 'E_ASTRBOT_INIT: AstrBot initialization failed.' 'Owned AstrBot directory replacement changed the primary initializer error.'
  Assert-True $directoryReplacementState.MoveBlocked 'Exclusive ownership marker did not keep the fresh AstrBot directory identity stable.'
  Assert-True (Test-Path -LiteralPath $directoryReplacementSentinel -PathType Leaf) 'Owned directory cleanup changed a pre-existing outside file.'
  Assert-True (-not (Test-Path -LiteralPath $directoryReplacementMoved)) 'Owned directory replacement left a moved directory behind.'
  Assert-True (-not (Test-Path -LiteralPath $directoryReplacement.Paths.AstrBotData)) 'Blocked directory replacement prevented safe cleanup of the owned AstrBot root.'

  $lateAstrMutation = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'late-astr-path-change'
  $largeAstrValue = New-AstrBotFixtureValue -IncludePlatform
  $largeAstrValue.preserve_fixture = 'x' * (64 * 1024 * 1024)
  Write-JsonAtomic -Path (Join-Path $lateAstrMutation.Paths.AstrBotData 'data\cmd_config.json') -Value $largeAstrValue
  $largeAstrValue = $null
  $lateAstrExternalTarget = Join-Path $lateAstrMutation.Root 'late-astr-target'
  New-Item -ItemType Directory -Force -Path $lateAstrExternalTarget | Out-Null
  $lateAstrExternalConfig = Join-Path $lateAstrExternalTarget 'outside-config.json'
  [System.IO.File]::WriteAllText($lateAstrExternalConfig, '{"outside":true}', (New-Object System.Text.UTF8Encoding($false)))
  $lateAstrExternalFingerprint = Get-FileFingerprint $lateAstrExternalConfig
  $lateAstrExternalSentinel = Join-Path $lateAstrExternalTarget 'keep.txt'
  Set-Content -LiteralPath $lateAstrExternalSentinel -Value 'keep' -Encoding ASCII
  $lateAstrWeFlowFingerprint = Get-FileFingerprint $lateAstrMutation.WeFlowConfigPath
  $lateAstrFirstLogin = Join-Path $lateAstrMutation.Paths.AstrBotData 'FIRST_LOGIN.txt'
  $lateAstrReady = Join-Path $lateAstrMutation.Root 'helper-ready.txt'
  $lateAstrChanged = Join-Path $lateAstrMutation.Root 'helper-changed.txt'
  $pathEncoding = [System.Text.Encoding]::UTF8
  $backupRootBase64 = [Convert]::ToBase64String($pathEncoding.GetBytes([string]$lateAstrMutation.Paths.Backups))
  $firstLoginBase64 = [Convert]::ToBase64String($pathEncoding.GetBytes($lateAstrFirstLogin))
  $externalTargetBase64 = [Convert]::ToBase64String($pathEncoding.GetBytes($lateAstrExternalTarget))
  $readyBase64 = [Convert]::ToBase64String($pathEncoding.GetBytes($lateAstrReady))
  $changedBase64 = [Convert]::ToBase64String($pathEncoding.GetBytes($lateAstrChanged))
  $lateAstrHelperBody = @"
`$ErrorActionPreference = 'Stop'
`$decode = { param(`$value) [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(`$value)) }
`$backupRoot = & `$decode '$backupRootBase64'
`$firstLogin = & `$decode '$firstLoginBase64'
`$externalTarget = & `$decode '$externalTargetBase64'
`$ready = & `$decode '$readyBase64'
`$changed = & `$decode '$changedBase64'
[System.IO.File]::WriteAllText(`$ready, 'ready', [System.Text.Encoding]::ASCII)
`$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (-not (Test-Path -LiteralPath `$backupRoot)) {
  if ([DateTime]::UtcNow -ge `$deadline) { exit 2 }
  Start-Sleep -Milliseconds 1
}
New-Item -ItemType Junction -Path `$firstLogin -Target `$externalTarget | Out-Null
[System.IO.File]::WriteAllText(`$changed, 'changed', [System.Text.Encoding]::ASCII)
"@
  $lateAstrHelperCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($lateAstrHelperBody))
  $lateAstrHelperProcess = $null
  $lateAstrError = $null
  try {
    $lateAstrHelperProcess = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $lateAstrHelperCommand) -WindowStyle Hidden -PassThru
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $lateAstrReady) -and -not $lateAstrHelperProcess.HasExited) {
      if ([DateTime]::UtcNow -ge $readyDeadline) { break }
      Start-Sleep -Milliseconds 10
    }
    Assert-True (Test-Path -LiteralPath $lateAstrReady -PathType Leaf) 'Late path helper did not become ready.'
    try {
      Initialize-AkashaConfiguration -Paths $lateAstrMutation.Paths -WeFlowConfigPath $lateAstrMutation.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
    } catch {
      $lateAstrError = $_
    }
    Assert-True $lateAstrHelperProcess.WaitForExit(10000) 'Late path helper did not finish.'
    Assert-Equal $lateAstrHelperProcess.ExitCode 0 'Late path helper failed.'
    Assert-True (Test-Path -LiteralPath $lateAstrChanged -PathType Leaf) 'Late path helper did not observe the real backup write.'
    Assert-True ($null -ne $lateAstrError) 'Late AstrBot path change was accepted before later transaction writes.'
    Assert-Equal $lateAstrError.Exception.Message 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Late AstrBot path change changed the primary error.'
    Assert-Equal ([string]$lateAstrError.Exception.Data['AkashaRollbackFailure']) 'E_CONFIG_ROLLBACK' 'Late AstrBot path change omitted the rollback trust signal.'
    Assert-Equal (Get-FileFingerprint $lateAstrMutation.WeFlowConfigPath) $lateAstrWeFlowFingerprint 'Late AstrBot path change allowed a later WeFlow write.'
    Assert-True (-not (Test-Path -LiteralPath $lateAstrMutation.Paths.BridgeConfig)) 'Late AstrBot path change allowed a later bridge write.'
    Assert-Equal (Get-FileFingerprint $lateAstrExternalConfig) $lateAstrExternalFingerprint 'Late AstrBot path change modified an outside configuration file.'
    Assert-True (Test-Path -LiteralPath $lateAstrExternalSentinel -PathType Leaf) 'Late AstrBot path validation changed an outside sentinel.'
  } finally {
    if ($null -ne $lateAstrHelperProcess -and -not $lateAstrHelperProcess.HasExited) {
      Stop-Process -Id $lateAstrHelperProcess.Id -Force -ErrorAction SilentlyContinue
      $lateAstrHelperProcess.WaitForExit()
    }
    if ((Test-Path -LiteralPath $lateAstrFirstLogin) -and
        ((Get-Item -LiteralPath $lateAstrFirstLogin -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      [System.IO.Directory]::Delete($lateAstrFirstLogin)
    }
  }

  $outsidePaths = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'outside-path'
  $outsideAstrBotData = Join-Path $outsidePaths.Root 'outside-astrbot'
  $outsidePaths.Paths.AstrBotData = $outsideAstrBotData
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $outsidePaths.Paths -WeFlowConfigPath $outsidePaths.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_CONFIG_PATH: Configuration paths must remain inside the install root.' 'Outside AstrBot data path was accepted.'
  Assert-True (-not (Test-Path -LiteralPath $outsideAstrBotData)) 'Outside AstrBot data was created before path validation.'
  Assert-True (-not (Test-Path -LiteralPath $outsidePaths.Paths.State)) 'Outside configuration path created state before side-effect-free preflight completed.'

  $initializerFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'initializer-failure'
  $initializerFailureState = New-AstrBotInitializerState
  $initializerFailureState.ThrowAfterCreate = $true
  $preservedEnvironmentPassword = 'old-' + ('q' * 24)
  $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = $preservedEnvironmentPassword
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $initializerFailure.Paths -WeFlowConfigPath $initializerFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $initializerFailureState)
  } 'E_ASTRBOT_INIT: AstrBot initialization failed.' 'AstrBot initializer failure used the wrong error.'
  Assert-Equal $initializerFailureState.Calls 1 'AstrBot initializer failure used the wrong call count.'
  Assert-True (-not (Test-Path -LiteralPath $initializerFailure.Paths.AstrBotData)) 'Failed fresh AstrBot initialization left data behind.'
  Assert-True ([string]$env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ceq $preservedEnvironmentPassword) 'Initializer failure did not restore the prior dashboard password environment value.'

  $missingAstrConfig = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'missing-astr-config'
  $missingAstrConfigState = New-AstrBotInitializerState
  $missingAstrConfigState.SkipConfig = $true
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $missingAstrConfig.Paths -WeFlowConfigPath $missingAstrConfig.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $missingAstrConfigState)
  } 'E_ASTRBOT_INIT: AstrBot did not create data\cmd_config.json.' 'Missing AstrBot config used the wrong error.'
  Assert-True (-not (Test-Path -LiteralPath $missingAstrConfig.Paths.AstrBotData)) 'Missing fresh AstrBot config left partial data behind.'

  $invalidWeFlow = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'invalid-weflow'
  [System.IO.File]::WriteAllText($invalidWeFlow.WeFlowConfigPath, '{', (New-Object System.Text.UTF8Encoding($false)))
  $invalidWeFlowFingerprint = Get-FileFingerprint $invalidWeFlow.WeFlowConfigPath
  $invalidWeFlowState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $invalidWeFlow.Paths -WeFlowConfigPath $invalidWeFlow.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $invalidWeFlowState)
  } 'E_CONFIGURATION_JSON: Required configuration JSON is invalid.' 'Invalid WeFlow JSON used the wrong error.'
  Assert-Equal $invalidWeFlowState.Calls 1 'Invalid WeFlow JSON was read before fresh AstrBot initialization.'
  Assert-True (-not (Test-Path -LiteralPath $invalidWeFlow.Paths.AstrBotData)) 'Invalid WeFlow JSON left fresh AstrBot data behind.'
  Assert-True (-not (Test-Path -LiteralPath $invalidWeFlow.Paths.BridgeConfig)) 'Invalid WeFlow JSON created bridge config.'
  Assert-Equal (Get-FileFingerprint $invalidWeFlow.WeFlowConfigPath) $invalidWeFlowFingerprint 'Invalid WeFlow JSON changed on failure.'

  $invalidTemplate = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'invalid-template'
  $templatePath = Join-Path $invalidTemplate.Paths.Bridge 'config.example.json'
  [System.IO.File]::WriteAllText($templatePath, '{', (New-Object System.Text.UTF8Encoding($false)))
  $invalidTemplateWeFlowFingerprint = Get-FileFingerprint $invalidTemplate.WeFlowConfigPath
  $invalidTemplateState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $invalidTemplate.Paths -WeFlowConfigPath $invalidTemplate.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $invalidTemplateState)
  } 'E_CONFIGURATION_JSON: Required configuration JSON is invalid.' 'Invalid bridge template JSON used the wrong error.'
  Assert-True (-not (Test-Path -LiteralPath $invalidTemplate.Paths.AstrBotData)) 'Invalid bridge template left fresh AstrBot data behind.'
  Assert-Equal (Get-FileFingerprint $invalidTemplate.WeFlowConfigPath) $invalidTemplateWeFlowFingerprint 'Invalid bridge template changed WeFlow config.'

  $invalidAstrSchema = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'invalid-astr-schema'
  $invalidAstrSchemaState = New-AstrBotInitializerState
  $invalidAstrSchemaState.InvalidSchema = $true
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $invalidAstrSchema.Paths -WeFlowConfigPath $invalidAstrSchema.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $invalidAstrSchemaState)
  } 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.' 'Invalid AstrBot schema used the wrong error.'
  Assert-True (-not (Test-Path -LiteralPath $invalidAstrSchema.Paths.AstrBotData)) 'Invalid AstrBot schema left fresh data behind.'

  foreach ($astrSchemaCase in @(
      @{ Name='dashboard-scalar'; Dashboard='text'; Platform=@() },
      @{ Name='platform-scalar'; Dashboard=[ordered]@{ enable=$false }; Platform=123 }
    )) {
    $invalidExistingAstr = New-ConfigurationFixture -BaseRoot $configurationRoot -Name $astrSchemaCase.Name
    Write-JsonAtomic -Path (Join-Path $invalidExistingAstr.Paths.AstrBotData 'data\cmd_config.json') -Value ([ordered]@{
        dashboard = $astrSchemaCase.Dashboard
        platform = $astrSchemaCase.Platform
      })
    Write-ExistingBridgeFixture -Paths $invalidExistingAstr.Paths -Token ('c' * 64)
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $invalidExistingAstr.Paths -WeFlowConfigPath $invalidExistingAstr.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
    } 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.' "AstrBot $($astrSchemaCase.Name) schema used the wrong error."
  }

  $astrRootArray = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'astr-root-array'
  $astrRootArrayPath = Join-Path $astrRootArray.Paths.AstrBotData 'data\cmd_config.json'
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $astrRootArrayPath) | Out-Null
  [System.IO.File]::WriteAllText($astrRootArrayPath, '[{"dashboard":{"enable":false},"platform":[]}]', (New-Object System.Text.UTF8Encoding($false)))
  Write-ExistingBridgeFixture -Paths $astrRootArray.Paths -Token ('e' * 64)
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $astrRootArray.Paths -WeFlowConfigPath $astrRootArray.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_ASTRBOT_SCHEMA: AstrBot configuration is missing dashboard or platform data.' 'AstrBot single-object root array was accepted as an object.'

  foreach ($weFlowSchemaCase in @(
      @{ Name='weflow-scalar'; Json='42' },
      @{ Name='weflow-array'; Json='[{"onboardingDone":true}]' }
    )) {
    $invalidWeFlowSchema = New-ConfigurationFixture -BaseRoot $configurationRoot -Name $weFlowSchemaCase.Name
    Write-ExistingAstrBotFixture $invalidWeFlowSchema.Paths
    Write-ExistingBridgeFixture -Paths $invalidWeFlowSchema.Paths -Token ('d' * 64)
    [System.IO.File]::WriteAllText($invalidWeFlowSchema.WeFlowConfigPath, $weFlowSchemaCase.Json, (New-Object System.Text.UTF8Encoding($false)))
    Assert-ThrowsExact {
      Initialize-AkashaConfiguration -Paths $invalidWeFlowSchema.Paths -WeFlowConfigPath $invalidWeFlowSchema.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
    } 'E_CONFIGURATION_SCHEMA: WeFlow configuration must be a JSON object.' "WeFlow $($weFlowSchemaCase.Name) schema used the wrong error."
  }

  Remove-Item Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ErrorAction SilentlyContinue
  $fresh = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'fresh-success'
  $freshState = New-AstrBotInitializerState
  $freshConsole = (& {
      Initialize-AkashaConfiguration -Paths $fresh.Paths -WeFlowConfigPath $fresh.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $freshState)
    } 6>&1 | Out-String)
  Assert-Equal $freshState.Calls 1 'Fresh AstrBot initializer used the wrong call count.'
  Assert-Equal $freshState.Python $fresh.Paths.AstrBotPython 'AstrBot initializer used the wrong Python path.'
  Assert-Equal $freshState.Root $fresh.Paths.AstrBotData 'AstrBot initializer used the wrong data root.'
  Assert-Equal $freshState.WorkingDirectory $fresh.Paths.AstrBotData 'AstrBot initializer used the wrong working directory.'
  Assert-Equal $freshState.OwnershipMarkers 1 'Fresh AstrBot initialization did not hold exactly one ownership marker.'
  Assert-True $freshState.AstrBotMarkerBeforeCall 'Fresh AstrBot initialization did not pre-confirm the installer-owned directory before invoking the noninteractive CLI.'
  Assert-True ($freshState.Password.Length -ge 16) 'AstrBot initializer did not receive a generated dashboard password.'
  Assert-True (-not (Test-Path Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD)) 'Fresh success left the dashboard password environment variable set.'
  Assert-True (-not $freshConsole.Contains($freshState.Password)) 'Dashboard password appeared in console output.'
  Assert-Equal @(Get-ChildItem -LiteralPath $fresh.Paths.AstrBotData -Force -Filter '.akasha-ownership-*.tmp').Count 0 'Fresh success left an ownership marker behind.'

  $freshAstr = Get-Content -LiteralPath (Join-Path $fresh.Paths.AstrBotData 'data\cmd_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $freshBridge = Get-Content -LiteralPath $fresh.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  $freshWeFlow = Get-Content -LiteralPath $fresh.WeFlowConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $freshToken = [string]$freshBridge.access_token
  Assert-True ($freshToken -cmatch '^[0-9a-f]{64}$') 'Fresh bridge token is not exactly 64 lowercase hexadecimal characters.'
  Assert-True ($freshToken -ceq [string]$freshWeFlow.httpApiToken) 'Bridge and WeFlow tokens differ.'
  Assert-True ([bool]$freshAstr.dashboard.enable) 'AstrBot dashboard was not enabled.'
  Assert-Equal ([string]$freshAstr.dashboard.host) '127.0.0.1' 'AstrBot dashboard host is wrong.'
  Assert-Equal ([int]$freshAstr.dashboard.port) 6185 'AstrBot dashboard port is wrong.'
  Assert-Equal ([int]$freshAstr.platform_settings.forward_threshold) 5000 'AstrBot forward threshold is wrong.'
  Assert-True ([bool]$freshAstr.platform_settings.segmented_reply.enable) 'AstrBot segmented reply was not enabled.'
  Assert-True ([bool]$freshAstr.platform_settings.segmented_reply.only_llm_result) 'AstrBot segmented reply is not limited to LLM results.'
  Assert-Equal ([string]$freshAstr.platform_settings.segmented_reply.interval_method) 'random' 'AstrBot segmented interval method is wrong.'
  Assert-Equal ([string]$freshAstr.platform_settings.segmented_reply.interval) '0.8,1.8' 'AstrBot segmented interval is wrong.'
  Assert-Equal ([int]$freshAstr.platform_settings.segmented_reply.words_count_threshold) 2147483647 'AstrBot segmented threshold is wrong.'
  Assert-Equal ([string]$freshAstr.platform_settings.segmented_reply.split_mode) 'regex' 'AstrBot segmented split mode is wrong.'
  Assert-Equal ([string]$freshAstr.platform_settings.segmented_reply.regex) '.{0,14}?(?:[\u3002\uff1f\uff01~\u2026\uff1b!?;](?![\u3002\uff1f\uff01~\u2026\uff1b!?;])|(?<!\d)\.(?![\d.])|\s(?!\s))|.{1,15}' 'AstrBot segmented regex is wrong.'
  Assert-Equal ([string]$freshAstr.platform_settings.segmented_reply.content_cleanup_rule) '\s+' 'AstrBot content cleanup rule is wrong.'
  $segmentPattern = [string]$freshAstr.platform_settings.segmented_reply.regex
  $segmentCleanupPattern = [string]$freshAstr.platform_settings.segmented_reply.content_cleanup_rule
  $segmentOptions = [System.Text.RegularExpressions.RegexOptions]::Singleline -bor [System.Text.RegularExpressions.RegexOptions]::Multiline
  $fullWidthSpace = [string][char]0x3000
  $chineseQuestion = [string][char]0xff1f
  $chineseExclamation = [string][char]0xff01
  $punctuationRun = $chineseQuestion + $chineseExclamation
  $segmentCases = @(
    [pscustomobject]@{ Name = 'ordinary spaces'; Input = 'alpha beta'; Expected = @('alpha', 'beta') },
    [pscustomobject]@{ Name = 'full-width spaces'; Input = ('alpha' + $fullWidthSpace + 'beta'); Expected = @('alpha', 'beta') },
    [pscustomobject]@{ Name = 'tabs'; Input = "alpha`tbeta"; Expected = @('alpha', 'beta') },
    [pscustomobject]@{ Name = 'blank lines'; Input = "alpha`r`n`r`nbeta"; Expected = @('alpha', 'beta') },
    [pscustomobject]@{ Name = 'punctuation runs'; Input = ('hello' + $punctuationRun + ' next'); Expected = @(('hello' + $punctuationRun), 'next') },
    [pscustomobject]@{ Name = 'decimal points'; Input = 'version3.14stable'; Expected = @('version3.14stab', 'le') },
    [pscustomobject]@{ Name = 'strict fifteen character cap'; Input = 'abcdefghijklmnop'; Expected = @('abcdefghijklmno', 'p') }
  )
  foreach ($segmentCase in $segmentCases) {
    $actualSegments = @(
      [regex]::Matches($segmentCase.Input, $segmentPattern, $segmentOptions) | ForEach-Object {
        $segment = [regex]::Replace($_.Value, $segmentCleanupPattern, '').Trim()
        if ($segment.Length -gt 0) {
          $segment
        }
      }
    )
    Assert-Equal ($actualSegments -join '|') ($segmentCase.Expected -join '|') "AstrBot segmented reply failed the $($segmentCase.Name) case."
    foreach ($actualSegment in $actualSegments) {
      Assert-True ($actualSegment.Length -le 15) "AstrBot segmented reply exceeded 15 characters in the $($segmentCase.Name) case."
    }
  }
  Assert-Equal @($freshAstr.platform).Count 1 'AstrBot platform count is wrong.'
  Assert-Equal ([string]$freshAstr.platform[0].id) 'akasha_ob11' 'AstrBot platform id is wrong.'
  Assert-Equal ([string]$freshAstr.platform[0].type) 'aiocqhttp' 'AstrBot platform type is wrong.'
  Assert-True ([bool]$freshAstr.platform[0].enable) 'AstrBot platform was not enabled.'
  Assert-Equal ([string]$freshAstr.platform[0].ws_reverse_host) '127.0.0.1' 'AstrBot reverse host is wrong.'
  Assert-Equal ([int]$freshAstr.platform[0].ws_reverse_port) 11229 'AstrBot reverse port is wrong.'
  Assert-Equal ([string]$freshAstr.platform[0].ws_reverse_token) '' 'AstrBot reverse token is not empty.'
  Assert-Equal ([string]$freshBridge.weflow_base_url) 'http://127.0.0.1:5031' 'Bridge WeFlow base URL is wrong.'
  Assert-Equal ([string]$freshBridge.astrbot_ob_url) 'ws://127.0.0.1:11229/ws' 'Bridge OneBot URL is wrong.'
  Assert-Equal ([string]$freshBridge.astrbot_attachments) (Join-Path $fresh.Paths.AstrBotData 'data\attachments') 'Bridge attachment path is wrong.'
  Assert-Equal @($freshBridge.bot_nicknames).Count 0 'Fresh bridge nicknames were not cleared.'
  Assert-Equal ([string]$freshBridge.bot_wxid) '' 'Fresh bridge wxid was not cleared.'
  Assert-Equal ([string]$freshBridge.image_caption_api_key) '' 'Fresh bridge image API key was not cleared.'
  $expectedUncompletedCalibration = New-UncompletedCalibrationFixture
  Assert-Equal ($freshBridge.uia_fixed_calibration | ConvertTo-Json -Depth 10 -Compress) ($expectedUncompletedCalibration | ConvertTo-Json -Depth 10 -Compress) 'Fresh bridge calibration is not the exact uncompleted schema.'
  foreach ($legacyKey in $legacyBridgeKeys) {
    Assert-True ($freshBridge.PSObject.Properties.Name -cnotcontains $legacyKey) "Fresh bridge retained legacy key: $legacyKey"
  }
  Assert-True ([bool]$freshWeFlow.httpApiEnabled) 'WeFlow HTTP API was not enabled.'
  Assert-Equal ([string]$freshWeFlow.httpApiHost) '127.0.0.1' 'WeFlow HTTP host is wrong.'
  Assert-Equal ([int]$freshWeFlow.httpApiPort) 5031 'WeFlow HTTP port is wrong.'
  Assert-True ([bool]$freshWeFlow.messagePushEnabled) 'WeFlow message push was not enabled.'
  Assert-Equal ([string]$freshWeFlow.messagePushFilterMode) 'all' 'WeFlow message filter mode is wrong.'
  Assert-Equal ([string]$freshWeFlow.preserve_fixture) 'keep' 'WeFlow unrelated fields were not preserved.'

  $firstLoginPath = Join-Path $fresh.Paths.AstrBotData 'FIRST_LOGIN.txt'
  Assert-True (Test-Path -LiteralPath $firstLoginPath -PathType Leaf) 'FIRST_LOGIN.txt is missing.'
  $firstLoginBytes = [System.IO.File]::ReadAllBytes($firstLoginPath)
  $hasBom = $firstLoginBytes.Length -ge 3 -and $firstLoginBytes[0] -eq 0xEF -and $firstLoginBytes[1] -eq 0xBB -and $firstLoginBytes[2] -eq 0xBF
  Assert-True (-not $hasBom) 'FIRST_LOGIN.txt contains a UTF-8 BOM.'
  $firstLoginText = [System.IO.File]::ReadAllText($firstLoginPath, [System.Text.Encoding]::UTF8)
  Assert-True $firstLoginText.Contains('URL: http://127.0.0.1:6185') 'FIRST_LOGIN.txt is missing the URL.'
  Assert-True $firstLoginText.Contains('Username: astrbot') 'FIRST_LOGIN.txt is missing the username.'
  $firstLoginCredentialLabel = 'Pass' + 'word: '
  Assert-True $firstLoginText.Contains(($firstLoginCredentialLabel + $freshState.Password)) 'FIRST_LOGIN.txt password differs from the initializer environment value.'
  $firstLoginFingerprint = Get-FileFingerprint $firstLoginPath

  $backupRoot = (Resolve-Path -LiteralPath $fresh.Paths.Backups).Path
  $backupFiles = @(Get-ChildItem -LiteralPath $backupRoot -Recurse -File)
  Assert-True ($backupFiles.Count -ge 1) 'Fresh configuration did not back up WeFlow config.'
  foreach ($backupFile in $backupFiles) {
    Assert-True $backupFile.FullName.StartsWith($backupRoot, [System.StringComparison]::OrdinalIgnoreCase) 'A configuration backup escaped Paths.Backups.'
  }

  $repeatState = New-AstrBotInitializerState
  Initialize-AkashaConfiguration -Paths $fresh.Paths -WeFlowConfigPath $fresh.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $repeatState)
  $repeatToken = [string](Get-Content -LiteralPath $fresh.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json).access_token
  Assert-Equal $repeatState.Calls 0 'Repeated configuration invoked AstrBot initializer.'
  Assert-True ($repeatToken -ceq $freshToken) 'Repeated configuration replaced the bridge token.'
  Assert-Equal (Get-FileFingerprint $firstLoginPath) $firstLoginFingerprint 'Repeated configuration replaced FIRST_LOGIN.txt.'

  $existing = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'existing-success'
  Write-ExistingAstrBotFixture $existing.Paths
  $existingToken = 'b' * 64
  Write-ExistingBridgeFixture -Paths $existing.Paths -Token $existingToken
  $existingBridgeBefore = Get-Content -LiteralPath $existing.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  $existingCalibration = New-CompletedCalibrationFixture
  Set-JsonProperty -Object $existingBridgeBefore -Name 'uia_fixed_calibration' -Value $existingCalibration
  Set-JsonProperty -Object $existingBridgeBefore -Name 'unknown_nonlegacy_field' -Value 'preserve-me'
  Write-JsonAtomic -Path $existing.Paths.BridgeConfig -Value $existingBridgeBefore
  $existingCalibrationJson = $existingCalibration | ConvertTo-Json -Depth 10 -Compress
  $existingCalibrationBytes = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($existingCalibrationJson))
  $existingState = New-AstrBotInitializerState
  Initialize-AkashaConfiguration -Paths $existing.Paths -WeFlowConfigPath $existing.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $existingState)
  Assert-Equal $existingState.Calls 0 'Existing AstrBot invoked the initializer.'
  $existingBridgeAfter = Get-Content -LiteralPath $existing.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-True ([string]$existingBridgeAfter.access_token -ceq $existingToken) 'Existing valid bridge token changed.'
  Assert-Equal ([string]$existingBridgeAfter.unknown_nonlegacy_field) 'preserve-me' 'Existing unknown non-legacy bridge field was lost.'
  Assert-Equal ($existingBridgeAfter.uia_fixed_calibration | ConvertTo-Json -Depth 10 -Compress) $existingCalibrationJson 'Existing schema 1 calibration was not preserved exactly.'
  $existingCalibrationBytesAfter = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($existingBridgeAfter.uia_fixed_calibration | ConvertTo-Json -Depth 10 -Compress)))
  Assert-Equal $existingCalibrationBytesAfter $existingCalibrationBytes 'Existing schema 1 calibration was not byte-equivalent after update.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $existing.Paths.AstrBotData 'FIRST_LOGIN.txt'))) 'Existing AstrBot received a new FIRST_LOGIN.txt.'
  $existingAstr = Get-Content -LiteralPath (Join-Path $existing.Paths.AstrBotData 'data\cmd_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal ([string]$existingAstr.preserve_fixture) 'astr-keep' 'Existing AstrBot root field was lost.'
  Assert-Equal ([string]$existingAstr.dashboard.preserve_fixture) 'dashboard-keep' 'Existing AstrBot dashboard field was lost.'
  Assert-Equal ([string]$existingAstr.platform_settings.preserve_fixture) 'settings-keep' 'Existing AstrBot platform settings field was lost.'
  Assert-Equal ([string]$existingAstr.platform_settings.segmented_reply.preserve_fixture) 'segment-keep' 'Existing AstrBot segmented reply field was lost.'
  Assert-Equal @($existingAstr.platform).Count 2 'Akasha platform upsert removed or duplicated an existing platform.'
  $preservedPlatform = @($existingAstr.platform | Where-Object { $_.id -ceq 'fixture-platform' })
  $akashaPlatform = @($existingAstr.platform | Where-Object { $_.id -ceq 'akasha_ob11' })
  Assert-Equal $preservedPlatform.Count 1 'Existing non-Akasha platform was not preserved.'
  Assert-Equal ([string]$preservedPlatform[0].preserve_fixture) 'platform-keep' 'Existing platform unknown field was not preserved.'
  Assert-Equal $akashaPlatform.Count 1 'Akasha platform was not upserted exactly once.'
  Assert-Equal ([string]$akashaPlatform[0].preserve_fixture) 'akasha-keep' 'Akasha platform unknown field was not preserved.'
  Assert-Equal ([string]$akashaPlatform[0].type) 'aiocqhttp' 'Akasha platform controlled field was not updated.'
  Initialize-AkashaConfiguration -Paths $existing.Paths -WeFlowConfigPath $existing.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $existingState)
  $existingAstrRepeat = Get-Content -LiteralPath (Join-Path $existing.Paths.AstrBotData 'data\cmd_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal @($existingAstrRepeat.platform | Where-Object { $_.id -ceq 'akasha_ob11' }).Count 1 'Repeated platform upsert duplicated akasha_ob11.'

  $legacy = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'legacy-bridge-success'
  Write-ExistingAstrBotFixture $legacy.Paths
  $legacyToken = 'c' * 64
  $legacyBridge = [pscustomobject][ordered]@{
    access_token = $legacyToken
    unknown_nonlegacy_field = 'keep-legacy-unknown'
    send_method = 'weflow_api'
    weflow_send_api = 'http://127.0.0.1:5031/api/v1/message'
    uia_fixed_search_x = 0.11
    uia_fixed_search_y = 0.12
    uia_fixed_first_result_x = 0.21
    uia_fixed_first_result_y = 0.22
    uia_fixed_input_x = 0.61
    uia_fixed_input_y = 0.82
    uia_fixed_send_x = 0.91
    uia_fixed_send_y = 0.92
    uia_fixed_search_delay = 0.45
    uia_fixed_switch_delay = 0.75
    uia_fixed_paste_delay = 0.15
    uia_fixed_clear_input = $true
    uia_fixed_use_enter_to_send = $true
  }
  Write-JsonAtomic -Path $legacy.Paths.BridgeConfig -Value $legacyBridge
  Initialize-AkashaConfiguration -Paths $legacy.Paths -WeFlowConfigPath $legacy.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  $legacyBridgeAfter = Get-Content -LiteralPath $legacy.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal ([string]$legacyBridgeAfter.access_token) $legacyToken 'Legacy cleanup changed the valid bridge token.'
  Assert-Equal ([string]$legacyBridgeAfter.unknown_nonlegacy_field) 'keep-legacy-unknown' 'Legacy cleanup removed an unknown non-legacy field.'
  Assert-Equal ($legacyBridgeAfter.uia_fixed_calibration | ConvertTo-Json -Depth 10 -Compress) ($expectedUncompletedCalibration | ConvertTo-Json -Depth 10 -Compress) 'Legacy flat coordinates were migrated instead of adding the uncompleted schema.'
  foreach ($legacyKey in $legacyBridgeKeys) {
    Assert-True ($legacyBridgeAfter.PSObject.Properties.Name -cnotcontains $legacyKey) "Legacy cleanup retained key: $legacyKey"
  }

  $existingBridgeRollback = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'existing-bridge-rollback'
  $rollbackToken = 'e' * 64
  Write-ExistingBridgeFixture -Paths $existingBridgeRollback.Paths -Token $rollbackToken
  $rollbackBridgeValue = Get-Content -LiteralPath $existingBridgeRollback.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  $rollbackBridgeValue.PSObject.Properties.Remove('uia_fixed_calibration')
  Set-JsonProperty -Object $rollbackBridgeValue -Name 'send_method' -Value 'uia'
  Set-JsonProperty -Object $rollbackBridgeValue -Name 'unknown_nonlegacy_field' -Value 'rollback-keep'
  Write-JsonAtomic -Path $existingBridgeRollback.Paths.BridgeConfig -Value $rollbackBridgeValue
  $existingBridgeRollbackFingerprint = Get-FileFingerprint $existingBridgeRollback.Paths.BridgeConfig
  $existingBridgeRollbackState = New-AstrBotInitializerState
  $existingBridgeRollbackState.MakeFirstLoginDirectory = $true
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $existingBridgeRollback.Paths -WeFlowConfigPath $existingBridgeRollback.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $existingBridgeRollbackState)
  } 'E_CONFIGURATION_WRITE: Configuration files could not be written.' 'Existing bridge rollback fixture used the wrong error.'
  Assert-Equal (Get-FileFingerprint $existingBridgeRollback.Paths.BridgeConfig) $existingBridgeRollbackFingerprint 'Existing bridge cleanup was not rolled back after a later transaction failure.'
  $existingBridgeRollbackBackups = @(Get-ChildItem -LiteralPath $existingBridgeRollback.Paths.Backups -Recurse -File)
  Assert-Equal $existingBridgeRollbackBackups.Count 2 'Existing bridge transaction did not back up both bridge and WeFlow configs.'

  $bridgeRollbackArmIndex = $configurationSource.IndexOf('$bridgeRollbackRequired = -not $freshBridge', [System.StringComparison]::Ordinal)
  $bridgeWriterCallIndex = $configurationSource.IndexOf('-Path $Paths.BridgeConfig -Value $bridge', [System.StringComparison]::Ordinal)
  Assert-True ($bridgeRollbackArmIndex -ge 0 -and $bridgeRollbackArmIndex -lt $bridgeWriterCallIndex) 'Existing bridge rollback responsibility is not armed before the atomic writer call.'

  $bridgeCleanupFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'bridge-cleanup-failure-after-replace'
  Write-ExistingAstrBotFixture $bridgeCleanupFailure.Paths
  $bridgeCleanupToken = 'f' * 64
  Write-ExistingBridgeFixture -Paths $bridgeCleanupFailure.Paths -Token $bridgeCleanupToken
  $bridgeCleanupValue = Get-Content -LiteralPath $bridgeCleanupFailure.Paths.BridgeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  $bridgeCleanupValue.PSObject.Properties.Remove('uia_fixed_calibration')
  Set-JsonProperty -Object $bridgeCleanupValue -Name 'send_method' -Value 'uia'
  Set-JsonProperty -Object $bridgeCleanupValue -Name 'unknown_nonlegacy_field' -Value 'cleanup-rollback-keep'
  Write-JsonAtomic -Path $bridgeCleanupFailure.Paths.BridgeConfig -Value $bridgeCleanupValue
  $bridgeCleanupOriginalFingerprint = Get-FileFingerprint $bridgeCleanupFailure.Paths.BridgeConfig
  $bridgeCleanupAstrPath = Join-Path $bridgeCleanupFailure.Paths.AstrBotData 'data\cmd_config.json'
  $bridgeCleanupAstrFingerprint = Get-FileFingerprint $bridgeCleanupAstrPath
  $bridgeCleanupWeFlowFingerprint = Get-FileFingerprint $bridgeCleanupFailure.WeFlowConfigPath
  $bridgeCleanupWriterState = [pscustomobject]@{ TargetWasReplaced = $false; Calls = 0 }
  $bridgeCleanupFailingWriter = {
    param([string]$Path, $Value)
    $bridgeCleanupWriterState.Calls++
    AkashaBot.Common\Write-JsonAtomic -Path $Path -Value $Value
    if ([System.IO.Path]::GetFullPath($Path).Equals([System.IO.Path]::GetFullPath($bridgeCleanupFailure.Paths.BridgeConfig), [System.StringComparison]::OrdinalIgnoreCase)) {
      $bridgeCleanupWriterState.TargetWasReplaced = (Get-FileFingerprint $Path) -cne $bridgeCleanupOriginalFingerprint
      throw 'E_JSON_ATOMIC_CLEANUP: Unable to remove temporary JSON artifacts.'
    }
  }.GetNewClosure()
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $bridgeCleanupFailure.Paths -WeFlowConfigPath $bridgeCleanupFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState)) -JsonWriter $bridgeCleanupFailingWriter
  } 'E_CONFIGURATION_WRITE: Configuration files could not be written.' 'Committed bridge replacement cleanup failure used the wrong transaction error.'
  Assert-True $bridgeCleanupWriterState.TargetWasReplaced 'Cleanup failure injection did not observe a committed bridge replacement.'
  Assert-Equal $bridgeCleanupWriterState.Calls 3 'Cleanup failure injection did not occur on the bridge write after AstrBot and WeFlow writes.'
  Assert-Equal (Get-FileFingerprint $bridgeCleanupFailure.Paths.BridgeConfig) $bridgeCleanupOriginalFingerprint 'Committed bridge replacement was not restored after cleanup failure.'
  Assert-Equal (Get-FileFingerprint $bridgeCleanupAstrPath) $bridgeCleanupAstrFingerprint 'Bridge cleanup failure did not restore AstrBot config.'
  Assert-Equal (Get-FileFingerprint $bridgeCleanupFailure.WeFlowConfigPath) $bridgeCleanupWeFlowFingerprint 'Bridge cleanup failure did not restore WeFlow config.'
  $bridgeCleanupBackupFingerprints = @(
    Get-ChildItem -LiteralPath $bridgeCleanupFailure.Paths.Backups -Recurse -File |
      ForEach-Object { Get-FileFingerprint $_.FullName }
  )
  Assert-True ($bridgeCleanupBackupFingerprints -ccontains $bridgeCleanupOriginalFingerprint) 'Bridge cleanup failure transaction did not retain an original bridge backup.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $bridgeCleanupFailure.Paths.State 'configuration.lock'))) 'Bridge cleanup failure left the transaction lock behind.'
  Assert-Equal @(Get-ChildItem -LiteralPath $bridgeCleanupFailure.Paths.BridgeData -Force -Filter '.config.json.*' -ErrorAction SilentlyContinue).Count 0 'Bridge cleanup failure left atomic JSON artifacts behind.'

  $invalidToken = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'invalid-token'
  Write-ExistingAstrBotFixture $invalidToken.Paths
  Write-ExistingBridgeFixture -Paths $invalidToken.Paths -Token 'bad'
  $invalidTokenAstrFingerprint = Get-FileFingerprint (Join-Path $invalidToken.Paths.AstrBotData 'data\cmd_config.json')
  $invalidTokenBridgeFingerprint = Get-FileFingerprint $invalidToken.Paths.BridgeConfig
  $invalidTokenWeFlowFingerprint = Get-FileFingerprint $invalidToken.WeFlowConfigPath
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $invalidToken.Paths -WeFlowConfigPath $invalidToken.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_BRIDGE_TOKEN: Existing bridge token is missing or invalid.' 'Invalid existing bridge token used the wrong error.'
  Assert-Equal (Get-FileFingerprint (Join-Path $invalidToken.Paths.AstrBotData 'data\cmd_config.json')) $invalidTokenAstrFingerprint 'Invalid token changed AstrBot config.'
  Assert-Equal (Get-FileFingerprint $invalidToken.Paths.BridgeConfig) $invalidTokenBridgeFingerprint 'Invalid token changed bridge config.'
  Assert-Equal (Get-FileFingerprint $invalidToken.WeFlowConfigPath) $invalidTokenWeFlowFingerprint 'Invalid token changed WeFlow config.'

  $missingToken = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'missing-token'
  Write-ExistingAstrBotFixture $missingToken.Paths
  $missingTokenBridge = Get-Content -LiteralPath (Join-Path $missingToken.Paths.Bridge 'config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $missingTokenBridge.PSObject.Properties.Remove('access_token')
  Write-JsonAtomic -Path $missingToken.Paths.BridgeConfig -Value $missingTokenBridge
  $missingTokenFingerprint = Get-FileFingerprint $missingToken.Paths.BridgeConfig
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $missingToken.Paths -WeFlowConfigPath $missingToken.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_BRIDGE_TOKEN: Existing bridge token is missing or invalid.' 'Missing existing bridge token used the wrong error.'
  Assert-Equal (Get-FileFingerprint $missingToken.Paths.BridgeConfig) $missingTokenFingerprint 'Missing token config changed on failure.'

  $bridgeScalar = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'bridge-scalar'
  Write-ExistingAstrBotFixture $bridgeScalar.Paths
  Write-JsonAtomic -Path $bridgeScalar.Paths.BridgeConfig -Value 42
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $bridgeScalar.Paths -WeFlowConfigPath $bridgeScalar.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_CONFIGURATION_SCHEMA: Bridge configuration must be a JSON object.' 'Bridge scalar schema used the wrong error.'

  $bridgeArray = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'bridge-array'
  Write-ExistingAstrBotFixture $bridgeArray.Paths
  New-Item -ItemType Directory -Force -Path $bridgeArray.Paths.BridgeData | Out-Null
  [System.IO.File]::WriteAllText($bridgeArray.Paths.BridgeConfig, '[{"fixture":true}]', (New-Object System.Text.UTF8Encoding($false)))
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $bridgeArray.Paths -WeFlowConfigPath $bridgeArray.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_CONFIGURATION_SCHEMA: Bridge configuration must be a JSON object.' 'Bridge single-object root array was accepted as an object.'

  $bridgePartial = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'bridge-partial'
  Write-ExistingAstrBotFixture $bridgePartial.Paths
  New-Item -ItemType Directory -Force -Path $bridgePartial.Paths.BridgeConfig | Out-Null
  $bridgePartialWeFlowFingerprint = Get-FileFingerprint $bridgePartial.WeFlowConfigPath
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $bridgePartial.Paths -WeFlowConfigPath $bridgePartial.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_BRIDGE_PARTIAL: Bridge config target exists but is not a regular file.' 'Pre-existing bridge config directory was treated as fresh.'
  Assert-True (Test-Path -LiteralPath $bridgePartial.Paths.BridgeConfig -PathType Container) 'Pre-existing bridge config directory was deleted.'
  Assert-Equal (Get-FileFingerprint $bridgePartial.WeFlowConfigPath) $bridgePartialWeFlowFingerprint 'Bridge partial target changed WeFlow config.'
  Assert-True (-not (Test-Path -LiteralPath $bridgePartial.Paths.State)) 'Bridge partial target created state before side-effect-free preflight completed.'

  $lastWriteFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'last-write-failure'
  $lastWriteWeFlowFingerprint = Get-FileFingerprint $lastWriteFailure.WeFlowConfigPath
  $lastWriteState = New-AstrBotInitializerState
  $lastWriteState.MakeFirstLoginDirectory = $true
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $lastWriteFailure.Paths -WeFlowConfigPath $lastWriteFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $lastWriteState)
  } 'E_CONFIGURATION_WRITE: Configuration files could not be written.' 'Real final credential write failure used the wrong error.'
  Assert-Equal (Get-FileFingerprint $lastWriteFailure.WeFlowConfigPath) $lastWriteWeFlowFingerprint 'Final write failure did not restore WeFlow config.'
  Assert-True (-not (Test-Path -LiteralPath $lastWriteFailure.Paths.AstrBotData)) 'Final write failure left fresh AstrBot data.'
  Assert-True (-not (Test-Path -LiteralPath $lastWriteFailure.Paths.BridgeConfig)) 'Final write failure left fresh bridge config.'

  $bridgeWriteFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'bridge-write-failure'
  Write-ExistingAstrBotFixture $bridgeWriteFailure.Paths
  $bridgeWriteAstrPath = Join-Path $bridgeWriteFailure.Paths.AstrBotData 'data\cmd_config.json'
  $bridgeWriteAstrFingerprint = Get-FileFingerprint $bridgeWriteAstrPath
  $bridgeWriteWeFlowFingerprint = Get-FileFingerprint $bridgeWriteFailure.WeFlowConfigPath
  $blockedBridgeParent = Join-Path $bridgeWriteFailure.Paths.Root 'blocked-bridge-parent'
  Set-Content -LiteralPath $blockedBridgeParent -Value 'file' -Encoding ASCII
  $bridgeWriteFailure.Paths.BridgeData = $blockedBridgeParent
  $bridgeWriteFailure.Paths.BridgeConfig = Join-Path $blockedBridgeParent 'config.json'
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $bridgeWriteFailure.Paths -WeFlowConfigPath $bridgeWriteFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer (New-AstrBotInitializerState))
  } 'E_CONFIGURATION_WRITE: Configuration files could not be written.' 'Real bridge write failure used the wrong error.'
  Assert-Equal (Get-FileFingerprint $bridgeWriteAstrPath) $bridgeWriteAstrFingerprint 'Bridge write failure did not restore existing AstrBot config.'
  Assert-Equal (Get-FileFingerprint $bridgeWriteFailure.WeFlowConfigPath) $bridgeWriteWeFlowFingerprint 'Bridge write failure did not restore WeFlow config.'
  Assert-True (Test-Path -LiteralPath $bridgeWriteFailure.Paths.AstrBotData -PathType Container) 'Bridge write failure deleted pre-existing AstrBot data.'

  $backupFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'backup-failure'
  $backupFailureWeFlowFingerprint = Get-FileFingerprint $backupFailure.WeFlowConfigPath
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupFailure.Paths.Backups) | Out-Null
  Set-Content -LiteralPath $backupFailure.Paths.Backups -Value 'file' -Encoding ASCII
  $backupFailureState = New-AstrBotInitializerState
  Assert-ThrowsExact {
    Initialize-AkashaConfiguration -Paths $backupFailure.Paths -WeFlowConfigPath $backupFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $backupFailureState)
  } 'E_CONFIGURATION_BACKUP: Configuration backups could not be created.' 'Backup failure used the wrong error.'
  Assert-Equal (Get-FileFingerprint $backupFailure.WeFlowConfigPath) $backupFailureWeFlowFingerprint 'Backup failure changed WeFlow config.'
  Assert-True (-not (Test-Path -LiteralPath $backupFailure.Paths.AstrBotData)) 'Backup failure left fresh AstrBot data.'

  $rollbackFailure = New-ConfigurationFixture -BaseRoot $configurationRoot -Name 'rollback-failure'
  [System.IO.File]::WriteAllText($rollbackFailure.WeFlowConfigPath, '{', (New-Object System.Text.UTF8Encoding($false)))
  $rollbackState = New-AstrBotInitializerState
  $rollbackState.LockRollbackFile = $true
  $rollbackError = $null
  try {
    Initialize-AkashaConfiguration -Paths $rollbackFailure.Paths -WeFlowConfigPath $rollbackFailure.WeFlowConfigPath -AstrBotInitializer (New-AstrBotInitializer $rollbackState)
  } catch {
    $rollbackError = $_
  }
  Assert-True ($null -ne $rollbackError) 'Rollback failure unexpectedly succeeded.'
  Assert-Equal $rollbackError.Exception.Message 'E_CONFIGURATION_JSON: Required configuration JSON is invalid.' 'Rollback failure masked the primary error.'
  Assert-Equal ([string]$rollbackError.Exception.Data['AkashaRollbackFailure']) 'E_CONFIG_ROLLBACK' 'Rollback failure omitted the fixed secondary signal.'
  Assert-True (-not $rollbackError.Exception.Message.Contains($rollbackState.Password)) 'Rollback error leaked the dashboard password.'
  Assert-True (-not $rollbackError.Exception.Message.Contains($rollbackFailure.Paths.Root)) 'Rollback error leaked an install path.'
  if ($null -ne $rollbackState.LockStream) {
    $rollbackState.LockStream.Dispose()
    $rollbackState.LockStream = $null
  }

  Write-Host 'Initialization tests: PASS' -ForegroundColor Green
} finally {
  if ($null -ne $weFlowFixtureProcess) {
    Stop-Process -Id $weFlowFixtureProcess.Id -Force -ErrorAction SilentlyContinue
  }
  if ($null -ne $rollbackState -and $null -ne $rollbackState.LockStream) {
    $rollbackState.LockStream.Dispose()
  }
  if ($dashboardPasswordWasPresent) {
    $env:ASTRBOT_DASHBOARD_INITIAL_PASSWORD = $originalDashboardPassword
  } else {
    Remove-Item Env:\ASTRBOT_DASHBOARD_INITIAL_PASSWORD -ErrorAction SilentlyContinue
  }
  if (Test-Path -LiteralPath $configurationRoot) {
    Remove-Item -LiteralPath $configurationRoot -Recurse -Force
  }
}
