@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ENV_DIR=%CD%\.fermi-env"
set "FERMI_PYTHON="

if exist "%ENV_DIR%\python.exe" set "FERMI_PYTHON=%ENV_DIR%\python.exe"
if exist "%ENV_DIR%\Scripts\python.exe" set "FERMI_PYTHON=%ENV_DIR%\Scripts\python.exe"
if defined FERMI_PYTHON goto :validate_environment

echo Creating an isolated Python 3.12 environment...

rem Prefer Conda so this works even when the base environment uses Python 3.9.
where conda >nul 2>nul
if not errorlevel 1 call conda create --prefix "%ENV_DIR%" python=3.12 pip -y
if exist "%ENV_DIR%\python.exe" set "FERMI_PYTHON=%ENV_DIR%\python.exe"
if defined FERMI_PYTHON goto :validate_environment

rem Locate common Anaconda and Miniconda installations when double-clicked.
if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" call "%USERPROFILE%\anaconda3\Scripts\conda.exe" create --prefix "%ENV_DIR%" python=3.12 pip -y
if exist "%ENV_DIR%\python.exe" set "FERMI_PYTHON=%ENV_DIR%\python.exe"
if defined FERMI_PYTHON goto :validate_environment

if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" call "%USERPROFILE%\miniconda3\Scripts\conda.exe" create --prefix "%ENV_DIR%" python=3.12 pip -y
if exist "%ENV_DIR%\python.exe" set "FERMI_PYTHON=%ENV_DIR%\python.exe"
if defined FERMI_PYTHON goto :validate_environment

rem Fall back to the Windows Python launcher.
where py >nul 2>nul
if errorlevel 1 goto :try_active_python
py -3.12 -m venv "%ENV_DIR%" 2>nul
if not exist "%ENV_DIR%\Scripts\python.exe" py -3.11 -m venv "%ENV_DIR%" 2>nul
if not exist "%ENV_DIR%\Scripts\python.exe" py -3.10 -m venv "%ENV_DIR%" 2>nul
if exist "%ENV_DIR%\Scripts\python.exe" set "FERMI_PYTHON=%ENV_DIR%\Scripts\python.exe"
if defined FERMI_PYTHON goto :validate_environment

:try_active_python
where python >nul 2>nul
if errorlevel 1 goto :python_missing
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :python_too_old
python -m venv "%ENV_DIR%"
if errorlevel 1 goto :environment_failed
set "FERMI_PYTHON=%ENV_DIR%\Scripts\python.exe"

:validate_environment
"%FERMI_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :environment_too_old

for /f "tokens=2" %%V in ('"%FERMI_PYTHON%" --version 2^>^&1') do set "PYTHON_VERSION=%%V"
echo Using Python %PYTHON_VERSION%
echo Installing required packages...
"%FERMI_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :install_failed
"%FERMI_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed

echo Checking the installation...
"%FERMI_PYTHON%" main.py doctor
if errorlevel 1 goto :install_failed

echo.
echo Setup completed successfully.
echo Double-click run_windows.bat to start Fermi LAT Sky Explorer.
pause
exit /b 0

:python_too_old
echo.
echo Your active Python is older than 3.10.
echo Open Anaconda Prompt and run this setup file again. The installer will create
echo its own Python 3.12 environment without changing your base environment.
pause
exit /b 1

:python_missing
echo.
echo Python 3.10 or newer and Conda were not found.
echo Install Python 3.12 or run this file from Anaconda Prompt.
pause
exit /b 1

:environment_too_old
echo.
echo The existing .fermi-env uses an unsupported Python version.
echo Delete only the .fermi-env folder beside this file and run setup_windows.bat again.
pause
exit /b 1

:environment_failed
echo.
echo The isolated Python environment could not be created.
echo Run setup_windows.bat from Anaconda Prompt and review the error above.
pause
exit /b 1

:install_failed
echo.
echo Installation failed. Review the error above and run setup_windows.bat again.
pause
exit /b 1
