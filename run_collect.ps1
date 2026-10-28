param(
    [ValidateSet("all", "current", "target")]
    [string]$Batch = "all",
    [int]$Pages = 10,
    [ValidateScript({ $_ -eq "" -or $_ -eq "chrome" -or $_ -eq "edge" })]
    [string]$Browser = "",
    [int]$CdpPort = 0,
    [int]$TotalShards = 1,
    [int]$Shard = 0,
    [string]$Root = "",
    [string]$ScraperRoot = "",
    [string]$ProfileDir = "",
    [string]$Python = "",
    [switch]$SetupBrowser,
    [switch]$SetupChrome,
    [switch]$SetupEdge,
    [switch]$SetupOnly,
    [switch]$DryRun,
    [switch]$ContinueOnError,
    [switch]$VerboseUpstream,
    [switch]$QuietUpstream,
    [switch]$NoWaitOnGate
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Join-Path $projectRoot "collect_architecture.py"
$config = Join-Path $projectRoot "config\architecture_collection.json"

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

if (-not (Test-Path -LiteralPath $collector -PathType Leaf)) {
    throw "collect_architecture.py was not found: $collector"
}
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "architecture_collection.json was not found: $config"
}
if ($Pages -lt 1 -or $Pages -gt 10) {
    throw "-Pages must be between 1 and 10"
}
if ($TotalShards -lt 1) {
    throw "-TotalShards must be at least 1"
}
if ($Shard -lt 0 -or $Shard -ge $TotalShards) {
    throw "-Shard must be between 0 and $($TotalShards - 1)"
}
if ($CdpPort -ne 0 -and ($CdpPort -lt 1024 -or $CdpPort -gt 65535)) {
    throw "-CdpPort must be 0 (automatic) or between 1024 and 65535"
}
if ($VerboseUpstream -and $QuietUpstream) {
    throw "-VerboseUpstream and -QuietUpstream cannot be used together"
}
if ($SetupBrowser -and $SetupChrome) {
    throw "-SetupBrowser and -SetupChrome are aliases; use only one"
}
if ($SetupBrowser -and $SetupEdge) {
    throw "-SetupBrowser and -SetupEdge cannot be used together"
}
if ($SetupChrome -and $SetupEdge) {
    throw "-SetupChrome and -SetupEdge cannot be used together"
}
if ($DryRun -and $SetupOnly) {
    throw "-DryRun and -SetupOnly cannot be used together"
}

$browserWasSpecified = -not [string]::IsNullOrWhiteSpace($Browser)
if (-not $browserWasSpecified) {
    $Browser = "chrome"
}
if ($SetupEdge) {
    if ($browserWasSpecified -and $Browser -ne "edge") {
        throw "-SetupEdge conflicts with -Browser $Browser"
    }
    $Browser = "edge"
}
if ($SetupChrome -and $browserWasSpecified -and $Browser -ne "chrome") {
    throw "-SetupChrome conflicts with -Browser $Browser"
}

if ($CdpPort -eq 0) {
    $CdpPort = if ($Browser -eq "edge") { 9223 } else { 9222 }
}

$setupRequested = $SetupBrowser -or $SetupChrome -or $SetupEdge -or $SetupOnly
$Root = Resolve-LocalPath $Root
$ScraperRoot = Resolve-LocalPath $ScraperRoot
$ProfileDir = Resolve-LocalPath $ProfileDir

$pythonCommand = $null
$pythonArgs = @()
if ($Python) {
    $pythonCommand = $Python
}
elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pyCommand = Get-Command py
    $pythonCommand = if ($pyCommand.Source) { $pyCommand.Source } else { $pyCommand.Definition }
    $pythonArgs = @("-3")
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommandInfo = Get-Command python
    $pythonCommand = if ($pythonCommandInfo.Source) { $pythonCommandInfo.Source } else { $pythonCommandInfo.Definition }
}
else {
    throw "Python 3.10+ was not found. Install Python or pass -Python."
}

$arguments = @()
if ($pythonArgs.Count -gt 0) { $arguments += $pythonArgs }
$arguments += $collector
$arguments += "--config"
$arguments += $config
$arguments += "--batch"
$arguments += $Batch
$arguments += "--pages"
$arguments += [string]$Pages
$arguments += "--browser"
$arguments += $Browser
if ($CdpPort -ne 0) {
    $arguments += "--cdp-port"
    $arguments += [string]$CdpPort
}
$arguments += "--total-shards"
$arguments += [string]$TotalShards
$arguments += "--shard"
$arguments += [string]$Shard
if ($Root) { $arguments += @("--root", $Root) }
if ($ScraperRoot) { $arguments += @("--scraper-root", $ScraperRoot) }
if ($ProfileDir) { $arguments += @("--profile-dir", $ProfileDir) }
if ($setupRequested) { $arguments += "--setup-browser" }
if ($SetupOnly) { $arguments += "--setup-only" }
if ($DryRun) { $arguments += "--dry-run" }
if ($ContinueOnError) { $arguments += "--continue-on-error" }
if ($NoWaitOnGate) { $arguments += "--no-wait-on-gate" }
if ($VerboseUpstream) { $arguments += "--verbose-upstream" }
if ($QuietUpstream) { $arguments += "--quiet-upstream" }

Write-Host "Project: $projectRoot"
Write-Host "Batch: $Batch | Pages: $Pages | Browser: $Browser | Shard: $Shard/$TotalShards"
Write-Host "CDP port: $CdpPort"
if ($Root) { Write-Host "Root: $Root" }
if ($setupRequested) { Write-Host "Browser setup requested: $Browser" }

& $pythonCommand @arguments
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) { $exitCode = 0 }
exit $exitCode
