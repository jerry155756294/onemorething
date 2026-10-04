@echo off
setlocal

set "ROOT=%~dp0"

echo Checking One More Thing backend on http://127.0.0.1:8000 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "One More Thing Backend" cmd /k "cd /d ""%ROOT%backend"" && ""%ROOT%backend\.venv\Scripts\python.exe"" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
) else (
    echo Backend is already running.
)

echo Checking One More Thing frontend on http://127.0.0.1:4173 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort 4173 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "One More Thing Frontend" cmd /k "cd /d ""%ROOT%web"" && npm run dev -- --host 127.0.0.1 --port 4173"
) else (
    echo Frontend is already running.
)

powershell -NoProfile -Command "Start-Sleep -Seconds 3"
start "" "http://127.0.0.1:4173/"

echo.
echo Backend:  http://127.0.0.1:8000
echo Frontend: http://127.0.0.1:4173
echo Close the two terminal windows to stop the test servers.

endlocal
