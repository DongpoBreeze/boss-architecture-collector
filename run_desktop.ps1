# Windows PowerShell 5.1 and PowerShell 7; no installation or configuration writes.
# Keep this file ASCII so Windows PowerShell can read it without a UTF-8 BOM.

$script:DesktopPythonProbe = @'
import json
import sys
if sys.version_info < (3, 10):
    raise SystemExit(10)
try:
    import tkinter
    tkinter.Tcl()
except Exception:
    raise SystemExit(11)
print(json.dumps({'executable': sys.executable}))
'@

# A console-free Python process still needs a visible, sanitized startup error.
# Paths/arguments travel as JSON in its private environment, not shell source.
$script:DesktopPythonBootstrap = @'
import ctypes
import json
import os
import runpy
import sys

failed = False
try:
    launch = json.loads(os.environ.pop('BOSS_ARCHITECTURE_DESKTOP_LAUNCH'))
    script = launch['script']
    sys.argv = [script, *launch['arguments']]
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name='__main__')
except SystemExit as exc:
    failed = exc.code not in (None, 0)
except BaseException:
    failed = True

if failed:
    ctypes.windll.user32.MessageBoxW(
        None,
        'The BOSS desktop workbench could not start or continue.\n\n'
        'Check that the project files are complete, Python has tkinter, '
        'and the selected paths are accessible.\n'
        'For local diagnostics, run: python desktop_app.py\n\n'
        'Exception details are hidden to protect credentials.',
        'BOSS desktop workbench - launch error',
        0x00000010 | 0x00040000,
    )
    raise SystemExit(1)
'@

function ConvertTo-DesktopNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # CommandLineToArgvW / Python quoting, including trailing backslashes.
    return '"' + [regex]::Replace(
        [regex]::Replace($Value, '(\\*)"', '$1$1\"'),
        '(\\+)$', '$1$1'
    ) + '"'
}

function Read-DesktopLaunchOptions {
    param([string[]]$Arguments = @())
    $values = @{ Root = ''; ScraperRoot = ''; Config = ''; Python = ''; Help = $false }
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        $parts = $Arguments[$index] -split '=', 2
        $name = $parts[0].ToLowerInvariant()
        if ($name -in @('-h', '--help', '-help', '-?')) {
            $values.Help = $true
            continue
        }
        $key = switch ($name) {
            { $_ -in @('-root', '--root') } { 'Root'; break }
            { $_ -in @('-scraperroot', '-scraper-root', '--scraper-root') } { 'ScraperRoot'; break }
            { $_ -in @('-config', '--config') } { 'Config'; break }
            { $_ -in @('-python', '--python') } { 'Python'; break }
            default { throw 'Unknown launcher option. Use --help for supported options.' }
        }
        if ($parts.Count -eq 2) {
            $value = $parts[1]
        }
        else {
            $index++
            if ($index -ge $Arguments.Count -or $Arguments[$index].StartsWith('-')) {
                throw 'A launcher path option is missing its value.'
            }
            $value = $Arguments[$index]
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw 'A launcher path option has an empty value.'
        }
        $values[$key] = $value
    }
    return [pscustomobject]$values
}

function Resolve-DesktopLaunchPath {
    param([string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    # PowerShell's current directory need not equal the .NET process directory.
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Value))
}

function Get-DesktopPythonCandidates {
    param([string]$ProjectRoot, [string]$Python = '')
    if ($Python) {
        $path = $null
        if (Test-Path -LiteralPath $Python -PathType Leaf) {
            $path = (Get-Item -LiteralPath $Python).FullName
        }
        else {
            $command = Get-Command -Name $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($command) { $path = $command.Source }
        }
        if ($path) { [pscustomobject]@{ Path = $path; Prefix = @() } }
        return
    }

    $local = @(
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
        (Join-Path $ProjectRoot 'venv\Scripts\python.exe')
    )
    if ($env:VIRTUAL_ENV) { $local += Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe' }
    if ($env:CONDA_PREFIX) { $local += Join-Path $env:CONDA_PREFIX 'python.exe' }
    foreach ($path in ($local | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            [pscustomobject]@{ Path = $path; Prefix = @() }
        }
    }
    foreach ($name in @('py.exe', 'python.exe')) {
        $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        # Do not open Microsoft Store through its unconfigured python alias.
        if ($command -and $command.Source -notmatch '\\Microsoft\\WindowsApps\\python[^\\]*\.exe$') {
            $prefix = @()
            if ($name -eq 'py.exe') { $prefix = @('-3') }
            [pscustomobject]@{ Path = $command.Source; Prefix = $prefix }
        }
    }
}

function Test-DesktopPython {
    param($Candidate)
    $process = $null
    try {
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $Candidate.Path
        $arguments = @($Candidate.Prefix) + @('-B', '-c', $script:DesktopPythonProbe)
        $start.Arguments = ($arguments | ForEach-Object { ConvertTo-DesktopNativeArgument $_ }) -join ' '
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $start
        if (-not $process.Start()) { return $null }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            [void]$process.WaitForExit(1000)
            return $null
        }
        if ($process.ExitCode -ne 0) { return $null }
        $result = $stdout.GetAwaiter().GetResult() | ConvertFrom-Json -ErrorAction Stop
        if ($result.executable -is [string] -and (Test-Path -LiteralPath $result.executable -PathType Leaf)) {
            return $result.executable
        }
    }
    catch {
        # Never echo interpreter stderr, which may contain paths or credentials.
        return $null
    }
    finally {
        if ($process) { $process.Dispose() }
    }
    return $null
}

function Find-DesktopPython {
    param([string]$ProjectRoot, [string]$Python = '')
    foreach ($candidate in @(Get-DesktopPythonCandidates -ProjectRoot $ProjectRoot -Python $Python)) {
        $executable = Test-DesktopPython $candidate
        if ($executable) { return $executable }
    }
    return $null
}

function Start-DesktopWindow {
    param([string]$Python, [string]$ProjectRoot, [string[]]$Arguments)
    $payload = @{
        script = Join-Path $ProjectRoot 'desktop_app.py'
        arguments = @($Arguments)
    } | ConvertTo-Json -Compress
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script:DesktopPythonBootstrap))
    $bootstrap = "exec(__import__('base64').b64decode('$encoded'))"
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Python
    $start.Arguments = (@('-B', '-c', $bootstrap) | ForEach-Object { ConvertTo-DesktopNativeArgument $_ }) -join ' '
    $start.WorkingDirectory = $ProjectRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.EnvironmentVariables['BOSS_ARCHITECTURE_DESKTOP_LAUNCH'] = $payload
    $start.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'The desktop process did not start.' }
    }
    finally {
        $process.Dispose()
    }
}

function Show-DesktopLaunchError {
    param([string]$Message)
    [Console]::Error.WriteLine('BOSS desktop launcher: ' + $Message)
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [void][System.Windows.Forms.MessageBox]::Show(
            $Message, 'BOSS desktop workbench - launch error',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
    }
    catch {
        # The .bat also pauses on failure if a system dialog is unavailable.
    }
}

function Invoke-DesktopLauncher {
    param([string]$ProjectRoot, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Stop'
    try {
        $options = Read-DesktopLaunchOptions -Arguments $Arguments
    }
    catch {
        Show-DesktopLaunchError 'Invalid launch options. Use --root, --scraper-root, --config and an optional --python, each followed by a path. Use --help for examples.'
        return 1
    }
    if ($options.Help) {
        Write-Host @'
BOSS desktop workbench (Python 3.10+ with tkinter)
  .\run_desktop.bat [--root PATH] [--scraper-root PATH] [--config PATH]
  .\run_desktop.ps1 [-Root PATH] [-ScraperRoot PATH] [-Config PATH] [-Python EXE]

Quote paths containing spaces. Relative paths use your current directory.
Opening the workbench does not start collection or model requests.
The launcher does not install packages or change global configuration.
'@
        return 0
    }
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'desktop_app.py') -PathType Leaf)) {
            Show-DesktopLaunchError 'desktop_app.py is missing. Keep run_desktop.bat, run_desktop.ps1 and all project Python files in the same project directory.'
            return 1
        }
        $python = Find-DesktopPython -ProjectRoot $ProjectRoot -Python $options.Python
        if (-not $python) {
            Show-DesktopLaunchError 'No usable Python 3.10+ with tkinter was found. Install Python with Tcl/Tk support, or pass -Python followed by its python.exe path. Nothing was installed or changed.'
            return 1
        }
        $forward = @()
        foreach ($pair in @(@('Root', '--root'), @('ScraperRoot', '--scraper-root'), @('Config', '--config'))) {
            $value = $options.($pair[0])
            if ($value) { $forward += @($pair[1], (Resolve-DesktopLaunchPath $value)) }
        }
        Start-DesktopWindow -Python $python -ProjectRoot $ProjectRoot -Arguments $forward
        return 0
    }
    catch {
        Show-DesktopLaunchError 'The desktop process could not be started. Check file permissions and the selected Python/path options. Exception details are hidden to protect credentials.'
        return 1
    }
}

# Dot-sourcing exposes only the functions for non-interactive unit tests.
if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-DesktopLauncher -ProjectRoot $PSScriptRoot -Arguments $args)
}
