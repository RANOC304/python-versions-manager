@echo off
setlocal EnableExtensions EnableDelayedExpansion
title PyManager - Python Version Manager
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

where uv >nul 2>&1
if errorlevel 1 (
    echo Installing uv...
    powershell.exe -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating local app environment...
    where uv >nul 2>&1
    if errorlevel 1 (
        set "PY_CMD=py -3"
        !PY_CMD! --version >nul 2>&1
        if errorlevel 1 set "PY_CMD=python"
        !PY_CMD! -m venv .venv
    ) else (
        uv venv --managed-python --python 3.14.6 .venv
        if errorlevel 1 uv venv --managed-python --python 3.14 .venv
    )
    if errorlevel 1 (
        echo Could not create .venv. Check that uv or Python is installed.
        pause
        exit /b 1
    )
)

echo Installing/updating requirements...
where uv >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
    uv pip install --python ".venv\Scripts\python.exe" -r requirements.txt
)
if errorlevel 1 (
    echo Could not install requirements.
    pause
    exit /b 1
)

echo.
if "%PYMANAGER_PORT%"=="" (
    echo  PyManager starting. The app will print its local URL.
) else (
    echo  PyManager starting at http://localhost:%PYMANAGER_PORT%
)
echo  Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" app.py
pause
