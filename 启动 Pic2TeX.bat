@echo off
setlocal
cd /d "%~dp0"
where pyw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pyw.exe "%~dp0Pic2TeX.pyw" %*
) else (
  start "" pythonw.exe "%~dp0Pic2TeX.pyw" %*
)
endlocal
