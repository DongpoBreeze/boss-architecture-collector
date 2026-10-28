@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0run_desktop.ps1" %*
if errorlevel 1 (
    echo.
    echo The desktop workbench could not start. Check Python 3.10+ and tkinter.
    echo See README.md or use run_desktop.ps1 -Python with a Python executable.
    pause
    exit /b 1
)
exit /b 0
