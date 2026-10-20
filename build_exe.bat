@echo off
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHON_CMD="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%~dp0.venv\Scripts\python.exe"
) else (
  where py >nul 2>nul
  if %ERRORLEVEL%==0 set "PYTHON_CMD=py"
)

if "%PYTHON_CMD%"=="" (
  echo Python was not found. Please install Python 3.10+ first.
  pause
  exit /b 2
)

"%PYTHON_CMD%" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed

rem Single-file windowed app: dist\hwp2md.exe runs without Python.
"%PYTHON_CMD%" -m PyInstaller --noconfirm --clean --onefile --windowed --name hwp2md hwp2md_gui.py
if errorlevel 1 goto failed

echo.
echo Done: dist\hwp2md.exe
exit /b 0

:failed
echo.
echo Build failed.
pause
exit /b 1
