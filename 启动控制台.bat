@echo off
chcp 65001 >nul
REM Start the console and open it in the default browser.
start "" pythonw "%~dp0app.py" --open
exit /b 0
