@echo off
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_collect.ps1" %*
exit /b %ERRORLEVEL%
