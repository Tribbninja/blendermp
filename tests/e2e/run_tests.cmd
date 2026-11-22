@echo off
setlocal
title Blendermp E2E
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all_fast.ps1" %*
echo.
echo Press any key to close this window.
pause >nul
