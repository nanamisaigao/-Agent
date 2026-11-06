@echo off
rem Keep this file ASCII-only. cmd.exe reads batch files using the console
rem code page; a UTF-8 batch containing Chinese breaks on some machines.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo.
  echo   Install Python 3.10 or newer first:
  echo     https://www.python.org/downloads/
  echo.
  echo   During setup tick "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

%PY% bootstrap.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
