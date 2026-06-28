@echo off
setlocal

set "NO_PAUSE="
if /i "%~1"=="--no-pause" (
  set "NO_PAUSE=1"
)

cd /d "%~dp0"

if not exist "gateway_config.json" (
  echo [ERROR] Missing gateway_config.json
  set "EXIT_CODE=1"
  goto :finish
)

if not exist "api_keys.json" (
  echo [ERROR] Missing api_keys.json
  set "EXIT_CODE=1"
  goto :finish
)

if not exist "orgs.json" (
  echo []> "orgs.json"
  echo [INFO] Created empty orgs.json
)

set "RETOOL_GATEWAY_HOST=127.0.0.1"
set "RETOOL_GATEWAY_PORT=8000"
if not "%~2"=="" set "RETOOL_GATEWAY_PORT=%~2"

echo [INFO] Working directory: %cd%
echo [INFO] Gateway host: %RETOOL_GATEWAY_HOST%
echo [INFO] Gateway port: %RETOOL_GATEWAY_PORT%
echo.

set "RETOOL_GATEWAY_CONFIG=%cd%\gateway_config.json"
set "RETOOL_GATEWAY_API_KEYS=%cd%\api_keys.json"
set "RETOOL_GATEWAY_BROWSER_PROVIDER=cloakbrowser"

python -m uvicorn main:app --host %RETOOL_GATEWAY_HOST% --port %RETOOL_GATEWAY_PORT%

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo [OK] Gateway exited normally.
) else (
  echo [ERROR] Gateway exited with code %EXIT_CODE%.
)

:finish
if not defined NO_PAUSE (
  echo.
  pause
)

exit /b %EXIT_CODE%
