$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Join-AkashaCharacters {
  param([int[]]$CodePoints)
  return -join @($CodePoints | ForEach-Object { [char]$_ })
}

function Get-AkashaPublishedFiles {
  $localOnly = @('.git', '.superpowers', '.worktrees')
  return @(
    foreach ($entry in Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop |
        Where-Object { $localOnly -cnotcontains $_.Name }) {
      if ($entry.PSIsContainer) {
        Get-ChildItem -LiteralPath $entry.FullName -Recurse -Force -File -ErrorAction Stop
      } else {
        $entry
      }
    }
  )
}

function Assert-AkashaContains {
  param(
    [Parameter(Mandatory)][string]$Text,
    [Parameter(Mandatory)][string]$Expected,
    [Parameter(Mandatory)][string]$Context
  )
  if (-not $Text.Contains($Expected)) {
    throw "Documentation/layout gate: $Context is missing '$Expected'."
  }
}

function Read-AkashaUtf8Strict {
  param([Parameter(Mandatory)][string]$Path)
  $encoding = New-Object System.Text.UTF8Encoding($false, $true)
  return [System.IO.File]::ReadAllText($Path, $encoding)
}

function Assert-AkashaTask7Layout {
  $requiredFiles = @(
    'tests\Run-All.ps1',
    '.github\workflows\ci.yml',
    'README.md',
    'INSTALL.md',
    'SECURITY.md'
  )
  foreach ($relativePath in $requiredFiles) {
    $path = Join-Path $root $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "Documentation/layout gate: missing $relativePath."
    }
  }

  $selfBytes = [System.IO.File]::ReadAllBytes((Join-Path $root 'tests\Run-All.ps1'))
  if (@($selfBytes | Where-Object { $_ -gt 127 }).Count -ne 0) {
    throw 'Documentation/layout gate: Run-All.ps1 must be ASCII-only.'
  }

  $publishedFiles = @(Get-AkashaPublishedFiles)
  if ($publishedFiles.Count -ne 44) {
    throw "Documentation/layout gate: expected 44 publish files, found $($publishedFiles.Count)."
  }

  $updateLauncher = (Join-AkashaCharacters @(0x66F4, 0x65B0)) + '.bat'
  if (Test-Path -LiteralPath (Join-Path $root $updateLauncher)) {
    throw 'Documentation/layout gate: Phase 1 must not publish an update launcher.'
  }

  $ci = Read-AkashaUtf8Strict -Path (Join-Path $root '.github\workflows\ci.yml')
  foreach ($token in @(
    'actions/checkout@v6',
    'actions/setup-python@v6',
    "python-version: '3.12'",
    "architecture: 'x64'",
    'shell: powershell',
    'permissions:',
    'contents: read',
    'timeout-minutes: 15',
    'push:',
    'pull_request:',
    'workflow_dispatch:',
    'run: .\tests\Run-All.ps1'
  )) {
    Assert-AkashaContains -Text $ci -Expected $token -Context 'ci.yml'
  }
  if (@([regex]::Matches($ci, '(?m)^\s*run:\s*')).Count -ne 1) {
    throw 'Documentation/layout gate: CI must have exactly one run entry.'
  }
  foreach ($forbidden in @('actions/checkout@v4', 'actions/checkout@v5', 'actions/setup-python@v4', 'actions/setup-python@v5', 'write-all', 'actions/cache@', 'upload-artifact', 'publish', 'deploy', 'secrets', 'contents: write', 'packages: write', 'id-token: write', 'pull-requests: write')) {
    if ($ci -match [regex]::Escape($forbidden)) {
      throw "Documentation/layout gate: CI contains forbidden token '$forbidden'."
    }
  }
  $expectedCi = @'
name: ci

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  windows-tests:
    runs-on: windows-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.12'
          architecture: 'x64'
      - name: Run unified installer and hygiene tests
        shell: powershell
        run: .\tests\Run-All.ps1
'@
  $newlineTrim = [char[]]@([char]13, [char]10)
  $normalizedCi = $ci.Replace("`r`n", "`n").TrimEnd($newlineTrim)
  $normalizedExpectedCi = $expectedCi.Replace("`r`n", "`n").TrimEnd($newlineTrim)
  if ($normalizedCi -cne $normalizedExpectedCi) {
    throw 'Documentation/layout gate: ci.yml differs from the frozen read-only Windows workflow.'
  }
  $uses = @([regex]::Matches($ci, '(?m)^\s*-\s*uses:\s*(?<action>\S+)\s*$') | ForEach-Object { $_.Groups['action'].Value })
  if ($uses.Count -ne 2 -or $uses[0] -cne 'actions/checkout@v6' -or $uses[1] -cne 'actions/setup-python@v6') {
    throw 'Documentation/layout gate: CI uses entries must be the two approved official actions.'
  }

  $readme = Read-AkashaUtf8Strict -Path (Join-Path $root 'README.md')
  $install = Read-AkashaUtf8Strict -Path (Join-Path $root 'INSTALL.md')
  $security = Read-AkashaUtf8Strict -Path (Join-Path $root 'SECURITY.md')
  $publicDocs = $readme + "`n" + $install + "`n" + $security
  foreach ($token in @(
    'Windows 10/11 x64',
    'Python 3.12 x64',
    'PyPI',
    '%LOCALAPPDATA%\AkashaBot-WeFlow-Bridge',
    'runtime\venvs\bridge',
    'runtime\venvs\astrbot',
    'FIRST_LOGIN.txt',
    '127.0.0.1:5031',
    '127.0.0.1:6185',
    '127.0.0.1:11229',
    '127.0.0.1:8766',
    'data\logs\install.log',
    'E_PYTHON_312_X64',
    'E_WEFLOW_CANCELLED',
    'E_WEFLOW_INSTALL_FAILED',
    'E_WEFLOW_NOT_DETECTED',
    'E_WEFLOW_CONFIG_MISSING',
    'E_WEFLOW_RUNNING',
    'E_LIFECYCLE_BUSY',
    'E_INSTALL_RUNNING',
    'E_PROCESS_STATE',
    'E_HEALTH_FAILED',
    'tests\Run-All.ps1',
    'INSTALL.md',
    'SECURITY.md',
    'MIT',
    'AstrBot',
    'WeFlow',
    'OneBot v11'
  )) {
    Assert-AkashaContains -Text $publicDocs -Expected $token -Context 'public documentation'
  }

  foreach ($relativeLink in @('INSTALL.md', 'SECURITY.md')) {
    Assert-AkashaContains -Text $readme -Expected ('](' + $relativeLink + ')') -Context 'README.md'
    if (-not (Test-Path -LiteralPath (Join-Path $root $relativeLink) -PathType Leaf)) {
      throw "Documentation/layout gate: broken README link $relativeLink."
    }
  }
  $markdownDocuments = @(
    [pscustomobject]@{ Path = Join-Path $root 'README.md'; Text = $readme },
    [pscustomobject]@{ Path = Join-Path $root 'INSTALL.md'; Text = $install },
    [pscustomobject]@{ Path = Join-Path $root 'SECURITY.md'; Text = $security }
  )
  foreach ($document in $markdownDocuments) {
    foreach ($linkMatch in @([regex]::Matches($document.Text, '\[[^\]]+\]\((?<target>[^\)#]+)(?:#[^\)]*)?\)'))) {
      $target = $linkMatch.Groups['target'].Value
      if ($target -match '^[A-Za-z][A-Za-z0-9+.-]*:') { continue }
      $linkPath = Join-Path (Split-Path -Parent $document.Path) ($target.Replace('/', '\'))
      if (-not (Test-Path -LiteralPath $linkPath -PathType Leaf)) {
        throw "Documentation/layout gate: broken local Markdown link '$target'."
      }
    }
  }
}

function Assert-AkashaPowerShellParses {
  $parseErrors = New-Object System.Collections.Generic.List[string]
  foreach ($file in @(Get-AkashaPublishedFiles | Where-Object { $_.Extension -in @('.ps1', '.psm1') })) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    foreach ($errorRecord in @($errors)) {
      $parseErrors.Add(($file.FullName.Substring($root.Length + 1) + ': ' + $errorRecord.Message))
    }
  }
  if ($parseErrors.Count -gt 0) {
    throw ('PowerShell parser gate failed: ' + ($parseErrors -join ' | '))
  }
}

try {
  Assert-AkashaTask7Layout
  Assert-AkashaPowerShellParses

  $testSuites = @(
    'Test-InstallerLayout.ps1',
    'Test-ProcessSafety.ps1',
    'Test-Initialization.ps1',
    'Test-Common.ps1',
    'Test-ReleaseHygiene.ps1',
    'Test-ReleaseHygieneRegression.ps1'
  )
  foreach ($testSuite in $testSuites) {
    $testPath = Join-Path $PSScriptRoot $testSuite
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $testPath
    $suiteExitCode = $LASTEXITCODE
    if ($suiteExitCode -ne 0) {
      [Console]::Error.WriteLine("$testSuite failed with exit code $suiteExitCode.")
      exit $suiteExitCode
    }
  }

  Import-Module (Join-Path $root 'scripts\AkashaBot.Common.psm1') -Force
  $python = Resolve-Python312
  $pythonArguments = @($python.Prefix) + @(
    '-B',
    '-m',
    'unittest',
    'discover',
    '-s',
    (Join-Path $PSScriptRoot 'python'),
    '-p',
    'test_*.py',
    '-v'
  )
  $previousBytecodeVariable = Get-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
  try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $python.FilePath @pythonArguments
    $pythonExitCode = $LASTEXITCODE
  } finally {
    if ($null -eq $previousBytecodeVariable) {
      Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    } else {
      $env:PYTHONDONTWRITEBYTECODE = [string]$previousBytecodeVariable.Value
    }
  }
  if ($pythonExitCode -ne 0) {
    [Console]::Error.WriteLine("Python tests failed with exit code $pythonExitCode.")
    exit $pythonExitCode
  }

  Write-Host 'All tests: PASS' -ForegroundColor Green
  exit 0
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
