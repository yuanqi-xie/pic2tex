@echo off
setlocal
cd /d "%~dp0"
where py.exe >nul 2>nul
if %errorlevel%==0 (
  py -3 -m pic2tex %*
) else (
  python -m pic2tex %*
)
endlocal
