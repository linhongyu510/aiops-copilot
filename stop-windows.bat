@echo off
setlocal

echo ====================================
echo Stopping AIOps Copilot services
echo ====================================
echo.

echo [1/6] Stopping FastAPI service...
if exist .runtime\api.pid (
    for /f %%p in (.runtime\api.pid) do taskkill /PID %%p /T /F >nul 2>&1
    del /q .runtime\api.pid >nul 2>&1
) else (
    taskkill /FI "WINDOWTITLE eq AIOps Copilot API*" /T /F >nul 2>&1
)
if errorlevel 1 (
    echo [info] FastAPI service was not running.
) else (
    echo [ok] FastAPI service stopped.
)
echo.

echo [2/6] Stopping CLS MCP server...
if exist .runtime\cls.pid (
    for /f %%p in (.runtime\cls.pid) do taskkill /PID %%p /T /F >nul 2>&1
    del /q .runtime\cls.pid >nul 2>&1
) else (
    taskkill /FI "WINDOWTITLE eq CLS MCP Server*" /T /F >nul 2>&1
)
if errorlevel 1 (
    echo [info] CLS MCP server was not running.
) else (
    echo [ok] CLS MCP server stopped.
)
echo.

echo [3/6] Stopping Monitor MCP server...
if exist .runtime\monitor.pid (
    for /f %%p in (.runtime\monitor.pid) do taskkill /PID %%p /T /F >nul 2>&1
    del /q .runtime\monitor.pid >nul 2>&1
) else (
    taskkill /FI "WINDOWTITLE eq Monitor MCP Server*" /T /F >nul 2>&1
)
if errorlevel 1 (
    echo [info] Monitor MCP server was not running.
) else (
    echo [ok] Monitor MCP server stopped.
)
echo.

echo [4/6] Stopping Ops MCP server...
if exist .runtime\ops.pid (
    for /f %%p in (.runtime\ops.pid) do taskkill /PID %%p /T /F >nul 2>&1
    del /q .runtime\ops.pid >nul 2>&1
) else (
    taskkill /FI "WINDOWTITLE eq Ops MCP Server*" /T /F >nul 2>&1
)
if errorlevel 1 (
    echo [info] Ops MCP server was not running.
) else (
    echo [ok] Ops MCP server stopped.
)
echo.

echo [5/6] Stopping Milvus containers...
docker compose -f vector-database.yml down
if errorlevel 1 (
    echo [warn] Docker Compose could not stop the Milvus stack. It may already be stopped.
) else (
    echo [ok] Milvus containers stopped.
)
echo.

echo [6/6] Stopping local Ollama container if present...
docker compose -f demo-ollama.yml down >nul 2>&1
if errorlevel 1 (
    echo [info] Local Ollama container was not running.
) else (
    echo [ok] Local Ollama container stopped.
)
echo.

echo ====================================
echo All services have been stopped
echo ====================================
echo.
echo To remove Docker data volumes too, run:
echo   docker compose -f vector-database.yml down -v
echo.
pause
