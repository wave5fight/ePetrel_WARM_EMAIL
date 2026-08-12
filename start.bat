@echo off
setlocal

cd /d "%~dp0"

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
set "APP_URL=http://%APP_HOST%:%APP_PORT%"
set "BUNDLED_PYTHON=%CD%\python_env\python.exe"
set "VENV_DIR=%CD%\mutualwarmenv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "DEPS_MARKER=%VENV_DIR%\.mutualwarm_deps_ready"

title MutualWarm

echo.
echo ==========================================
echo   MutualWarm - Windows
echo ==========================================
echo.

if not exist "web_app.py" (
  echo [ERROR] web_app.py was not found.
  echo Please run this script from the MutualWarm project folder.
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

if exist "%BUNDLED_PYTHON%" (
  set "APP_PYTHON=%BUNDLED_PYTHON%"
  goto check_runtime
)

if exist "%VENV_PYTHON%" (
  set "APP_PYTHON=%VENV_PYTHON%"
  goto install_deps
)

echo No bundled python_env found. Creating local Python environment...
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv "%VENV_DIR%"
  if errorlevel 1 goto python_error
  set "APP_PYTHON=%VENV_PYTHON%"
  goto install_deps
)

where python >nul 2>nul
if not errorlevel 1 (
  python -m venv "%VENV_DIR%"
  if errorlevel 1 goto python_error
  set "APP_PYTHON=%VENV_PYTHON%"
  goto install_deps
)

echo [ERROR] Python 3 was not found.
echo Please install Python 3.10 or newer from https://www.python.org/downloads/.
echo.
pause
exit /b 1

:install_deps
if not exist "%DEPS_MARKER%" (
  echo Installing Python dependencies. This needs internet on first launch...
  "%APP_PYTHON%" -m pip install --upgrade pip
  if errorlevel 1 goto deps_error
  "%APP_PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 goto deps_error
  echo ready>"%DEPS_MARKER%"
)

:check_runtime
if exist "%CD%\python_env" (
  for /r "%CD%\python_env" %%F in (*.so *.dylib) do (
    set "BAD_RUNTIME_FILE=%%F"
    goto bad_python_runtime
  )
)

echo Checking Python runtime...
"%APP_PYTHON%" -c "import fastapi, uvicorn, cryptography, requests, google_auth_oauthlib, openai" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python runtime or dependencies are invalid.
  echo.
  echo Details:
  "%APP_PYTHON%" -c "import fastapi, uvicorn, cryptography, requests, google_auth_oauthlib, openai"
  echo.
  echo If this is a source checkout, delete mutualwarmenv and run start.bat again.
  echo If this is a release package, rebuild python_env on Windows.
  echo.
  pause
  exit /b 1
)
echo Python runtime check passed.
echo.

if not exist "database" mkdir database
if not exist "logs" mkdir logs

echo Starting local server at %APP_URL%
echo Keep this window open while using MutualWarm.
echo.

start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%APP_URL%'"

"%APP_PYTHON%" -m uvicorn web_app:app --host %APP_HOST% --port %APP_PORT%

echo.
echo MutualWarm has stopped. If this was unexpected, check the error message above.
pause
exit /b 0

:python_error
echo [ERROR] Could not create the local Python environment.
echo Please install Python 3.10 or newer and try again.
echo.
pause
exit /b 1

:deps_error
echo [ERROR] Could not install Python dependencies.
echo Check your internet connection, then run start.bat again.
echo.
pause
exit /b 1

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
