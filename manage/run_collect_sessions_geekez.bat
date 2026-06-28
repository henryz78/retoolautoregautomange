@echo off
setlocal

set "NO_PAUSE="
if /i "%~1"=="--no-pause" (
  set "NO_PAUSE=1"
  shift
)

cd /d "%~dp0"

if not exist "accounts_import_template.csv" (
  echo [ERROR] Missing accounts_import_template.csv
  set "EXIT_CODE=1"
  goto :finish
)

if not exist "gateway_config.json" (
  echo [ERROR] Missing gateway_config.json
  set "EXIT_CODE=1"
  goto :finish
)

echo [INFO] Working directory: %cd%
echo [INFO] Browser mode: GeekEZ API only
echo [INFO] Output bundle: runtime\session_bundle.json
echo.

python scripts\build_org_sessions_from_accounts.py ^
  --accounts-csv accounts_import_template.csv ^
  --gateway-config gateway_config.json ^
  --bundle-output runtime\session_bundle.json ^
  --browser-provider geekez ^
  --check-model gpt-5.5 ^
  --check-model claude-sonnet-4-6 ^
  --ignore-cooldown

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo [OK] Session collection finished.
) else (
  echo [ERROR] Session collection failed with exit code %EXIT_CODE%.
)

:finish
if not defined NO_PAUSE (
  echo.
  pause
)

exit /b %EXIT_CODE%
