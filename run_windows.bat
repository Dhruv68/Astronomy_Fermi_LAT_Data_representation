@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "FERMI_PYTHON="
if exist ".fermi-env\python.exe" set "FERMI_PYTHON=.fermi-env\python.exe"
if exist ".fermi-env\Scripts\python.exe" set "FERMI_PYTHON=.fermi-env\Scripts\python.exe"

if not defined FERMI_PYTHON (
    echo The application environment has not been created yet.
    echo Run setup_windows.bat first.
    pause
    exit /b 1
)

"%FERMI_PYTHON%" main.py
if errorlevel 1 (
    echo.
    echo The application exited with an error.
    pause
)
