@echo off
rem Builds dist\pgstress-setup-<version>.exe, a Windows installer (Start menu entry, uninstaller).
rem Needs Python and Inno Setup 6:  winget install JRSoftware.InnoSetup
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"

set "ISCC="
for %%I in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if not defined ISCC if exist %%I set "ISCC=%%~I"
)
if not defined ISCC (
  echo Inno Setup 6 not found. Install it with:  winget install JRSoftware.InnoSetup
  goto :fail
)

%PY% -m pip install --upgrade "psycopg[binary]" pywebview pyinstaller || goto :fail
for /f %%V in ('%PY% installer\make_version_info.py') do set "VERSION=%%V"
if not defined VERSION goto :fail

rem One-folder build: starts faster than the single-file exe because nothing is unpacked at launch.
rem It is staged in a short temp path: a deep checkout plus PyInstaller's nesting can pass Windows' 260-char limit.
set "STAGE=%TEMP%\pgstress-build"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --distpath "%STAGE%\dist" --workpath "%STAGE%\work" --specpath "%STAGE%" ^
  --name pgstress ^
  --icon "%CD%\assets\pgstress.ico" ^
  --version-file "%CD%\build\version_info.txt" ^
  --add-data "%CD%\ui;ui" ^
  --collect-all webview ^
  pgstress_app.py || goto :fail

"%ISCC%" /Qp /DAppVersion=%VERSION% "/DAppDir=%STAGE%\dist\pgstress" installer\pgstress.iss || goto :fail

echo.
echo Built dist\pgstress-setup-%VERSION%.exe
if not "%1"=="--no-pause" pause
exit /b 0

:fail
echo.
echo Build failed.
if not "%1"=="--no-pause" pause
exit /b 1
