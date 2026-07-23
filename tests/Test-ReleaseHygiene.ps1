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
$localOnlyRootEntries = @('.git', '.superpowers', '.worktrees', 'docs')
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
  'bridge/calibrate_uia_fixed.py',
  'bridge/config.example.json',
  'bridge/config.py',
  'bridge/main.py',
  'bridge/ob_client.py',
  'bridge/ob_protocol.py',
  'bridge/privacy.py',
  'bridge/requirements.lock',
  'bridge/requirements.txt',
  'bridge/state.py',
  'bridge/uia_fixed_sender.py',
  'bridge/uia_support.py',
  'bridge/web_panel.py',
  'scripts/AkashaBot.Common.psm1',
  'scripts/Calibrate-Uia.ps1',
  'scripts/Initialize-Configuration.ps1',
  'scripts/Initialize-Environments.ps1',
  'scripts/Install.ps1',
  'scripts/Start-Services.ps1',
  'scripts/Stop-Services.ps1',
  'scripts/Test-Prerequisites.ps1',
  'scripts/Test-Health.ps1',
  'tests/Test-Common.ps1',
  'tests/Test-Calibration.ps1',
  'tests/Test-Initialization.ps1',
  'tests/Test-InstallerLayout.ps1',
  'tests/Test-ProcessSafety.ps1',
  'tests/Test-ReleaseHygiene.ps1',
  'tests/Test-ReleaseHygieneRegression.ps1',
  'tests/Run-All.ps1',
  'tests/python/test_bridge_runtime.py',
  'tests/python/test_uia_calibration.py',
  'tests/python/test_uia_fixed_sender.py',
  'tests/python/test_uia_support.py',
  ((-join @([char]0x5B89, [char]0x88C5)) + '.bat'),
  ((-join @([char]0x542F, [char]0x52A8)) + '.bat'),
  ((-join @([char]0x505C, [char]0x6B62)) + '.bat'),
  ((-join @([char]0x5065, [char]0x5EB7, [char]0x68C0, [char]0x67E5)) + '.bat'),
  ((-join @([char]0x6821, [char]0x51C6)) + '.bat')
)
if ($expectedPublishFiles.Count -ne 50) {
  throw "Release allowlist invariant is not 50 files: $($expectedPublishFiles.Count)"
}
$uniqueExpectedPublishFiles = @($expectedPublishFiles | Sort-Object -Unique)
if ($uniqueExpectedPublishFiles.Count -ne 50) {
  throw "Release allowlist must contain 50 unique entries; duplicate entries were found."
}
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
$uniqueActualPublishFiles = @($actualPublishFiles | Sort-Object -Unique)
if ($actualPublishFiles.Count -ne 50 -or $uniqueActualPublishFiles.Count -ne 50) {
  throw "Published files must contain exactly 50 unique entries; found $($actualPublishFiles.Count) entries and $($uniqueActualPublishFiles.Count) unique entries."
}

$bridgeRoot = Join-Path $root 'bridge'
$expectedLockedRequirements = @(
  'requests==2.34.2',
  'pyperclip==1.11.0',
  'Pillow==12.2.0',
  'websockets==16.0'
)
$actualRequirements = @(Get-Content -LiteralPath (Join-Path $bridgeRoot 'requirements.lock') -Encoding UTF8)
if ($actualRequirements.Count -ne $expectedLockedRequirements.Count -or
    [string]::Join("`n", $actualRequirements) -cne [string]::Join("`n", $expectedLockedRequirements)) {
  throw 'requirements.lock does not match the exact dependency pins.'
}
$expectedRequirementNames = @('requests', 'pyperclip', 'Pillow', 'websockets')
foreach ($requirementsFileName in @('requirements.txt', 'requirements.lock')) {
  $requirementsPath = Join-Path $bridgeRoot $requirementsFileName
  $requirementNames = @(
    foreach ($line in @(Get-Content -LiteralPath $requirementsPath -Encoding UTF8)) {
      if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) {
        continue
      }
      $match = [regex]::Match($line, '^\s*(?<name>[A-Za-z0-9_.-]+)\s*(?:[<>=!~].*)?$')
      if (-not $match.Success) {
        throw "Unsupported dependency declaration in bridge/$requirementsFileName"
      }
      $match.Groups['name'].Value
    }
  )
  if ($requirementNames.Count -ne $expectedRequirementNames.Count -or
      [string]::Join("`n", $requirementNames) -cne [string]::Join("`n", $expectedRequirementNames)) {
    throw "bridge/$requirementsFileName dependency names do not match the exact allowlist."
  }
}

$legacyMarkers = @(
  'weflow_api',
  'WeFlowApiSender',
  'uia_sender',
  'UiaSender',
  'send_method',
  'weflow_send_api',
  'uia_fixed_search_x',
  'use_enter_to_send'
)
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
$forbiddenLegacyTokens = @($legacyMarkers + $legacyBridgeKeys)
$initializerRelativePath = 'scripts/Initialize-Configuration.ps1'
$zeroHitFiles = @(
  foreach ($relativePath in $expectedPublishFiles) {
    if ($relativePath.StartsWith('tests/', [System.StringComparison]::Ordinal) -or
        $relativePath -ceq $initializerRelativePath) {
      continue
    }
    Get-Item -LiteralPath (Join-Path $root ($relativePath.Replace('/', '\'))) -ErrorAction Stop
  }
)
foreach ($file in $zeroHitFiles) {
  $source = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
  foreach ($marker in $forbiddenLegacyTokens) {
    if ($source.Contains($marker)) {
      throw "Legacy sender/config marker '$marker' found in $($file.FullName.Substring($root.Length + 1))."
    }
  }
}

$placeholderMarker = 'place' + 'holder'
$expectedPlaceholderLinesText = @'
        html += '<textarea id="cfg_' + f.key + '" placeholder="' + escapeHtml(f.ph||'') + '" rows="2">' + safeVal + '</textarea>';
        html += '<input type="number" id="cfg_' + f.key + '" value="' + safeVal + '" placeholder="' + escapeHtml(f.ph||'') + '">';
        html += '<input type="' + f.type + '" id="cfg_' + f.key + '" value="' + safeVal + '" placeholder="' + escapeHtml(f.ph||'') + '">';
'@
$expectedPlaceholderHits = @(
  $expectedPlaceholderLinesText.Replace("`r`n", "`n").TrimEnd("`n").Split("`n") |
    ForEach-Object { 'bridge/web_panel.py|' + $_ }
) | Sort-Object
$actualPlaceholderHits = @(
  foreach ($file in @(
    Get-ChildItem -LiteralPath $bridgeRoot -File -ErrorAction Stop
    Get-Item -LiteralPath (Join-Path $root 'README.md'), (Join-Path $root 'INSTALL.md'), (Join-Path $root 'CHANGELOG.md')
  )) {
    $relativePath = $file.FullName.Substring($root.Length + 1).Replace('\', '/')
    foreach ($line in @(Get-Content -LiteralPath $file.FullName -Encoding UTF8)) {
      if ($line.Contains($placeholderMarker)) {
        $relativePath + '|' + $line
      }
    }
  }
) | Sort-Object
if ($actualPlaceholderHits.Count -ne $expectedPlaceholderHits.Count -or
    [string]::Join("`n", $actualPlaceholderHits) -cne [string]::Join("`n", $expectedPlaceholderHits)) {
  throw 'Placeholder marker found outside the three approved HTML attribute lines.'
}

$expectedLegacyLines = @('      $legacyBridgeKeys = @(') +
  @($legacyBridgeKeys | ForEach-Object { "        '$_'," }) +
  @(
    '      )',
    '      foreach ($legacyBridgeKey in $legacyBridgeKeys) {',
    '        Remove-JsonProperty -Object $bridge -Name $legacyBridgeKey',
    '      }'
  )
$expectedLegacyLines[$legacyBridgeKeys.Count] = $expectedLegacyLines[$legacyBridgeKeys.Count].TrimEnd(',')
$expectedLegacyBlock = [string]::Join("`n", $expectedLegacyLines)
$expectedLegacyContextLines = @(
  '        if ($null -eq $bridge.PSObject.Properties[''uia_fixed_calibration'']) {',
  '          Set-JsonProperty -Object $bridge -Name ''uia_fixed_calibration'' -Value $calibrationTemplateProperty.Value',
  '        }',
  '      }',
  ''
) + $expectedLegacyLines + @(
  '',
  '      $weFlow = Read-AkashaConfigurationJson -Path $WeFlowConfigPath',
  '      if ($weFlow.GetType() -ne [System.Management.Automation.PSCustomObject]) {'
)
$expectedLegacyContext = [string]::Join("`n", $expectedLegacyContextLines)
$initializerPath = Join-Path $root ($initializerRelativePath.Replace('/', '\'))
$initializerSource = (Get-Content -LiteralPath $initializerPath -Raw -Encoding UTF8).Replace("`r`n", "`n")
if ([regex]::Matches($initializerSource, [regex]::Escape($expectedLegacyBlock)).Count -ne 1 -or
    [regex]::Matches($initializerSource, [regex]::Escape($expectedLegacyContext)).Count -ne 1) {
  throw 'Initializer legacy deletion allowlist does not match the exact approved context.'
}
$initializerOutsideLegacyContext = $initializerSource.Replace($expectedLegacyContext, '')
foreach ($marker in $forbiddenLegacyTokens) {
  if ($initializerOutsideLegacyContext.Contains($marker)) {
    throw "Legacy sender/config marker '$marker' found outside the approved initializer deletion allowlist."
  }
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
    $expectedUiContextLines = @(
      'ICAgICAge2tleTonYXN0cmJvdF9vYl91cmwnLCBsYWJlbDonQXN0ckJvdCBPQiDlnLDlnYAnLCB0eXBlOid0ZXh0JywgcGg6J3dzOi8vMTI3LjAuMC4xOjExMjI5L3dzJ30s',
      'ICAgICAge2tleTonYXN0cmJvdF9hdHRhY2htZW50cycsIGxhYmVsOifpmYTku7bnm67lvZXvvIhBc3RyQm90IOWtmOaUvuWbvueJh+eahOi3r+W+hO+8iScsIHR5cGU6J3RleHQnLCBwaDonQzpcXGFzdHJib3RcXGF0dGFjaG1lbnRzJ30s',
      'ICAgIF19LA=='
    ) | ForEach-Object { [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
    $expectedUiLine = $expectedUiContextLines[1]
    $expectedUiContext = [string]::Join("`n", $expectedUiContextLines)
    $normalizedText = $Text.Replace("`r`n", "`n")
    if ([regex]::Matches($normalizedText, [regex]::Escape($expectedUiLine)).Count -ne 1 -or
        [regex]::Matches($normalizedText, [regex]::Escape($expectedUiContext)).Count -ne 1) {
      return $false
    }
    $lineStart = $Text.LastIndexOf("`n", $Index)
    $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
    $lineEnd = $Text.IndexOf("`n", $Index)
    if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
    $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
    return $matchedLine -ceq $expectedUiLine
  }

  if ($RelativePath -ceq 'tests/python/test_uia_support.py') {
    $wechatExecutablePath = 'C:' + $backslash + 'Program Files' + $backslash + 'Tencent' + $backslash + 'WeChat.exe'
    $weixinExecutablePath = 'C:' + $backslash + 'Program Files' + $backslash + 'Tencent' + $backslash + 'Weixin.exe'
    if ($candidate -ceq $wechatExecutablePath -or $candidate -ceq $weixinExecutablePath) {
      $wechatExecutableLine = '            100: r"' + $wechatExecutablePath + '",'
      $weixinExecutableLine = '            200: r"' + $weixinExecutablePath + '",'
      $expectedProcessImageContext = [string]::Join("`n", @(
        '        self.process_images = {'
        $wechatExecutableLine
        $weixinExecutableLine
        '        }'
      ))
      $expectedLine = if ($candidate -ceq $wechatExecutablePath) { $wechatExecutableLine } else { $weixinExecutableLine }
      $normalizedText = $Text.Replace("`r`n", "`n")
      if ([regex]::Matches($normalizedText, [regex]::Escape($expectedLine)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedProcessImageContext)).Count -ne 1) {
        return $false
      }
      $lineStart = $Text.LastIndexOf("`n", $Index)
      $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
      $lineEnd = $Text.IndexOf("`n", $Index)
      if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
      $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
      return $matchedLine -ceq $expectedLine
    }

    $otherExecutablePath = 'C:' + $backslash + 'Other' + $backslash + 'NotWeChat.exe'
    $portableExecutablePath = 'D:' + $backslash + 'Portable' + $backslash + 'WeChat.exe'
    if ($candidate -ceq $otherExecutablePath -or $candidate -ceq $portableExecutablePath) {
      if ($candidate -ceq $otherExecutablePath) {
        $expectedLine = '                kernel32.process_images[100] = r"' + $otherExecutablePath + '"'
        $expectedContext = [string]::Join("`n", @(
          '                kernel32 = FakeKernel32()'
          $expectedLine
          ''
          '                self.assert_window_error('
        ))
      } else {
        $expectedLine = '                    kernel32.process_images[100] = r"' + $portableExecutablePath + '"'
        $expectedContext = [string]::Join("`n", @(
          '                else:'
          $expectedLine
          ''
          '                self.assert_window_error(lambda: driver.get_client_metrics(10))'
        ))
      }
      $normalizedText = $Text.Replace("`r`n", "`n")
      if ([regex]::Matches($normalizedText, [regex]::Escape($expectedLine)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedContext)).Count -ne 1) {
        return $false
      }
      $lineStart = $Text.LastIndexOf("`n", $Index)
      $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
      $lineEnd = $Text.IndexOf("`n", $Index)
      if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
      $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
      return $matchedLine -ceq $expectedLine
    }

    $canonicalizedWechatPath = 'c:' + '/' + 'program files' + '/' + 'tencent' + '/' + 'WECHAT.EXE'
    if ($candidate -ceq $canonicalizedWechatPath) {
      $expectedCanonicalizedLine = '        kernel32.process_images[100] = r"' + $canonicalizedWechatPath + '"'
      $expectedCanonicalizedContext = [string]::Join("`n", @(
        '        self.assertEqual(driver.find_wechat_window(), 10)'
        $expectedCanonicalizedLine
        ''
        '        self.assertEqual(driver.get_client_metrics(10), METRICS)'
      ))
      $normalizedText = $Text.Replace("`r`n", "`n")
      if ([regex]::Matches($normalizedText, [regex]::Escape($expectedCanonicalizedLine)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedCanonicalizedContext)).Count -ne 1) {
        return $false
      }
      $lineStart = $Text.LastIndexOf("`n", $Index)
      $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
      $lineEnd = $Text.IndexOf("`n", $Index)
      if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
      $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
      return $matchedLine -ceq $expectedCanonicalizedLine
    }

    $testImagePath = 'C:' + $backslash + 'private' + $backslash + 'never-log-this.png'
    if ($candidate -ceq $testImagePath) {
      $approvedLines = @(
        ('                r"' + $testImagePath + '"'),
        ('            r"' + $testImagePath + '",'),
        ('                    r"' + $testImagePath + '"')
      )
      $normalizedText = $Text.Replace("`r`n", "`n")
      foreach ($approvedLine in $approvedLines) {
        if ([regex]::Matches($normalizedText, ('(?m)^' + [regex]::Escape($approvedLine) + '$')).Count -ne 1) {
          return $false
        }
      }
      $expectedOwnershipTail = [string]::Join("`n", @(
        '        self.assertTrue('
        '            any(call[:2] == ("SetClipboardData", 8) for call in user32.calls)'
        '        )'
        '        self.assertFalse('
        '            any(call[0] == "GlobalFree" for call in kernel32.calls)'
        '        )'
      ))
      $expectedFailureTail = [string]::Join("`n", @(
        '        allocated_handle = next('
        '            call[3] for call in kernel32.calls if call[0] == "GlobalAlloc"'
        '        )'
        '        self.assertIn(("GlobalFree", allocated_handle), kernel32.calls)'
        '        self.assertIn(("CloseClipboard",), user32.calls)'
      ))
      $expectedOwnershipContext = [string]::Join("`n", @(
        '    def test_copy_image_transfers_dib_ownership_to_clipboard(self):'
        '        user32 = FakeUser32()'
        '        kernel32 = FakeKernel32()'
        '        image_module = types.ModuleType("PIL.Image")'
        '        image_module.open = mock.Mock(return_value=FakeImage())'
        '        pil_module = types.ModuleType("PIL")'
        '        pil_module.Image = image_module'
        ''
        '        with mock.patch.dict('
        '            sys.modules, {"PIL": pil_module, "PIL.Image": image_module}'
        '        ):'
        '            self.make_driver(user32, kernel32).copy_image_to_clipboard('
        $approvedLines[0]
        '            )'
        ''
        '        self.assertEqual('
        '            image_module.open.call_args.args[0],'
        $approvedLines[1]
        '        )'
        $expectedOwnershipTail
        ''
        '    def test_copy_image_frees_untransferred_memory_on_failure(self):'
      ))
      $expectedFailureContext = [string]::Join("`n", @(
        '    def test_copy_image_frees_untransferred_memory_on_failure(self):'
        '        user32 = FakeUser32()'
        '        user32.set_clipboard_result = 0'
        '        kernel32 = FakeKernel32()'
        '        image_module = types.ModuleType("PIL.Image")'
        '        image_module.open = mock.Mock(return_value=FakeImage())'
        '        pil_module = types.ModuleType("PIL")'
        '        pil_module.Image = image_module'
        ''
        '        with mock.patch.dict('
        '            sys.modules, {"PIL": pil_module, "PIL.Image": image_module}'
        '        ):'
        '            with self.assertRaises(OSError):'
        '                self.make_driver(user32, kernel32).copy_image_to_clipboard('
        $approvedLines[2]
        '                )'
        ''
        $expectedFailureTail
        ''
        ''
        'if __name__ == "__main__":'
      ))
      if ([regex]::Matches($normalizedText, [regex]::Escape($expectedOwnershipContext)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedFailureContext)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedOwnershipTail)).Count -ne 1 -or
          [regex]::Matches($normalizedText, [regex]::Escape($expectedFailureTail)).Count -ne 1) {
        return $false
      }
      $lineStart = $Text.LastIndexOf("`n", $Index)
      $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
      $lineEnd = $Text.IndexOf("`n", $Index)
      if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
      $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
      return $approvedLines -ccontains $matchedLine
    }
  }

  $fixturePythonPath = 'C:' + $backslash + 'fixture' + $backslash + 'py.exe'
  if ($RelativePath -ceq 'tests/Test-Initialization.ps1' -and $candidate -ceq $fixturePythonPath) {
    $expectedFixtureLine = '  $python = [pscustomobject]@{ FilePath = ''C:' +
      $backslash + 'fixture' + $backslash + 'py.exe''; Prefix = @(''-3.12'') }'
    $expectedFixtureContext = [string]::Join("`n", @(
      '  }'
      $expectedFixtureLine
      '  Initialize-AkashaEnvironments -Paths $paths -Python $python -Runner $runner'
    ))
    $normalizedText = $Text.Replace("`r`n", "`n")
    if ([regex]::Matches($normalizedText, [regex]::Escape($expectedFixtureLine)).Count -ne 1 -or
        [regex]::Matches($normalizedText, [regex]::Escape($expectedFixtureContext)).Count -ne 1) {
      return $false
    }
    $lineStart = $Text.LastIndexOf("`n", $Index)
    $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
    $lineEnd = $Text.IndexOf("`n", $Index)
    if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
    $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
    return $matchedLine -ceq $expectedFixtureLine
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

  if ($RelativePath -ceq 'tests/Test-InstallerLayout.ps1') {
    $expectedLauncherAssertion = 'Assert-True ($calibrateBat -match ''%~dp0scripts' +
      ($backslash * 2) + 'Calibrate-Uia' + $backslash +
      '.ps1'') "$($launchers.Calibrate) does not use the source-relative calibration script."'
    $normalizedText = $Text.Replace("`r`n", "`n")
    if ([regex]::Matches($normalizedText, [regex]::Escape($expectedLauncherAssertion)).Count -eq 1) {
      $lineStart = $Text.LastIndexOf("`n", $Match.Index)
      $lineStart = if ($lineStart -lt 0) { 0 } else { $lineStart + 1 }
      $lineEnd = $Text.IndexOf("`n", $Match.Index)
      if ($lineEnd -lt 0) { $lineEnd = $Text.Length }
      $matchedLine = $Text.Substring($lineStart, $lineEnd - $lineStart).TrimEnd("`r")
      if ($matchedLine -ceq $expectedLauncherAssertion) {
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

function Test-AllowedPublishedTestSecretAssignment {
  param(
    [Parameter(Mandatory)][string]$Text,
    [Parameter(Mandatory)][System.Text.RegularExpressions.Match]$Match,
    [Parameter(Mandatory)][string]$RelativePath
  )

  $fixtureKey = 'access_' + 'token'
  if ($RelativePath -ceq 'tests/python/test_bridge_runtime.py') {
    $fixtureValue = 'private-' + 'token'
    $expectedLines = @(
      '            config_path.write_text(',
      '                json.dumps(',
      '                    {',
      ('                        "' + $fixtureKey + '": "' + $fixtureValue + '",'),
      '                        "buffer_seconds": 5,',
      '                        "uia_fixed_calibration": calibration,',
      '                    }',
      '                ),',
      '                encoding="utf-8",',
      '            )'
    )
  } elseif ($RelativePath -ceq 'tests/python/test_uia_calibration.py') {
    $fixtureValue = 'opaque-' + 'token-value'
    $otherKey = -join @([char]0x5176, [char]0x4ED6, [char]0x952E)
    $preserved = -join @([char]0x4FDD, [char]0x7559)
    $expectedLines = @(
      '        self.original = {',
      ('            "' + $fixtureKey + '": "' + $fixtureValue + '",'),
      ('            "' + $otherKey + '": {"' + $preserved + '": True},'),
      '            "uia_fixed_calibration": {"completed": False},',
      '        }'
    )
  } else {
    return $false
  }
  $expectedContext = [string]::Join("`n", $expectedLines)
  $normalizedText = $Text.Replace("`r`n", "`n")
  if ([regex]::Matches($normalizedText, [regex]::Escape($expectedContext)).Count -ne 1) {
    return $false
  }
  $expectedAssignment = '"' + $fixtureKey + '": "' + $fixtureValue + '"'
  if ($Match.Value -cne $expectedAssignment) {
    return $false
  }
  $contextIndex = $normalizedText.IndexOf($expectedContext, [System.StringComparison]::Ordinal)
  return $Match.Index -ge $contextIndex -and $Match.Index -lt ($contextIndex + $expectedContext.Length)
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
    if ($value.Length -ge 12 -and
        $allowedSecretPlaceholders -cnotcontains $value -and
        -not (Test-AllowedPublishedTestSecretAssignment -Text $text -Match $match -RelativePath $relativePath)) {
      throw "Secret-shaped value found in $($file.FullName.Substring($root.Length + 1))"
    }
  }
  foreach ($pattern in $secretPatterns) {
    if ($text -match $pattern) {
      throw "Secret-shaped value found in $($file.FullName.Substring($root.Length + 1))"
    }
  }
}

$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw -Encoding UTF8).Trim()
if ($version -cne '0.2.7') {
  throw "VERSION must be 0.2.7, found '$version'."
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
$calibration = $template.uia_fixed_calibration
if ($calibration.completed -ne $false -or $null -ne $calibration.reference) {
  throw 'Bridge template must contain only an incomplete calibration placeholder.'
}
foreach ($pointName in @('search_box', 'first_result', 'message_input', 'send_button')) {
  if ($null -ne $calibration.points.PSObject.Properties[$pointName].Value) {
    throw "Bridge template contains a real calibration point: $pointName"
  }
}

$publicDocumentation = @(
  Get-Item -LiteralPath (Join-Path $root 'README.md'), (Join-Path $root 'INSTALL.md'), (Join-Path $root 'CHANGELOG.md')
)
$contactLabels = @(
  (-join @([char]0x8054, [char]0x7CFB, [char]0x4EBA)),
  (-join @([char]0x8054, [char]0x7CFB, [char]0x4EBA, [char]0x6837, [char]0x4F8B)),
  (-join @([char]0x8054, [char]0x7CFB, [char]0x5BF9, [char]0x8C61))
)
$messageLabels = @(
  (-join @([char]0x6D88, [char]0x606F, [char]0x6837, [char]0x4F8B)),
  (-join @([char]0x6D88, [char]0x606F, [char]0x793A, [char]0x4F8B)),
  (-join @([char]0x6D4B, [char]0x8BD5, [char]0x6D88, [char]0x606F))
)
$windowLabels = @(
  (-join @([char]0x7A97, [char]0x53E3, [char]0x6807, [char]0x9898)),
  (-join @([char]0x5FAE, [char]0x4FE1, [char]0x7A97, [char]0x53E3, [char]0x6807, [char]0x9898))
)
$pointLabels = @(
  (-join @([char]0x641C, [char]0x7D22, [char]0x6846)),
  (-join @([char]0x7B2C, [char]0x4E00, [char]0x6761, [char]0x641C, [char]0x7D22, [char]0x7ED3, [char]0x679C)),
  (-join @([char]0x6D88, [char]0x606F, [char]0x8F93, [char]0x5165, [char]0x6846)),
  (-join @([char]0x53D1, [char]0x9001, [char]0x6309, [char]0x94AE))
)
$pointQualifiers = @(
  (-join @([char]0x70B9, [char]0x4F4D)),
  (-join @([char]0x5750, [char]0x6807))
)
$labelSeparator = ':' + [char]0xFF1A + '='
$coordinateSeparator = ',' + [char]0xFF0C
$sensitiveExamplePatterns = @(
  ('(?im)^\s*(?:' + (($contactLabels | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')\s*[' + $labelSeparator + ']\s*\S+'),
  ('(?im)^\s*(?:' + (($messageLabels | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')\s*[' + $labelSeparator + ']\s*\S+'),
  ('(?im)^\s*(?:' + (($windowLabels | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')\s*[' + $labelSeparator + ']\s*\S+'),
  ('(?im)^\s*(?:' + (($pointLabels | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')(?:' + (($pointQualifiers | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')?\s*[' + $labelSeparator + ']\s*[\(\[]?\s*(?:0?\.\d+|\d{2,})\s*[' + $coordinateSeparator + ']\s*(?:0?\.\d+|\d{2,})')
)
foreach ($document in $publicDocumentation) {
  $documentText = Get-Content -LiteralPath $document.FullName -Raw -Encoding UTF8
  foreach ($pattern in $sensitiveExamplePatterns) {
    if ($documentText -match $pattern) {
      throw "Sensitive calibration/contact/message/window example found in $($document.Name)."
    }
  }
}

Write-Host 'Release hygiene: PASS' -ForegroundColor Green
