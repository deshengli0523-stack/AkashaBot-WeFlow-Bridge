[CmdletBinding()]
param(
  [string]$RepositoryRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
  $root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  $root = (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).Path
}
$localOnlyRootEntries = @('.git', '.superpowers', '.worktrees')
$expectedPublishFiles = @(
  '.github/workflows/ci.yml',
  '.gitattributes',
  '.gitignore',
  'CHANGELOG.md',
  'INSTALL.md',
  'LICENSE',
  'README.md',
  'SECURITY.md',
  'THIRD_PARTY_NOTICES.md',
  'VERSION',
  'bridge/bridge_core.py',
  'bridge/config.example.json',
  'bridge/config.py',
  'bridge/main.py',
  'bridge/ob_client.py',
  'bridge/ob_protocol.py',
  'bridge/privacy.py',
  'bridge/requirements.lock',
  'bridge/requirements.txt',
  'bridge/senders.py',
  'bridge/state.py',
  'bridge/uia_fixed_sender.py',
  'bridge/uia_sender.py',
  'bridge/web_panel.py',
  'scripts/AkashaBot.Common.psm1',
  'scripts/Initialize-Configuration.ps1',
  'scripts/Initialize-Environments.ps1',
  'scripts/Install.ps1',
  'scripts/Start-Services.ps1',
  'scripts/Stop-Services.ps1',
  'scripts/Test-Prerequisites.ps1',
  'scripts/Test-Health.ps1',
  'tests/Test-Common.ps1',
  'tests/Test-Initialization.ps1',
  'tests/Test-InstallerLayout.ps1',
  'tests/Test-ProcessSafety.ps1',
  'tests/Test-ReleaseHygiene.ps1',
  'tests/Test-ReleaseHygieneRegression.ps1',
  'tests/Run-All.ps1',
  'tests/python/test_bridge_runtime.py',
  ((-join @([char]0x5B89, [char]0x88C5)) + '.bat'),
  ((-join @([char]0x542F, [char]0x52A8)) + '.bat'),
  ((-join @([char]0x505C, [char]0x6B62)) + '.bat'),
  ((-join @([char]0x5065, [char]0x5EB7, [char]0x68C0, [char]0x67E5)) + '.bat')
)
$publishFiles = @(
  foreach ($entry in Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop |
      Where-Object { $localOnlyRootEntries -cnotcontains $_.Name }) {
    if ($entry.PSIsContainer) {
      Get-ChildItem -LiteralPath $entry.FullName -Recurse -Force -File -ErrorAction Stop
    } else {
      $entry
    }
  }
)
$actualPublishFiles = @(
  $publishFiles | ForEach-Object {
    $_.FullName.Substring($root.Length + 1).Replace('\', '/')
  }
)
foreach ($relativePath in $expectedPublishFiles) {
  if ($actualPublishFiles -cnotcontains $relativePath) {
    throw "Missing publish file: $relativePath"
  }
}
foreach ($relativePath in $actualPublishFiles) {
  if ($expectedPublishFiles -cnotcontains $relativePath) {
    throw "Unexpected publish file: $relativePath"
  }
}

$bridgeRoot = Join-Path $root 'bridge'
$expectedRequirements = @(
  'requests==2.34.2',
  'PyAutoGUI==0.9.54',
  'pyperclip==1.11.0',
  'PyGetWindow==0.0.9',
  'uiautomation==2.0.29',
  'Pillow==12.2.0',
  'websockets==16.0'
)
$actualRequirements = @(Get-Content -LiteralPath (Join-Path $bridgeRoot 'requirements.lock') -Encoding UTF8)
if ($actualRequirements.Count -ne $expectedRequirements.Count -or
    [string]::Join("`n", $actualRequirements) -cne [string]::Join("`n", $expectedRequirements)) {
  throw 'requirements.lock does not match the exact dependency pins.'
}

$payload = $publishFiles

function Test-ProbablyTextFile {
  param([System.IO.FileInfo]$File)

  if ($File.Length -eq 0) {
    return $true
  }

  $stream = [System.IO.File]::Open($File.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
  try {
    $sampleLength = [int][Math]::Min(4096, $File.Length)
    $buffer = New-Object byte[] $sampleLength
    $bytesRead = $stream.Read($buffer, 0, $sampleLength)
  } finally {
    $stream.Dispose()
  }

  if ($bytesRead -eq 0) {
    return $false
  }
  foreach ($byte in $buffer[0..($bytesRead - 1)]) {
    if ($byte -eq 0 -or $byte -lt 9 -or ($byte -gt 13 -and $byte -lt 32)) {
      return $false
    }
  }
  return $true
}

function Test-AllowedPublishedDrivePath {
  param(
    [Parameter(Mandatory)][string]$Text,
    [Parameter(Mandatory)][int]$Index,
    [Parameter(Mandatory)][string]$RelativePath
  )

  $tail = $Text.Substring($Index)
  $backslash = [string][char]0x5c
  $pathSeparatorPattern = '[' + [regex]::Escape('/') + [regex]::Escape($backslash) + ']'
  $candidatePattern = '^(?<path>[A-Z]:' + $pathSeparatorPattern + '[^\r\n`"''<>|,;)\]}]*)'
  $candidateMatch = [regex]::Match($tail, $candidatePattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
  if (-not $candidateMatch.Success) {
    return $false
  }
  $candidate = $candidateMatch.Groups['path'].Value.TrimEnd()

  $uiPlaceholder = 'C:' + ($backslash * 2) + 'astrbot' + ($backslash * 2) + 'attachments'
  if ($RelativePath -ceq 'bridge/web_panel.py' -and $candidate -ceq $uiPlaceholder) {
    return $true
  }

  $fixturePrefix = 'C:' + $backslash + 'fixture'
  if ($candidate.Equals($fixturePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
      $candidate.StartsWith($fixturePrefix + $backslash, [System.StringComparison]::OrdinalIgnoreCase)) {
    if ($candidate.Contains('/')) {
      return $false
    }
    $segments = @($candidate.Substring(3).Split($backslash[0]))
    if (@($segments | Where-Object { $_ -ceq '.' -or $_ -ceq '..' }).Count -gt 0) {
      return $false
    }
    try {
      $canonicalFixture = [System.IO.Path]::GetFullPath($fixturePrefix).TrimEnd($backslash[0])
      $canonicalCandidate = [System.IO.Path]::GetFullPath($candidate).TrimEnd($backslash[0])
      if ($canonicalCandidate.Equals($canonicalFixture, [System.StringComparison]::OrdinalIgnoreCase) -or
          $canonicalCandidate.StartsWith($canonicalFixture + $backslash, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
      }
    } catch {
      return $false
    }
  }

  return $false
}

function Test-AllowedPublishedUncMatch {
  param(
    [Parameter(Mandatory)][string]$Text,
    [Parameter(Mandatory)][System.Text.RegularExpressions.Match]$Match,
    [Parameter(Mandatory)][string]$RelativePath,
    [Parameter(Mandatory)][string]$ApprovedUrlPattern
  )

  $backslash = [string][char]0x5c
  if (-not $Match.Value.Contains($backslash)) {
    foreach ($urlMatch in [regex]::Matches($Text, $ApprovedUrlPattern)) {
      if ($Match.Index -ge $urlMatch.Index -and
          $Match.Index -lt $urlMatch.Index + $urlMatch.Length) {
        return $true
      }
    }
  }

  if ($RelativePath -cne 'scripts/AkashaBot.Common.psm1') {
    return $false
  }

  $apostrophe = [string][char]0x27
  $doubleQuote = [string][char]0x22
  $escapedRegexCharacterClassTail = ($backslash * 3) + 'r' + $backslash + 'n])'
  if ($Match.Value -cne $escapedRegexCharacterClassTail) {
    return $false
  }

  $expectedWindowLines = @(
    ('  $prefix = ' + $apostrophe + '(?<prefix>(?<keyQuote>' + ($backslash * 2) + '?[' + $doubleQuote + ($apostrophe * 2) + ']?)' + $apostrophe + ' + $KeyPattern + ' + $apostrophe + $backslash + 'k<keyQuote>' + $backslash + 's*[=:]' + $backslash + 's*)' + $apostrophe)
    '  $patterns = @('
    ('    ($prefix + ' + $apostrophe + '(?<open>' + ($backslash * 2) + $doubleQuote + ')(?<quoted>.*?)(?<discard>(?<!' + ($backslash * 2) + ')(?:' + ($backslash * 8) + ')*)(?<close>' + ($backslash * 2) + $doubleQuote + ')' + $apostrophe + '),')
    ('    ($prefix + ' + $apostrophe + '(?<open>' + ($backslash * 2) + ($apostrophe * 2) + ')(?<quoted>.*?)(?<discard>(?<!' + ($backslash * 2) + ')(?:' + ($backslash * 8) + ')*)(?<close>' + ($backslash * 2) + ($apostrophe * 2) + ')' + $apostrophe + '),')
    ('    ($prefix + ' + $apostrophe + '(?<open>' + $doubleQuote + ')(?<quoted>(?:' + ($backslash * 2) + '.|[^' + $doubleQuote + ($backslash * 3) + 'r' + $backslash + 'n])*)(?<close>' + $doubleQuote + ')' + $apostrophe + '),')
    ('    ($prefix + ' + $apostrophe + '(?<open>' + ($apostrophe * 2) + ')(?<quoted>(?:' + ($backslash * 2) + '.|[^' + ($apostrophe * 2) + ($backslash * 3) + 'r' + $backslash + 'n])*)(?<close>' + ($apostrophe * 2) + ')' + $apostrophe + ')')
    '  )'
    '  $safe = $Text'
  )
  $normalizedText = $Text.Replace("`r`n", "`n")
  $expectedWindow = [string]::Join("`n", $expectedWindowLines)
  if ([regex]::Matches($normalizedText, [regex]::Escape($expectedWindow)).Count -ne 1) {
    return $false
  }

  $expectedContextLines = @(
    'function Protect-AkashaQuotedAssignments {'
    '  param('
    '    [Parameter(Mandatory)][AllowEmptyString()][string]$Text,'
    '    [Parameter(Mandatory)][string]$KeyPattern,'
    '    [Parameter(Mandatory)]$Options,'
    '    [string[]]$AllowedPlaceholders = @()'
    '  )'
    ''
  ) + $expectedWindowLines + @(
    '  foreach ($pattern in $patterns) {'
  )
  $expectedContext = [string]::Join("`n", $expectedContextLines)
  if ([regex]::Matches($normalizedText, [regex]::Escape($expectedContext)).Count -ne 1) {
    return $false
  }

  $allowedPatternLines = @($expectedWindowLines[4], $expectedWindowLines[5])
  foreach ($allowedLine in $allowedPatternLines) {
    if ([regex]::Matches($normalizedText, [regex]::Escape($allowedLine)).Count -ne 1) {
      return $false
    }
  }

  $lineStart = $Text.LastIndexOf("`n", $Match.Index)
  $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
  $lineEnd = $Text.IndexOf("`n", $Match.Index)
  if ($lineEnd -lt 0) {
    $lineEnd = $Text.Length
  }
  $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
  return $allowedPatternLines -ccontains $matchedLine
}

function Test-AllowedPublishedNativeNtMatch {
  param(
    [Parameter(Mandatory)][string]$Text,
    [Parameter(Mandatory)][System.Text.RegularExpressions.Match]$Match
  )

  foreach ($provider in @('Env:', 'HKCU:', 'HKLM:')) {
    $providerStart = $Match.Index - $provider.Length
    if ($providerStart -lt 0) {
      continue
    }
    $candidate = $Text.Substring($providerStart, $provider.Length)
    if (-not $candidate.Equals($provider, [System.StringComparison]::OrdinalIgnoreCase)) {
      continue
    }
    if ($providerStart -eq 0) {
      return $true
    }
    $previous = $Text[$providerStart - 1]
    if (-not [char]::IsLetterOrDigit($previous) -and
        @('_', '.', ':', '$', '/', [char]0x5c, '-') -cnotcontains $previous) {
      return $true
    }
  }
  return $false
}

$textExtensions = @('.py', '.ps1', '.psm1', '.bat', '.md', '.json', '.txt', '.yml', '.yaml', '.toml', '.ini')
$bareSecretExtensions = @('.json', '.toml', '.ini', '.yml', '.yaml')
$secretKeyPattern = '(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|jwt[_-]?secret|password|token|jwt)'
$quotedSecretAssignmentPattern = '(?im)["'']?' + $secretKeyPattern + '["'']?\s*[=:]\s*["''](?<value>[^"''\r\n]*)["'']'
$bareSecretAssignmentPattern = '(?im)^\s*(?:[\{,]\s*)?["'']?' + $secretKeyPattern + '["'']?\s*[=:]\s*(?<value>[^"''#;,\}\]\s][^#;,\}\]\r\n]*)\s*[,\}\]]?\s*(?:[#;].*)?$'
$allowedSecretPlaceholders = @('your_weflow_access_token')
$secretPatterns = @(
  '(?i)sk-[A-Za-z0-9_-]{16,}',
  '(?i)Bearer\s+[A-Za-z0-9._-]{20,}'
)
$regexBackslash = [regex]::Escape([string][char]0x5c)
$regexForwardSlash = [regex]::Escape([string][char]0x2f)
$separatorCharacterClass = '[' + $regexBackslash + $regexForwardSlash + ']'
$driveAbsolutePattern = '(?i)(?<![A-Za-z0-9_])[A-Z]:' + $separatorCharacterClass
$uncSegmentPattern = '[^' + $regexBackslash + $regexForwardSlash + '\x00-\x1F\x22<>:|?*]+'
$backslashOrMixedUncPrefixPattern = '(?<!' + $separatorCharacterClass + ')(?=' + $separatorCharacterClass + '*' + $regexBackslash + ')' + $separatorCharacterClass + '{2,}'
$slashUncPrefixPattern = '(?<!' + $separatorCharacterClass + ')' + $regexForwardSlash + '{2,}'
$uncPrefixPattern = '(?:' + $backslashOrMixedUncPrefixPattern + '|' + $slashUncPrefixPattern + ')'
$standardUncPattern = '(?i)' + $uncPrefixPattern + $uncSegmentPattern + $separatorCharacterClass + $uncSegmentPattern
$extendedUncPattern = '(?i)' + $uncPrefixPattern + '\?' + $separatorCharacterClass + 'UNC' + $separatorCharacterClass + $uncSegmentPattern + $separatorCharacterClass + $uncSegmentPattern
$extendedDevicePattern = '(?i)' + $uncPrefixPattern + '(?:\?' + $separatorCharacterClass + '(?!UNC(?:' + $separatorCharacterClass + '|$))|\.' + $separatorCharacterClass + ')' + $uncSegmentPattern
$nativeNtPathPattern = '(?i)(?<![A-Za-z0-9_.$' + $regexForwardSlash + $regexBackslash + '-])' + $regexBackslash + '(?:\?\?|GLOBALROOT|GLOBAL\?\?|DosDevices|Device|SystemRoot)' + $regexBackslash
$approvedUrlPattern = '(?i)(?<![A-Za-z0-9+.-])(?:https?|wss?):' + $regexForwardSlash + '{2}[^\s<>\x22'']+'
$fileUriPattern = '(?i)(?<![A-Za-z0-9+.-])' + 'fi' + 'le:(?=\S)'
$workspaceNamePattern = '(?i)\bAkashaBot-OneClick-Native-\d{8}\b'
$textFiles = $payload | Where-Object {
  if ($_.PSIsContainer) {
    return $false
  }
  if ($textExtensions -contains $_.Extension) {
    return $true
  }
  return [string]::IsNullOrEmpty($_.Extension) -and (Test-ProbablyTextFile -File $_)
}
foreach ($file in $textFiles) {
  $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
  if ($null -eq $text) {
    $text = ''
  }
  $relativePath = $file.FullName.Substring($root.Length + 1).Replace('\', '/')
  if ($text -match $workspaceNamePattern -or
      $text -match $extendedUncPattern -or
      $text -match $extendedDevicePattern -or
      $text -match $fileUriPattern) {
    throw "Private workspace path found in $relativePath"
  }
  foreach ($uncMatch in [regex]::Matches($text, $standardUncPattern)) {
    if (-not (Test-AllowedPublishedUncMatch -Text $text -Match $uncMatch -RelativePath $relativePath -ApprovedUrlPattern $approvedUrlPattern)) {
      throw "Private workspace path found in $relativePath"
    }
  }
  foreach ($nativeNtMatch in [regex]::Matches($text, $nativeNtPathPattern)) {
    if (-not (Test-AllowedPublishedNativeNtMatch -Text $text -Match $nativeNtMatch)) {
      throw "Private workspace path found in $relativePath"
    }
  }
  foreach ($pathMatch in [regex]::Matches($text, $driveAbsolutePattern)) {
    if (-not (Test-AllowedPublishedDrivePath -Text $text -Index $pathMatch.Index -RelativePath $relativePath)) {
      throw "Private workspace path found in $relativePath"
    }
  }
  $assignmentMatches = @([regex]::Matches($text, $quotedSecretAssignmentPattern))
  if ($bareSecretExtensions -contains $file.Extension -or [string]::IsNullOrEmpty($file.Extension)) {
    $assignmentMatches += @([regex]::Matches($text, $bareSecretAssignmentPattern))
  }
  foreach ($match in $assignmentMatches) {
    $value = $match.Groups['value'].Value.Trim()
    if ($value.Length -ge 12 -and $allowedSecretPlaceholders -cnotcontains $value) {
      throw "Secret-shaped value found in $($file.FullName.Substring($root.Length + 1))"
    }
  }
  foreach ($pattern in $secretPatterns) {
    if ($text -match $pattern) {
      throw "Secret-shaped value found in $($file.FullName.Substring($root.Length + 1))"
    }
  }
}

$template = Get-Content -LiteralPath (Join-Path $root 'bridge\config.example.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$template.access_token -ne 'your_weflow_access_token') {
  throw 'Bridge template access_token must be the documented placeholder.'
}
if (-not [string]::IsNullOrEmpty([string]$template.image_caption_api_key)) {
  throw 'Bridge template image_caption_api_key must be empty.'
}
if (-not [string]::IsNullOrEmpty([string]$template.bot_wxid)) {
  throw 'Bridge template bot_wxid must be empty.'
}
$expectedImageCaptionPrompt = -join @(
  [char]0x8BF7, [char]0x7528, [char]0x4E2D, [char]0x6587,
  [char]0x7B80, [char]0x77ED, [char]0x63CF, [char]0x8FF0,
  [char]0x8FD9, [char]0x5F20, [char]0x56FE, [char]0x7247,
  [char]0x7684, [char]0x5185, [char]0x5BB9
)
if ([string]$template.image_caption_prompt -cne $expectedImageCaptionPrompt) {
  throw 'Bridge template image_caption_prompt must match the documented UTF-8 text.'
}

Write-Host 'Release hygiene: PASS' -ForegroundColor Green
