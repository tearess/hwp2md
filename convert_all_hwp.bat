@echo off
setlocal
set OUTDIR=%~1
if "%OUTDIR%"=="" set OUTDIR=converted

set PYTHON_CMD=
if exist "%~dp0.venv\Scripts\python.exe" (
  set PYTHON_CMD=%~dp0.venv\Scripts\python.exe
) else (
  where py >nul 2>nul
  if %ERRORLEVEL%==0 set PYTHON_CMD=py
)
if "%PYTHON_CMD%"=="" (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 set PYTHON_CMD=python
)
if "%PYTHON_CMD%"=="" (
  echo Python was not found. Please install Python 3.10+.
  exit /b 2
)

"%PYTHON_CMD%" "%~dp0hwp2md.py" --batch "%~dp0*.hwp" -o "%~dp0%OUTDIR%"
set HWP_CODE=%ERRORLEVEL%
"%PYTHON_CMD%" "%~dp0hwp2md.py" --batch "%~dp0*.hwpx" -o "%~dp0%OUTDIR%"
set HWPX_CODE=%ERRORLEVEL%

if %HWP_CODE% NEQ 0 if %HWPX_CODE% NEQ 0 exit /b 1
exit /b 0
