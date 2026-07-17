[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AkashaBot-WeFlow-Bridge'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'Start-Services.ps1') -InstallRoot $InstallRoot

if ($null -eq ('AkashaBotNativeProcessLeaseV2' -as [type])) {
  Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public sealed class AkashaBotNativeProcessLeaseV2 : IDisposable {
    private const uint PROCESS_TERMINATE = 0x0001;
    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    private const uint SYNCHRONIZE = 0x00100000;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private IntPtr handle;

    [StructLayout(LayoutKind.Sequential)]
    private struct UNICODE_STRING {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME {
        public uint Low;
        public uint High;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(IntPtr processHandle, uint flags, StringBuilder path, ref int pathLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(
        IntPtr processHandle, out FILETIME creationTime, out FILETIME exitTime,
        out FILETIME kernelTime, out FILETIME userTime
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr processHandle, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        IntPtr processHandle,
        int processInformationClass,
        IntPtr processInformation,
        int processInformationLength,
        out int returnLength
    );

    private AkashaBotNativeProcessLeaseV2(IntPtr processHandle) {
        handle = processHandle;
    }

    public static AkashaBotNativeProcessLeaseV2 TryOpen(int processId) {
        IntPtr processHandle = OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, processId);
        if (processHandle == IntPtr.Zero) return null;
        return new AkashaBotNativeProcessLeaseV2(processHandle);
    }

    public string ExecutablePath {
        get {
            StringBuilder path = new StringBuilder(32768);
            int length = path.Capacity;
            if (!QueryFullProcessImageName(handle, 0, path, ref length)) return null;
            return path.ToString();
        }
    }

    public DateTime StartTimeUtc {
        get {
            FILETIME creation;
            FILETIME exit;
            FILETIME kernel;
            FILETIME user;
            if (!GetProcessTimes(handle, out creation, out exit, out kernel, out user)) return DateTime.MinValue;
            long ticks = ((long)creation.High << 32) | creation.Low;
            return DateTime.FromFileTimeUtc(ticks);
        }
    }

    public string CommandLine {
        get {
            if (handle == IntPtr.Zero) return null;
            int length;
            NtQueryInformationProcess(handle, 60, IntPtr.Zero, 0, out length);
            if (length <= 0 || length > 1048576) return null;
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try {
                int status = NtQueryInformationProcess(handle, 60, buffer, length, out length);
                if (status < 0) return null;
                UNICODE_STRING value = (UNICODE_STRING)Marshal.PtrToStructure(buffer, typeof(UNICODE_STRING));
                if (value.Buffer == IntPtr.Zero || value.Length == 0) return String.Empty;
                return Marshal.PtrToStringUni(value.Buffer, value.Length / 2);
            } finally {
                Marshal.FreeHGlobal(buffer);
            }
        }
    }

    public bool TerminateAndWait(int timeoutMilliseconds) {
        if (handle == IntPtr.Zero) return false;
        if (WaitForSingleObject(handle, 0) == WAIT_OBJECT_0) return true;
        if (!TerminateProcess(handle, 1)) return WaitForSingleObject(handle, 0) == WAIT_OBJECT_0;
        uint waitResult = WaitForSingleObject(handle, (uint)timeoutMilliseconds);
        return waitResult == WAIT_OBJECT_0;
    }

    public void Dispose() {
        if (handle != IntPtr.Zero) {
            CloseHandle(handle);
            handle = IntPtr.Zero;
        }
        GC.SuppressFinalize(this);
    }

    ~AkashaBotNativeProcessLeaseV2() {
        try {
            if (handle != IntPtr.Zero) CloseHandle(handle);
        } catch {
        }
    }
}
'@
}

function Get-AkashaStopProcessIdentity {
  param([Parameter(Mandatory)][int]$ProcessId)

  $lease = [AkashaBotNativeProcessLeaseV2]::TryOpen($ProcessId)
  if ($null -eq $lease) { return $null }
  try {
    $path = [System.IO.Path]::GetFullPath([string]$lease.ExecutablePath)
    $startTime = $lease.StartTimeUtc
    $commandLine = $lease.CommandLine
  } catch {
    $lease.Dispose()
    return $null
  }
  return [pscustomobject]@{
    Lease = $lease
    ExecutablePath = $path
    StartTimeUtc = $startTime
    CommandLine = $commandLine
  }
}

function Test-AkashaStopIdentity {
  param([Parameter(Mandatory)]$Record, [Parameter(Mandatory)]$Identity)

  try {
    $recordPath = [System.IO.Path]::GetFullPath([string]$Record.ExecutablePath)
    $recordStart = [datetime]::Parse([string]$Record.StartTimeUtc, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
  } catch { return $false }
  if (-not [string]$Identity.ExecutablePath -or
      -not ([string]$Identity.ExecutablePath).Equals($recordPath, [System.StringComparison]::OrdinalIgnoreCase) -or
      $Identity.StartTimeUtc.Ticks -ne $recordStart.Ticks) {
    return $false
  }
  if ([string]$Record.CommandKind -ceq 'WeFlowApp') { return $true }
  if ($null -eq $Identity.CommandLine) { return $false }
  return Test-AkashaCommandIdentity -CommandKind ([string]$Record.CommandKind) -CommandLine ([string]$Identity.CommandLine)
}

function Get-AkashaStopPreflight {
  param([Parameter(Mandatory)]$Paths)

  Assert-AkashaLifecyclePathBoundary -Paths $Paths -Candidates @($Paths.State, $Paths.Logs, $Paths.ProcessState, $Paths.BridgePython, $Paths.AstrBotPython)
}

function Get-AkashaStopWeFlowDiscovery {
  param([Parameter(Mandatory)]$Paths)

  if (-not (Test-AkashaLifecycleInternalPath -Root $Paths.Root -Candidate $Paths.WeFlowPathState) -or
      -not (Test-Path -LiteralPath $Paths.WeFlowPathState -PathType Leaf)) {
    return $null
  }
  try {
    $candidate = (Get-Content -LiteralPath $Paths.WeFlowPathState -Raw -Encoding UTF8 -ErrorAction Stop).Trim()
    $candidate = [System.IO.Path]::GetFullPath($candidate)
  } catch {
    return $null
  }
  if (-not (Test-AkashaCanonicalExternalExecutable -Path $candidate)) { return $null }
  return $candidate
}

function Stop-AkashaServices {
  param([Parameter(Mandatory)][string]$InstallRoot)

  $rootContext = Open-AkashaLifecycleRootContext -Root $InstallRoot -CreateIfMissing
  try {
  $paths = Get-AkashaBotPaths -Root $InstallRoot
  Get-AkashaStopPreflight -Paths $paths
  $lockPath = Join-Path $paths.State 'lifecycle.lock'
  if (-not (Test-AkashaLifecycleInternalPath -Root $paths.Root -Candidate $lockPath)) {
    throw 'E_LIFECYCLE_PATH: Product paths must remain inside the install root without reparse points.'
  }
  try {
    foreach ($directory in @($paths.State, $paths.Logs)) {
      Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$directory)
      New-Item -ItemType Directory -Force -Path $directory | Out-Null
      Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$directory)
    }
  } catch {
    throw 'E_LIFECYCLE_PATH: Unable to create lifecycle directories.'
  }
  $lockStream = $null
  try {
    Assert-AkashaLifecyclePathBoundary -Paths $paths -Candidates @([string]$paths.State, [string]$lockPath)
    try {
      $lockStream = New-Object System.IO.FileStream(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None,
        1,
        [System.IO.FileOptions]::DeleteOnClose
      )
    } catch {
      throw 'E_LIFECYCLE_BUSY: Another lifecycle operation is already running.'
    }
    $lockFinalPath = [AkashaBotNativePathV1]::GetFinalPathFromHandle($lockStream.SafeFileHandle.DangerousGetHandle())
    if ([string]::IsNullOrWhiteSpace($lockFinalPath) -or
        -not ([System.IO.Path]::GetFullPath($lockFinalPath)).Equals([System.IO.Path]::GetFullPath($lockPath), [System.StringComparison]::OrdinalIgnoreCase)) {
      throw 'E_LIFECYCLE_PATH: Lifecycle lock resolved outside its expected path.'
    }
    Get-AkashaStopPreflight -Paths $paths
    Write-AkashaLifecycleLog -Paths $paths -Level 'info' -Message 'Lifecycle stop requested.'
    $records = @(Read-AkashaProcessState -Path $paths.ProcessState -Paths $paths)
    if ($records.Count -eq 0) {
      Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records @()
      return
    }

    Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $records
    $remaining = @($records)
    $events = New-Object System.Collections.Generic.List[string]
    $identityRefused = $false
    $weFlowDiscoveryRefused = $false
    $orderedRecords = @($records | Sort-Object @{ Expression = {
          switch ([string]$_.Name) {
            'bridge' { 1 }
            'astrbot' { 2 }
            'weflow' { 3 }
            default { 99 }
          }
    } })
    foreach ($record in $orderedRecords) {
      if (-not [bool]$record.Owned) {
        $remaining = @($remaining | Where-Object { [int]$_.Pid -ne [int]$record.Pid })
        Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $remaining
        $events.Add("skip-unowned name=$($record.Name)")
        continue
      }
      if ([string]$record.Name -ceq 'weflow') {
        $discoveredWeFlow = Get-AkashaStopWeFlowDiscovery -Paths $paths
        $recordedWeFlow = [System.IO.Path]::GetFullPath([string]$record.ExecutablePath)
        if ([string]::IsNullOrWhiteSpace($discoveredWeFlow) -or
            -not $recordedWeFlow.Equals($discoveredWeFlow, [System.StringComparison]::OrdinalIgnoreCase)) {
          $weFlowDiscoveryRefused = $true
          $events.Add('refused-discovery name=weflow')
          continue
        }
      }
      $identity = $null
      try {
        $identity = Get-AkashaStopProcessIdentity -ProcessId ([int]$record.Pid)
        if ($null -eq $identity) {
          $remaining = @($remaining | Where-Object { [int]$_.Pid -ne [int]$record.Pid })
          Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $remaining
          $events.Add("stale name=$($record.Name)")
          continue
        }
        if (-not (Test-AkashaStopIdentity -Record $record -Identity $identity)) {
          $identityRefused = $true
          $events.Add("refused name=$($record.Name)")
          continue
        }
        try {
          Get-AkashaStopPreflight -Paths $paths
          if (-not $identity.Lease.TerminateAndWait(5000)) { throw 'timeout' }
        } catch {
          $identityRefused = $true
          $events.Add("refused name=$($record.Name)")
          continue
        }
        $remaining = @($remaining | Where-Object { [int]$_.Pid -ne [int]$record.Pid })
        Write-AkashaProcessState -Path $paths.ProcessState -Paths $paths -Records $remaining
        $events.Add("stop name=$($record.Name)")
      } finally {
        if ($null -ne $identity -and $null -ne $identity.Lease) { $identity.Lease.Dispose() }
      }
    }

    $logError = $null
    foreach ($message in $events) {
      try { Write-AkashaLifecycleLog -Paths $paths -Level 'info' -Message $message } catch { $logError = $_; break }
    }
    if ($weFlowDiscoveryRefused) {
      $weFlowError = New-Object System.InvalidOperationException('E_WEFLOW_EXE: Refused to stop owned WeFlow because its recorded discovery path is unavailable or changed.')
      if ($identityRefused) { $weFlowError.Data['AkashaIdentityFailure'] = 'E_PROCESS_IDENTITY' }
      if ($null -ne $logError) { $weFlowError.Data['AkashaLogFailure'] = 'E_LIFECYCLE_LOG' }
      throw $weFlowError
    }
    if ($identityRefused) {
      $identityError = New-Object System.InvalidOperationException('E_PROCESS_IDENTITY: Refused to stop one or more processes whose live identity did not match product state.')
      if ($null -ne $logError) { $identityError.Data['AkashaLogFailure'] = 'E_LIFECYCLE_LOG' }
      throw $identityError
    }
    if ($null -ne $logError) { throw 'E_LIFECYCLE_LOG: Unable to write lifecycle log.' }
  } finally {
    if ($null -ne $lockStream) { $lockStream.Dispose() }
  }
  } finally {
    Close-AkashaLifecycleRootContext -Context $rootContext
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  Stop-AkashaServices -InstallRoot $InstallRoot
}
