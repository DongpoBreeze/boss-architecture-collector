param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [ValidateSet("merge", "shortlist", "fetch-jd", "llm", "report", "all")]
    [string]$Stage = "all",
    [string]$City = "",
    [ValidateRange(1, 1000000)]
    [int]$SampleSize = 0,
    [ValidateRange(1, 32)]
    [int]$Workers = 10,
    [ValidateRange(1, 100)]
    [int]$ConversationSize = 1,
    [ValidateRange(1, 32)]
    [int]$JdWorkers = 3,
    [ValidateRange(1, 100)]
    [int]$BatchSize = 10,
    [string]$ScraperRoot = "",
    [string]$Python = "",
    [ValidateRange(1, 3600)]
    [int]$Timeout = 120,
    [ValidateRange(0, 10)]
    [int]$MaxRetries = 3,
    [ValidateSet("safe", "fast")]
    [string]$DetailSpeedProfile = "fast",
    [string]$CodexHome = "",
    [string]$BaseUrl = "",
    [string]$ApiKey = "",
    [ValidateSet("", "responses", "chat_completions")]
    [string]$ApiMode = "",
    [string]$Model = "",
    [string]$ReasoningEffort = "",
    [switch]$NoWaitOnGate
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $projectRoot "process_architecture.py"

if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    throw "process_architecture.py was not found: $script"
}

if ($Python) {
    $pythonCommand = $Python
    $pythonArgs = @()
}
elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = (Get-Command py).Source
    $pythonArgs = @("-3")
}
elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = (Get-Command python).Source
    $pythonArgs = @()
}
else {
    throw "Python 3 was not found. Install Python or pass -Python."
}

$arguments = @()
$arguments += $pythonArgs
$arguments += $script
$arguments += @("--root", $Root, "--stage", $Stage, "--workers", [string]$Workers, "--conversation-size", [string]$ConversationSize, "--jd-workers", [string]$JdWorkers, "--batch-size", [string]$BatchSize, "--detail-speed-profile", $DetailSpeedProfile, "--timeout", [string]$Timeout, "--max-retries", [string]$MaxRetries)
if ($NoWaitOnGate) { $arguments += "--no-wait-on-gate" }
if ($City) { $arguments += @("--city", $City) }
if ($SampleSize -gt 0) { $arguments += @("--sample-size", [string]$SampleSize) }
if ($ScraperRoot) { $arguments += @("--scraper-root", $ScraperRoot) }
if ($CodexHome) { $arguments += @("--codex-home", $CodexHome) }
if ($BaseUrl) { $arguments += @("--base-url", $BaseUrl) }
if ($ApiKey) { $arguments += @("--api-key", $ApiKey) }
if ($ApiMode) { $arguments += @("--api-mode", $ApiMode) }
if ($Model) { $arguments += @("--model", $Model) }
if ($ReasoningEffort) { $arguments += @("--reasoning-effort", $ReasoningEffort) }

Write-Host "Project: $projectRoot"
Write-Host "Stage: $Stage | Root: $Root | City: $City | Sample: $SampleSize | AI workers: $Workers | AI jobs/session: $ConversationSize | JD pages: $JdWorkers | Scrolls/page: 3 | JD speed: $DetailSpeedProfile"
& $pythonCommand @arguments
exit $LASTEXITCODE
