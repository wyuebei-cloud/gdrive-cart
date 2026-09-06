@echo off
title GDrive Context Cart — Context Bridge
echo ========================================================
echo   GDrive Context Cart (Context Bridge)
echo ========================================================
echo.

set "PY_EXE=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
if not exist "%PY_EXE%" (
    set "PY_EXE=python"
)

"%PY_EXE%" "%~dp0server.py"
if errorlevel 1 (
    echo.
    echo Server exited with an error.
    pause
)