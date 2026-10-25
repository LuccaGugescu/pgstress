@echo off
rem Builds dist\pgstress.exe - a single-file Windows app (no Python needed to run it).
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"

%PY% -m pip install --upgrade "psycopg[binary]" pywebview pyinstaller || goto :fail

%PY% installer\make_version_info.py >nul || goto :fail

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name pgstress ^
  --icon assets\pgstress.ico ^
  --version-file build\version_info.txt ^
  --add-data "ui;ui" ^
  --collect-all webview ^
  pgstress_app.py || goto :fail

echo.
echo Built dist\pgstress.exe
pause
exit /b 0

:fail
echo.
echo Build failed.
pause
exit /b 1
