param(
    [ValidateSet("all", "current", "target")]
    [string]$Batch = "all",
    [int]$Pages = 10,
    [double]$PageWaitMin = 12,
    [double]$PageWaitMax = 22,
    [string]$Root = "",
    [string]$ScraperRoot = "",
    [string]$ChromeProfileDir = "",
    [string]$EdgeProfileDir = "",
    [string]$Python = "",
    [switch]$SetupBrowsers,
    [switch]$SetupOnly,
    [switch]$DryRun,
    [switch]$ContinueOnError,
    [switch]$VerboseUpstream,
    [switch]$QuietUpstream,
    [switch]$NoWaitOnGate
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runCollect = Join-Path $projectRoot "run_collect.ps1"

function Resolve-LocalPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    if ($expanded.StartsWith("~")) {
        $expanded = Join-Path $HOME $expanded.Substring(1).TrimStart("\", "/")
    }
    if ([IO.Path]::IsPathRooted($expanded)) {
        return [IO.Path]::GetFullPath($expanded)
    }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $expanded))
}

function Quote-ProcessArgument {
    param([string]$Value)

    if ($null -eq $Value -or $Value -eq "") {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    # Escape quotes and trailing backslashes for the Windows argv parser.
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

if (-not (Test-Path -LiteralPath $runCollect -PathType Leaf)) {
    throw "run_collect.ps1 was not found: $runCollect"
}
if ($Pages -lt 1 -or $Pages -gt 10) {
    throw "-Pages must be between 1 and 10"
}
if ([double]::IsNaN($PageWaitMin) -or [double]::IsInfinity($PageWaitMin) -or
    [double]::IsNaN($PageWaitMax) -or [double]::IsInfinity($PageWaitMax) -or
    $PageWaitMin -lt 0 -or $PageWaitMax -gt 300 -or $PageWaitMin -gt $PageWaitMax) {
    throw "Page waits must be finite seconds with 0 <= PageWaitMin <= PageWaitMax <= 300"
}
if ($DryRun -and $SetupOnly) {
    throw "-DryRun and -SetupOnly cannot be used together"
}
if ($SetupOnly) {
    $SetupBrowsers = $true
}
if ($VerboseUpstream -and $QuietUpstream) {
    throw "-VerboseUpstream and -QuietUpstream cannot be used together"
}

$Root = Resolve-LocalPath $Root
if (-not $Root) {
    $datePart = Get-Date -Format "yyyy-MM-dd"
    $Root = [IO.Path]::GetFullPath((Join-Path $projectRoot (Join-Path "job-result" "$datePart-architecture-screen-full")))
}
$ScraperRoot = Resolve-LocalPath $ScraperRoot
$ChromeProfileDir = Resolve-LocalPath $ChromeProfileDir
$EdgeProfileDir = Resolve-LocalPath $EdgeProfileDir

function New-CollectParameters {
    param(
        [ValidateSet("chrome", "edge")]
        [string]$BrowserName,
        [int]$Port,
        [int]$ShardNumber,
        [string]$ProfileDir
    )

    $parameters = @{
        Batch = $Batch
        Pages = $Pages
        PageWaitMin = $PageWaitMin.ToString([Globalization.CultureInfo]::InvariantCulture)
        PageWaitMax = $PageWaitMax.ToString([Globalization.CultureInfo]::InvariantCulture)
        Browser = $BrowserName
        CdpPort = $Port
        TotalShards = 2
        Shard = $ShardNumber
        Root = $Root
    }
    if ($ScraperRoot) { $parameters.ScraperRoot = $ScraperRoot }
    if ($Python) { $parameters.Python = $Python }
    if ($ProfileDir) { $parameters.ProfileDir = $ProfileDir }
    if ($SetupBrowsers) { $parameters.SetupBrowser = $true }
    if ($SetupOnly) { $parameters.SetupOnly = $true }
    if ($ContinueOnError) { $parameters.ContinueOnError = $true }
    if ($VerboseUpstream) { $parameters.VerboseUpstream = $true }
    if ($QuietUpstream) { $parameters.QuietUpstream = $true }
    if ($NoWaitOnGate) { $parameters.NoWaitOnGate = $true }
    return $parameters
}

function Convert-ParametersToArguments {
    param([hashtable]$Parameters)

    $arguments = @()
    foreach ($entry in $Parameters.GetEnumerator()) {
        $arguments += "-$($entry.Key)"
        if ($entry.Value -is [bool]) {
            if ($entry.Value) { continue }
            $arguments = $arguments[0..($arguments.Count - 2)]
        }
        else {
            $arguments += [string]$entry.Value
        }
    }
    return $arguments
}

$chromeParameters = New-CollectParameters -BrowserName "chrome" -Port 9222 -ShardNumber 0 -ProfileDir $ChromeProfileDir
$edgeParameters = New-CollectParameters -BrowserName "edge" -Port 9223 -ShardNumber 1 -ProfileDir $EdgeProfileDir
$chromeArgs = Convert-ParametersToArguments $chromeParameters
$edgeArgs = Convert-ParametersToArguments $edgeParameters

Write-Host "Dual collection root: $Root"
Write-Host "Chrome: port 9222, shard 0/2"
Write-Host "Edge: port 9223, shard 1/2"

if ($DryRun) {
    & $runCollect @chromeParameters -DryRun
    $chromeExitCode = $LASTEXITCODE
    if ($null -eq $chromeExitCode) { $chromeExitCode = 0 }
    & $runCollect @edgeParameters -DryRun
    $edgeExitCode = $LASTEXITCODE
    if ($null -eq $edgeExitCode) { $edgeExitCode = 0 }
    if ($chromeExitCode -ne 0 -or $edgeExitCode -ne 0) {
        throw "Dry-run failed (Chrome=$chromeExitCode, Edge=$edgeExitCode)"
    }
    Write-Host "Dry-run completed: both shards exited with code 0."
    exit 0
}

$shellCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
if ($null -eq $shellCommand) {
    $shellCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue
}
if ($null -eq $shellCommand) {
    throw "PowerShell executable was not found."
}
$shellPath = if ($shellCommand.Source) { $shellCommand.Source } else { $shellCommand.Definition }

function Start-CollectionWindow {
    param([string[]]$ChildArguments)

    $processArguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $runCollect) + $ChildArguments
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $shellPath
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.UseShellExecute = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Normal
    $startInfo.Arguments = (($processArguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " ")
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Could not start collection window."
    }
    Write-Host "Started collection window (PID $($process.Id))."
}

Start-CollectionWindow $chromeArgs
Start-CollectionWindow $edgeArgs
Write-Host "Both collection windows are open. They intentionally remain interactive."
exit 0
