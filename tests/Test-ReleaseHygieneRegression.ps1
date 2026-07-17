$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$gate = Join-Path $PSScriptRoot 'Test-ReleaseHygiene.ps1'
$fixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("akashabot-release-hygiene-{0}" -f [guid]::NewGuid().ToString('N'))
$localOnlyRootEntries = @('.git', '.superpowers', '.worktrees', 'docs')

function Get-FixturePath {
  param([string]$RelativePath)

  return Join-Path $fixtureRoot ($RelativePath -replace '/', '\')
}

function Copy-RepositoryFileToFixture {
  param([string]$RelativePath)

  $sourcePath = Join-Path $repositoryRoot ($RelativePath -replace '/', '\')
  $destinationPath = Get-FixturePath -RelativePath $RelativePath
  $destinationDirectory = Split-Path -Parent $destinationPath
  New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
  Copy-Item -LiteralPath $sourcePath -Destination $destinationPath -Force
}

function New-CleanFixture {
  New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
  foreach ($entry in Get-ChildItem -LiteralPath $repositoryRoot -Force -ErrorAction Stop |
      Where-Object { $localOnlyRootEntries -cnotcontains $_.Name }) {
    Copy-Item -LiteralPath $entry.FullName -Destination (Join-Path $fixtureRoot $entry.Name) -Recurse -Force
  }
}

function Invoke-FixtureGate {
  param([string]$GatePath)

  if ([string]::IsNullOrWhiteSpace($GatePath)) {
    $GatePath = $gate
  }
  $previousErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GatePath -RepositoryRoot $fixtureRoot 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }
  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = $output
  }
}

function Assert-GatePass {
  param(
    [string]$Case,
    [string]$GatePath
  )

  $result = Invoke-FixtureGate -GatePath $GatePath
  if ($result.ExitCode -ne 0) {
    throw "$Case expected PASS but gate exited $($result.ExitCode): $($result.Output)"
  }
}

function Assert-GateFail {
  param(
    [string]$Case,
    [string]$ExpectedMessage,
    [string]$GatePath
  )

  $result = Invoke-FixtureGate -GatePath $GatePath
  if ($result.ExitCode -eq 0) {
    throw "$Case expected gate failure, but it passed."
  }
  if ($result.Output -notmatch [regex]::Escape($ExpectedMessage)) {
    throw "$Case failed for the wrong reason. Expected '$ExpectedMessage'; output: $($result.Output)"
  }
}

function Set-FixtureConfigProperty {
  param(
    [string]$Name,
    [string]$Value
  )

  $configPath = Get-FixturePath -RelativePath 'bridge/config.example.json'
  $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $property = $config.PSObject.Properties[$Name]
  if ($null -eq $property) {
    $config | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
  } else {
    $property.Value = $Value
  }
  $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding UTF8
}

function Assert-PrivatePathRejected {
  param(
    [Parameter(Mandatory)][string]$Case,
    [Parameter(Mandatory)][string]$Value
  )

  $readmePath = Get-FixturePath -RelativePath 'README.md'
  Add-Content -LiteralPath $readmePath -Value ("Local source: {0}" -f $Value) -Encoding UTF8
  Assert-GateFail -Case $Case -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'
}

function Assert-UiaSupportMutationRejected {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$RelativePath,
    [Parameter(Mandatory)][string]$Case,
    [Parameter(Mandatory)][scriptblock]$Mutation
  )

  $originalText = (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Replace("`r`n", "`n")
  $mutatedText = & $Mutation $originalText
  if ($mutatedText -ceq $originalText) {
    throw "$Case mutation did not change the fixture."
  }
  [System.IO.File]::WriteAllText($Path, $mutatedText, (New-Object System.Text.UTF8Encoding($false)))
  Assert-GateFail -Case $Case -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $RelativePath
}

try {
  New-CleanFixture
  Assert-GatePass -Case 'clean release fixture'

  $duplicateGatePath = Get-FixturePath -RelativePath 'docs/release-hygiene-duplicate.ps1'
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $duplicateGatePath) | Out-Null
  $gateText = (Get-Content -LiteralPath $gate -Raw -Encoding UTF8).Replace("`r`n", "`n")
  $uniqueAllowlistPair = "  'THIRD_PARTY_NOTICES.md',`n  'VERSION',"
  if ([regex]::Matches($gateText, [regex]::Escape($uniqueAllowlistPair)).Count -ne 1) {
    throw 'Expected allowlist mutation precondition is missing.'
  }
  $duplicateAllowlistPair = "  'VERSION',`n  'VERSION',"
  $gateText = $gateText.Replace($uniqueAllowlistPair, $duplicateAllowlistPair)
  [System.IO.File]::WriteAllText($duplicateGatePath, $gateText, (New-Object System.Text.UTF8Encoding($false)))
  Assert-GateFail -Case 'duplicate expected release allowlist entry' -ExpectedMessage 'duplicate entries' -GatePath $duplicateGatePath

  $legacySenderMarker = 'weflow_' + 'api'
  $startLauncherName = (-join @([char]0x542F, [char]0x52A8)) + '.bat'
  $startLauncherPath = Get-FixturePath -RelativePath $startLauncherName
  Add-Content -LiteralPath $startLauncherPath -Value ('rem copied legacy marker ' + $legacySenderMarker) -Encoding UTF8
  Assert-GateFail -Case 'legacy marker in a root launcher' -ExpectedMessage 'Legacy sender/config marker'
  Copy-RepositoryFileToFixture -RelativePath $startLauncherName

  $securityPath = Get-FixturePath -RelativePath 'SECURITY.md'
  Add-Content -LiteralPath $securityPath -Value ('Copied legacy marker: ' + $legacySenderMarker) -Encoding UTF8
  Assert-GateFail -Case 'legacy marker in another public document' -ExpectedMessage 'Legacy sender/config marker'
  Copy-RepositoryFileToFixture -RelativePath 'SECURITY.md'

  $backslash = [string][char]0x5c
  $copiedFixturePath = 'C:' + $backslash + 'fixture' + $backslash + 'py.exe'
  Assert-PrivatePathRejected -Case 'synthetic fixture path copied into public documentation' -Value $copiedFixturePath

  $userProfilePath = 'C:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private-project' + $backslash + 'source'
  Assert-PrivatePathRejected -Case 'personal Windows workspace path' -Value $userProfilePath

  $otherProfilePath = 'D:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private-workspace'
  Assert-PrivatePathRejected -Case 'personal Windows workspace path on another drive' -Value $otherProfilePath

  $workspaceMarker = 'AkashaBot-OneClick-' + 'Native-' + '20260618'
  $workspacePath = 'E:' + $backslash + $workspaceMarker + $backslash + 'source'
  Assert-PrivatePathRejected -Case 'real workspace marker' -Value $workspacePath

  $driveSourcePath = 'F:' + $backslash + 'private-project' + $backslash + 'source'
  Assert-PrivatePathRejected -Case 'drive-qualified private source path' -Value $driveSourcePath

  $uncPath = ($backslash * 2) + 'fileserver' + $backslash + 'alice' + $backslash + 'private-project' + $backslash + 'source'
  Assert-PrivatePathRejected -Case 'UNC private workspace path' -Value $uncPath

  $uncShareRoot = ($backslash * 2) + 'fileserver' + $backslash + 'private-workspace'
  Assert-PrivatePathRejected -Case 'UNC private workspace share root' -Value $uncShareRoot

  $extendedUncRoot = ($backslash * 2) + '?' + $backslash + 'UNC' + $backslash + 'fileserver' + $backslash + 'private-workspace'
  Assert-PrivatePathRejected -Case 'extended UNC private workspace share root' -Value $extendedUncRoot

  $unicodeServer = -join @([char]0x670D, [char]0x52A1, [char]0x5668)
  $unicodeShare = -join @([char]0x79C1, [char]0x4EBA, [char]0x76EE, [char]0x5F55)
  $unicodeUncRoot = ($backslash * 2) + $unicodeServer + $backslash + $unicodeShare
  Assert-PrivatePathRejected -Case 'Unicode UNC private workspace share root' -Value $unicodeUncRoot

  $symbolUncRoot = ($backslash * 2) + 'fileserver' + $backslash + '@private'
  Assert-PrivatePathRejected -Case 'symbol UNC private workspace share root' -Value $symbolUncRoot

  $apostropheUncRoot = ($backslash * 2) + 'fileserver' + $backslash + [char]0x27 + 'private'
  Assert-PrivatePathRejected -Case 'apostrophe UNC private workspace share root' -Value $apostropheUncRoot

  $backtickUncRoot = ($backslash * 2) + 'fileserver' + $backslash + [char]0x60 + 'private'
  Assert-PrivatePathRejected -Case 'backtick UNC private workspace share root' -Value $backtickUncRoot

  $providerPrefixedUnc = ('Map' + 'ping:') + ($backslash * 2) + 'fileserver' + $backslash + 'share'
  Assert-PrivatePathRejected -Case 'provider-prefixed UNC private workspace path' -Value $providerPrefixedUnc

  foreach ($label in @('Mapping', 'Path', 'Source', 'ftp', 'custom')) {
    $labelPrefixedSlashUnc = $label + ':' + ('/' * 2) + 'fileserver/share'
    Assert-PrivatePathRejected -Case "$label-prefixed slash UNC private workspace path" -Value $labelPrefixedSlashUnc
  }

  $slashUncPath = ('/' * 2) + 'fileserver/share/private'
  Assert-PrivatePathRejected -Case 'slash UNC private workspace path' -Value $slashUncPath

  $mixedSlashUncPath = '/' + $backslash + 'fileserver/share/private'
  Assert-PrivatePathRejected -Case 'mixed slash UNC private workspace path' -Value $mixedSlashUncPath

  $mixedBackslashUncPath = $backslash + '/' + 'fileserver' + $backslash + 'share' + $backslash + 'private'
  Assert-PrivatePathRejected -Case 'mixed backslash UNC private workspace path' -Value $mixedBackslashUncPath

  $tripleSlashUncPath = ('/' * 3) + 'fileserver/share/private'
  Assert-PrivatePathRejected -Case 'triple slash UNC private workspace path' -Value $tripleSlashUncPath

  $quadBackslashUncPath = ($backslash * 4) + 'fileserver' + $backslash + 'share' + $backslash + 'private'
  Assert-PrivatePathRejected -Case 'quad backslash UNC private workspace path' -Value $quadBackslashUncPath

  $repeatedMixedSlashUncPath = '/' + $backslash + '/' + 'fileserver/share/private'
  Assert-PrivatePathRejected -Case 'repeated mixed slash UNC private workspace path' -Value $repeatedMixedSlashUncPath

  $repeatedMixedBackslashUncPath = $backslash + '/' + $backslash + 'fileserver' + $backslash + 'share' + $backslash + 'private'
  Assert-PrivatePathRejected -Case 'repeated mixed backslash UNC private workspace path' -Value $repeatedMixedBackslashUncPath

  $repeatedUnicodeUncRoot = ('/' * 4) + $unicodeServer + '/' + $unicodeShare
  Assert-PrivatePathRejected -Case 'repeated separator Unicode UNC private workspace share root' -Value $repeatedUnicodeUncRoot

  $deviceProfilePath = ($backslash * 2) + '?' + $backslash + 'C:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private-project'
  Assert-PrivatePathRejected -Case 'device drive personal workspace path' -Value $deviceProfilePath

  $globalRootDevicePath = ($backslash * 2) + '?' + $backslash + ('GLOBAL' + 'ROOT') + $backslash + 'Device' + $backslash + 'HarddiskVolume1' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'GLOBALROOT device workspace path' -Value $globalRootDevicePath

  $volumeDevicePath = ($backslash * 2) + '?' + $backslash + 'Volume{' + '01234567-89AB-CDEF-0123-456789ABCDEF' + '}' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'volume GUID device workspace path' -Value $volumeDevicePath

  $slashExtendedDrivePath = ('/' * 2) + '?/' + 'C:' + '/' + ('Us' + 'ers') + '/alice/private.txt'
  Assert-PrivatePathRejected -Case 'slash extended drive workspace path' -Value $slashExtendedDrivePath

  $slashExtendedUncPath = ('/' * 2) + '?/UNC/fileserver/share/private.txt'
  Assert-PrivatePathRejected -Case 'slash extended UNC workspace path' -Value $slashExtendedUncPath

  $slashDotDevicePath = ('/' * 2) + './PhysicalDrive0'
  Assert-PrivatePathRejected -Case 'slash dot device path' -Value $slashDotDevicePath

  $repeatedSlashExtendedUncPath = ('/' * 3) + '?/UNC/fileserver/share/private.txt'
  Assert-PrivatePathRejected -Case 'repeated slash extended UNC workspace path' -Value $repeatedSlashExtendedUncPath

  $repeatedBackslashDevicePath = ($backslash * 4) + '?' + $backslash + 'C:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'repeated backslash device workspace path' -Value $repeatedBackslashDevicePath

  $repeatedMixedDotDevicePath = '/' + $backslash + '/' + './PhysicalDrive0'
  Assert-PrivatePathRejected -Case 'repeated mixed dot device path' -Value $repeatedMixedDotDevicePath

  $slashVolumePath = ('/' * 2) + '?/Volume{' + '01234567-89AB-CDEF-0123-456789ABCDEF' + '}/' + ('Us' + 'ers') + '/alice/private.txt'
  Assert-PrivatePathRejected -Case 'slash volume GUID workspace path' -Value $slashVolumePath

  $nativeGlobalRootPath = $backslash + '??' + $backslash + ('GLOBAL' + 'ROOT') + $backslash + 'Device' + $backslash + 'HarddiskVolume3' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native NT GLOBALROOT workspace path' -Value $nativeGlobalRootPath

  $nativeDrivePath = $backslash + '??' + $backslash + 'C:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native NT drive workspace path' -Value $nativeDrivePath

  $nativeUncPath = $backslash + '??' + $backslash + 'UNC' + $backslash + 'fileserver' + $backslash + 'share' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native NT UNC workspace path' -Value $nativeUncPath

  $dosDevicesPath = $backslash + 'Dos' + 'Devices' + $backslash + 'C:' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native DosDevices workspace path' -Value $dosDevicesPath

  $nativeDevicePath = $backslash + 'Device' + $backslash + 'HarddiskVolume3' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native Device workspace path' -Value $nativeDevicePath

  $systemRootPath = $backslash + 'System' + 'Root' + $backslash + 'System32' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'native SystemRoot workspace path' -Value $systemRootPath

  $unknownProviderNativePaths = @(
    [pscustomobject]@{ Label = 'Mapping'; Path = $backslash + 'Device' + $backslash + 'HarddiskVolume3' },
    [pscustomobject]@{ Label = 'Path'; Path = $backslash + 'System' + 'Root' + $backslash + 'System32' },
    [pscustomobject]@{ Label = 'Source'; Path = $backslash + 'GLOBAL' + 'ROOT' + $backslash + 'Device' },
    [pscustomobject]@{ Label = 'Unknown'; Path = $backslash + 'Dos' + 'Devices' + $backslash + 'VolumeName' },
    [pscustomobject]@{ Label = 'Foo:Env'; Path = $backslash + 'Device' + $backslash + 'HarddiskVolume3' },
    [pscustomobject]@{ Label = 'Foo:HKCU'; Path = $backslash + 'System' + 'Root' + $backslash + 'System32' }
  )
  foreach ($example in $unknownProviderNativePaths) {
    Assert-PrivatePathRejected -Case "$($example.Label)-prefixed native NT workspace path" -Value ($example.Label + ':' + $example.Path)
  }

  $fileUri = ('fi' + 'le:') + '///' + 'C:' + '/Users/alice/private-project/source'
  Assert-PrivatePathRejected -Case 'file URI private workspace path' -Value $fileUri

  $backslashFileUri = ('fi' + 'le:') + ($backslash * 2) + 'fileserver' + $backslash + 'share' + $backslash + 'private.txt'
  Assert-PrivatePathRejected -Case 'backslash file URI private workspace path' -Value $backslashFileUri

  $singleSlashFileUri = ('fi' + 'le:') + '/home/alice/private.txt'
  Assert-PrivatePathRejected -Case 'single slash file URI private workspace path' -Value $singleSlashFileUri

  $encodedFileUri = ('fi' + 'le:') + '%2f%2ffileserver%2fshare%2fprivate.txt'
  Assert-PrivatePathRejected -Case 'encoded file URI private workspace path' -Value $encodedFileUri

  $fixtureTraversal = 'C:' + $backslash + 'fixture' + $backslash + '..' + $backslash + ('Us' + 'ers') + $backslash + 'alice' + $backslash + 'private-project'
  Assert-PrivatePathRejected -Case 'fixture path traversal' -Value $fixtureTraversal

  $readmePath = Get-FixturePath -RelativePath 'README.md'
  Add-Content -LiteralPath $readmePath -Value @(
    'PowerShell provider examples:',
    'Env:\AKASHABOT_BRIDGE_SOURCE',
    'HKCU:\Software\WeFlow',
    'HKLM:\Software\WeFlow',
    ('Env:' + $backslash + 'Device' + $backslash + 'Value'),
    ('HKCU:' + $backslash + 'Device' + $backslash + 'Setting'),
    ('HKLM:' + $backslash + 'System' + 'Root' + $backslash + 'Setting'),
    ('HKCU:' + $backslash + 'Software' + $backslash + 'Device' + $backslash + 'Setting'),
    ('HKLM:' + $backslash + 'Software' + $backslash + 'System' + 'Root' + $backslash + 'Setting')
  ) -Encoding UTF8
  Assert-GatePass -Case 'approved providers including namespace-like keys'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  $approvedUrlExamples = @(
    foreach ($scheme in @('http', 'https', 'ws', 'wss')) {
      $authority = $scheme + ':' + ('/' * 2) + 'example.com'
      $authority + '/device/path'
      $authority + '/systemroot/path'
      $authority + ('/' * 2) + 'server/share'
    }
  )
  Add-Content -LiteralPath $readmePath -Value $approvedUrlExamples -Encoding UTF8
  Assert-GatePass -Case 'approved URL schemes with namespace-like or slash UNC path content'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  $commonRelativePath = 'scripts/AkashaBot.Common.psm1'
  $commonPath = Get-FixturePath -RelativePath $commonRelativePath
  $commonLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $commonPath -Encoding UTF8)
  $allowedPatternLineIndex = -1
  for ($i = 0; $i -lt $commonLines.Count; $i++) {
    if ($commonLines[$i].Contains('(?<open>")(?<quoted>')) {
      $allowedPatternLineIndex = $i
      break
    }
  }
  if ($allowedPatternLineIndex -lt 0) {
    throw 'Common regex pattern regression precondition is missing.'
  }
  $allowedPatternLine = $commonLines[$allowedPatternLineIndex]
  $allowedWindowStart = $allowedPatternLineIndex - 4
  $allowedWindowCount = 8
  $allowedWindowEnd = $allowedWindowStart + $allowedWindowCount - 1
  if ($allowedWindowStart -lt 0 -or
      -not $commonLines[$allowedWindowStart].StartsWith('  $prefix = ') -or
      $commonLines[$allowedWindowEnd] -cne '  $safe = $Text') {
    throw 'Common regex window regression precondition is missing.'
  }
  $allowedWindow = @($commonLines.GetRange($allowedWindowStart, $allowedWindowCount))

  $commonLines.Insert($allowedPatternLineIndex + 1, $allowedPatternLine)
  Set-Content -LiteralPath $commonPath -Value $commonLines -Encoding UTF8
  Assert-GateFail -Case 'duplicated full Common regex pattern line' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $commonRelativePath

  $commonLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $commonPath -Encoding UTF8)
  $commonLines.RemoveAt($allowedPatternLineIndex)
  $relocationIndex = $commonLines.FindIndex([Predicate[string]]{ param($line) $line -ceq '  $safe = $Text' })
  if ($relocationIndex -lt 0) {
    throw 'Common regex relocation regression precondition is missing.'
  }
  $commonLines.Insert($relocationIndex + 1, $allowedPatternLine)
  Set-Content -LiteralPath $commonPath -Value $commonLines -Encoding UTF8
  Assert-GateFail -Case 'relocated full Common regex pattern line' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $commonRelativePath

  Add-Content -LiteralPath $readmePath -Value $allowedPatternLine -Encoding UTF8
  Assert-GateFail -Case 'Common regex pattern line copied to another file' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  $commonLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $commonPath -Encoding UTF8)
  $commonLines.RemoveRange($allowedWindowStart, $allowedWindowCount)
  Set-Content -LiteralPath $commonPath -Value (@($allowedWindow) + @($commonLines)) -Encoding UTF8
  Assert-GateFail -Case 'full Common regex window moved to file start' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $commonRelativePath

  $commonLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $commonPath -Encoding UTF8)
  $commonLines.RemoveRange($allowedWindowStart, $allowedWindowCount)
  $otherFunctionIndex = $commonLines.FindIndex([Predicate[string]]{ param($line) $line -ceq 'function Protect-AkashaLogText {' })
  if ($otherFunctionIndex -lt 0) {
    throw 'Other Common function regression precondition is missing.'
  }
  $remainingLines = @($commonLines)
  $movedToOtherFunction = @($remainingLines[0..$otherFunctionIndex]) + @($allowedWindow) + @($remainingLines[($otherFunctionIndex + 1)..($remainingLines.Count - 1)])
  Set-Content -LiteralPath $commonPath -Value $movedToOtherFunction -Encoding UTF8
  Assert-GateFail -Case 'full Common regex window moved to another function' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $commonRelativePath

  $commonLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $commonPath -Encoding UTF8)
  Set-Content -LiteralPath $commonPath -Value (@($allowedWindow) + @($commonLines)) -Encoding UTF8
  Assert-GateFail -Case 'full Common regex window copied within the same file' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $commonRelativePath

  Add-Content -LiteralPath $readmePath -Value $allowedWindow -Encoding UTF8
  Assert-GateFail -Case 'full Common regex window copied to another file' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  Assert-GatePass -Case 'original Common regex window in its stable function context'

  $webPanelPath = Get-FixturePath -RelativePath 'bridge/web_panel.py'
  $webPanelText = Get-Content -LiteralPath $webPanelPath -Raw -Encoding UTF8
  $uiPlaceholder = 'C:' + ($backslash * 2) + 'astrbot' + ($backslash * 2) + 'attachments'
  if (-not $webPanelText.Contains($uiPlaceholder)) {
    throw 'UI placeholder regression precondition is missing.'
  }
  Assert-GatePass -Case 'UI-only generic attachment placeholder'

  $secretValue = 'abcdefgh' + 'ijklmnop'
  $passwordKey = 'pass' + 'word'
  $accessTokenKey = 'access_' + 'token'
  $tokenKey = 'to' + 'ken'

  $versionPath = Get-FixturePath -RelativePath 'VERSION'
  Set-Content -LiteralPath $versionPath -Value ("$passwordKey=$secretValue") -Encoding UTF8
  Assert-GateFail -Case 'unquoted password assignment' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath 'VERSION'

  Set-Content -LiteralPath $versionPath -Value ("$accessTokenKey`: $secretValue # regression comment") -Encoding UTF8
  Assert-GateFail -Case 'unquoted access token with comment' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath 'VERSION'

  $gitIgnorePath = Get-FixturePath -RelativePath '.gitignore'
  Add-Content -LiteralPath $gitIgnorePath -Value ("$passwordKey=short") -Encoding UTF8
  Assert-GatePass -Case 'short ordinary assignment value'
  Copy-RepositoryFileToFixture -RelativePath '.gitignore'

  Set-FixtureConfigProperty -Name $passwordKey -Value $secretValue
  Assert-GateFail -Case 'quoted JSON password' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/config.example.json'

  Set-FixtureConfigProperty -Name $tokenKey -Value $secretValue
  Assert-GateFail -Case 'quoted JSON token' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/config.example.json'

  foreach ($keyFragments in @(
    @('api_', 'key'),
    @('auth_', 'token'),
    @('refresh_', 'token'),
    @('jwt_', 'secret'),
    @('j', 'wt'),
    @('client_', 'secret')
  )) {
    $secretKeyName = $keyFragments -join ''
    Set-FixtureConfigProperty -Name $secretKeyName -Value $secretValue
    Assert-GateFail -Case "quoted JSON key $secretKeyName" -ExpectedMessage 'Secret-shaped value found'
    Copy-RepositoryFileToFixture -RelativePath 'bridge/config.example.json'
  }

  $uppercasePlaceholder = 'YOUR_WEFLOW_' + 'ACCESS_TOKEN'
  Set-FixtureConfigProperty -Name $accessTokenKey -Value $uppercasePlaceholder
  Assert-GateFail -Case 'uppercase placeholder is not exempt' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/config.example.json'

  foreach ($relativePath in @('tests/resources.pak', 'chat-photo.jpg')) {
    $unexpectedPath = Get-FixturePath -RelativePath $relativePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $unexpectedPath) | Out-Null
    [System.IO.File]::WriteAllBytes($unexpectedPath, [byte[]](0x00, 0x01, 0x02, 0x03))
    Assert-GateFail -Case "unexpected publish artifact $relativePath" -ExpectedMessage "Unexpected publish file: $relativePath"
    Remove-Item -LiteralPath $unexpectedPath -Force
  }
  $localDocsPath = Get-FixturePath -RelativePath 'docs/private-notes.txt'
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $localDocsPath) | Out-Null
  Set-Content -LiteralPath $localDocsPath -Value ('local-only ' + $userProfilePath + ' ' + $secretValue) -Encoding UTF8
  Assert-GatePass -Case 'development-only docs directory exclusion'

  $extraBridgePath = Get-FixturePath -RelativePath 'bridge/extra.py'
  Set-Content -LiteralPath $extraBridgePath -Value '# unexpected bridge file' -Encoding UTF8
  Assert-GateFail -Case 'unexpected bridge file' -ExpectedMessage 'Unexpected publish file: bridge/extra.py'
  Remove-Item -LiteralPath $extraBridgePath -Force

  $requiredBridgePath = Get-FixturePath -RelativePath 'bridge/web_panel.py'
  Remove-Item -LiteralPath $requiredBridgePath -Force
  Assert-GateFail -Case 'missing bridge file' -ExpectedMessage 'Missing publish file: bridge/web_panel.py'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/web_panel.py'

  $unexpectedRootPath = Get-FixturePath -RelativePath 'unexpected-root.txt'
  Set-Content -LiteralPath $unexpectedRootPath -Value 'unexpected' -Encoding UTF8
  Assert-GateFail -Case 'unexpected release root file' -ExpectedMessage 'Unexpected publish file: unexpected-root.txt'
  Remove-Item -LiteralPath $unexpectedRootPath -Force

  $lockPath = Get-FixturePath -RelativePath 'bridge/requirements.lock'
  Set-Content -LiteralPath $lockPath -Value @(
    'requests==0.0.0',
    'pyperclip==1.11.0',
    'Pillow==12.2.0',
    'websockets==16.0'
  ) -Encoding UTF8
  Assert-GateFail -Case 'dependency pin drift' -ExpectedMessage 'requirements.lock does not match the exact dependency pins.'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/requirements.lock'

  $requirementsPath = Get-FixturePath -RelativePath 'bridge/requirements.txt'
  Add-Content -LiteralPath $requirementsPath -Value 'PyAutoGUI>=0.9.54' -Encoding UTF8
  Assert-GateFail -Case 'direct dependency allowlist drift' -ExpectedMessage 'dependency names do not match the exact allowlist.'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/requirements.txt'

  $installerLayoutRelativePath = 'tests/Test-InstallerLayout.ps1'
  $installerLayoutPath = Get-FixturePath -RelativePath $installerLayoutRelativePath
  $launcherAssertion = @(Get-Content -LiteralPath $installerLayoutPath -Encoding UTF8 |
    Where-Object { $_.Contains('Calibrate-Uia') -and $_.Contains('$calibrateBat -match') })
  if ($launcherAssertion.Count -ne 1) {
    throw 'Calibration launcher exact-context regression precondition is missing.'
  }
  Add-Content -LiteralPath $installerLayoutPath -Value $launcherAssertion[0] -Encoding UTF8
  Assert-GateFail -Case 'calibration launcher regex copied outside exact context' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $installerLayoutRelativePath

  $bridgeRuntimeTestRelativePath = 'tests/python/test_bridge_runtime.py'
  $bridgeRuntimeTestPath = Get-FixturePath -RelativePath $bridgeRuntimeTestRelativePath
  $fixtureSecretLine = '"access_' + 'token": "private-' + 'token"'
  Add-Content -LiteralPath $bridgeRuntimeTestPath -Value $fixtureSecretLine -Encoding UTF8
  Assert-GateFail -Case 'test secret fixture copied outside exact context' -ExpectedMessage 'Secret-shaped value found'
  Copy-RepositoryFileToFixture -RelativePath $bridgeRuntimeTestRelativePath

  $uiaSupportTestRelativePath = 'tests/python/test_uia_support.py'
  $uiaSupportTestPath = Get-FixturePath -RelativePath $uiaSupportTestRelativePath
  $privateTestImagePath = 'C:' + $backslash + 'private' + $backslash + 'never-log-this.png'
  Add-Content -LiteralPath $uiaSupportTestPath -Value ('# copied path ' + $privateTestImagePath) -Encoding UTF8
  Assert-GateFail -Case 'test private path copied outside exact context' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $uiaSupportTestRelativePath

  $relocatedPrivatePathLine = '            r"' + $privateTestImagePath + '",'
  $uiaSupportLines = [System.Collections.Generic.List[string]]@(
    Get-Content -LiteralPath $uiaSupportTestPath -Encoding UTF8
  )
  $relocatedPrivatePathIndex = $uiaSupportLines.IndexOf($relocatedPrivatePathLine)
  if ($relocatedPrivatePathIndex -lt 0 -or
      @($uiaSupportLines | Where-Object { $_ -ceq $relocatedPrivatePathLine }).Count -ne 1) {
    throw 'Private image path relocation regression precondition is missing.'
  }
  $uiaSupportLines.RemoveAt($relocatedPrivatePathIndex)
  $uiaSupportLines.Insert(0, $relocatedPrivatePathLine)
  Set-Content -LiteralPath $uiaSupportTestPath -Value @($uiaSupportLines) -Encoding UTF8
  Assert-GateFail -Case 'approved private image path relocated outside exact context' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $uiaSupportTestRelativePath

  $ownershipTailLine = '            any(call[:2] == ("SetClipboardData", 8) for call in user32.calls)'
  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image ownership function tail mutated' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($ownershipTailLine)).Count -ne 1) {
        throw 'Clipboard ownership tail mutation regression precondition is missing.'
      }
      return $text.Replace(
        $ownershipTailLine,
        '            any(call[:2] == ("SetClipboardData", 7) for call in user32.calls)'
      )
    }

  $ownershipTail = [string]::Join("`n", @(
    '        self.assertTrue('
    $ownershipTailLine
    '        )'
    '        self.assertFalse('
    '            any(call[0] == "GlobalFree" for call in kernel32.calls)'
    '        )'
  ))
  $swappedOwnershipTail = [string]::Join("`n", @(
    '        self.assertFalse('
    '            any(call[0] == "GlobalFree" for call in kernel32.calls)'
    '        )'
    '        self.assertTrue('
    $ownershipTailLine
    '        )'
  ))
  $failureTail = [string]::Join("`n", @(
    '        allocated_handle = next('
    '            call[3] for call in kernel32.calls if call[0] == "GlobalAlloc"'
    '        )'
    '        self.assertIn(("GlobalFree", allocated_handle), kernel32.calls)'
    '        self.assertIn(("CloseClipboard",), user32.calls)'
  ))

  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image failure function tail moved' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($failureTail)).Count -ne 1) {
        throw 'Clipboard failure tail move regression precondition is missing.'
      }
      return $text.Replace($failureTail, '') + "`n" + $failureTail
    }

  $failureTailLine = '        self.assertIn(("CloseClipboard",), user32.calls)'
  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image failure function tail mutated' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($failureTailLine)).Count -ne 1) {
        throw 'Clipboard failure tail mutation regression precondition is missing.'
      }
      return $text.Replace(
        $failureTailLine,
        '        self.assertNotIn(("CloseClipboard",), user32.calls)'
      )
    }

  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image ownership assertions swapped' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($ownershipTail)).Count -ne 1) {
        throw 'Clipboard ownership assertion swap regression precondition is missing.'
      }
      return $text.Replace($ownershipTail, $swappedOwnershipTail)
    }

  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image failure function tail copied' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($failureTail)).Count -ne 1) {
        throw 'Clipboard failure tail copy regression precondition is missing.'
      }
      return $text + "`n" + $failureTail
    }

  $ownershipBoundary = $ownershipTail + "`n`n    def test_copy_image_frees_untransferred_memory_on_failure(self):"
  Assert-UiaSupportMutationRejected `
    -Path $uiaSupportTestPath `
    -RelativePath $uiaSupportTestRelativePath `
    -Case 'approved private image function boundary modified' `
    -Mutation {
      param($text)
      if ([regex]::Matches($text, [regex]::Escape($ownershipBoundary)).Count -ne 1) {
        throw 'Clipboard function boundary mutation regression precondition is missing.'
      }
      return $text.Replace(
        $ownershipBoundary,
        $ownershipTail + "`n    # adjacent mutation`n`n    def test_copy_image_frees_untransferred_memory_on_failure(self):"
      )
    }

  $wechatExecutablePath = 'C:' + $backslash + 'Program Files' + $backslash + 'Tencent' + $backslash + 'WeChat.exe'
  $wechatExecutableLines = @(Get-Content -LiteralPath $uiaSupportTestPath -Encoding UTF8 |
    Where-Object { $_.Contains($wechatExecutablePath) })
  if ($wechatExecutableLines.Count -ne 1) {
    throw 'Synthetic WeChat executable path regression precondition is missing.'
  }
  Add-Content -LiteralPath $uiaSupportTestPath -Value $wechatExecutableLines[0] -Encoding UTF8
  Assert-GateFail -Case 'test executable fixture path duplicated outside exact context' -ExpectedMessage 'Private workspace path found'
  Copy-RepositoryFileToFixture -RelativePath $uiaSupportTestRelativePath

  $placeholderMarker = 'place' + 'holder'
  Add-Content -LiteralPath $readmePath -Value ('unfinished ' + $placeholderMarker) -Encoding UTF8
  Assert-GateFail -Case 'placeholder marker outside approved HTML file' -ExpectedMessage 'outside the three approved HTML attribute lines'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  $webPanelRelativePath = 'bridge/web_panel.py'
  $webPanelPath = Get-FixturePath -RelativePath $webPanelRelativePath
  $approvedPlaceholderLines = @(Get-Content -LiteralPath $webPanelPath -Encoding UTF8 |
    Where-Object { $_.Contains($placeholderMarker) })
  if ($approvedPlaceholderLines.Count -ne 3) {
    throw 'Approved HTML placeholder regression precondition is missing.'
  }
  Add-Content -LiteralPath $webPanelPath -Value $approvedPlaceholderLines[0] -Encoding UTF8
  Assert-GateFail -Case 'approved HTML placeholder line duplicated' -ExpectedMessage 'outside the three approved HTML attribute lines'
  Copy-RepositoryFileToFixture -RelativePath $webPanelRelativePath

  $realLegacyKey = 'uia_fixed_' + 'send_y'
  $readmePath = Get-FixturePath -RelativePath 'README.md'
  Add-Content -LiteralPath $readmePath -Value ('Old coordinate: ' + $realLegacyKey) -Encoding UTF8
  Assert-GateFail -Case 'real migration key in public documentation' -ExpectedMessage 'Legacy sender/config marker'
  Copy-RepositoryFileToFixture -RelativePath 'README.md'

  $runtimeRelativePath = 'bridge/config.py'
  $runtimePath = Get-FixturePath -RelativePath $runtimeRelativePath
  Add-Content -LiteralPath $runtimePath -Value ('# old coordinate ' + $realLegacyKey) -Encoding UTF8
  Assert-GateFail -Case 'real migration key in bridge runtime code' -ExpectedMessage 'Legacy sender/config marker'
  Copy-RepositoryFileToFixture -RelativePath $runtimeRelativePath

  $initializerRelativePath = 'scripts/Initialize-Configuration.ps1'
  $initializerPath = Get-FixturePath -RelativePath $initializerRelativePath
  Add-Content -LiteralPath $initializerPath -Value ('$outside = ' + "'" + $realLegacyKey + "'") -Encoding UTF8
  Assert-GateFail -Case 'real migration key outside initializer deletion allowlist' -ExpectedMessage 'found outside the approved initializer'
  Copy-RepositoryFileToFixture -RelativePath $initializerRelativePath

  $initializerLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $initializerPath -Encoding UTF8)
  $legacyBlockStart = $initializerLines.IndexOf('      $legacyBridgeKeys = @(')
  $legacyRemovalLine = $initializerLines.FindIndex([Predicate[string]]{
    param($line)
    $line -ceq '        Remove-JsonProperty -Object $bridge -Name $legacyBridgeKey'
  })
  $legacyBlockEnd = $legacyRemovalLine + 1
  if ($legacyBlockStart -lt 0 -or
      $legacyRemovalLine -lt 0 -or
      $legacyBlockEnd -ge $initializerLines.Count -or
      $initializerLines[$legacyBlockEnd] -cne '      }') {
    throw 'Initializer legacy deletion block regression precondition is missing.'
  }
  $legacyBlockCount = $legacyBlockEnd - $legacyBlockStart + 1
  $legacyBlock = @($initializerLines.GetRange($legacyBlockStart, $legacyBlockCount))

  Set-Content -LiteralPath $initializerPath -Value (@($initializerLines) + @('') + $legacyBlock) -Encoding UTF8
  Assert-GateFail -Case 'initializer legacy deletion block duplicated' -ExpectedMessage 'does not match the exact approved context'
  Copy-RepositoryFileToFixture -RelativePath $initializerRelativePath

  $initializerLines = [System.Collections.Generic.List[string]]@(Get-Content -LiteralPath $initializerPath -Encoding UTF8)
  $initializerLines.RemoveRange($legacyBlockStart, $legacyBlockCount)
  Set-Content -LiteralPath $initializerPath -Value (@($initializerLines) + @('') + $legacyBlock) -Encoding UTF8
  Assert-GateFail -Case 'initializer legacy deletion block relocated' -ExpectedMessage 'does not match the exact approved context'
  Copy-RepositoryFileToFixture -RelativePath $initializerRelativePath

  $configPath = Get-FixturePath -RelativePath 'bridge/config.example.json'
  $configText = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
  $configText = $configText.Replace('"completed": false', '"completed": true')
  Set-Content -LiteralPath $configPath -Value $configText -Encoding UTF8
  Assert-GateFail -Case 'completed calibration template' -ExpectedMessage 'incomplete calibration placeholder'
  Copy-RepositoryFileToFixture -RelativePath 'bridge/config.example.json'

  $sensitiveExamples = @(
    (-join @([char]0x8054, [char]0x7CFB, [char]0x4EBA, [char]0xFF1A, [char]0x793A, [char]0x4F8B, [char]0x7528, [char]0x6237)),
    (-join @([char]0x6D88, [char]0x606F, [char]0x793A, [char]0x4F8B, [char]0xFF1A, [char]0x793A, [char]0x4F8B, [char]0x5185, [char]0x5BB9)),
    (-join @([char]0x7A97, [char]0x53E3, [char]0x6807, [char]0x9898, [char]0xFF1A, [char]0x793A, [char]0x4F8B, [char]0x7A97, [char]0x53E3)),
    ((-join @([char]0x641C, [char]0x7D22, [char]0x6846, [char]0x5750, [char]0x6807, [char]0xFF1A)) + '123,456')
  )
  foreach ($sensitiveExample in $sensitiveExamples) {
    Add-Content -LiteralPath $readmePath -Value $sensitiveExample -Encoding UTF8
    Assert-GateFail -Case "sensitive public documentation example $sensitiveExample" -ExpectedMessage 'Sensitive calibration/contact/message/window example'
    Copy-RepositoryFileToFixture -RelativePath 'README.md'
  }

  $weflowDocumentationDirectory = Get-FixturePath -RelativePath 'docs/weflow-integration'
  New-Item -ItemType Directory -Force -Path $weflowDocumentationDirectory | Out-Null
  Assert-GatePass -Case 'empty WeFlow documentation directory'
  Remove-Item -LiteralPath $weflowDocumentationDirectory -Force

  $localLedger = Get-FixturePath -RelativePath '.superpowers/sdd'
  New-Item -ItemType Directory -Force -Path $localLedger | Out-Null
  $localSecretKey = '"pass' + 'word"'
  $localSecretValue = '"abcdefgh' + 'ijklmnop"'
  Set-Content -LiteralPath (Join-Path $localLedger 'review-example.json') -Value ("{ $localSecretKey`: $localSecretValue }") -Encoding UTF8
  $localWorktree = Get-FixturePath -RelativePath '.worktrees/scratch/runtime'
  New-Item -ItemType Directory -Force -Path $localWorktree | Out-Null
  Set-Content -LiteralPath (Join-Path $localWorktree 'debug.log') -Value 'local only' -Encoding UTF8
  Assert-GatePass -Case 'approved local development infrastructure exclusion'

  Write-Host 'Release hygiene regression: PASS' -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $fixtureRoot) {
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
  }
}
