@echo off
setlocal

set "NO_PAUSE="
if /i "%~1"=="--no-pause" (
  set "NO_PAUSE=1"
)

cd /d "%~dp0"

if not exist "runtime\session_bundle.json" (
  echo [ERROR] Missing runtime\session_bundle.json
  set "EXIT_CODE=1"
  goto :finish
)

if not exist "gateway_config.json" (
  echo [ERROR] Missing gateway_config.json
  set "EXIT_CODE=1"
  goto :finish
)

echo [INFO] Working directory: %cd%
echo [INFO] Bundle input: runtime\session_bundle.json
echo [INFO] Gateway config: gateway_config.json
echo.

python scripts\import_session_bundle.py ^
  --bundle runtime\session_bundle.json ^
  --gateway-config gateway_config.json

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo [OK] Session bundle import finished.
) else (
  echo [ERROR] Session bundle import failed with exit code %EXIT_CODE%.
)

:finish
if not defined NO_PAUSE (
  echo.
  pause
)

exit /b %EXIT_CODE%
