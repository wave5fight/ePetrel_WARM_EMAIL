@echo off
setlocal

cd /d "%~dp0"

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
set "APP_URL=http://%APP_HOST%:%APP_PORT%"
set "PYTHON_EXE=%CD%\python_env\python.exe"

title MutualWarm

echo.
echo ==========================================
echo   MutualWarm - Windows
echo ==========================================
echo.

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Missing Windows embedded Python:
  echo         %PYTHON_EXE%
  echo.
  echo Please make sure the python_env folder is included next to start.bat.
  echo.
  pause
  exit /b 1
)

if not exist "web_app.py" (
  echo [ERROR] web_app.py was not found.
  echo Please run this script from the project folder.
  echo.
  pause
  exit /b 1
)

if not exist "templates" (
  echo [ERROR] templates folder was not found.
  echo.
  pause
  exit /b 1
)

if not exist "static" (
  echo [ERROR] static folder was not found.
  echo.
  pause
  exit /b 1
)

if not exist "static\app.css" (
  echo [ERROR] Missing UI stylesheet: static\app.css
  echo Please use a complete release package.
  echo.
  pause
  exit /b 1
)

if not exist "static\tailwind-local.css" (
  echo [ERROR] Missing offline UI stylesheet: static\tailwind-local.css
  echo Please rebuild or download the complete release package.
  echo.
  pause
  exit /b 1
)

for /r "%CD%\python_env" %%F in (*.so *.dylib) do (
  set "BAD_RUNTIME_FILE=%%F"
  goto bad_python_runtime
)

echo Checking bundled Python runtime...
"%PYTHON_EXE%" -c "import fastapi, uvicorn, pandas, numpy, openpyxl, cryptography" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] The bundled Python runtime or dependencies are invalid.
  echo.
  echo Details:
  "%PYTHON_EXE%" -c "import fastapi, uvicorn, pandas, numpy, openpyxl, cryptography"
  echo.
  echo Please rebuild the release package with a clean Windows python_env.
  echo Do not install dependencies into python_env from macOS or Linux.
  echo.
  pause
  exit /b 1
)
echo Python runtime check passed.
echo.

if not exist "database" mkdir database
if not exist "logs" mkdir logs

echo Starting local server at %APP_URL%
echo Keep this window open while using ePetrel.
echo.

start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"

"%PYTHON_EXE%" -m uvicorn web_app:app --host %APP_HOST% --port %APP_PORT%

echo.
echo ePetrel has stopped. If this was unexpected, check the error message above.
pause
exit /b 0

:bad_python_runtime
echo [ERROR] The bundled Windows Python runtime is invalid.
echo.
echo Found a non-Windows Python dependency file:
echo         %BAD_RUNTIME_FILE%
echo.
echo This usually means python_env was packaged after dependencies were installed on macOS or Linux.
echo Please rebuild python_env on a real Windows machine and create the release package again.
echo.
pause
exit /b 1
