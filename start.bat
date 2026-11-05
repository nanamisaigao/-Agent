@echo off
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
  echo   [错误] 没有找到 Python。
  echo.
  echo   请先安装 Python 3.10 或更高版本：
  echo     https://www.python.org/downloads/
  echo.
  echo   安装时务必勾选 "Add python.exe to PATH"，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

%PY% bootstrap.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" pause
exit /b %CODE%
