@echo off
setlocal enabledelayedexpansion

echo ====================================
echo Starting AIOps Copilot services
echo ====================================
echo.

if not exist .runtime mkdir .runtime

rem Local ports can be overridden before launch, for example:
rem   set MCP_CLS_PORT=18003 && start-windows.bat
if not defined AIOPS_API_PORT set "AIOPS_API_PORT=9900"
if not defined MCP_CLS_PORT for /f "tokens=1,* delims==" %%a in ('findstr /B /I "MCP_CLS_PORT=" .env 2^>nul') do set "MCP_CLS_PORT=%%b"
if not defined MCP_CLS_HOST set "MCP_CLS_HOST=127.0.0.1"
if not defined MCP_CLS_PORT set "MCP_CLS_PORT=8003"
if not defined MCP_MONITOR_PORT for /f "tokens=1,* delims==" %%a in ('findstr /B /I "MCP_MONITOR_PORT=" .env 2^>nul') do set "MCP_MONITOR_PORT=%%b"
if not defined MCP_MONITOR_HOST set "MCP_MONITOR_HOST=127.0.0.1"
if not defined MCP_MONITOR_PORT set "MCP_MONITOR_PORT=8004"
if not defined MCP_OPS_PORT for /f "tokens=1,* delims==" %%a in ('findstr /B /I "MCP_OPS_PORT=" .env 2^>nul') do set "MCP_OPS_PORT=%%b"
if not defined MCP_OPS_HOST set "MCP_OPS_HOST=127.0.0.1"
if not defined MCP_OPS_PORT set "MCP_OPS_PORT=8005"
if not defined LLM_PROVIDER (
    for /f "tokens=1,* delims==" %%a in ('findstr /B /I "LLM_PROVIDER=" .env 2^>nul') do set "LLM_PROVIDER=%%b"
)
if not defined LLM_PROVIDER set "LLM_PROVIDER=ollama"
if not defined LLM_MODEL (
    for /f "tokens=1,* delims==" %%a in ('findstr /B /I "LLM_MODEL=" .env 2^>nul') do set "LLM_MODEL=%%b"
)
if not defined LLM_MODEL set "LLM_MODEL=qwen3:8b"

rem Keep the API client configuration aligned with the local MCP processes.
set "AIOPS_PORT=!AIOPS_API_PORT!"
set "MCP_CLS_URL=http://!MCP_CLS_HOST!:!MCP_CLS_PORT!/mcp"
set "MCP_MONITOR_URL=http://!MCP_MONITOR_HOST!:!MCP_MONITOR_PORT!/mcp"
set "MCP_OPS_URL=http://!MCP_OPS_HOST!:!MCP_OPS_PORT!/mcp"

echo [1/9] Checking package manager...
where uv >nul 2>&1
if errorlevel 1 (
    echo [info] uv was not found; pip will be used when needed.
    set "USE_UV=0"
) else (
    echo [ok] uv found.
    set "USE_UV=1"
)
echo.

echo [2/9] Checking Python version config...
if exist .python-version (
    set /p PYTHON_VERSION=<.python-version
    echo [info] .python-version: !PYTHON_VERSION!
    echo !PYTHON_VERSION! | findstr /C:"3.10" >nul
    if not errorlevel 1 (
        echo [warn] Python 3.10 is not supported by this project. Updating to 3.13.
        echo 3.13> .python-version
    )
) else (
    echo [info] Creating .python-version.
    echo 3.13> .python-version
)
echo.

echo [3/9] Creating or syncing virtual environment...
if exist .venv\Scripts\python.exe (
    echo [info] Existing virtual environment found.
    if "%USE_UV%"=="1" (
        uv sync --frozen --extra local-embeddings
        if errorlevel 1 (
            echo [warn] uv sync failed; falling back to pip install -e .
            .venv\Scripts\python.exe -m pip install -e ".[local-embeddings]" -q
        )
    ) else (
        .venv\Scripts\python.exe -m pip install -e ".[local-embeddings]" -q
    )
) else (
    if "%USE_UV%"=="1" (
        echo [info] Creating environment with uv sync...
        uv sync --extra local-embeddings
        if not errorlevel 1 goto :venv_ready
        echo [warn] uv sync failed; falling back to python -m venv.
    )

    python -m venv .venv
    if errorlevel 1 (
        echo [error] Failed to create virtual environment. Install Python 3.11+ and try again.
        pause
        exit /b 1
    )

    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -e ".[local-embeddings]" -q
    if errorlevel 1 (
        echo [error] Failed to install project dependencies.
        pause
        exit /b 1
    )
)

:venv_ready
set "PYTHON_CMD=.venv\Scripts\python.exe"
echo [ok] Virtual environment is ready.
echo.

echo [info] Checking local service ports...
call :ensure_port_available !AIOPS_API_PORT! "FastAPI"
if errorlevel 1 goto :port_conflict
call :ensure_port_available !MCP_CLS_PORT! "CLS MCP"
if errorlevel 1 goto :port_conflict
call :ensure_port_available !MCP_MONITOR_PORT! "Monitor MCP"
if errorlevel 1 goto :port_conflict
call :ensure_port_available !MCP_OPS_PORT! "Ops MCP"
if errorlevel 1 goto :port_conflict
echo [ok] Local service ports are available.
echo.

echo [4/9] Starting Milvus stack...
docker info >nul 2>&1
if errorlevel 1 (
    echo [error] Docker is not available. Start Docker Desktop and try again.
    pause
    exit /b 1
)

docker compose -f vector-database.yml up -d
if errorlevel 1 (
    echo [error] Docker Compose failed to start the Milvus stack.
    pause
    exit /b 1
)

call :wait_for_container_health milvus-standalone 180
if errorlevel 1 (
    echo [error] Milvus did not become healthy. Recent logs:
    docker logs --tail 80 milvus-standalone
    pause
    exit /b 1
)
echo [ok] Milvus is healthy.
echo.

if /i "!LLM_PROVIDER!"=="ollama" (
    echo [info] Checking local Ollama service...
    set "OLLAMA_IN_CONTAINER=0"
    curl -fsS http://127.0.0.1:11434/api/tags >nul 2>&1
    if errorlevel 1 (
        echo [info] Starting Ollama GPU container...
        docker compose -f demo-ollama.yml up -d
        if errorlevel 1 (
            echo [error] Failed to start the Ollama container.
            pause
            exit /b 1
        )
        call :wait_for_container_health oncall-ollama 180
        if errorlevel 1 (
            echo [error] Ollama did not become healthy.
            pause
            exit /b 1
        )
        set "OLLAMA_IN_CONTAINER=1"
    ) else (
        docker inspect --format "{{.State.Running}}" oncall-ollama 2>nul | findstr /I "true" >nul
        if not errorlevel 1 set "OLLAMA_IN_CONTAINER=1"
    )
    if "!OLLAMA_IN_CONTAINER!"=="1" (
        docker exec oncall-ollama ollama show !LLM_MODEL! >nul 2>&1
        if errorlevel 1 (
            echo [info] Pulling local model !LLM_MODEL!; first run may take several minutes...
            docker exec oncall-ollama ollama pull !LLM_MODEL!
            if errorlevel 1 (
                echo [error] Failed to pull Ollama model !LLM_MODEL!.
                pause
                exit /b 1
            )
        )
    ) else (
        curl -fsS http://127.0.0.1:11434/api/tags | findstr /C:"!LLM_MODEL!" >nul
        if errorlevel 1 (
            echo [info] Pulling !LLM_MODEL! through the existing Ollama service...
            curl -fsS -X POST http://127.0.0.1:11434/api/pull -H "Content-Type: application/json" -d "{\"name\":\"!LLM_MODEL!\",\"stream\":false}" >nul
            if errorlevel 1 (
                echo [error] Failed to pull Ollama model !LLM_MODEL!.
                pause
                exit /b 1
            )
        )
    )
    echo [ok] Ollama model !LLM_MODEL! is ready.
    echo.
)

echo [5/9] Starting CLS MCP server...
start "CLS MCP Server" /min "%PYTHON_CMD%" mcp_servers/cls_server.py
call :wait_for_port !MCP_CLS_PORT! 30
if errorlevel 1 (
    echo [error] CLS MCP server did not listen on port !MCP_CLS_PORT!.
    goto :startup_failed
)
echo [ok] CLS MCP server started.
powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort !MCP_CLS_PORT! ^| Select-Object -First 1 -ExpandProperty OwningProcess) ^| Set-Content -Encoding ascii .runtime\cls.pid"
echo.

echo [6/9] Starting Monitor MCP server...
start "Monitor MCP Server" /min "%PYTHON_CMD%" mcp_servers/monitor_server.py
call :wait_for_port !MCP_MONITOR_PORT! 30
if errorlevel 1 (
    echo [error] Monitor MCP server did not listen on port !MCP_MONITOR_PORT!.
    goto :startup_failed
)
echo [ok] Monitor MCP server started.
powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort !MCP_MONITOR_PORT! ^| Select-Object -First 1 -ExpandProperty OwningProcess) ^| Set-Content -Encoding ascii .runtime\monitor.pid"
echo.

echo [7/9] Starting Ops MCP server...
start "Ops MCP Server" /min "%PYTHON_CMD%" mcp_servers/ops_server.py
call :wait_for_port !MCP_OPS_PORT! 30
if errorlevel 1 (
    echo [error] Ops MCP server did not listen on port !MCP_OPS_PORT!.
    goto :startup_failed
)
echo [ok] Ops MCP server started.
powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort !MCP_OPS_PORT! ^| Select-Object -First 1 -ExpandProperty OwningProcess) ^| Set-Content -Encoding ascii .runtime\ops.pid"
echo.

echo [8/9] Starting FastAPI service...
start "AIOps Copilot API" "%PYTHON_CMD%" -m app.run --host 0.0.0.0 --port !AIOPS_API_PORT!
call :wait_for_http http://localhost:!AIOPS_API_PORT!/health 180
if errorlevel 1 (
    echo [warn] FastAPI did not report healthy yet. Check the AIOps Copilot API window or logs.
    goto :done
)
echo [ok] FastAPI is healthy.
powershell -NoProfile -Command "(Get-NetTCPConnection -State Listen -LocalPort !AIOPS_API_PORT! ^| Select-Object -First 1 -ExpandProperty OwningProcess) ^| Set-Content -Encoding ascii .runtime\api.pid"
echo.

echo [9/9] Uploading documents to vector database...
for %%f in (aiops-docs\*.md) do (
    echo   Uploading: %%~nxf
    curl -fsS -X POST http://localhost:!AIOPS_API_PORT!/api/upload -F "file=@%%f" >nul 2>&1
)
echo [ok] Document upload finished.

:done
echo.
echo ====================================
echo Services are ready
echo ====================================
echo Web UI: http://localhost:!AIOPS_API_PORT!
echo API docs: http://localhost:!AIOPS_API_PORT!/docs
echo Attu:    http://localhost:8000
echo.
echo Logs:
echo   FastAPI: logs\app_*.log
echo   CLS MCP: type mcp_cls.log
echo   Monitor: type mcp_monitor.log
echo   Ops MCP: type mcp_ops.log
echo Stop services: stop-windows.bat
echo ====================================
pause
exit /b 0

:port_conflict
echo [error] A required port is already in use.
echo [info] Override the conflicting port before launch, for example:
echo        set MCP_CLS_PORT=18003 ^&^& start-windows.bat
pause
exit /b 1

:startup_failed
echo [error] Service startup failed. Run stop-windows.bat before retrying.
pause
exit /b 1

:ensure_port_available
set "CHECK_PORT=%~1"
set "CHECK_SERVICE=%~2"
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort !CHECK_PORT! -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
    echo [error] !CHECK_SERVICE! port !CHECK_PORT! is already in use.
    exit /b 1
)
exit /b 0

:wait_for_port
set "CHECK_PORT=%~1"
set "MAX_SECONDS=%~2"
set /a "ELAPSED=0"

:port_loop
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort !CHECK_PORT! -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 exit /b 0
if !ELAPSED! GEQ !MAX_SECONDS! exit /b 1
timeout /t 2 /nobreak >nul
set /a "ELAPSED+=2"
goto :port_loop

:wait_for_container_health
set "CONTAINER_NAME=%~1"
set "MAX_SECONDS=%~2"
set /a "ELAPSED=0"

:container_health_loop
set "CONTAINER_STATUS="
for /f "delims=" %%s in ('docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" "%CONTAINER_NAME%" 2^>nul') do set "CONTAINER_STATUS=%%s"

if /i "!CONTAINER_STATUS!"=="healthy" exit /b 0
if /i "!CONTAINER_STATUS!"=="exited" (
    echo [error] %CONTAINER_NAME% exited before becoming healthy.
    exit /b 1
)
if !ELAPSED! GEQ !MAX_SECONDS! (
    echo [error] Timed out waiting for %CONTAINER_NAME%. Last status: !CONTAINER_STATUS!
    exit /b 1
)

echo [info] Waiting for %CONTAINER_NAME% health... !CONTAINER_STATUS! (!ELAPSED!/!MAX_SECONDS!s)
timeout /t 5 /nobreak >nul
set /a "ELAPSED+=5"
goto :container_health_loop

:wait_for_http
set "URL=%~1"
set "MAX_SECONDS=%~2"
set /a "ELAPSED=0"

:http_loop
curl -fsS "%URL%" >nul 2>&1
if not errorlevel 1 exit /b 0

if !ELAPSED! GEQ !MAX_SECONDS! (
    echo [error] Timed out waiting for %URL%.
    exit /b 1
)

echo [info] Waiting for %URL%... (!ELAPSED!/!MAX_SECONDS!s)
timeout /t 5 /nobreak >nul
set /a "ELAPSED+=5"
goto :http_loop
