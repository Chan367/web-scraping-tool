@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
echo Starting the scraping web app...
python "%~dp0webapp\app.py"
if errorlevel 1 (
  echo.
  echo The app stopped with an error. Press any key to close.
  pause >nul
)
endlocal
