@echo off
rem One command on Windows: .\start.cmd [-Check] [-NoOpen]. Runs start.ps1 without changing the ExecutionPolicy.
rem After Ctrl+C, cmd asks "Terminate batch job (Y/N)?": either answer is fine.
setlocal
cd /d "%~dp0"
set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"
%PS% -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"
rem Started with /c (double-click, or from PowerShell): pause on failure so the window does not close on the error.
rem -1073741510 is 0xC000013A, Ctrl+C: not an error. Delayed expansion only here: %* above may contain "!".
setlocal EnableDelayedExpansion
if not "%RC%"=="0" if not "%RC%"=="-1073741510" if /i not "!CMDCMDLINE:/c=!"=="!CMDCMDLINE!" (echo. & pause)
exit /b %RC%
