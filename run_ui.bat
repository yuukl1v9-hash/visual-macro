@echo off
REM ============================================================
REM  visual-macro launcher
REM  Double-click this file to start the app. On first run it
REM  creates a virtual environment and installs dependencies;
REM  after that it just launches the UI.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- find a Python launcher -------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo   Python was not found on your PATH.
    echo   Install it from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

REM --- create the venv on first run ------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ^(first run only^)...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
)

REM --- launch ----------------------------------------------------------
".venv\Scripts\python.exe" ui\app.py
if errorlevel 1 (
    echo.
    echo The app exited with an error ^(see above^).
    pause
)

endlocal
