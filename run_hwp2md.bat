@echo off
setlocal

if "%~1"=="" (
  echo Usage: run_hwp2md.bat input.hwp [output.md]
  echo Example: run_hwp2md.bat sample.hwp sample.md
  echo Batch example: .venv\Scripts\python.exe hwp2md.py --batch "*.hwp" -o converted
  exit /b 1
)

set INPUT=%~1
set OUTPUT=%~2
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
  echo Python was not found. Please install Python 3.10+ and run:
  echo python -m pip install -r requirements.txt
  exit /b 2
)

if "%OUTPUT%"=="" (
  "%PYTHON_CMD%" "%~dp0hwp2md.py" "%INPUT%"
) else (
  "%PYTHON_CMD%" "%~dp0hwp2md.py" "%INPUT%" -o "%OUTPUT%"
)
exit /b %ERRORLEVEL%
