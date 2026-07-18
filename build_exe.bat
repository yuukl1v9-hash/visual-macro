@echo off
REM ============================================================
REM  Build visual-macro into a standalone Windows .exe.
REM  Output: dist\visual-macro\visual-macro.exe  (a folder you can zip/share;
REM  end users then need NO Python install).
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run run_ui.bat once first to create the .venv, then re-run this.
    pause
    exit /b 1
)
set "PY=.venv\Scripts\python.exe"

echo Installing PyInstaller (first build only)...
"%PY%" -m pip install --upgrade pyinstaller >nul

echo Building...
"%PY%" -m PyInstaller --noconfirm --clean --windowed --name visual-macro ^
    --collect-submodules cv2 ^
    --collect-data mss ^
    ui\app.py

if errorlevel 1 (
    echo Build failed ^(see above^).
    pause
    exit /b 1
)

echo.
echo Done. Your app is at:  dist\visual-macro\visual-macro.exe
echo Zip the dist\visual-macro folder to share it.
echo.
echo NOTE: OCR (rapidocr-onnxruntime) and the ML detector (onnxruntime) are
echo optional and NOT bundled. If you use text/object steps, either install
echo them into the .venv before building, or run from source for those.
pause
endlocal
