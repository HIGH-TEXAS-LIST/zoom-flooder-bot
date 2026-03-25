@ECHO OFF
cd /d "%~dp0"
title Zoom Bot Control Center
cls
echo.
echo   Zoom Bot Control Center
echo   =======================
echo.

:: Check Python is installed (try python then python3)
set PYTHON=
python --version >nul 2>&1 && set PYTHON=python
if not defined PYTHON (
    python3 --version >nul 2>&1 && set PYTHON=python3
)
if not defined PYTHON (
    echo   [ERROR] Python not found. Install Python 3.9+ first.
    pause
    exit
)

:: Auto-install dependencies if any are missing
%PYTHON% -c "import flask; import flask_socketio; import selenium" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   Installing dependencies...
    %PYTHON% -m pip install -r requirements.txt
)

:: Verify install worked
%PYTHON% -c "import flask; import flask_socketio; import selenium" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo   [ERROR] Dependencies still missing. Try running manually:
    echo   %PYTHON% -m pip install -r requirements.txt
    pause
    exit
)

cls
echo.
echo   Zoom Bot Control Center
echo   =======================
echo.
echo   Starting dashboard...
echo.

:: Open browser after a short delay, then start the server
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"
%PYTHON% web_app.py
pause
