@echo off
REM ==============================================================
REM   kai - K8s AI Agent launcher
REM   Opens a WSL window in this project's folder and runs server.py.
REM   server.py auto-opens the browser to http://127.0.0.1:8000.
REM   The WSL window stays open so you can see logs / Ctrl+C to stop.
REM ==============================================================

setlocal

REM Resolve this script's own folder, then convert to a WSL path.
set "WIN_DIR=%~dp0"
if "%WIN_DIR:~-1%"=="\" set "WIN_DIR=%WIN_DIR:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%WIN_DIR%"`) do set "WSL_DIR=%%i"

if "%WSL_DIR%"=="" (
    echo ERROR: could not convert "%WIN_DIR%" to a WSL path.
    echo Is WSL installed and running?  Try:  wsl --status
    pause
    exit /b 1
)

start "K8s AI Agent" wsl --cd "%WSL_DIR%" bash -c "python3 server.py; echo; echo --- server exited. Press Enter to close. ---; read"

endlocal
