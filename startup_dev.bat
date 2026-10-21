@echo off
chcp 65001 >nul
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM mini-agent development launcher (Windows CMD)
REM
REM   startup_dev.bat
REM
REM   1) backend  : http://0.0.0.0:8123  (API + SSE, all interfaces / LAN)
REM   2) frontend : http://0.0.0.0:5173  (Vite dev server, /api proxied)
REM
REM Hardening (for machines where "localhost just won't open"):
REM   - kill leftover node/backend processes belonging to THIS project
REM     (repeated launches share one .vite cache and cause EPERM rename)
REM   - fail loudly when 5173/8123 is held by an unrelated program
REM   - Vite --strictPort: never silently move to 5174
REM   - open the browser only after BOTH ports are actually listening
REM   - pause on error so a double-clicked window never just disappears
REM
REM Always uses the project virtualenv interpreter (needed so that
REM MCP servers, spawned as Python child processes, share the same
REM interpreter and can complete their handshake).
REM ------------------------------------------------------------------

set "PY=%~dp0.venv\Scripts\python.exe"
set "WEB_PORT=5173"
set "API_PORT=8123"

REM ---- 1) prerequisites --------------------------------------
if not exist "%PY%" (
  echo [x] Virtualenv not found: %PY%
  echo.
  echo     Create one first:
  echo       scripts\setup_venv.bat
  goto fail
)

where node >nul 2>nul
if errorlevel 1 (
  echo [x] Node.js not found in PATH. Install Node.js 18+ and retry.
  goto fail
)
where npm >nul 2>nul
if errorlevel 1 (
  echo [x] npm not found in PATH. Reinstall Node.js ^(npm ships with it^) and retry.
  goto fail
)

REM ---- 2) clean leftovers of this project --------------------
echo ==^> cleaning up leftover processes of this project ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root='%~dp0'; Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like ('*'+$root+'*') } | ForEach-Object { Write-Host ('   - killed node PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; foreach ($port in %WEB_PORT%,%API_PORT%) { $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { $p = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $c.OwningProcess } | Select-Object -First 1; if (($p.CommandLine -like ('*'+$root+'*')) -or ($p.CommandLine -like '*src\core\main.py*') -or ($p.ExecutablePath -like ('*'+$root+'*'))) { Write-Host ('   - killed PID ' + $p.ProcessId + ' on port ' + $port); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } else { Write-Host ('   ! port ' + $port + ' is occupied by: ' + $p.Name) } } }"

REM Ports must be free now; otherwise we would silently talk to a foreign service.
netstat -ano | findstr /R /C:":%WEB_PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [x] Port %WEB_PORT% is still in use by another program.
  echo     Close it first, then run this script again.
  goto fail
)
netstat -ano | findstr /R /C:":%API_PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [x] Port %API_PORT% is still in use by another program.
  echo     Close it first, then run this script again.
  goto fail
)

REM ---- 3) npm dependencies -----------------------------------
if not exist "web\node_modules\.bin\vite.cmd" (
  echo ==^> installing npm dependencies ...
  pushd web
  call npm install
  if errorlevel 1 (
    popd
    echo [x] npm install failed. See the messages above.
    goto fail
  )
  popd
)

REM ---- 4) start backend + frontend ---------------------------
echo ==^> starting backend on http://0.0.0.0:%API_PORT%
start "mini-agent backend :%API_PORT%" cmd /k ".venv\Scripts\python.exe src\core\main.py --web --port %API_PORT% --host 0.0.0.0"

echo ==^> starting frontend on http://localhost:%WEB_PORT%
REM Vite reads VITE_API_TARGET from the environment; the child cmd inherits it
set "VITE_API_TARGET=http://127.0.0.1:%API_PORT%"
pushd web
start "mini-agent frontend :%WEB_PORT%" cmd /k "npm run dev -- --port %WEB_PORT% --strictPort"
popd

REM ---- 5) wait until both ports answer -----------------------
echo ==^> waiting for services to become ready ...
call :wait_port %WEB_PORT% 90
if errorlevel 1 (
  echo [x] Frontend did not listen on port %WEB_PORT% within 90s.
  echo     Check the "mini-agent frontend" window for errors.
  echo     If it mentions EPERM / .vite, run:
  echo       rmdir /s /q web\node_modules\.vite
  goto fail
)
call :wait_port %API_PORT% 90
if errorlevel 1 (
  echo [x] Backend did not listen on port %API_PORT% within 90s.
  echo     Check the "mini-agent backend" window for errors.
  goto fail
)

REM ---- 6) open the browser -----------------------------------
echo ==^> opening browser ...
start "" "http://localhost:%WEB_PORT%"

endlocal
exit /b 0

REM ------------------------------------------------------------
REM :wait_port <port> <timeout-seconds>
REM   0 = listening, 1 = timed out (checked once per second)
REM ------------------------------------------------------------
:wait_port
set /a _waited=0
:wait_port_loop
netstat -ano | findstr /R /C:":%~1 .*LISTENING" >nul 2>nul
if not errorlevel 1 exit /b 0
if %_waited% GEQ %~2 exit /b 1
ping -n 2 127.0.0.1 >nul 2>nul
set /a _waited+=1
goto wait_port_loop

:fail
echo.
echo Press any key to close this window.
pause >nul
endlocal
exit /b 1
