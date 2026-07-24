$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Wait-WinRtOperation {
  param(
    [Parameter(Mandatory = $true)]
    $Operation,

    [Parameter(Mandatory = $true)]
    [Type]$ResultType
  )

  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {
      $_.Name -eq 'AsTask' -and
      $_.IsGenericMethodDefinition -and
      $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } |
    Select-Object -First 1
  if ($null -eq $method) {
    throw 'E_UIA_OCR_RUNTIME'
  }

  $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.GetAwaiter().GetResult()
}

function Normalize-OcrText {
  param([Parameter(Mandatory = $true)][string]$Text)

  $normalized = $Text.Normalize([System.Text.NormalizationForm]::FormC)
  $normalized = $normalized -replace '\p{Cc}', ''
  return (($normalized -replace '\s+', ' ').Trim())
}

function ConvertFrom-CodePoints {
  param([Parameter(Mandatory = $true)][int[]]$CodePoints)

  return -join @($CodePoints | ForEach-Object { [char]$_ })
}

function Test-HanOnly {
  param([Parameter(Mandatory = $true)][string]$Text)

  if ([string]::IsNullOrEmpty($Text)) {
    return $false
  }
  foreach ($character in $Text.ToCharArray()) {
    $codePoint = [int]$character
    if (
      -not (
        ($codePoint -ge 0x3400 -and $codePoint -le 0x4DBF) -or
        ($codePoint -ge 0x4E00 -and $codePoint -le 0x9FFF) -or
        ($codePoint -ge 0xF900 -and $codePoint -le 0xFAFF)
      )
    ) {
      return $false
    }
  }
  return $true
}

function Get-OcrLineRecords {
  param([Parameter(Mandatory = $true)]$Result)

  $records = @()
  foreach ($line in $Result.Lines) {
    $words = @($line.Words)
    if ($words.Count -eq 0) { continue }
    $left = ($words | ForEach-Object { $_.BoundingRect.X } |
        Measure-Object -Minimum).Minimum
    $top = ($words | ForEach-Object { $_.BoundingRect.Y } |
        Measure-Object -Minimum).Minimum
    $right = ($words | ForEach-Object {
        $_.BoundingRect.X + $_.BoundingRect.Width
      } | Measure-Object -Maximum).Maximum
    $bottom = ($words | ForEach-Object {
        $_.BoundingRect.Y + $_.BoundingRect.Height
      } | Measure-Object -Maximum).Maximum
    $normalized = Normalize-OcrText -Text ([string]$line.Text)
    $height = [double]($bottom - $top)
    $compactSafe = $true
    if ($normalized.Contains(' ')) {
      $orderedWords = @($words | Sort-Object { $_.BoundingRect.X })
      if ($orderedWords.Count -lt 2) {
        $compactSafe = $false
      } else {
        foreach ($word in $orderedWords) {
          $wordText = Normalize-OcrText -Text ([string]$word.Text)
          if (
            $wordText.Length -ne 1 -or
            -not (Test-HanOnly -Text $wordText)
          ) {
            $compactSafe = $false
            break
          }
        }
        for ($index = 1; $index -lt $orderedWords.Count; $index++) {
          $previousRight = [double](
            $orderedWords[$index - 1].BoundingRect.X +
            $orderedWords[$index - 1].BoundingRect.Width
          )
          $gap = [double]$orderedWords[$index].BoundingRect.X - $previousRight
          if ($gap -gt 1.0) {
            $compactSafe = $false
            break
          }
        }
      }
    }
    $records += [pscustomobject]@{
      Normalized = $normalized
      Compact = $normalized -replace '\s', ''
      CompactSafe = $compactSafe
      Left = [double]$left
      Top = [double]$top
      Width = [double]($right - $left)
      Height = $height
      Bottom = [double]$bottom
    }
  }
  return $records
}

function Test-OcrLineMatch {
  param(
    [Parameter(Mandatory = $true)]$Line,
    [Parameter(Mandatory = $true)][string]$Expected
  )

  if ($Line.Normalized -ceq $Expected) {
    return $true
  }
  if ($Expected.Contains(' ') -or -not [bool]$Line.CompactSafe) {
    return $false
  }
  if (
    -not (Test-HanOnly -Text $Expected) -or
    -not (Test-HanOnly -Text ([string]$Line.Compact))
  ) {
    return $false
  }
  return $Line.Compact -ceq ($Expected -replace '\s', '')
}

function Write-Result {
  param([Parameter(Mandatory = $true)]$Value)

  $Value | ConvertTo-Json -Depth 4 -Compress
}

try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
  $null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
  $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

  $rawRequest = [Console]::In.ReadToEnd()
  if ([string]::IsNullOrWhiteSpace($rawRequest) -or $rawRequest.Length -gt 4194304) {
    throw 'E_UIA_OCR_REQUEST'
  }
  $request = $rawRequest | ConvertFrom-Json
  $mode = [string]$request.mode
  $imageBase64 = [string]$request.image_base64
  $expected = Normalize-OcrText -Text ([string]$request.expected_text)
  if (
    $mode -notin @('search', 'title') -or
    [string]::IsNullOrWhiteSpace($imageBase64) -or
    $imageBase64.Length -gt 4194304 -or
    [string]::IsNullOrWhiteSpace($expected)
  ) {
    throw 'E_UIA_OCR_REQUEST'
  }

  try {
    $imageBytes = [Convert]::FromBase64String($imageBase64)
  } catch {
    throw 'E_UIA_OCR_REQUEST'
  }
  if ($imageBytes.Length -eq 0 -or $imageBytes.Length -gt 3145728) {
    throw 'E_UIA_OCR_REQUEST'
  }

  $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
  $outputStream = $stream.GetOutputStreamAt(0)
  $writer = [Windows.Storage.Streams.DataWriter]::new($outputStream)
  try {
    $writer.WriteBytes($imageBytes)
    $null = Wait-WinRtOperation -Operation (
      $writer.StoreAsync()
    ) -ResultType ([uint32])
    $null = Wait-WinRtOperation -Operation (
      $writer.FlushAsync()
    ) -ResultType ([bool])
  } finally {
    try { $null = $writer.DetachStream() } catch { }
    $writer.Dispose()
    $outputStream.Dispose()
  }
  $stream.Seek(0)
  $bitmap = $null
  try {
    $decoder = Wait-WinRtOperation -Operation (
      [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
    ) -ResultType ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wait-WinRtOperation -Operation (
      $decoder.GetSoftwareBitmapAsync()
    ) -ResultType ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
      $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
      if ($null -eq $engine) {
        throw 'E_UIA_OCR_ENGINE'
      }
      $result = Wait-WinRtOperation -Operation (
        $engine.RecognizeAsync($bitmap)
      ) -ResultType ([Windows.Media.Ocr.OcrResult])
      $lines = @(Get-OcrLineRecords -Result $result)
    } finally {
      if ($null -ne $bitmap) { $bitmap.Dispose() }
    }
  } finally {
    $stream.Dispose()
  }

  if ($mode -ceq 'title') {
    $titleMatches = @($lines | Where-Object {
        Test-OcrLineMatch -Line $_ -Expected $expected
      })
    Write-Result -Value ([pscustomobject]@{
        status = 'ok'
        matched = $titleMatches.Count -eq 1
      })
    exit 0
  }

  $section = Normalize-OcrText -Text ([string]$request.section_text)
  if ([string]::IsNullOrWhiteSpace($section)) {
    throw 'E_UIA_OCR_REQUEST'
  }
  $sectionMatches = @($lines | Where-Object {
      Test-OcrLineMatch -Line $_ -Expected $section
    })
  if ($sectionMatches.Count -ne 1) {
    Write-Result -Value ([pscustomobject]@{
        status = 'ok'
        matched = $false
      })
    exit 0
  }

  $headerNames = @(
    (ConvertFrom-CodePoints @(0x641C, 0x7D22, 0x7F51, 0x7EDC, 0x7ED3, 0x679C)),
    (ConvertFrom-CodePoints @(0x8054, 0x7CFB, 0x4EBA)),
    (ConvertFrom-CodePoints @(0x7FA4, 0x804A)),
    (ConvertFrom-CodePoints @(0x516C, 0x4F17, 0x53F7)),
    (ConvertFrom-CodePoints @(0x529F, 0x80FD)),
    (ConvertFrom-CodePoints @(0x804A, 0x5929, 0x8BB0, 0x5F55))
  ) | ForEach-Object { Normalize-OcrText -Text $_ }
  $sectionLine = $sectionMatches[0]
  $nextHeaders = @($lines | Where-Object {
      $line = $_
      @($headerNames | Where-Object {
          Test-OcrLineMatch -Line $line -Expected $_
        }).Count -gt 0 -and
      $line.Top -gt $sectionLine.Bottom
    } | Sort-Object Top)
  $upperBound = if ($nextHeaders.Count -gt 0) {
    [double]$nextHeaders[0].Top
  } else {
    [double]::PositiveInfinity
  }
  $candidates = @($lines | Where-Object {
      (Test-OcrLineMatch -Line $_ -Expected $expected) -and
      $_.Top -gt $sectionLine.Bottom -and
      $_.Top -lt $upperBound
    })
  if ($candidates.Count -ne 1) {
    Write-Result -Value ([pscustomobject]@{
        status = 'ok'
        matched = $false
      })
    exit 0
  }

  $candidate = $candidates[0]
  Write-Result -Value ([pscustomobject]@{
      status = 'ok'
      matched = $true
      candidate = [pscustomobject]@{
        x = [math]::Round($candidate.Left + ($candidate.Width / 2), 1)
        y = [math]::Round($candidate.Top + ($candidate.Height / 2), 1)
      }
    })
  exit 0
} catch {
  Write-Result -Value ([pscustomobject]@{
      status = 'error'
      code = 'E_UIA_OCR_FAILED'
    })
  exit 20
}
