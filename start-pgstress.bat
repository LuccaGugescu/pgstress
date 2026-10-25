@echo off
rem Double-click to start the pgstress UI in your browser.
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"
where %PY% >nul 2>nul || (
  echo Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

%PY% -c "import psycopg" 2>nul || (
  echo Installing psycopg...
  %PY% -m pip install --user "psycopg[binary]" || ( pause & exit /b 1 )
)

%PY% pgstress_ui.py %*
pause
