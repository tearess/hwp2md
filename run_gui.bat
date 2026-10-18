@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON_CMD="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
) else (
  where py >nul 2>nul
  if %ERRORLEVEL%==0 set "PYTHON_CMD=py"
)

if "%PYTHON_CMD%"=="" (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 set "PYTHON_CMD=python"
)

if "%PYTHON_CMD%"=="" (
  echo Python was not found. Please install Python 3.10+ and run:
  echo python -m pip install -r requirements.txt
  pause
  exit /b 2
)

"%PYTHON_CMD%" "%~dp0hwp2md_gui.py"

if errorlevel 1 (
  echo.
  echo hwp2md GUI could not start.
  pause
)
