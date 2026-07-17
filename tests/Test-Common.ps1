$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

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

function Assert-TextExcludes {
  param([AllowEmptyString()][string]$Text, [string[]]$Forbidden, [string]$Message)
  foreach ($value in $Forbidden) {
    if (-not [string]::IsNullOrEmpty($value) -and $Text.Contains($value)) {
      throw "$Message Leaked=[$value]"
    }
  }
}

function Assert-ThrowsExact {
  param([scriptblock]$Action, [string]$Expected, [string]$Message)
  $actual = $null
  try {
    & $Action | Out-Null
    $actual = '[NO ERROR]'
  } catch {
    $actual = $_.Exception.Message
  }
  Assert-Equal $actual $Expected $Message
}

function New-TestSecret {
  param([string]$Label)
  return ('sensitive-' + $Label + '-value-' + ('9' * 16))
}

$modulePath = Join-Path $PSScriptRoot '..\scripts\AkashaBot.Common.psm1'
$prerequisitePath = Join-Path $PSScriptRoot '..\scripts\Test-Prerequisites.ps1'
Assert-True (Test-Path -LiteralPath $prerequisitePath -PathType Leaf) 'Prerequisite validation script is missing.'
Import-Module $modulePath -Force -ErrorAction Stop
$moduleInfo = Get-Module AkashaBot.Common
$pythonError = 'E_PYTHON_312_X64: Python 3.12 x64 was not found. Install it and enable the py launcher or PATH entry.'

$expectedExports = @(
  'Get-AkashaBotPaths',
  'Protect-AkashaLogText',
  'Write-AkashaLog',
  'Write-JsonAtomic',
  'Backup-AkashaFile',
  'Resolve-Python312',
  'Invoke-AkashaNative',
  'Get-WeFlowExecutable'
)
foreach ($name in $expectedExports) {
  $command = Get-Command $name -CommandType Function -ErrorAction SilentlyContinue
  Assert-True ($null -ne $command) "Expected exported function is missing: $name"
}
$actualExports = @((Get-Command -Module AkashaBot.Common -CommandType Function).Name | Sort-Object)
$sortedExpectedExports = @($expectedExports | Sort-Object)
Assert-Equal $actualExports.Count $sortedExpectedExports.Count 'Common module export count changed.'
Assert-Equal ($actualExports -join '|') ($sortedExpectedExports -join '|') 'Common module export surface changed.'

$pythonProbeParser = & $moduleInfo {
  Get-Command ConvertFrom-AkashaPythonProbeOutput -CommandType Function -ErrorAction SilentlyContinue
}
Assert-True ($null -ne $pythonProbeParser) 'Private strict Python probe parser is missing.'
$validPythonRecord = '{"version":[3,12,10],"bits":"64bit"}'
$parsedPythonRecord = & $pythonProbeParser -OutputRecords @($validPythonRecord)
Assert-Equal ([int]$parsedPythonRecord.version[0]) 3 'Strict Python parser changed the major version.'
Assert-Equal ([int]$parsedPythonRecord.version[1]) 12 'Strict Python parser changed the minor version.'
Assert-Equal ([string]$parsedPythonRecord.bits) '64bit' 'Strict Python parser changed the architecture.'
$invalidPythonRecordSets = @(
  @{ Name='whitespace second record'; Records=@($validPythonRecord, '   ') },
  @{ Name='malformed JSON'; Records=@('{not-json') },
  @{ Name='noise record'; Records=@('launcher noise', $validPythonRecord) },
  @{ Name='wrong version'; Records=@('{"version":[3,13,1],"bits":"64bit"}') },
  @{ Name='wrong architecture'; Records=@('{"version":[3,12,10],"bits":"32bit"}') }
)
foreach ($case in $invalidPythonRecordSets) {
  $records = @($case.Records)
  Assert-ThrowsExact { & $pythonProbeParser -OutputRecords $records } $pythonError "Strict Python parser accepted $($case.Name)."
}

$platformJudge = & $moduleInfo {
  Get-Command Test-AkashaWindowsClientDescriptor -CommandType Function -ErrorAction SilentlyContinue
}
$platformAssertion = & $moduleInfo {
  Get-Command Assert-AkashaWindowsClient -CommandType Function -ErrorAction SilentlyContinue
}
$platformRetriever = & $moduleInfo {
  Get-Command Get-AkashaOperatingSystemProductType -CommandType Function -ErrorAction SilentlyContinue
}
$prerequisiteValidator = & $moduleInfo {
  Get-Command Invoke-AkashaPrerequisiteValidation -CommandType Function -ErrorAction SilentlyContinue
}
$displayIconParser = & $moduleInfo {
  Get-Command Resolve-AkashaDisplayIconExecutable -CommandType Function -ErrorAction SilentlyContinue
}
$atomicCleanup = & $moduleInfo {
  Get-Command Remove-AkashaAtomicArtifacts -CommandType Function -ErrorAction SilentlyContinue
}
$atomicOutcome = & $moduleInfo {
  Get-Command Complete-AkashaAtomicOutcome -CommandType Function -ErrorAction SilentlyContinue
}
$exactValueProtector = & $moduleInfo {
  Get-Command Protect-AkashaExactValues -CommandType Function -ErrorAction SilentlyContinue
}
Assert-True ($null -ne $platformJudge) 'Private Windows client descriptor judge is missing.'
Assert-True ($null -ne $platformAssertion) 'Private Windows client assertion is missing.'
Assert-True ($null -ne $platformRetriever) 'Private Windows ProductType retriever is missing.'
Assert-True ($null -ne $prerequisiteValidator) 'Private prerequisite validator is missing.'
Assert-True ($null -ne $displayIconParser) 'Private WeFlow DisplayIcon parser is missing.'
Assert-True ($null -ne $atomicCleanup) 'Private atomic cleanup helper is missing.'
Assert-True ($null -ne $atomicOutcome) 'Private atomic outcome helper is missing.'
Assert-True ($null -ne $exactValueProtector) 'Private native exact-value protector is missing.'
Assert-Equal (& $exactValueProtector -Text 'y' -Values @('y')) '[REDACTED]' 'Standalone short native value was not redacted.'
Assert-Equal (& $exactValueProtector -Text 'Successfully initialized by Python' -Values @('y')) 'Successfully initialized by Python' 'Short native value corrupted a larger word.'
Assert-Equal (& $exactValueProtector -Text 'init' -Values @('init')) '[REDACTED]' 'Standalone init argument was not redacted.'
Assert-Equal (& $exactValueProtector -Text 'initialization' -Values @('init')) 'initialization' 'Init argument corrupted a larger word.'
Assert-Equal (& $exactValueProtector -Text 'initialization complete; mode=init' -Values @('init')) 'initialization complete; mode=[REDACTED]' 'Init argument boundary handling changed unrelated text.'
Assert-Equal (& $exactValueProtector -Text '--Command' -Values @('-Command')) '--Command' 'Native flag protection matched inside a larger flag.'
Assert-Equal (& $exactValueProtector -Text '-Command' -Values @('-Command')) '[REDACTED]' 'Standalone native flag was not redacted.'
$longOpaqueBoundaryValue = 'opaque-' + ('X' * 28)
Assert-Equal (& $exactValueProtector -Text ('value=' + $longOpaqueBoundaryValue + '-suffix') -Values @($longOpaqueBoundaryValue)) 'value=[REDACTED]-suffix' 'Long opaque value leaked before a hyphenated suffix.'
Assert-Equal (& $exactValueProtector -Text ('value=' + $longOpaqueBoundaryValue + 'suffix') -Values @($longOpaqueBoundaryValue)) 'value=[REDACTED]suffix' 'Long opaque value leaked before an alphanumeric suffix.'
Assert-Equal (Protect-AkashaLogText '') '' 'Empty log text was not preserved.'
Assert-True (& $platformJudge -Platform 'Win32NT' -VersionMajor 10 -ProductType 1 -Is64Bit $true) 'Windows 10/11 x64 client descriptor was rejected.'
$unsupportedPlatformCases = @(
  @{ Name='server SKU'; Platform='Win32NT'; VersionMajor=10; ProductType=3; Is64Bit=$true },
  @{ Name='old Windows'; Platform='Win32NT'; VersionMajor=6; ProductType=1; Is64Bit=$true },
  @{ Name='wrong platform'; Platform='Unix'; VersionMajor=10; ProductType=1; Is64Bit=$true },
  @{ Name='32-bit OS'; Platform='Win32NT'; VersionMajor=10; ProductType=1; Is64Bit=$false }
)
foreach ($case in $unsupportedPlatformCases) {
  $accepted = & $platformJudge -Platform $case.Platform -VersionMajor $case.VersionMajor -ProductType $case.ProductType -Is64Bit $case.Is64Bit
  Assert-True (-not $accepted) "Unsupported platform descriptor was accepted: $($case.Name)"
  $fixtureProductType = [int]$case.ProductType
  $unsupportedReader = { [pscustomobject]@{ ProductType = $fixtureProductType } }
  Assert-ThrowsExact {
    & $platformAssertion -Platform $case.Platform -VersionMajor $case.VersionMajor -Is64Bit $case.Is64Bit -OperatingSystemReader $unsupportedReader -Sleeper { param($Milliseconds) }
  } 'E_OS_WINDOWS_CLIENT_X64: Windows 10/11 x64 client is required.' "Unsupported platform assertion used the wrong error: $($case.Name)"
}
$flakyCimState = [pscustomobject]@{ Attempts = 0; Sleeps = 0 }
$flakyCimReader = {
  $flakyCimState.Attempts++
  if ($flakyCimState.Attempts -eq 1) { throw 'transient fixture failure' }
  [pscustomobject]@{ ProductType = 1 }
}
$flakyCimSleeper = { param($Milliseconds) $flakyCimState.Sleeps++ }
$retrievedProductType = & $platformRetriever -OperatingSystemReader $flakyCimReader -Sleeper $flakyCimSleeper -MaxAttempts 3 -DelayMilliseconds 200
Assert-Equal ([int]$retrievedProductType) 1 'Windows ProductType retry did not return the successful record.'
Assert-Equal ([int]$flakyCimState.Attempts) 2 'Windows ProductType retry did not stop after the first success.'
Assert-Equal ([int]$flakyCimState.Sleeps) 1 'Windows ProductType retry used the wrong delay count before success.'

$failedCimState = [pscustomobject]@{ Attempts = 0; Sleeps = 0 }
$failedCimSecret = New-TestSecret 'cim-retry-failure'
$failedCimReader = { $failedCimState.Attempts++; throw $failedCimSecret }
$failedCimSleeper = { param($Milliseconds) $failedCimState.Sleeps++ }
Assert-ThrowsExact {
  & $platformRetriever -OperatingSystemReader $failedCimReader -Sleeper $failedCimSleeper -MaxAttempts 3 -DelayMilliseconds 200
} 'E_OS_DETECTION: Unable to verify Windows 10/11 x64 client.' 'Exhausted CIM retries did not use the fixed safe error.'
Assert-Equal ([int]$failedCimState.Attempts) 3 'Windows ProductType retry used the wrong maximum attempt count.'
Assert-Equal ([int]$failedCimState.Sleeps) 2 'Windows ProductType retry used the wrong delay count on total failure.'
$cimFailureSecret = New-TestSecret 'cim-failure'
$cimFailure = { throw $cimFailureSecret }
$noOpSleeper = { param($Milliseconds) }
Assert-ThrowsExact {
  & $platformAssertion -Platform 'Win32NT' -VersionMajor 10 -Is64Bit $true -OperatingSystemReader $cimFailure -Sleeper $noOpSleeper
} 'E_OS_DETECTION: Unable to verify Windows 10/11 x64 client.' 'CIM failure did not use the fixed safe error.'
$missingProductType = { [pscustomobject]@{ Caption = 'fixture' } }
Assert-ThrowsExact {
  & $platformAssertion -Platform 'Win32NT' -VersionMajor 10 -Is64Bit $true -OperatingSystemReader $missingProductType -Sleeper $noOpSleeper
} 'E_OS_DETECTION: Unable to verify Windows 10/11 x64 client.' 'Missing ProductType did not use the fixed safe error.'

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('akasha-common-' + [guid]::NewGuid().ToString('N'))
try {
  $paths = Get-AkashaBotPaths -Root (Join-Path $testRoot '..\akasha-root\.')
  $expectedRoot = [System.IO.Path]::GetFullPath((Join-Path $testRoot '..\akasha-root\.'))
  $expectedPaths = [ordered]@{
    Root = $expectedRoot
    App = Join-Path $expectedRoot 'app'
    Bridge = Join-Path $expectedRoot 'app\bridge'
    Scripts = Join-Path $expectedRoot 'scripts'
    Runtime = Join-Path $expectedRoot 'runtime'
    BridgeVenv = Join-Path $expectedRoot 'runtime\venvs\bridge'
    AstrBotVenv = Join-Path $expectedRoot 'runtime\venvs\astrbot'
    BridgePython = Join-Path $expectedRoot 'runtime\venvs\bridge\Scripts\python.exe'
    AstrBotPython = Join-Path $expectedRoot 'runtime\venvs\astrbot\Scripts\python.exe'
    Data = Join-Path $expectedRoot 'data'
    BridgeData = Join-Path $expectedRoot 'data\bridge'
    BridgeConfig = Join-Path $expectedRoot 'data\bridge\config.json'
    AstrBotData = Join-Path $expectedRoot 'data\astrbot'
    Logs = Join-Path $expectedRoot 'data\logs'
    State = Join-Path $expectedRoot 'data\state'
    Backups = Join-Path $expectedRoot 'data\backups'
    InstallLog = Join-Path $expectedRoot 'data\logs\install.log'
    ProcessState = Join-Path $expectedRoot 'data\state\processes.json'
    InstallState = Join-Path $expectedRoot 'data\state\install.json'
    WeFlowPathState = Join-Path $expectedRoot 'data\state\weflow-path.txt'
  }
  Assert-Equal @($paths.PSObject.Properties).Count $expectedPaths.Count 'Path object has the wrong field count.'
  foreach ($property in $expectedPaths.Keys) {
    Assert-True ($paths.PSObject.Properties.Name -ccontains $property) "Path property is missing: $property"
    Assert-Equal ([string]$paths.$property) ([string]$expectedPaths[$property]) "Path is incorrect: $property"
  }

  $displayIconRoot = Join-Path $testRoot 'display icon fixtures'
  New-Item -ItemType Directory -Force -Path $displayIconRoot | Out-Null
  $displayIconExe = Join-Path $displayIconRoot 'WeFlow.exe'
  $displayIconDll = Join-Path $displayIconRoot 'WeFlowIcons.dll'
  Set-Content -LiteralPath $displayIconExe -Value 'fixture' -Encoding ASCII
  Set-Content -LiteralPath $displayIconDll -Value 'fixture' -Encoding ASCII
  $resolvedDisplayIconExe = (Resolve-Path -LiteralPath $displayIconExe).Path
  foreach ($displayIconValue in @(
      ('"' + $displayIconExe + '", -12'),
      ($displayIconExe + ',0'),
      ('  "' + $displayIconExe + '"  ,   7  '),
      ('"' + $displayIconExe + '"'))) {
    Assert-Equal (& $displayIconParser -DisplayIcon $displayIconValue) $resolvedDisplayIconExe "DisplayIcon parser rejected: $displayIconValue"
  }
  foreach ($displayIconValue in @(
      ('"' + $displayIconDll + '",-1'),
      ((Join-Path $displayIconRoot 'Missing.exe') + ',0'),
      ($displayIconExe + ',resource'))) {
    Assert-True ($null -eq (& $displayIconParser -DisplayIcon $displayIconValue)) "DisplayIcon parser accepted unsafe input: $displayIconValue"
  }

  $prerequisiteRoot = Join-Path $testRoot 'prerequisite-probe'
  $fixtureOperatingSystemReader = { [pscustomobject]@{ ProductType = 1 } }
  $fixturePythonResolver = { [pscustomobject]@{ FilePath = 'fixture-python.exe'; Prefix = @('-3.12') } }
  $prerequisiteResult = & $prerequisiteValidator -InstallRoot $prerequisiteRoot -OperatingSystemReader $fixtureOperatingSystemReader -Sleeper $noOpSleeper -PythonResolver $fixturePythonResolver
  $prerequisitePaths = Get-AkashaBotPaths -Root $prerequisiteRoot
  Assert-True (Test-Path -LiteralPath $prerequisitePaths.Logs -PathType Container) 'Prerequisite validator did not create Logs.'
  Assert-True (Test-Path -LiteralPath $prerequisitePaths.State -PathType Container) 'Prerequisite validator did not create State.'
  Assert-True (-not (Test-Path -LiteralPath (Join-Path $prerequisitePaths.State '.write-test'))) 'Prerequisite validator write probe was not cleaned.'
  foreach ($unexpected in @($prerequisitePaths.App, $prerequisitePaths.Runtime, $prerequisitePaths.Backups, $prerequisitePaths.BridgeData, $prerequisitePaths.AstrBotData)) {
    Assert-True (-not (Test-Path -LiteralPath $unexpected)) "Prerequisite validator created an out-of-scope path: $unexpected"
  }
  Assert-Equal $prerequisiteResult.Paths.Root ([System.IO.Path]::GetFullPath($prerequisiteRoot)) 'Prerequisite validator returned the wrong root.'
  Assert-True ($null -ne $prerequisiteResult.Python) 'Prerequisite validator omitted Python.'

  $secretCases = @(
    @{ Key='api_key'; Text={ param($value) '{"api_key":"' + $value + '"}' } },
    @{ Key='access_token'; Text={ param($value) "access_token = '$value'" } },
    @{ Key='password'; Text={ param($value) 'password=' + $value } },
    @{ Key='jwt_secret'; Text={ param($value) 'jwt_secret: ' + $value } },
    @{ Key='token'; Text={ param($value) 'token = ' + $value } },
    @{ Key='auth_token'; Text={ param($value) 'auth_token:' + $value } },
    @{ Key='refresh_token'; Text={ param($value) 'refresh_token = ' + $value } },
    @{ Key='jwt'; Text={ param($value) 'jwt=' + $value } },
    @{ Key='client_secret'; Text={ param($value) 'client_secret: "' + $value + '"' } }
  )
  $allSecrets = @()
  foreach ($case in $secretCases) {
    $secret = New-TestSecret $case.Key
    $allSecrets += $secret
    $inputText = & $case.Text $secret
    $protected = Protect-AkashaLogText $inputText
    Assert-TextExcludes $protected @($secret) "Secret assignment was not redacted for $($case.Key)."
    Assert-True ($protected.Contains('[REDACTED]')) "Redaction marker is missing for $($case.Key)."
  }

  $keyWithHyphensSecret = New-TestSecret 'hyphen-key'
  $allSecrets += $keyWithHyphensSecret
  $hyphenProtected = Protect-AkashaLogText ('access-token=' + $keyWithHyphensSecret)
  Assert-TextExcludes $hyphenProtected @($keyWithHyphensSecret) 'Hyphenated secret key was not redacted.'

  $skSecret = ('s' + 'k-' + ('A' * 24))
  $bearerSecret = ('B' * 30) + '.segment_value'
  $allSecrets += $skSecret, $bearerSecret
  $tokenProtected = Protect-AkashaLogText ('credential=' + $skSecret + ' Authorization: Bearer ' + $bearerSecret)
  Assert-TextExcludes $tokenProtected @($skSecret, $bearerSecret) 'Token-shaped log values were not redacted.'

  $documentedPlaceholder = 'your_weflow_access_token'
  $placeholderText = 'access_token=' + $documentedPlaceholder
  Assert-Equal (Protect-AkashaLogText $placeholderText) $placeholderText 'Exact documented placeholder should remain visible.'
  $wrongCasePlaceholder = ('YOUR_' + 'WEFLOW_ACCESS_TOKEN')
  $wrongCaseText = ('access_' + 'token=' + $wrongCasePlaceholder)
  Assert-TextExcludes (Protect-AkashaLogText $wrongCaseText) @($wrongCasePlaceholder) 'Placeholder allowlist must be case-sensitive.'
  $benignText = 'token_count=4 api_key_name=primary jwt_version=1 password_hint=present'
  Assert-Equal (Protect-AkashaLogText $benignText) $benignText 'Non-secret key names were over-redacted.'

  $compoundSecretCases = @(
    @{ Name='OPENAI_API_KEY'; Text={ param($value) ('OPENAI_' + 'API_KEY=' + $value) } },
    @{ Name='WEFLOW_ACCESS_TOKEN'; Text={ param($value) ('WEFLOW_' + 'ACCESS_TOKEN=' + $value) } },
    @{ Name='image_caption_api_key'; Text={ param($value) ('image_caption_' + 'api_key=' + $value) } },
    @{ Name='CLI equals'; Text={ param($value) ('--api-' + 'key=' + $value) } },
    @{ Name='CLI space'; Text={ param($value) ('--api-' + 'key ' + $value) } }
  )
  foreach ($case in $compoundSecretCases) {
    $compoundSecret = New-TestSecret ($case.Name -replace '[^A-Za-z]', '-')
    $allSecrets += $compoundSecret
    $compoundProtected = Protect-AkashaLogText (& $case.Text $compoundSecret)
    Assert-TextExcludes $compoundProtected @($compoundSecret) "Compound secret key was not redacted: $($case.Name)"
  }

  $yamlWords = @('correct', 'horse', 'battery', 'staple')
  $yamlSecret = $yamlWords -join ' '
  $yamlText = ('pass' + 'word: ' + $yamlSecret)
  $yamlProtected = Protect-AkashaLogText $yamlText
  Assert-TextExcludes $yamlProtected $yamlWords 'YAML plain scalar leaked one or more words.'

  $escapedJsonSecret = New-TestSecret 'escaped-json'
  $allSecrets += $escapedJsonSecret
  $escapedJson = '{\"' + 'api_' + 'key\":\"' + $escapedJsonSecret + '\"}'
  Assert-TextExcludes (Protect-AkashaLogText $escapedJson) @($escapedJsonSecret) 'Escaped JSON secret was not redacted.'

  $jsonEscapedQuoteValue = 'abc\"def-secret'
  $jsonEscapedQuoteText = '{"' + 'pass' + 'word":"' + $jsonEscapedQuoteValue + '"}'
  $jsonEscapedQuoteProtected = Protect-AkashaLogText $jsonEscapedQuoteText
  Assert-TextExcludes $jsonEscapedQuoteProtected @($jsonEscapedQuoteValue, 'def-secret') 'JSON escaped quote leaked a secret suffix.'
  Assert-Equal $jsonEscapedQuoteProtected '{"password":"[REDACTED]"}' 'JSON escaped quote redaction changed the container shape.'

  $escapedContainerQuoteValue = 'abc\\\"def-secret'
  $escapedContainerQuoteText = '{\"' + 'pass' + 'word\":\"' + $escapedContainerQuoteValue + '\"}'
  $escapedContainerQuoteProtected = Protect-AkashaLogText $escapedContainerQuoteText
  Assert-TextExcludes $escapedContainerQuoteProtected @($escapedContainerQuoteValue, 'def-secret') 'Escaped-container quoted value leaked a secret suffix.'
  Assert-Equal $escapedContainerQuoteProtected '{\"password\":\"[REDACTED]\"}' 'Escaped-container redaction changed the container shape.'

  $slashCharacter = [string][char]92
  $fiveBackslashes = -join @($slashCharacter, $slashCharacter, $slashCharacter, $slashCharacter, $slashCharacter)
  $escapedTrailingSlashText = '{\"' + 'pass' + 'word\":\"abc' + $fiveBackslashes + '"}'
  $escapedTrailingSlashProtected = Protect-AkashaLogText $escapedTrailingSlashText
  Assert-Equal $escapedTrailingSlashProtected '{\"password\":\"[REDACTED]\"}' 'Escaped-container trailing backslash value was not fully redacted.'

  $singleQuotedValue = "abc\'def-secret"
  $singleQuotedText = "pass" + "word='" + $singleQuotedValue + "'"
  Assert-TextExcludes (Protect-AkashaLogText $singleQuotedText) @($singleQuotedValue, 'def-secret') 'Single-quoted escaped value leaked a secret suffix.'

  foreach ($punctuatedBareValue in @('abc,def-secret and tail', 'abc;def-secret and tail')) {
    $punctuatedBareText = ('pass' + 'word: ' + $punctuatedBareValue)
    Assert-TextExcludes (Protect-AkashaLogText $punctuatedBareText) @($punctuatedBareValue, 'def-secret', 'tail') "Bare secret punctuation leaked a suffix: $punctuatedBareValue"
  }

  foreach ($punctuatedCliValue in @('abc,def-secret', 'abc;def-secret')) {
    $punctuatedCliText = ('--pass' + 'word ' + $punctuatedCliValue)
    $punctuatedCliProtected = Protect-AkashaLogText $punctuatedCliText
    Assert-TextExcludes $punctuatedCliProtected @($punctuatedCliValue, 'def-secret') "CLI space-form secret punctuation leaked a suffix: $punctuatedCliValue"
    Assert-Equal $punctuatedCliProtected '--password [REDACTED]' 'CLI space-form secret was not redacted through line end.'
  }

  $jwtHeader = 'eyJhbGciOiJI' + 'UzI1NiJ9'
  $rawJwt = $jwtHeader + '.' + ('p' * 24) + '.' + ('s' * 43)
  $allSecrets += $rawJwt
  Assert-TextExcludes (Protect-AkashaLogText ('opaque=' + $rawJwt)) @($rawJwt) 'Raw JWT was not redacted.'

  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  $signedHeader = [Convert]::ToBase64String($utf8NoBom.GetBytes('{"alg":"HS256"}')).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  $signedPayload = [Convert]::ToBase64String($utf8NoBom.GetBytes('{}')).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  $unsignedJwt = $signedHeader + '.' + $signedPayload
  $hmac = New-Object System.Security.Cryptography.HMACSHA256
  try {
    $hmac.Key = [byte[]](1..32)
    $signedSignature = [Convert]::ToBase64String($hmac.ComputeHash($utf8NoBom.GetBytes($unsignedJwt))).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  } finally {
    $hmac.Dispose()
  }
  $shortPayloadJwt = $unsignedJwt + '.' + $signedSignature
  Assert-Equal (($shortPayloadJwt.Split('.') | ForEach-Object { $_.Length }) -join '/') '20/3/43' 'Signed JWT fixture shape drifted.'
  $allSecrets += $shortPayloadJwt
  Assert-TextExcludes (Protect-AkashaLogText ('signed=' + $shortPayloadJwt)) @($shortPayloadJwt) 'JWT with a short valid payload shape was not redacted.'
  Assert-Equal (Protect-AkashaLogText ('token ' + $shortPayloadJwt + '.')) 'token [REDACTED].' 'JWT before sentence punctuation was not redacted.'
  Assert-Equal (Protect-AkashaLogText ('prefix.' + $shortPayloadJwt + ';')) 'prefix.[REDACTED];' 'JWT after dotted punctuation was not redacted.'

  foreach ($benignDottedEvidence in @('3.12.10', '127.0.0.1', 'config.example.json')) {
    Assert-Equal (Protect-AkashaLogText $benignDottedEvidence) $benignDottedEvidence "Benign dotted evidence was over-redacted: $benignDottedEvidence"
  }

  $shortBearer = 'q7'
  Assert-TextExcludes (Protect-AkashaLogText ('Authorization: Bearer ' + $shortBearer)) @($shortBearer) 'Short Bearer value was not redacted.'

  $compoundPlaceholderText = ('WEFLOW_' + 'ACCESS_TOKEN=' + $documentedPlaceholder)
  Assert-Equal (Protect-AkashaLogText $compoundPlaceholderText) $compoundPlaceholderText 'Exact documented placeholder should remain visible for a compound key.'
  foreach ($spacedQuotedPlaceholder in @(
      ('access_' + 'token = "' + $documentedPlaceholder + '"'),
      ("access_" + "token = '" + $documentedPlaceholder + "'"))) {
    Assert-Equal (Protect-AkashaLogText $spacedQuotedPlaceholder) $spacedQuotedPlaceholder 'Spaced quoted placeholder should remain visible.'
  }

  $logPath = Join-Path $testRoot 'logs\safe.log'
  $logSecret = New-TestSecret 'write-log'
  $allSecrets += $logSecret
  $consoleText = (& { Write-AkashaLog -Path $logPath -Level 'info' -Message ('auth_token=' + $logSecret) } 6>&1 | Out-String)
  $fileText = Get-Content -LiteralPath $logPath -Raw -Encoding UTF8
  Assert-TextExcludes $consoleText @($logSecret) 'Write-AkashaLog leaked to the console stream.'
  Assert-TextExcludes $fileText @($logSecret) 'Write-AkashaLog leaked to the log file.'
  Assert-True ($consoleText -match '\[INFO\]') 'Console log level was not normalized.'
  Assert-True ($fileText -match '\[REDACTED\]') 'Log file does not contain a redaction marker.'

  $jsonDirectory = Join-Path $testRoot 'json'
  $jsonPath = Join-Path $jsonDirectory 'value.json'
  $utf8Text = [string][char]0x96EA
  $overwriteText = -join @([char]0x8986, [char]0x5199)
  Write-JsonAtomic -Path $jsonPath -Value ([ordered]@{ name = 'first'; text = $utf8Text; count = 1 })
  $firstValue = Get-Content -LiteralPath $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal ([string]$firstValue.name) 'first' 'First atomic JSON write failed.'
  Assert-Equal ([string]$firstValue.text) $utf8Text 'UTF-8 JSON text was corrupted.'
  $firstBytes = [System.IO.File]::ReadAllBytes($jsonPath)
  $hasBom = $firstBytes.Length -ge 3 -and $firstBytes[0] -eq 0xEF -and $firstBytes[1] -eq 0xBB -and $firstBytes[2] -eq 0xBF
  Assert-True (-not $hasBom) 'Atomic JSON contains a UTF-8 BOM.'
  Write-JsonAtomic -Path $jsonPath -Value ([ordered]@{ name = 'second'; text = $overwriteText; count = 2 })
  $secondValue = Get-Content -LiteralPath $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
  Assert-Equal ([string]$secondValue.name) 'second' 'Atomic JSON overwrite failed.'
  Assert-Equal ([int]$secondValue.count) 2 'Atomic JSON overwrite kept stale data.'
  Assert-True (@(Get-ChildItem -LiteralPath $jsonDirectory -Filter '.value.json.*' -Force -ErrorAction SilentlyContinue).Count -eq 0) 'Atomic JSON left a temporary or replacement-backup file after success.'

  $badTarget = Join-Path $jsonDirectory 'directory-target.json'
  New-Item -ItemType Directory -Force -Path $badTarget | Out-Null
  $badWriteFailed = $false
  try {
    Write-JsonAtomic -Path $badTarget -Value ([ordered]@{ fail = $true })
  } catch {
    $badWriteFailed = $true
  }
  Assert-True $badWriteFailed 'Atomic JSON should fail when the target is a directory.'
  Assert-True (@(Get-ChildItem -LiteralPath $jsonDirectory -Filter '.directory-target.json.*' -Force -ErrorAction SilentlyContinue).Count -eq 0) 'Atomic JSON left a temporary or replacement-backup file after failure.'
  $moduleSource = Get-Content -LiteralPath $modulePath -Raw -Encoding UTF8
  Assert-True ($moduleSource -match '\[System\.IO\.File\]::Replace\s*\(') 'Atomic overwrite does not use System.IO.File.Replace.'

  $cleanupFixture = Join-Path $jsonDirectory '.cleanup-fixture.tmp'
  Set-Content -LiteralPath $cleanupFixture -Value 'fixture' -Encoding ASCII
  $cleanupSucceeded = & $atomicCleanup -Paths @($cleanupFixture)
  Assert-True $cleanupSucceeded 'Atomic cleanup helper rejected a successful removal.'
  Assert-True (-not (Test-Path -LiteralPath $cleanupFixture)) 'Atomic cleanup helper did not remove its fixture.'

  $cleanupFailureFixture = Join-Path $jsonDirectory '.cleanup-failure-fixture.tmp'
  Set-Content -LiteralPath $cleanupFailureFixture -Value 'fixture' -Encoding ASCII
  $cleanupFailureSecret = New-TestSecret 'atomic-cleanup-failure'
  $failingRemover = { param($Path) throw $cleanupFailureSecret }
  $cleanupFailedSafely = & $atomicCleanup -Paths @($cleanupFailureFixture) -Remover $failingRemover
  Assert-True (-not $cleanupFailedSafely) 'Atomic cleanup helper reported success after a remover failure.'
  Assert-ThrowsExact {
    & $atomicOutcome -OperationError $null -CleanupSucceeded $false
  } 'E_JSON_ATOMIC_CLEANUP: Unable to remove temporary JSON artifacts.' 'Successful JSON operation did not surface a fixed cleanup error.'
  $mainOperationError = $null
  try {
    throw 'main atomic operation sentinel'
  } catch {
    $mainOperationError = $_
  }
  $combinedAtomicFailure = $null
  try {
    & $atomicOutcome -OperationError $mainOperationError -CleanupSucceeded $false
  } catch {
    $combinedAtomicFailure = $_
  }
  Assert-True ($null -ne $combinedAtomicFailure) 'Combined atomic operation and cleanup failure did not throw.'
  Assert-Equal $combinedAtomicFailure.Exception.Message 'main atomic operation sentinel' 'Atomic cleanup failure masked the main operation error.'
  $cleanupFailureSignal = [string]$combinedAtomicFailure.Exception.Data['AkashaCleanupFailure']
  Assert-Equal $cleanupFailureSignal 'E_ATOMIC_CLEANUP' 'Combined atomic failure omitted the fixed cleanup signal.'
  Assert-TextExcludes $cleanupFailureSignal @($cleanupFailureSecret, $cleanupFailureFixture) 'Atomic cleanup signal exposed sensitive details.'
  Remove-Item -LiteralPath $cleanupFailureFixture -Force -ErrorAction Stop
  $atomicWriteSource = (Get-Command Write-JsonAtomic -CommandType Function).ScriptBlock.ToString()
  Assert-True ($atomicWriteSource -match 'Remove-AkashaAtomicArtifacts') 'Write-JsonAtomic does not use the checked cleanup helper.'
  Assert-True ($atomicWriteSource -match 'Complete-AkashaAtomicOutcome') 'Write-JsonAtomic does not use explicit outcome precedence.'

  $backupRoot = Join-Path $testRoot 'backups'
  $backup = Backup-AkashaFile -Path $jsonPath -BackupRoot $backupRoot
  Assert-True (-not [string]::IsNullOrWhiteSpace([string]$backup)) 'Backup did not return a destination.'
  Assert-True (Test-Path -LiteralPath $backup -PathType Leaf) 'Backup file was not created.'
  Assert-Equal ([System.IO.File]::ReadAllText($backup)) ([System.IO.File]::ReadAllText($jsonPath)) 'Backup contents differ from the source.'
  $missingBackup = Backup-AkashaFile -Path (Join-Path $testRoot 'missing.json') -BackupRoot $backupRoot
  Assert-True ($null -eq $missingBackup) 'Missing source should not create a backup.'

  $python = $null
  try {
    $python = Resolve-Python312
  } catch {
    Assert-Equal $_.Exception.Message $pythonError 'Resolve-Python312 returned the wrong failure.'
  }
  if ($null -ne $python) {
    Assert-True (-not [string]::IsNullOrWhiteSpace([string]$python.FilePath)) 'Python descriptor has no FilePath.'
    Assert-True ($null -ne $python.Prefix) 'Python descriptor has no Prefix.'
    $pythonProbe = & $python.FilePath @($python.Prefix) -c "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'bits':platform.architecture()[0]}))"
    Assert-True ($LASTEXITCODE -eq 0) 'Resolved Python descriptor could not execute.'
    $pythonInfo = $pythonProbe | ConvertFrom-Json
    Assert-True ([int]$pythonInfo.version[0] -eq 3 -and [int]$pythonInfo.version[1] -eq 12) 'Resolved Python is not 3.12.'
    Assert-Equal ([string]$pythonInfo.bits) '64bit' 'Resolved Python is not x64.'
  }

  $nativeExe = Join-Path $PSHOME 'powershell.exe'
  Assert-True (Test-Path -LiteralPath $nativeExe -PathType Leaf) 'System Windows PowerShell is missing.'
  $nativeLog = Join-Path $testRoot 'logs\native.log'
  $nativeOutSecret = New-TestSecret 'native-stdout'
  $nativeErrSecret = New-TestSecret 'native-stderr'
  $allSecrets += $nativeOutSecret, $nativeErrSecret
  $successCommand = "Write-Output 'token=$nativeOutSecret'; [Console]::Error.WriteLine('api_key=$nativeErrSecret'); exit 0"
  $nativeCombined = (& { Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $successCommand) -LogPath $nativeLog } 6>&1 | Out-String)
  Assert-TextExcludes $nativeCombined @($nativeOutSecret, $nativeErrSecret) 'Successful native invocation leaked sensitive output or return data.'
  $nativeLogText = Get-Content -LiteralPath $nativeLog -Raw -Encoding UTF8
  Assert-TextExcludes $nativeLogText @($nativeOutSecret, $nativeErrSecret) 'Successful native invocation leaked to its log.'

  $opaqueArgument = New-TestSecret 'native-opaque-argument'
  $allSecrets += $opaqueArgument
  $argumentEchoCommand = 'param($value) Write-Output $value'
  $argumentEchoCombined = (& {
      Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $argumentEchoCommand, $opaqueArgument) -LogPath $nativeLog
    } 6>&1 | Out-String)
  Assert-TextExcludes $argumentEchoCombined @($opaqueArgument) 'Native argument echo leaked through console or return data.'
  Assert-TextExcludes (Get-Content -LiteralPath $nativeLog -Raw -Encoding UTF8) @($opaqueArgument) 'Native argument echo leaked to its log.'

  $initEchoCommand = 'param($value) Write-Output $value; Write-Output ''initialization'''
  $initEchoOutput = @(Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $initEchoCommand, 'init') -LogPath $nativeLog)
  Assert-Equal $initEchoOutput.Count 2 'Init argument probe returned the wrong record count.'
  Assert-True ($initEchoOutput -ccontains '[REDACTED]') 'Init argument echo was not redacted.'
  Assert-True ($initEchoOutput -ccontains 'initialization') 'Init argument corrupted a larger word.'

  $opaqueStdin = New-TestSecret 'native-opaque-stdin'
  $allSecrets += $opaqueStdin
  $stdinEchoCommand = '$input | ForEach-Object { Write-Output $_ }; exit 0'
  $stdinEchoCombined = (& {
      Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $stdinEchoCommand) -LogPath $nativeLog -StandardInput @($opaqueStdin)
    } 6>&1 | Out-String)
  Assert-TextExcludes $stdinEchoCombined @($opaqueStdin) 'Native stdin echo leaked through console or return data.'
  Assert-TextExcludes (Get-Content -LiteralPath $nativeLog -Raw -Encoding UTF8) @($opaqueStdin) 'Native stdin echo leaked to its log.'

  $shortStdinCommand = '$received = @($input); Write-Output $received[0]; Write-Output ''python ready'''
  $shortStdinOutput = @(Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $shortStdinCommand) -LogPath $nativeLog -StandardInput @('y'))
  Assert-Equal ($shortStdinOutput -join '|') '[REDACTED]|python ready' 'Short native stdin protection leaked or corrupted output.'

  $ambientSensitiveValue = New-TestSecret 'native-ambient-value'
  $allSecrets += $ambientSensitiveValue
  $oldAmbientValue = $env:AKASHA_NATIVE_SENSITIVE_FIXTURE
  try {
    $env:AKASHA_NATIVE_SENSITIVE_FIXTURE = $ambientSensitiveValue
    $ambientCommand = 'Write-Output $env:AKASHA_NATIVE_SENSITIVE_FIXTURE'
    $ambientCombined = (& {
        Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $ambientCommand) -LogPath $nativeLog -SensitiveValues @($ambientSensitiveValue)
      } 6>&1 | Out-String)
    Assert-TextExcludes $ambientCombined @($ambientSensitiveValue) 'Explicit native sensitive value leaked through console or return data.'
    Assert-TextExcludes (Get-Content -LiteralPath $nativeLog -Raw -Encoding UTF8) @($ambientSensitiveValue) 'Explicit native sensitive value leaked to its log.'
  } finally {
    if ($null -eq $oldAmbientValue) {
      Remove-Item Env:\AKASHA_NATIVE_SENSITIVE_FIXTURE -ErrorAction SilentlyContinue
    } else {
      $env:AKASHA_NATIVE_SENSITIVE_FIXTURE = $oldAmbientValue
    }
  }

  $failureSecret = New-TestSecret 'native-failure'
  $allSecrets += $failureSecret
  $failureCommand = "[Console]::Error.WriteLine('refresh_token=$failureSecret'); exit 7"
  $failureMessage = $null
  try {
    Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $failureCommand) -LogPath $nativeLog | Out-Null
    throw 'Expected native exit code 7 to fail.'
  } catch {
    $failureMessage = $_.Exception.Message
  }
  Assert-Equal $failureMessage 'E_NATIVE_7: native command failed: powershell.exe' 'Native failure message is not fixed and safe.'
  Assert-TextExcludes $failureMessage @($failureSecret, $failureCommand) 'Native failure exposed its arguments.'

  $stdinSecret = New-TestSecret 'native-stdin'
  $allSecrets += $stdinSecret
  $stdinCommand = '$input | ForEach-Object { [Console]::Error.WriteLine((''token='' + $_)) }; exit 9'
  $stdinFailureMessage = $null
  try {
    Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $stdinCommand) -LogPath $nativeLog -StandardInput @($stdinSecret) | Out-Null
    throw 'Expected native exit code 9 to fail.'
  } catch {
    $stdinFailureMessage = $_.Exception.Message
  }
  Assert-Equal $stdinFailureMessage 'E_NATIVE_9: native command failed: powershell.exe' 'Native stdin failure message is not fixed and safe.'
  Assert-TextExcludes $stdinFailureMessage @($stdinSecret, $stdinCommand) 'Native failure exposed standard input or arguments.'

  $unwritableLogPath = Join-Path $testRoot 'logs\directory-as-log'
  New-Item -ItemType Directory -Force -Path $unwritableLogPath | Out-Null
  $logFailureCommand = "Write-Output 'safe native output'; exit 7"
  $logFailureMessage = $null
  try {
    Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $logFailureCommand) -LogPath $unwritableLogPath | Out-Null
    throw 'Expected native exit code 7 with an unwritable log to fail.'
  } catch {
    $logFailureMessage = $_.Exception.Message
  }
  Assert-Equal $logFailureMessage 'E_NATIVE_7: native command failed: powershell.exe' 'Log failure masked the required native nonzero error.'

  $safeLogFailureCommand = "Write-Output 'safe native output'; exit 0"
  $safeLogFailureMessage = $null
  try {
    Invoke-AkashaNative -FilePath $nativeExe -Arguments @('-NoProfile', '-Command', $safeLogFailureCommand) -LogPath $unwritableLogPath | Out-Null
    throw 'Expected successful native execution with an unwritable log to fail safely.'
  } catch {
    $safeLogFailureMessage = $_.Exception.Message
  }
  Assert-Equal $safeLogFailureMessage 'E_NATIVE_LOG: native output could not be logged: powershell.exe' 'Successful native log failure did not use a fixed safe error.'

  $nativeLogText = Get-Content -LiteralPath $nativeLog -Raw -Encoding UTF8
  Assert-TextExcludes $nativeLogText $allSecrets 'Native log contains a raw secret.'

  $weFlow = Get-WeFlowExecutable
  if ($null -ne $weFlow) {
    Assert-True (Test-Path -LiteralPath $weFlow -PathType Leaf) 'Get-WeFlowExecutable returned a missing file.'
  }

  Write-Host 'Common module tests: PASS' -ForegroundColor Green
} finally {
  Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
