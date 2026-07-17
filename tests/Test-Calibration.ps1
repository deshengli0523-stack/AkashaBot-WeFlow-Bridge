$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$commonPath = Join-Path $root 'scripts\AkashaBot.Common.psm1'
$startPath = Join-Path $root 'scripts\Start-Services.ps1'
$calibrationPath = Join-Path $root 'scripts\Calibrate-Uia.ps1'

function Assert-True {
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) { throw $Message }
}

function Assert-Equal {
  param($Actual, $Expected, [string]$Message)
  if ([string]$Actual -cne [string]$Expected) {
    throw "$Message Expected=[$Expected] Actual=[$Actual]"
  }
}

function Assert-ThrowsExact {
  param([scriptblock]$Action, [string]$Expected, [string]$Message)
  $actual = '[[NO ERROR]]'
  try { & $Action | Out-Null } catch { $actual = $_.Exception.Message }
  Assert-Equal $actual $Expected $Message
}

function Assert-InvalidCalibrationConfig {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Message)

  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $Path) 'invalid' $Message
  Assert-ThrowsExact {
    Assert-AkashaUiaCalibrationReady -ConfigPath $Path
  } 'E_UIA_CALIBRATION_INVALID: Run the calibration launcher to replace the invalid calibration.' ($Message + ' Assertion used the wrong error.')
}

function Write-Utf8Json {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
  $json = $Value | ConvertTo-Json -Depth 16
  [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function New-ValidCalibration {
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

function Copy-CalibrationValue {
  param([Parameter(Mandatory)]$Value)
  return ($Value | ConvertTo-Json -Depth 16 | ConvertFrom-Json)
}

function Write-CalibrationConfig {
  param([Parameter(Mandatory)][string]$Path, $Calibration)
  Write-Utf8Json -Path $Path -Value ([pscustomobject][ordered]@{ uia_fixed_calibration = $Calibration })
}

function New-CalibrationInstallFixture {
  param([Parameter(Mandatory)][string]$BaseRoot, [Parameter(Mandatory)][string]$Name, [switch]$WithLifecycleDirectories)

  $installRoot = Join-Path $BaseRoot $Name
  $paths = Get-AkashaBotPaths -Root $installRoot
  $directories = @(
    $paths.Bridge,
    (Split-Path -Parent $paths.BridgePython),
    (Split-Path -Parent $paths.AstrBotPython),
    $paths.BridgeData,
    $paths.AstrBotData,
    (Join-Path $installRoot 'external')
  )
  if ($WithLifecycleDirectories) { $directories += @($paths.State, $paths.Logs, $paths.Backups) }
  New-Item -ItemType Directory -Force -Path $directories | Out-Null
  [System.IO.File]::WriteAllText($paths.BridgePython, 'fixture', (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText($paths.AstrBotPython, 'fixture', (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText((Join-Path $paths.Bridge 'main.py'), '# fixture', (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText((Join-Path $paths.Bridge 'calibrate_uia_fixed.py'), '# fixture', (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText((Join-Path $paths.AstrBotData 'fixture.txt'), 'fixture', (New-Object System.Text.UTF8Encoding($false)))
  Write-CalibrationConfig -Path $paths.BridgeConfig -Calibration (New-ValidCalibration)
  $weFlow = Join-Path $installRoot 'external\WeFlow.exe'
  Copy-Item -LiteralPath (Get-Process -Id $PID).Path -Destination $weFlow -Force
  if ($WithLifecycleDirectories) {
    [System.IO.File]::WriteAllText($paths.WeFlowPathState, $weFlow, (New-Object System.Text.UTF8Encoding($false)))
  } else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $paths.WeFlowPathState) | Out-Null
    [System.IO.File]::WriteAllText($paths.WeFlowPathState, $weFlow, (New-Object System.Text.UTF8Encoding($false)))
    Remove-Item -LiteralPath $paths.State -Recurse -Force
  }
  return [pscustomobject]@{ Root = $installRoot; Paths = $paths; WeFlow = $weFlow }
}

function New-RunnerState {
  param([int]$ExitCode)
  return [pscustomobject]@{ ExitCode = $ExitCode; Calls = 0; FilePath = ''; Arguments = @() }
}

function New-TestRunner {
  param([Parameter(Mandatory)]$State)
  return {
    param([string]$FilePath, [object[]]$Arguments)
    $State.Calls++
    $State.FilePath = $FilePath
    $State.Arguments = @($Arguments)
    return [int]$State.ExitCode
  }.GetNewClosure()
}

function Invoke-DirectStartCalibrationProbe {
  param(
    [Parameter(Mandatory)][string]$ScriptPath,
    [Parameter(Mandatory)][string]$InstallRoot,
    [Parameter(Mandatory)][string]$CompilerMarkerPath
  )

  $quotedScriptPath = "'" + $ScriptPath.Replace("'", "''") + "'"
  $quotedInstallRoot = "'" + $InstallRoot.Replace("'", "''") + "'"
  $quotedMarkerPath = "'" + $CompilerMarkerPath.Replace("'", "''") + "'"
  $command = @"
function global:Add-Type {
  [System.IO.File]::WriteAllText($quotedMarkerPath, 'compiler reached')
  throw 'COMPILER_REACHED'
}
try {
  & $quotedScriptPath -InstallRoot $quotedInstallRoot
  [Console]::Out.WriteLine('[[NO ERROR]]')
  exit 0
} catch {
  [Console]::Out.WriteLine(`$_.Exception.Message)
  exit 41
}
"@
  $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
  $output = @(& powershell.exe -NoProfile -EncodedCommand $encoded 2>&1)
  return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = (($output | Out-String).Trim()) }
}

Assert-True (Test-Path -LiteralPath $calibrationPath -PathType Leaf) 'Calibrate-Uia.ps1 is missing.'
Import-Module $commonPath -Force -ErrorAction Stop
. $startPath
. $calibrationPath

$statusCommand = Get-Command Get-AkashaUiaCalibrationStatus -CommandType Function -ErrorAction SilentlyContinue
$assertCommand = Get-Command Assert-AkashaUiaCalibrationReady -CommandType Function -ErrorAction SilentlyContinue
Assert-True ($null -ne $statusCommand) 'Get-AkashaUiaCalibrationStatus is missing.'
Assert-True ($null -ne $assertCommand) 'Assert-AkashaUiaCalibrationReady is missing.'
Assert-True ($null -ne (Get-Command Invoke-AkashaUiaCalibration -CommandType Function -ErrorAction SilentlyContinue)) 'Invoke-AkashaUiaCalibration is missing.'

$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('akashabot-calibration-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
try {
  $requiredDirectRoot = Join-Path $fixtureRoot 'direct-required'
  $requiredCompilerMarker = Join-Path $fixtureRoot 'required-compiler.txt'
  $requiredDirectProbe = Invoke-DirectStartCalibrationProbe -ScriptPath $startPath -InstallRoot $requiredDirectRoot -CompilerMarkerPath $requiredCompilerMarker
  Assert-Equal $requiredDirectProbe.ExitCode 41 'Direct required-calibration probe used the wrong child exit code.'
  Assert-Equal $requiredDirectProbe.Output 'E_UIA_CALIBRATION_REQUIRED: Run the calibration launcher before starting services.' 'Direct start did not reject missing calibration before compiler initialization.'
  Assert-True (-not (Test-Path -LiteralPath $requiredCompilerMarker)) 'Direct missing-calibration start reached Add-Type.'
  Assert-True (-not (Test-Path -LiteralPath $requiredDirectRoot)) 'Direct missing-calibration start created the install root.'

  $invalidDirectRoot = Join-Path $fixtureRoot 'direct-invalid'
  $invalidDirectConfig = Join-Path $invalidDirectRoot 'data\bridge\config.json'
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $invalidDirectConfig) | Out-Null
  [System.IO.File]::WriteAllText($invalidDirectConfig, '{bad', (New-Object System.Text.UTF8Encoding($false)))
  $invalidCompilerMarker = Join-Path $fixtureRoot 'invalid-compiler.txt'
  $invalidDirectProbe = Invoke-DirectStartCalibrationProbe -ScriptPath $startPath -InstallRoot $invalidDirectRoot -CompilerMarkerPath $invalidCompilerMarker
  Assert-Equal $invalidDirectProbe.ExitCode 41 'Direct invalid-calibration probe used the wrong child exit code.'
  Assert-Equal $invalidDirectProbe.Output 'E_UIA_CALIBRATION_INVALID: Run the calibration launcher to replace the invalid calibration.' 'Direct start did not reject invalid calibration before compiler initialization.'
  Assert-True (-not (Test-Path -LiteralPath $invalidCompilerMarker)) 'Direct invalid-calibration start reached Add-Type.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $invalidDirectRoot 'data\logs'))) 'Direct invalid-calibration start created lifecycle logs.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $invalidDirectRoot 'data\state'))) 'Direct invalid-calibration start created lifecycle state.'

  $configPath = Join-Path $fixtureRoot 'config.json'
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Missing config did not require calibration.'

  [System.IO.File]::WriteAllText($configPath, '{}', (New-Object System.Text.UTF8Encoding($false)))
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Missing calibration property did not return required.'

  Write-CalibrationConfig -Path $configPath -Calibration ([pscustomobject]@{})
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Empty calibration did not return required.'

  $missingSchema = Copy-CalibrationValue (New-ValidCalibration)
  $missingSchema.PSObject.Properties.Remove('schema_version')
  Write-CalibrationConfig -Path $configPath -Calibration $missingSchema
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Missing schema property did not return required.'

  $incomplete = New-ValidCalibration
  $incomplete.completed = $false
  Write-CalibrationConfig -Path $configPath -Calibration $incomplete
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Incomplete calibration did not return required.'

  $wrongSchema = New-ValidCalibration
  $wrongSchema.schema_version = 2
  Write-CalibrationConfig -Path $configPath -Calibration $wrongSchema
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Unsupported schema did not return required.'

  Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
  $schemaJson = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  $schemaDecimalJson = [regex]::Replace($schemaJson, '"schema_version"\s*:\s*1', '"schema_version": 1.0', 1)
  [System.IO.File]::WriteAllText($configPath, $schemaDecimalJson, (New-Object System.Text.UTF8Encoding($false)))
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'Decimal schema 1.0 did not return invalid.'

  $schemaNanJson = [regex]::Replace($schemaJson, '"schema_version"\s*:\s*1', '"schema_version": NaN', 1)
  [System.IO.File]::WriteAllText($configPath, $schemaNanJson, (New-Object System.Text.UTF8Encoding($false)))
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'required' 'Non-finite unsupported schema did not return required.'

  [System.IO.File]::WriteAllText($configPath, '{broken-json', (New-Object System.Text.UTF8Encoding($false)))
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'Malformed JSON did not return invalid.'

  $badNumber = Copy-CalibrationValue (New-ValidCalibration)
  $badNumber.points.search_box.x = '0.1'
  Write-CalibrationConfig -Path $configPath -Calibration $badNumber
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'String coordinate did not return invalid.'

  Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
  $coordinateJson = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  $coordinateNanJson = [regex]::Replace($coordinateJson, '"x"\s*:\s*0\.1', '"x": NaN', 1)
  [System.IO.File]::WriteAllText($configPath, $coordinateNanJson, (New-Object System.Text.UTF8Encoding($false)))
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'Non-finite coordinate did not return invalid.'

  foreach ($boundary in @(0, 1)) {
    $boundaryCalibration = Copy-CalibrationValue (New-ValidCalibration)
    $boundaryCalibration.points.send_button.x = $boundary
    Write-CalibrationConfig -Path $configPath -Calibration $boundaryCalibration
    Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' "Boundary coordinate $boundary did not return invalid."
  }

  $badReference = Copy-CalibrationValue (New-ValidCalibration)
  $badReference.reference.client_width = 1200.5
  Write-CalibrationConfig -Path $configPath -Calibration $badReference
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'Fractional reference width did not return invalid.'

  $badAspect = Copy-CalibrationValue (New-ValidCalibration)
  $badAspect.reference.aspect_ratio = 0
  Write-CalibrationConfig -Path $configPath -Calibration $badAspect
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'invalid' 'Non-positive aspect ratio did not return invalid.'

  $caseVariants = @(
    @{ Exact = 'uia_fixed_calibration'; Variant = 'UIA_FIXED_CALIBRATION'; Layer = 'top-level calibration' },
    @{ Exact = 'schema_version'; Variant = 'Schema_Version'; Layer = 'calibration schema' },
    @{ Exact = 'completed'; Variant = 'Completed'; Layer = 'calibration completed' },
    @{ Exact = 'coordinate_space'; Variant = 'Coordinate_Space'; Layer = 'calibration coordinate space' },
    @{ Exact = 'points'; Variant = 'Points'; Layer = 'calibration points' },
    @{ Exact = 'reference'; Variant = 'Reference'; Layer = 'calibration reference' },
    @{ Exact = 'search_box'; Variant = 'Search_Box'; Layer = 'points name' },
    @{ Exact = 'x'; Variant = 'X'; Layer = 'point axis' },
    @{ Exact = 'client_width'; Variant = 'Client_Width'; Layer = 'reference field' }
  )
  foreach ($case in $caseVariants) {
    Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
    $caseJson = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
    $casePattern = '"' + [regex]::Escape([string]$case.Exact) + '"'
    $caseReplacement = '"' + [string]$case.Variant + '"'
    $variantJson = [regex]::Replace($caseJson, $casePattern, $caseReplacement, 1)
    Assert-True ($variantJson -cne $caseJson) "Case-variant fixture did not replace $($case.Exact)."
    [System.IO.File]::WriteAllText($configPath, $variantJson, (New-Object System.Text.UTF8Encoding($false)))
    Assert-InvalidCalibrationConfig -Path $configPath -Message "Case-variant $($case.Layer) property was accepted."
  }

  Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
  $duplicateCaseJson = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  $duplicateCaseJson = $duplicateCaseJson.Insert($duplicateCaseJson.IndexOf('{') + 1, '"UIA_FIXED_CALIBRATION":null,')
  [System.IO.File]::WriteAllText($configPath, $duplicateCaseJson, (New-Object System.Text.UTF8Encoding($false)))
  Assert-InvalidCalibrationConfig -Path $configPath -Message 'Exact calibration property plus a case variant was accepted.'

  foreach ($missingName in @('completed', 'coordinate_space', 'points', 'reference')) {
    $missingOuter = Copy-CalibrationValue (New-ValidCalibration)
    $missingOuter.PSObject.Properties.Remove($missingName)
    Write-CalibrationConfig -Path $configPath -Calibration $missingOuter
    Assert-InvalidCalibrationConfig -Path $configPath -Message "Missing calibration property $missingName did not return invalid."
  }

  $missingReference = Copy-CalibrationValue (New-ValidCalibration)
  $missingReference.reference.PSObject.Properties.Remove('client_width')
  Write-CalibrationConfig -Path $configPath -Calibration $missingReference
  Assert-InvalidCalibrationConfig -Path $configPath -Message 'Missing reference property did not return invalid.'

  Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
  $coordinateTypeJson = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  foreach ($coordinateValue in @('["client_area_ratio"]', '1', 'true')) {
    $typedCoordinateJson = [regex]::Replace(
      $coordinateTypeJson,
      '"coordinate_space"\s*:\s*"client_area_ratio"',
      ('"coordinate_space": ' + $coordinateValue),
      1
    )
    [System.IO.File]::WriteAllText($configPath, $typedCoordinateJson, (New-Object System.Text.UTF8Encoding($false)))
    Assert-InvalidCalibrationConfig -Path $configPath -Message "Non-string coordinate_space value $coordinateValue was accepted."
  }

  $outerExtra = Copy-CalibrationValue (New-ValidCalibration)
  $outerExtra | Add-Member -NotePropertyName extra_outer -NotePropertyValue 'unexpected'
  Write-CalibrationConfig -Path $configPath -Calibration $outerExtra
  Assert-InvalidCalibrationConfig -Path $configPath -Message 'Extra calibration property was accepted.'

  $referenceExtra = Copy-CalibrationValue (New-ValidCalibration)
  $referenceExtra.reference | Add-Member -NotePropertyName extra_reference -NotePropertyValue 'unexpected'
  Write-CalibrationConfig -Path $configPath -Calibration $referenceExtra
  Assert-InvalidCalibrationConfig -Path $configPath -Message 'Extra reference property was accepted.'

  Write-CalibrationConfig -Path $configPath -Calibration (New-ValidCalibration)
  Assert-Equal (Get-AkashaUiaCalibrationStatus -ConfigPath $configPath) 'ready' 'Valid schema did not return ready.'

  Remove-Item -LiteralPath $configPath -Force
  Assert-ThrowsExact {
    Assert-AkashaUiaCalibrationReady -ConfigPath $configPath
  } 'E_UIA_CALIBRATION_REQUIRED: Run the calibration launcher before starting services.' 'Required assertion used the wrong error.'
  [System.IO.File]::WriteAllText($configPath, '{bad', (New-Object System.Text.UTF8Encoding($false)))
  Assert-ThrowsExact {
    Assert-AkashaUiaCalibrationReady -ConfigPath $configPath
  } 'E_UIA_CALIBRATION_INVALID: Run the calibration launcher to replace the invalid calibration.' 'Invalid assertion used the wrong error.'

  $preflight = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'required-preflight'
  [System.IO.File]::WriteAllText($preflight.Paths.BridgeConfig, '{}', (New-Object System.Text.UTF8Encoding($false)))
  Assert-ThrowsExact {
    Start-AkashaServices -InstallRoot $preflight.Root
  } 'E_UIA_CALIBRATION_REQUIRED: Run the calibration launcher before starting services.' 'Start did not fail at the calibration preflight.'
  Assert-True (-not (Test-Path -LiteralPath $preflight.Paths.State)) 'Calibration preflight created the lifecycle state directory.'
  Assert-True (-not (Test-Path -LiteralPath $preflight.Paths.Logs)) 'Calibration preflight created the lifecycle log directory.'
  Assert-True (-not (Test-Path -LiteralPath $preflight.Paths.ProcessState)) 'Calibration preflight changed process state.'

  $invalidPreflight = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'invalid-preflight'
  [System.IO.File]::WriteAllText($invalidPreflight.Paths.BridgeConfig, '{bad', (New-Object System.Text.UTF8Encoding($false)))
  Assert-ThrowsExact {
    Start-AkashaServices -InstallRoot $invalidPreflight.Root
  } 'E_UIA_CALIBRATION_INVALID: Run the calibration launcher to replace the invalid calibration.' 'Start did not reject invalid calibration before lifecycle side effects.'
  Assert-True (-not (Test-Path -LiteralPath $invalidPreflight.Paths.State)) 'Invalid calibration preflight created the lifecycle state directory.'
  Assert-True (-not (Test-Path -LiteralPath $invalidPreflight.Paths.Logs)) 'Invalid calibration preflight created the lifecycle log directory.'
  Assert-True (-not (Test-Path -LiteralPath $invalidPreflight.Paths.ProcessState)) 'Invalid calibration preflight changed process state.'

  $runnerFixture = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'runner' -WithLifecycleDirectories
  $runnerState = New-RunnerState -ExitCode 0
  $runnerResult = Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner (New-TestRunner -State $runnerState)
  Assert-Equal ([int]$runnerResult) 0 'Successful runner did not return 0.'
  Assert-Equal $runnerState.Calls 1 'Calibration runner call count changed.'
  Assert-Equal $runnerState.FilePath $runnerFixture.Paths.BridgePython 'Calibration used the wrong Python interpreter.'
  $expectedArguments = @((Join-Path $runnerFixture.Paths.Bridge 'calibrate_uia_fixed.py'), '--config', $runnerFixture.Paths.BridgeConfig, '--backup-dir', $runnerFixture.Paths.Backups)
  Assert-Equal ($runnerState.Arguments -join '|') ($expectedArguments -join '|') 'Calibration runner arguments changed.'

  $heldLockState = [pscustomobject]@{ Blocked = $false; Calls = 0 }
  $heldLockRunner = {
    param([string]$FilePath, [object[]]$Arguments)
    $heldLockState.Calls++
    $probeStream = $null
    try {
      $probeStream = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
      $heldLockState.Blocked = $true
    } finally {
      if ($null -ne $probeStream) { $probeStream.Dispose() }
    }
    return 0
  }.GetNewClosure()
  Assert-Equal (Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner $heldLockRunner) 0 'Lock-holding runner failed.'
  Assert-Equal $heldLockState.Calls 1 'Lock-holding runner call count changed.'
  Assert-True $heldLockState.Blocked 'Calibration did not retain the lifecycle lock for the entire runner interaction.'

  $lockPath = Join-Path $runnerFixture.Paths.State 'lifecycle.lock'
  $busyStream = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  try {
    Assert-ThrowsExact {
      Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner (New-TestRunner -State (New-RunnerState -ExitCode 0))
    } 'E_UIA_CALIBRATION_BUSY' 'Shared lifecycle lock contention used the wrong error.'
  } finally {
    $busyStream.Dispose()
  }

  $recorded = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'recorded-service' -WithLifecycleDirectories
  $startTime = [datetime]::UtcNow
  $record = [pscustomobject][ordered]@{
    Name = 'bridge'; Pid = 43210; ExecutablePath = $recorded.Paths.BridgePython
    StartTimeUtc = $startTime.ToString('o'); Owned = $true; CommandKind = 'BridgeMain'
  }
  $recordJson = '[' + ($record | ConvertTo-Json -Depth 16 -Compress) + ']'
  [System.IO.File]::WriteAllText($recorded.Paths.ProcessState, $recordJson, (New-Object System.Text.UTF8Encoding($false)))
  $recordedIdentity = [pscustomobject]@{ Pid = 43210; ExecutablePath = $recorded.Paths.BridgePython; CommandLine = 'python.exe main.py'; StartTimeUtc = $startTime }
  $recordedReader = { param([int]$ProcessId) return $recordedIdentity }.GetNewClosure()
  $recordedRunner = New-RunnerState -ExitCode 0
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $recorded.Root -ProcessReader $recordedReader -Runner (New-TestRunner -State $recordedRunner)
  } 'E_UIA_CALIBRATION_BUSY' 'Live recorded service did not block calibration.'
  Assert-Equal $recordedRunner.Calls 0 'Busy recorded service invoked calibration.'

  $bridgePid = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'bridge-pid' -WithLifecycleDirectories
  [System.IO.File]::WriteAllText((Join-Path $bridgePid.Paths.State 'bridge.pid'), '54321', (New-Object System.Text.UTF8Encoding($false)))
  $bridgeIdentity = [pscustomobject]@{ Pid = 54321; ExecutablePath = $bridgePid.Paths.BridgePython; CommandLine = 'python.exe main.py'; StartTimeUtc = [datetime]::UtcNow }
  $bridgeReader = { param([int]$ProcessId) return $bridgeIdentity }.GetNewClosure()
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $bridgePid.Root -ProcessReader $bridgeReader -Runner (New-TestRunner -State (New-RunnerState -ExitCode 0))
  } 'E_UIA_CALIBRATION_BUSY' 'Verified BridgeMain pid did not block calibration.'

  $unknownBridgeIdentity = [pscustomobject]@{ Pid = 54321; ExecutablePath = $bridgePid.Paths.BridgePython; CommandLine = ''; StartTimeUtc = [datetime]::UtcNow }
  $unknownBridgeReader = { param([int]$ProcessId) return $unknownBridgeIdentity }.GetNewClosure()
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $bridgePid.Root -ProcessReader $unknownBridgeReader -Runner (New-TestRunner -State (New-RunnerState -ExitCode 0))
  } 'E_PROCESS_STATE: Unable to verify bridge pid identity.' 'Unreadable BridgePython command line did not fail closed.'

  $unrelated = New-CalibrationInstallFixture -BaseRoot $fixtureRoot -Name 'unrelated-pid' -WithLifecycleDirectories
  [System.IO.File]::WriteAllText((Join-Path $unrelated.Paths.State 'bridge.pid'), [string]$PID, (New-Object System.Text.UTF8Encoding($false)))
  $currentProcess = Get-Process -Id $PID
  $unrelatedIdentity = [pscustomobject]@{ Pid = $PID; ExecutablePath = $currentProcess.Path; CommandLine = 'powershell.exe -NoProfile'; StartTimeUtc = $currentProcess.StartTime.ToUniversalTime() }
  $unrelatedReader = { param([int]$ProcessId) return $unrelatedIdentity }.GetNewClosure()
  $unrelatedState = New-RunnerState -ExitCode 0
  Assert-Equal (Invoke-AkashaUiaCalibration -InstallRoot $unrelated.Root -ProcessReader $unrelatedReader -Runner (New-TestRunner -State $unrelatedState)) 0 'Unrelated bridge.pid was misreported as busy.'
  Assert-True ($null -ne (Get-Process -Id $PID -ErrorAction SilentlyContinue)) 'Calibration terminated an unrelated process.'
  Assert-Equal $unrelatedState.Calls 1 'Unrelated bridge.pid prevented calibration runner execution.'

  $exitCases = @(
    @{ Code = 0; Result = 0; Error = $null },
    @{ Code = 2; Result = 2; Error = $null },
    @{ Code = 20; Result = $null; Error = 'E_UIA_CALIBRATION_INVALID' },
    @{ Code = 21; Result = $null; Error = 'E_UIA_CALIBRATION_WINDOW' },
    @{ Code = 22; Result = $null; Error = 'E_UIA_CALIBRATION_REQUIRED' },
    @{ Code = 23; Result = $null; Error = 'E_UIA_CALIBRATION_BUSY' },
    @{ Code = 24; Result = $null; Error = 'E_UIA_RECALIBRATION_REQUIRED' },
    @{ Code = 7; Result = $null; Error = 'E_UIA_CALIBRATION_INVALID' }
  )
  foreach ($case in $exitCases) {
    $caseState = New-RunnerState -ExitCode ([int]$case.Code)
    $caseRunner = New-TestRunner -State $caseState
    if ($null -eq $case.Error) {
      $actualResult = Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner $caseRunner
      Assert-Equal ([int]$actualResult) ([int]$case.Result) "Runner exit $($case.Code) returned the wrong result."
    } else {
      Assert-ThrowsExact {
        Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner $caseRunner
      } $case.Error "Runner exit $($case.Code) used the wrong fixed error."
    }
  }

  $stringRunner = { param([string]$FilePath, [object[]]$Arguments) return '21' }
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner $stringRunner
  } 'E_UIA_CALIBRATION_INVALID' 'String runner result was parsed as an integer exit code.'
  $noisyRunner = { param([string]$FilePath, [object[]]$Arguments) 'noise'; return 21 }
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner $noisyRunner
  } 'E_UIA_CALIBRATION_INVALID' 'Mixed runner output was parsed as an exit code.'
  $runnerSecret = 'runner-secret-' + ('x' * 20)
  Assert-ThrowsExact {
    Invoke-AkashaUiaCalibration -InstallRoot $runnerFixture.Root -ProcessReader { param([int]$ProcessId) return $null } -Runner { param([string]$FilePath, [object[]]$Arguments) throw $runnerSecret }.GetNewClosure()
  } 'E_UIA_CALIBRATION_INVALID' 'Runner exception was replayed instead of mapped safely.'
  $releasedProbe = New-Object System.IO.FileStream($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  $releasedProbe.Dispose()

  $calibrationSource = Get-Content -LiteralPath $calibrationPath -Raw -Encoding UTF8
  Assert-True ($calibrationSource.Contains('& $paths.BridgePython @arguments')) 'Calibration does not directly invoke Bridge Python.'
  Assert-True (-not $calibrationSource.Contains('Invoke-AkashaNative')) 'Calibration buffers Python output through Invoke-AkashaNative.'
  Assert-True ($calibrationSource -notmatch '(?i)Stop-Process|\.Kill\(|TerminateProcess') 'Calibration contains a process termination path.'
  $startSource = Get-Content -LiteralPath $startPath -Raw -Encoding UTF8
  $bootstrapGateIndex = $startSource.IndexOf('Assert-AkashaUiaCalibrationReady -ConfigPath $bootstrapConfigPath', [System.StringComparison]::Ordinal)
  $nativeTypeIndex = $startSource.IndexOf("if (`$null -eq ('AkashaBotNativePathV1' -as [type]))", [System.StringComparison]::Ordinal)
  Assert-True ($bootstrapGateIndex -ge 0) 'Start is missing the script-level bootstrap calibration gate.'
  Assert-True ($nativeTypeIndex -ge 0 -and $bootstrapGateIndex -lt $nativeTypeIndex) 'Start initializes the native compiler before its script-level calibration gate.'
  Assert-True (@([regex]::Matches($startSource, 'Get-AkashaLifecyclePreflight\s+-Paths\s+\$paths', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)).Count -ge 3) 'Start no longer runs preflight before directories, after lock, and at launch boundaries.'
  Assert-Equal @([regex]::Matches($startSource, 'Assert-AkashaLaunchBoundary\s+-Paths\s+\$paths', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)).Count 3 'Start no longer revalidates all three launch boundaries.'
  foreach ($asciiPath in @($commonPath, $calibrationPath)) {
    Assert-Equal @([System.IO.File]::ReadAllBytes($asciiPath) | Where-Object { $_ -gt 127 }).Count 0 "ASCII-only file contains non-ASCII bytes: $asciiPath"
  }

  $batchName = (-join @([char]0x6821, [char]0x51C6)) + '.bat'
  $batchPath = Join-Path $root $batchName
  Assert-True (Test-Path -LiteralPath $batchPath -PathType Leaf) 'Calibration batch launcher is missing.'
  $batchText = (Get-Content -LiteralPath $batchPath -Raw -Encoding UTF8).Replace("`r`n", "`n").TrimEnd()
  $expectedBatch = @'
@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Calibrate-Uia.ps1" -InstallRoot "%~dp0"
set "code=%ERRORLEVEL%"
echo.
pause
exit /b %code%
'@.Replace("`r`n", "`n").TrimEnd()
  Assert-Equal $batchText $expectedBatch 'Calibration batch launcher semantics changed.'

  Write-Host 'Calibration tests: PASS' -ForegroundColor Green
} finally {
  Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
}
