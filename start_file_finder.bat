@echo off
setlocal
chcp 65001 >nul
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -I -X utf8 "%~dp0launch.py" %*
) else (
    echo Please create .venv with a patched Python 3.12+ and install requirements.txt.
    pause
    exit /b 1
)
set "FINDER_EXIT_CODE=%ERRORLEVEL%"
if not "%FINDER_EXIT_CODE%"=="0" pause
endlocal & exit /b %FINDER_EXIT_CODE%
